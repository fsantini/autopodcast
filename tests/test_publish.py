import hashlib
import json
from email.parser import BytesParser
from urllib.parse import parse_qsl

import httpx
import pytest

from podcast import publish as pub
from podcast.config import SiteConfig
from podcast.publish import PublishError, publish_episode
from podcast.state import State

URL = "https://site.test/podcast/upload.php"
AUDIO = b"\x00\x00\x00\x20ftypM4A " + bytes(range(256)) * 1000  # 256 KB, starts like an m4a
SHA = hashlib.sha256(AUDIO).hexdigest()
UPLOAD_ID = SHA[:40]
CHUNK = 65536  # smallest allowed chunk -> 4 chunks


def parse_body(request: httpx.Request) -> tuple[dict, bytes | None]:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        msg = BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + request.content)
        fields, blob = {}, None
        for part in msg.get_payload():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if part.get_filename():
                blob = payload
            else:
                fields[name] = payload.decode()
        return fields, blob
    return dict(parse_qsl(request.content.decode())), None


class FakeSite:
    """In-memory model of site/upload.php's chunked protocol with switches for failure injection."""

    def __init__(self, max_chunk=0, token="tok"):
        self.parts: dict[str, bytearray] = {}
        self.episodes: dict[str, dict] = {}
        self.max_chunk, self.token = max_chunk, token
        self.chunk_requests: list[int] = []  # offsets of chunk POSTs seen
        self.fail_next = []  # statuses to answer with before behaving (e.g. [502, 502])
        self.apply_then_fail = 0  # apply the next N chunks but answer 503 (lost response)
        self.force_409_once = None  # received value to report on the next chunk POST
        self.max_body = None  # answer 413 above this many bytes
        self.lose_part_on_final = False
        self.final_status = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "bad token"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="busy")
        if request.method == "GET":
            uid = request.url.params["upload_id"]
            got = len(self.parts.get(uid, b""))
            return httpx.Response(200, json={"status": "in_progress" if uid in self.parts else "unknown",
                                             "upload_id": uid, "received": got, "max_chunk_bytes": self.max_chunk})
        fields, blob = parse_body(request)
        uid = fields["upload_id"]
        if fields.get("abort"):
            self.parts.pop(uid, None)
            return httpx.Response(200, json={"status": "aborted"})
        if fields.get("final"):
            return self.final(uid, fields)
        return self.chunk(uid, int(fields["offset"]), blob, len(request.content))

    def chunk(self, uid, offset, blob, body_len):
        if self.max_body and body_len > self.max_body:
            return httpx.Response(413, json={"error": "too large"})
        self.chunk_requests.append(offset)
        if self.force_409_once is not None:
            received, self.force_409_once = self.force_409_once, None
            return httpx.Response(409, json={"error": "gap", "received": received})
        part = self.parts.setdefault(uid, bytearray())
        if offset + len(blob) <= len(part):
            return httpx.Response(200, json={"status": "chunk_duplicate", "received": len(part)})
        if offset != len(part):
            return httpx.Response(409, json={"error": "gap", "received": len(part)})
        part.extend(blob)
        if self.apply_then_fail:
            self.apply_then_fail -= 1
            return httpx.Response(503, text="lost")
        return httpx.Response(200, json={"status": "chunk_ok", "received": len(part)})

    def final(self, uid, f):
        if self.final_status:
            return httpx.Response(self.final_status, json={"error": "Invalid metadata.", "details": ["title is required"]})
        if uid not in self.parts:
            return httpx.Response(404, json={"error": "unknown upload"})
        if self.lose_part_on_final:
            self.lose_part_on_final = False
            self.parts.pop(uid)
            return httpx.Response(404, json={"error": "unknown upload"})
        data = bytes(self.parts[uid])
        assert len(data) == int(f["total_bytes"]) and hashlib.sha256(data).hexdigest() == f["sha256"]
        json.loads(f["metadata"])
        guid = "sha256:" + f["sha256"]
        state = "exists" if guid in self.episodes else "created"
        self.episodes[guid] = {"audio": data}
        del self.parts[uid]
        return httpx.Response(200 if state == "exists" else 201, json={"status": state, "id": 1, "page_url": "https://site.test/?e=x"})


