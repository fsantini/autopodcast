"""Download open-access PDFs locally so NotebookLM never has to reach a publisher site.

Strategy per URL: (1) plain HTTP; if the reply is an HTML landing page, follow its ``citation_pdf_url`` meta tag;
(2) fallback to headless Chromium (passes JS/cookie checks, shares cookies for the actual file request).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .http import get
from .models import Paper

log = logging.getLogger(__name__)

MIN_BYTES = 20_000
MAX_BYTES = 100 * 1024 * 1024  # NotebookLM accepts up to 200 MB; stay well below
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
_META_RE = re.compile(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_META_RE2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', re.I)


def slug(paper: Paper) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (paper.doi or f"{paper.source}_{paper.source_id}")).strip("_")[:120]


def is_pdf(data: bytes) -> bool:
    return MIN_BYTES <= len(data) <= MAX_BYTES and data[:1024].lstrip().startswith(b"%PDF-")


def is_pdf_file(path: Path) -> bool:
    """Same test as is_pdf, without reading the whole file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            return MIN_BYTES <= size <= MAX_BYTES and f.read(1024).lstrip().startswith(b"%PDF-")
    except OSError:
        return False


def find_pdf_link(html: str, base_url: str) -> str | None:
    m = _META_RE.search(html) or _META_RE2.search(html)
    return urljoin(base_url, m.group(1)) if m else None


def _http_fetch(client: httpx.Client, url: str) -> bytes | None:
    r = get(client, url, headers={"User-Agent": BROWSER_UA, "Accept": "application/pdf,*/*"})
    if is_pdf(r.content):
        return r.content
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        link = find_pdf_link(r.text, str(r.url))
        if link and link != url:
            r2 = get(client, link, headers={"User-Agent": BROWSER_UA, "Referer": str(r.url)})
            if is_pdf(r2.content):
                return r2.content
    return None


def _browser_fetch(url: str, timeout_ms: int = 60_000) -> bytes | None:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=BROWSER_UA, accept_downloads=True)
            page = ctx.new_page()
            body: bytes | None = None
            try:
                with page.expect_download(timeout=timeout_ms // 2) as dl:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except PWError:
                        pass  # "Download is starting" is raised by goto for file responses
                path = dl.value.path()
                body = Path(path).read_bytes() if path else None
            except PWError:
                pass  # no download: landing page with cookies set
            if body and is_pdf(body):
                return body
            link = page.evaluate(
                "() => document.querySelector('meta[name=citation_pdf_url]')?.content || null")
            if link:
                r = ctx.request.get(urljoin(page.url, link), headers={"Referer": page.url}, timeout=timeout_ms)
                if r.ok and is_pdf(r.body()):
                    return r.body()
            r = ctx.request.get(url, timeout=timeout_ms)  # same URL, now with cookies from the page visit
            return r.body() if r.ok and is_pdf(r.body()) else None
        finally:
            browser.close()


def download_pdf(client: httpx.Client, paper: Paper, dest_dir: Path, use_browser: bool = True) -> Path | None:
    """Try every candidate URL; return the saved path or None."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug(paper)}.pdf"
    if dest.exists() and is_pdf(dest.read_bytes()):
        return dest
    data, used = None, None
    for url in paper.pdf_urls:  # cheap pass: plain HTTP on every candidate URL first
        try:
            if data := _http_fetch(client, url):
                used = url
                break
        except Exception as e:
            log.debug("http fetch failed %s: %s", url, e)
    if data is None and use_browser:  # expensive pass: headless Chromium
        for url in paper.pdf_urls:
            try:
                if data := _browser_fetch(url):
                    used = url
                    break
            except Exception as e:
                log.debug("browser fetch failed %s: %s", url, e)
    if data:
        dest.write_bytes(data)
        log.info("pdf ok  %s (%d KB) <- %s", paper.key, len(data) // 1024, used)
        return dest
    log.warning("no PDF obtained for %s", paper.key)
    return None
