from datetime import date

import pytest

from podcast.config import Config, Topic
from podcast.dedupe import dedupe
from podcast.licenses import normalize_license
from podcast.models import Paper
from podcast.pipeline import filter_candidates
from podcast.sources import arxiv, biorxiv, europepmc, openalex
from podcast.sources.base import keyword_in, matches, terms


@pytest.mark.parametrize("raw,expected", [
    ("cc by", "cc-by"), ("CC BY 4.0", "cc-by"), ("cc_by", "cc-by"), ("cc-by-sa", "cc-by-sa"),
    ("cc_by_nc_nd", "cc-by-nc-nd"), ("CC BY-NC 4.0", "cc-by-nc"), ("cc0", "cc0"),
    ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
    ("https://creativecommons.org/licenses/by-nc-sa/4.0/", "cc-by-nc-sa"),
    ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
    ("other-oa", None), ("", None), (None, None), ("all rights reserved", None),
])
def test_normalize_license(raw, expected):
    assert normalize_license(raw) == expected


def P(**kw):
    base = dict(source="europepmc", source_id="1", title="A Study of Muscle", abstract="x")
    return Paper(**{**base, **kw})


def test_dedupe_by_doi_and_title_merges_fields():
    a = P(doi="10.1/X", license="cc-by", pdf_urls=["u1"])
    b = P(source="openalex", source_id="W1", doi="10.1/x", pdf_urls=["u2"], journal="J")
    c = P(source="arxiv", source_id="2", title="a study of  MUSCLE!", pdf_urls=["u3"])  # same title, no doi
    d = P(source_id="9", title="Other")
    out = dedupe([a, b, c, d])
    assert len(out) == 2
    m = next(p for p in out if p.title_key == "a study of muscle")
    assert m.doi == "10.1/X" and m.license == "cc-by" and m.journal == "J"
    assert set(m.pdf_urls) == {"u1", "u2", "u3"} and set(m.found_in) == {"europepmc", "openalex", "arxiv"}


def test_openalex_parse_rebuilds_abstract():
    rec = {"id": "https://openalex.org/W42", "title": "T", "doi": "https://doi.org/10.1/a",
           "abstract_inverted_index": {"hello": [0], "world": [1, 2]}, "publication_date": "2026-10-10",
           "best_oa_location": {"license": "cc-by", "pdf_url": "http://p.pdf", "is_oa": True,
                                "landing_page_url": "http://l", "source": {"display_name": "J"}},
           "authorships": [{"author": {"display_name": "Ann"}}], "type": "article"}
    p = openalex.parse(rec)
    assert p.abstract == "hello world world" and p.doi == "10.1/a" and p.license == "cc-by"
    assert p.pdf_urls == ["http://p.pdf"] and p.source_id == "W42" and p.authors == ["Ann"]


def test_europepmc_parse_and_query():
    rec = {"id": "PMC1", "source": "MED", "pmcid": "PMC1", "doi": "10.1/e", "title": "Title.",
           "abstractText": "Abs", "license": "cc by", "firstPublicationDate": "2026-10-09",
           "authorList": {"author": [{"fullName": "Bob B"}]},
           "fullTextUrlList": {"fullTextUrl": [{"availability": "Open access", "documentStyle": "pdf", "url": "http://x.pdf"}]}}
    p = europepmc.parse(rec)
    assert p.title == "Title" and p.license == "cc-by"
    assert p.pdf_urls[0] == "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/PMC1.1.pdf"
    assert "http://x.pdf" in p.pdf_urls and "blobtype=pdf" in p.pdf_urls[-1]
    assert not p.is_preprint
    assert europepmc.parse({**rec, "abstractText": ""}) is None
    q = europepmc.build_query(["a b", "c"], date(2026, 10, 6), date(2026, 10, 13))
    assert '(("a" AND "b") OR ("c"))' in q and "FIRST_PDATE:[2026-10-06 TO 2026-10-13]" in q and "OPEN_ACCESS:y" in q
    assert "TITLE_ABS" not in q  # new PMC records have no indexed abstract