@pytest.fixture
def episode_dir(tmp_path):
    (tmp_path / "episode.m4a").write_bytes(AUDIO)
    (tmp_path / "episode.json").write_text(json.dumps({"title": "T", "papers": []}))
    return tmp_path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pub.time, "sleep", lambda s: None)


def run(site: FakeSite, directory, chunk_mb=CHUNK / 1048576, retries=4):
    cfg = SiteConfig(upload_url=URL, chunk_mb=chunk_mb, retries=retries)
    client = httpx.Client(transport=httpx.MockTransport(site), headers={"Authorization": f"Bearer {site.token}"})
    return publish_episode(cfg, directory, client)


def test_chunked_upload_end_to_end(episode_dir):
    site = FakeSite()
    result = run(site, episode_dir)
    assert result["status"] == "created"
    assert site.chunk_requests == [0, CHUNK, 2 * CHUNK, 3 * CHUNK]
    assert site.episodes["sha256:" + SHA]["audio"] == AUDIO
    assert json.loads((episode_dir / "published.json").read_text())["page_url"] == "https://site.test/?e=x"


def test_publishing_twice_is_idempotent(episode_dir):
    site = FakeSite()
    assert run(site, episode_dir)["status"] == "created"
    assert run(site, episode_dir)["status"] == "exists"


def test_resumes_from_partial_upload(episode_dir):
    site = FakeSite()
    site.parts[UPLOAD_ID] = bytearray(AUDIO[: 2 * CHUNK])  # an earlier run got this far
    run(site, episode_dir)
    assert site.chunk_requests == [2 * CHUNK, 3 * CHUNK]
    assert site.episodes["sha256:" + SHA]["audio"] == AUDIO


def test_stale_longer_partial_is_aborted_and_restarted(episode_dir):
    site = FakeSite()
    site.parts[UPLOAD_ID] = bytearray(AUDIO + b"extra")
    run(site, episode_dir)
    assert site.chunk_requests[0] == 0 and site.episodes["sha256:" + SHA]["audio"] == AUDIO


def test_transient_server_errors_are_retried(episode_dir):
    site = FakeSite()
    site.fail_next = [502, 503]
    assert run(site, episode_dir)["status"] == "created"


def test_gives_up_after_retries(episode_dir):
    site = FakeSite()
    site.fail_next = [502] * 10
    with pytest.raises(PublishError, match="kept failing"):
        run(site, episode_dir, retries=3)


def test_lost_response_is_recovered_through_duplicate_ack(episode_dir):
    site = FakeSite()
    site.apply_then_fail = 2  # chunks 0 and 1 are stored but the client sees 503
    run(site, episode_dir)
    assert site.episodes["sha256:" + SHA]["audio"] == AUDIO  # nothing appended twice, nothing missing


def test_offset_conflict_follows_the_server(episode_dir):
    site = FakeSite()
    site.parts[UPLOAD_ID] = bytearray(AUDIO[:CHUNK])
    # client believes 0 bytes are stored (status says so), server then reports the real position
    site.force_409_once = CHUNK
    run(site, episode_dir)
    assert site.episodes["sha256:" + SHA]["audio"] == AUDIO


def test_server_limit_lowers_chunk_size(episode_dir):
    site = FakeSite(max_chunk=CHUNK)  # advertised via status; client asked for 1 MB
    run(site, episode_dir, chunk_mb=1)
    assert site.chunk_requests == [0, CHUNK, 2 * CHUNK, 3 * CHUNK]


def test_chunk_too_large_is_halved(episode_dir):
    site = FakeSite(max_chunk=0)  # server does not advertise a limit...
    site.max_body = 100_000  # ...but rejects big bodies
    run(site, episode_dir, chunk_mb=0.25)  # 256 KB first, halved to 128 KB, then 64 KB
    assert site.episodes["sha256:" + SHA]["audio"] == AUDIO


def test_unusably_small_server_limit(episode_dir):
    with pytest.raises(PublishError, match="too small"):
        run(FakeSite(max_chunk=1000), episode_dir)


def test_bad_token(episode_dir):
    site = FakeSite(token="right")
    client = httpx.Client(transport=httpx.MockTransport(site), headers={"Authorization": "Bearer wrong"})
    with pytest.raises(PublishError, match="SITE_UPLOAD_TOKEN"):
        publish_episode(SiteConfig(upload_url=URL), episode_dir, client)


