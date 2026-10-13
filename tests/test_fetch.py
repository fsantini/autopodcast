import httpx

from podcast.fetch_pdf import download_pdf, find_pdf_link, is_pdf, slug
from podcast.models import Paper

PDF = b"%PDF-1.7\n" + b"x" * 30_000 + b"\n%%EOF"


def paper(*urls, doi="10.1/a b"):
    return Paper(source="openalex", source_id="W1", doi=doi, title="t", pdf_urls=list(urls))


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_is_pdf():
    assert is_pdf(PDF) and not is_pdf(b"<html>" + b"x" * 30_000) and not is_pdf(b"%PDF-1.4 tiny")


def test_slug():
    assert slug(paper()) == "10.1_a_b"
    assert slug(Paper(source="arxiv", source_id="2610.1", title="t")) == "arxiv_2610.1"


def test_find_pdf_link_both_attribute_orders():
    a = '<meta name="citation_pdf_url" content="/files/x.pdf">'
    b = '<meta content="https://h.org/y.pdf" name="citation_pdf_url" />'
    assert find_pdf_link(a, "https://pub.org/art/1") == "https://pub.org/files/x.pdf"
    assert find_pdf_link(b, "https://pub.org/art/1") == "https://h.org/y.pdf"
    assert find_pdf_link("<html></html>", "https://pub.org") is None


def test_download_direct(tmp_path):
    c = client(lambda r: httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"}))
    path = download_pdf(c, paper("https://x.org/a.pdf"), tmp_path, use_browser=False)
    assert path and path.read_bytes() == PDF and path.name == "10.1_a_b.pdf"


def test_download_via_landing_page_meta_tag(tmp_path):
    def h(r):
        if r.url.path == "/landing":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<meta name="citation_pdf_url" content="/real.pdf">')
        return httpx.Response(200, content=PDF)
    assert download_pdf(client(h), paper("https://x.org/landing"), tmp_path, use_browser=False)


def test_download_falls_back_to_next_url_and_fails_cleanly(tmp_path):
    def h(r):
        if r.url.path == "/blocked":
            return httpx.Response(403)
        return httpx.Response(200, content=PDF) if r.url.path == "/ok.pdf" else httpx.Response(200, text="nope")
    assert download_pdf(client(h), paper("https://x.org/blocked", "https://x.org/ok.pdf"), tmp_path, use_browser=False)
    assert download_pdf(client(h), paper("https://x.org/blocked", "https://x.org/junk", doi="10.2/z"),
                        tmp_path, use_browser=False) is None


def test_http_on_all_urls_before_browser(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("podcast.fetch_pdf._browser_fetch", lambda url, **k: calls.append(url))
    def h(r):
        return httpx.Response(200, content=PDF) if r.url.path == "/second.pdf" else httpx.Response(403)
    assert download_pdf(client(h), paper("https://x.org/first", "https://x.org/second.pdf"), tmp_path)
    assert calls == []  # browser never launched because plain HTTP succeeded on the 2nd URL