def test_arxiv_parse_feed():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
    <entry><id>http://arxiv.org/abs/2610.01234v2</id><published>2026-10-08T10:00:00Z</published>
    <title>Deep
      Muscle</title><summary> An  abstract. </summary><author><name>A One</name></author>
    <arxiv:doi>10.1/ax</arxiv:doi></entry></feed>"""
    [p] = arxiv.parse_feed(xml)
    assert p.source_id == "2610.01234" and p.title == "Deep Muscle" and p.abstract == "An abstract."
    assert p.is_preprint and p.license is None and p.pdf_urls == ["https://arxiv.org/pdf/2610.01234"]


def test_biorxiv_parse():
    rec = {"doi": "10.1101/2026.1", "title": "T", "abstract": "A", "authors": "Doe, J.; Roe, R.",
           "date": "2026-10-07", "license": "cc_by_nc", "version": "2"}
    p = biorxiv.parse(rec, "medrxiv")
    assert p.license == "cc-by-nc" and p.authors == ["Doe, J.", "Roe, R."]
    assert p.pdf_urls == ["https://www.medrxiv.org/content/10.1101/2026.1v2.full.pdf"]


def test_filter_candidates():
    cfg = Config(topics=[Topic(name="t", keywords=["k"])], licenses=["cc0", "cc-by", "cc-by-sa"])
    ok = P(source_id="1", title="a", license="cc-by", pdf_urls=["u"])
    nc = P(source_id="2", title="b", license="cc-by-nc", pdf_urls=["u"])
    nopdf = P(source_id="3", title="c", license="cc-by")
    unk_ax = P(source="arxiv", source_id="4", title="d", pdf_urls=["u"], found_in=["arxiv"])
    unk_other = P(source_id="5", title="e", pdf_urls=["u"], found_in=["europepmc"])
    assert filter_candidates(cfg, [ok, nc, nopdf, unk_ax, unk_other]) == [ok, unk_ax]


def test_europepmc_abstract_fallback_and_jats():
    rec = {"id": "PMC2", "source": "PMC", "pmcid": "PMC2", "title": "No abstract in search", "license": "cc by",
           "firstPublicationDate": "2026-10-09"}
    assert europepmc.parse(rec) is None
    assert europepmc.parse(rec, "Fetched abstract").abstract == "Fetched abstract"
    jats = """<article><front><article-meta>
      <abstract abstract-type="graphical"><p>Teaser</p></abstract>
      <abstract><sec><title>Background</title><p>Real   abstract <italic>text</italic>.</p></sec></abstract>
    </article-meta></front></article>"""
    assert europepmc.abstract_from_jats(jats) == "Background Real abstract text."
    assert europepmc.abstract_from_jats("<article/>") == ""


def test_arxiv_id_survives_merge():
    ax = Paper(source="arxiv", source_id="2610.01234", title="x", pdf_urls=["https://arxiv.org/pdf/2610.01234"])
    assert arxiv.arxiv_id(ax) == "2610.01234"
    merged = Paper(source="openalex", source_id="W99", title="x", doi="10.48550/arxiv.2610.08109",
                   pdf_urls=["https://arxiv.org/pdf/2610.08109v2.pdf"])
    assert arxiv.arxiv_id(merged) == "2610.08109"
    by_doi = Paper(source="openalex", source_id="W9", title="x", doi="10.48550/arXiv.2610.08109")
    assert arxiv.arxiv_id(by_doi) == "2610.08109"
    old = Paper(source="openalex", source_id="W9", title="x", pdf_urls=["https://arxiv.org/pdf/hep-th/9901001v1"])
    assert arxiv.arxiv_id(old) == "hep-th/9901001"
    assert arxiv.arxiv_id(Paper(source="openalex", source_id="W9", title="x")) is None


def test_default_accepts_any_open_license_but_needs_pdf():
    cfg = Config(topics=[Topic(name="t", keywords=["k"])])
    nc = P(source_id="2", title="b", license="cc-by-nc", pdf_urls=["u"])
    unknown = P(source_id="5", title="e", pdf_urls=["u"])
    nopdf = P(source_id="3", title="c", license="cc-by")
    assert filter_candidates(cfg, [nc, unknown, nopdf]) == [nc, unknown]


def test_arxiv_license_normalized():
    assert normalize_license("http://arxiv.org/licenses/nonexclusive-distrib/1.0/") == "arxiv-nonexclusive"


# ---------------------------------------------------------------- keyword semantics: all words, any order

def test_terms_split_words_and_keep_quoted_phrases():
    assert terms("Deep learning MRI") == ["Deep", "learning", "MRI"]
    assert terms('"deep learning" MRI') == ["deep learning", "MRI"]
    assert terms("Parkinson's disease") == ["Parkinson's", "disease"]
    assert terms("  MSK   MRI ") == ["MSK", "MRI"]


def test_keyword_needs_every_word_but_in_any_order():
    text = "we apply learning methods for fast mri, a deep approach".lower()
    assert keyword_in(text, "Deep learning MRI")
    assert not keyword_in(text, "Deep learning CT")
    assert not keyword_in("deep learning mri", '"learning deep" MRI')  # quoted = exact phrase
    assert keyword_in("a deep learning model for mri", '"deep learning" MRI')
    assert not keyword_in("admiration of images", "MRI")  # whole words only, no substring hits


def test_matches_uses_any_keyword_and_exclusions():
    topic = Topic(name="t", keywords=["Deep learning MRI", "muscle imaging"], exclude=["case report"])
    assert matches(topic, "MRI reconstruction with deep neural learning")
    assert matches(topic, "Imaging of the muscle")
    assert not matches(topic, "Only deep learning here")
    assert not matches(topic, "Deep learning for MRI: a case report")


def test_openalex_and_arxiv_queries_use_all_words():
    assert openalex.search_expr(["Deep learning MRI", '"image segmentation"']) == '(Deep AND learning AND MRI) OR ("image segmentation")'
    q = arxiv.build_query(["Deep learning MRI", '"image segmentation" CT'], date(2026, 10, 6), date(2026, 10, 13))
    assert "(all:Deep AND all:learning AND all:MRI) OR (all:\"image segmentation\" AND all:CT)" in q
    assert "submittedDate:[202610060000 TO 202610132359]" in q


def test_biorxiv_source_filters_with_all_words(monkeypatch):
    recs = [
        {"doi": "10.1101/a", "title": "Learning to see: a deep model", "abstract": "We reconstruct MRI data.", "authors": "", "date": "2026-10-07", "version": "1"},
        {"doi": "10.1101/b", "title": "Deep learning for CT", "abstract": "No magnetic resonance here.", "authors": "", "date": "2026-10-07", "version": "1"},
    ]
    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    monkeypatch.setattr(biorxiv, "get", lambda client, url, **k: R({"collection": recs if "/biorxiv/" in url else [], "messages": [{"total": "2"}]}))
    found = biorxiv.BioMedRxiv().search(None, Topic(name="t", keywords=["Deep learning MRI"]), date(2026, 10, 6), date(2026, 10, 13), 10)
    assert [p.doi for p in found] == ["10.1101/a"]


def test_openalex_sends_key_and_hides_it_in_errors(monkeypatch):
    import httpx
    monkeypatch.setenv("OPENALEX_API_KEY", "sekret")
    seen = []

    def handler(req):
        seen.append(req.url.params)
        return httpx.Response(401, json={"error": "x"})

    from podcast.config import Topic
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        openalex.OpenAlex().search(client, Topic(name="t", keywords=["mri"]), "2026-01-01", "2026-01-08", 50)
    except RuntimeError as e:
        assert "sekret" not in str(e)
    else:
        raise AssertionError("expected failure")
    assert seen[0]["api_key"] == "sekret" and seen[0]["per-page"] == "100"