def test_invalid_metadata_is_not_retried_and_reports_details(episode_dir):
    site = FakeSite()
    site.final_status = 422
    with pytest.raises(PublishError, match="title is required"):
        run(site, episode_dir)


def test_server_losing_the_partial_triggers_a_restart(episode_dir):
    site = FakeSite()
    site.lose_part_on_final = True
    assert run(site, episode_dir)["status"] == "created"
    assert site.chunk_requests.count(0) == 2  # uploaded from scratch twice


def test_missing_audio_or_url(tmp_path, monkeypatch):
    (tmp_path / "episode.json").write_text("{}")
    with pytest.raises(PublishError, match="no episode audio"):
        publish_episode(SiteConfig(upload_url=URL), tmp_path, httpx.Client())
    monkeypatch.delenv("SITE_UPLOAD_URL", raising=False)
    with pytest.raises(RuntimeError, match="SITE_UPLOAD_URL"):
        SiteConfig().resolved_url()


def test_state_tracks_pending_and_published(tmp_path):
    st = State(tmp_path / "s.db")
    d = tmp_path / "ep1"
    d.mkdir()
    st.record_episode("r1", "Topic A", d, "built")
    assert st.pending_episodes() == [("r1", "Topic A", d.resolve())]
    assert st.mark_published(d) == 1
    assert st.pending_episodes() == []


# ---------------------------------------------------------------- redirects (the real site redirects apex -> www)

def redirect_layer(site: FakeSite, rules: dict, seen: list):
    """rules: {"host": ("status", "location")}; every other host is served by the fake site."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.host, request.url.scheme))
        rule = rules.get(request.url.host)
        if rule and (request.url.scheme, request.url.host) == rule[2]:
            return httpx.Response(rule[0], headers={"location": rule[1]})
        return site(request)
    return handler


def run_via(handler, directory, url):
    cfg = SiteConfig(upload_url=url, chunk_mb=CHUNK / 1048576)
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer tok"})
    return publish_episode(cfg, directory, client)


def test_apex_to_www_redirect_is_resolved_once_and_posts_go_to_the_final_address(episode_dir):
    site, seen = FakeSite(), []
    rules = {"site.test": (301, "https://www.site.test/podcast/upload.php?upload_id=x", ("https", "site.test"))}
    result = run_via(redirect_layer(site, rules, seen), episode_dir, "https://site.test/podcast/upload.php")
    assert result["status"] == "created" and site.episodes["sha256:" + SHA]["audio"] == AUDIO
    to_apex = [m for m, host, _ in seen if host == "site.test"]
    assert to_apex == ["GET"]  # only the single probe hit the redirecting address; no POST was ever redirected
    assert all(host == "www.site.test" for m, host, _ in seen if m == "POST")


def test_http_to_https_upgrade_is_followed(episode_dir):
    site, seen = FakeSite(), []
    rules = {"site.test": (301, "https://site.test/podcast/upload.php", ("http", "site.test"))}
    assert run_via(redirect_layer(site, rules, seen), episode_dir, "http://site.test/podcast/upload.php")["status"] == "created"
    assert all(scheme == "https" for m, _, scheme in seen if m == "POST")


def test_redirect_to_another_site_is_refused(episode_dir):
    rules = {"site.test": (301, "https://evil.example/upload.php", ("https", "site.test"))}
    with pytest.raises(PublishError, match="different site"):
        run_via(redirect_layer(FakeSite(), rules, []), episode_dir, "https://site.test/podcast/upload.php")


def test_https_to_http_downgrade_is_refused(episode_dir):
    rules = {"site.test": (302, "http://site.test/podcast/upload.php", ("https", "site.test"))}
    with pytest.raises(PublishError, match="downgrade"):
        run_via(redirect_layer(FakeSite(), rules, []), episode_dir, "https://site.test/podcast/upload.php")


def test_redirect_loop_gives_up(episode_dir):
    rules = {"site.test": (301, "https://www.site.test/u.php", ("https", "site.test")),
             "www.site.test": (301, "https://site.test/u.php", ("https", "www.site.test"))}
    with pytest.raises(PublishError, match="too many redirects"):
        run_via(redirect_layer(FakeSite(), rules, []), episode_dir, "https://site.test/u.php")
