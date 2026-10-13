"""Stage 6 client: upload an episode (audio + episode.json) to the site's upload.php using its chunked protocol.

The upload id is derived from the file's SHA-256, so an interrupted upload resumes where it stopped, even from a
later run, and publishing the same file twice is harmless (the server answers "exists").
Protocol: see site/README.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

from .config import SiteConfig, require_env

log = logging.getLogger(__name__)

MIN_CHUNK = 64 * 1024
_TRANSIENT = (429, 500, 502, 503, 504)
_REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
AUDIO_NAMES = ("episode.m4a", "episode.mp3")


class PublishError(RuntimeError):
    """Upload failed in a way retrying the same request will not fix (or retries ran out)."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def find_audio(directory: Path) -> Path:
    for name in AUDIO_NAMES:
        if (directory / name).is_file():
            return directory / name
    raise PublishError(f"no episode audio ({' / '.join(AUDIO_NAMES)}) in {directory}")


def _send(client: httpx.Client, method: str, url: str, retries: int, **kwargs) -> httpx.Response:
    """One request with retries for network errors and 429/5xx; every other status is returned to the caller."""
    for attempt in range(1, retries + 1):
        try:
            r = client.request(method, url, **kwargs)
            if r.status_code not in _TRANSIENT:
                return r
            reason = f"HTTP {r.status_code}"
        except httpx.TransportError as e:
            reason = type(e).__name__
        if attempt == retries:
            raise PublishError(f"{method} {url} kept failing ({reason}) after {retries} attempts")
        delay = min(2 ** attempt, 30)
        log.warning("%s %s: %s, retrying in %ds (%d/%d)", method, url, reason, delay, attempt, retries)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _same_site(a: httpx.URL, b: httpx.URL) -> bool:
    """Same host, treating www.example.org and example.org as one site (the common apex/www redirect)."""
    bare = lambda u: u.host.lower().removeprefix("www.")  # noqa: E731
    return bare(a) == bare(b)


def resolve_upload_url(client: httpx.Client, url: str, upload_id: str, retries: int) -> str:
    """Follow redirects of the upload address once, up front, with a harmless GET.

    A redirect must not be followed blindly on the POSTs that carry the data: for 301/302 an HTTP client turns the POST
    into a GET and drops the file. So the redirect is resolved here and every later request goes to the final address.
    Redirects to another site and https->http downgrades are refused (they would send the token somewhere else).
    """
    current = httpx.URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        r = _send(client, "GET", str(current), retries, params={"upload_id": upload_id})
        if r.status_code not in _REDIRECTS:
            return str(current)
        target = current.join(r.headers.get("location", "")).copy_with(query=None, fragment=None)
        if not _same_site(current, target):
            raise PublishError(f"the upload address redirects to a different site ({target}); "
                               f"set site.upload_url to the correct address instead of following it")
        if current.scheme == "https" and target.scheme != "https":
            raise PublishError(f"refusing a redirect from https to {target} (downgrade); fix site.upload_url")
        log.warning("upload address redirects to %s; set site.upload_url to it to skip this step", target)
        current = target
    raise PublishError(f"too many redirects starting at {url}")


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
        return f"HTTP {r.status_code}: {body.get('error', body)}" + (f" {body['details']}" if body.get("details") else "")
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:200]}"


def publish_episode(site: SiteConfig, directory: Path, client: httpx.Client | None = None) -> dict:
    """Upload ``directory/episode.json`` + audio. Returns the server's JSON (status created|exists, URLs)."""
    url = site.resolved_url()
    metadata = (directory / "episode.json").read_text(encoding="utf-8")
    audio = find_audio(directory)
    total = audio.stat().st_size
    sha = sha256_file(audio)
    upload_id = sha[:40]
    own_client = client is None
    client = client or httpx.Client(headers={"Authorization": f"Bearer {require_env('SITE_UPLOAD_TOKEN')}"},
                                    timeout=site.timeout, follow_redirects=False)
    try:
        url = resolve_upload_url(client, url, upload_id, site.retries)
        for attempt in range(1, 4):  # a restart is only needed if the server lost the partial file
            result = _upload_once(client, site, url, audio, total, sha, upload_id, metadata)
            if result is not None:
                record = directory / "published.json"
                if result.get("status") == "created" or not record.exists():  # keep the original "created" record
                    record.write_text(json.dumps(result, indent=2))
                log.info("published %s: %s (%s)", directory, result.get("page_url"), result.get("status"))
                return result
            log.warning("server lost the partial upload; restarting (%d/3)", attempt)
        raise PublishError("server repeatedly lost the partial upload")
    finally:
        if own_client:
            client.close()


def _upload_once(client, site, url, audio: Path, total: int, sha: str, upload_id: str, metadata: str) -> dict | None:
    """Resume/upload the chunks, then finalize. Returns None when the server no longer knows the upload."""
    r = _send(client, "GET", url, site.retries, params={"upload_id": upload_id})
    if r.status_code == 401:
        raise PublishError("the server rejected SITE_UPLOAD_TOKEN (401)")
    if r.status_code != 200:
        raise PublishError(f"status check failed - {_error_text(r)}")
    info = r.json()
    received = int(info.get("received", 0))
    if received > total:  # stale partial from a different file: start over
        _send(client, "POST", url, site.retries, data={"upload_id": upload_id, "abort": "1"})
        received = 0
    server_max = int(info.get("max_chunk_bytes") or 0)  # 0 = no PHP limit
    chunk = min(site.chunk_bytes, server_max) if server_max else site.chunk_bytes
    if chunk < MIN_CHUNK:
        raise PublishError(f"server allows only {server_max} bytes per request; too small to upload in chunks")
    if received:
        log.info("resuming upload at %d of %d bytes", received, total)

    offset, resyncs = received, 0
    with audio.open("rb") as f:
        while offset < total:
            f.seek(offset)
            data = f.read(min(chunk, total - offset))
            r = _send(client, "POST", url, site.retries, data={"upload_id": upload_id, "offset": str(offset)},
                      files={"audio": ("chunk.bin", data, "application/octet-stream")})
            if r.status_code == 200:
                offset = int(r.json()["received"])
                log.info("uploaded %d / %d bytes (%.0f%%)", offset, total, 100 * offset / total)
            elif r.status_code == 409 and resyncs < 5:  # server and client disagree on the offset: follow the server
                resyncs += 1
                offset = int(r.json()["received"])
                log.warning("offset mismatch, continuing from the server's %d", offset)
            elif r.status_code == 413 and chunk > MIN_CHUNK:  # request too big for this server: halve and retry
                chunk = max(MIN_CHUNK, chunk // 2)
                log.warning("chunk rejected as too large, retrying with %d bytes", chunk)
            else:
                raise PublishError(f"chunk at offset {offset} failed - {_error_text(r)}")

    r = _send(client, "POST", url, site.retries, data={
        "upload_id": upload_id, "final": "1", "total_bytes": str(total), "sha256": sha, "metadata": metadata})
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 404:
        return None
    raise PublishError(f"finalizing failed - {_error_text(r)}")
