import random
import time

import pytest

from podcast.dedupe import Sig, TitleIndex, author_relation, dedupe, drop_seen_duplicates, likely_same_work, surname
from podcast.models import Paper
from podcast.state import State


def P(title, authors=("Jane Smith", "Li Wang"), doi=None, preprint=False, source="openalex", sid="1", **kw):
    return Paper(source=source, source_id=sid, title=title, authors=list(authors), doi=doi, is_preprint=preprint, **kw)


def same(a: Paper, b: Paper) -> bool:
    return likely_same_work(Sig.of(a), Sig.of(b))


@pytest.mark.parametrize("name,expected", [
    ("Smith J", "smith"), ("John Smith", "smith"), ("Smith, J.", "smith"), ("Smith JK", "smith"),
    ("Muhammad Usama Mirza", "mirza"), ("José Álvarez", "alvarez"), ("J. K. Rowling", "rowling"), ("Wang", "wang"),
    ("Smith J et al.", "smith"), ("", ""),
])
def test_surname_formats(name, expected):
    assert surname(name) == expected


def test_author_relation():
    s = lambda *names: Sig.build("t", list(names), None, False)  # noqa: E731
    assert author_relation(s("Smith J", "Wang L"), s("Jane Smith", "Li Wang")) == "same"
    assert author_relation(s("Smith J"), s("Doe A")) == "different"
    assert author_relation(s("Smith J", "Doe A", "Roe B"), s("Poe C", "Doe A")) == "unknown"  # one shared name only
    assert author_relation(s(), s("Doe A")) == "unknown"


# ---- merges that must happen

def test_preprint_and_its_published_version_with_reworded_title():
    pre = P("Deep learning reconstruction of knee MRI: a multicenter study", ("Smith J", "Wang L"), "10.1101/abc", True, "biorxiv")
    pub = P("Deep learning reconstruction for knee MRI - a multi-center study", ("Jane Smith", "Li Wang"), "10.1016/xyz")
    assert same(pre, pub)


def test_same_work_from_two_sources_without_doi():
    a = P("Quantitative fat fraction mapping of thigh muscles in dystrophy", ("Smith J",), None, False, "europepmc")
    b = P("Quantitative fat-fraction mapping of thigh muscles in dystrophy.", ("John Smith",), None, False, "openalex")
    assert same(a, b)


def test_spelling_variants_do_not_block_a_merge():
    pre = P("A randomised trial of exercise for muscle strength in older people", ("Smith J",), "10.1101/a", True)
    pub = P("A randomized trial of exercise for muscle strength in older people", ("Smith J",), "10.1016/b")
    assert same(pre, pub)


def test_same_doi_is_always_the_same_work():
    assert same(P("Completely different title one", doi="10.1/x"), P("Nothing alike here at all", doi="10.1/X"))


# ---- near-misses that must NOT merge

def test_two_published_articles_with_different_dois_stay_apart():
    a = P("Muscle fat fraction in children with dystrophy", doi="10.1016/a")
    b = P("Muscle fat fraction in children with dystrophy", doi="10.1016/b")
    assert not same(a, b)


def test_sibling_studies_differing_by_one_content_word_stay_apart():
    pre = P("Effect of exercise on bone density in children", ("Smith J",), "10.1101/a", True)
    pub = P("Effect of exercise on bone density in adults", ("Smith J",), "10.1016/b")
    assert not same(pre, pub)


@pytest.mark.parametrize("a,b", [
    ("Diffusion MRI of the brain Part I", "Diffusion MRI of the brain Part II"),
    ("Annual update on MRI safety 2025", "Annual update on MRI safety 2026"),
    ("Type I collagen MRI contrast", "Type II collagen MRI contrast"),
])
def test_series_and_numbered_titles_stay_apart(a, b):
    assert not same(P(a, preprint=True, doi="10.1/a"), P(b, doi="10.2/b"))


def test_short_generic_titles_by_different_authors_stay_apart():
    assert not same(P("Letter to the editor", ("Smith J",)), P("Letter to the editor", ("Doe A",)))


def test_unrelated_papers_with_overlapping_vocabulary():
    assert not same(P("Deep learning for MRI reconstruction", ("Smith J",), "10.1/a", True),
                    P("Deep learning for CT denoising in pediatric patients", ("Smith J",), "10.2/b"))


def test_identical_title_but_clearly_different_authors_needs_a_long_title():
    long_t = "Quantitative fat fraction mapping of thigh muscles in neuromuscular disease"
    assert same(P(long_t, ("Smith J", "Wang L")), P(long_t, ("Doe A", "Roe B")))  # same long title: duplicate records
    assert not same(P("Muscle MRI", ("Smith J",)), P("Muscle MRI", ("Doe A",)))


# ---- dedupe() end to end

def test_dedupe_merges_preprint_and_published_and_keeps_the_rest():
    pre = P("Deep learning reconstruction of knee MRI: a multicenter study", ("Smith J", "Wang L"), "10.1101/abc", True,
            "biorxiv", "1", pdf_urls=["https://biorxiv/x.pdf"], abstract="a")
    pub = P("Deep learning reconstruction for knee MRI - a multi-center study", ("Jane Smith", "Li Wang"), "10.1016/xyz",
            False, "openalex", "2", pdf_urls=["https://j/y.pdf"], license="cc-by", abstract="a longer abstract")
    other = P("T2 mapping of cartilage at 7 tesla", ("Doe A",), "10.1/zzz", False, "europepmc", "3")
    out = dedupe([pre, other, pub])
    assert len(out) == 2
    merged = next(p for p in out if "knee" in p.title)
    assert merged.license == "cc-by" and set(merged.pdf_urls) == {"https://biorxiv/x.pdf", "https://j/y.pdf"}
    assert set(merged.found_in) == {"biorxiv", "openalex"}
    assert len(dedupe([pre, other, pub], fuzzy=False)) == 3  # the switch turns it off


def test_dedupe_logs_each_fuzzy_merge(caplog):
    caplog.set_level("INFO", logger="podcast.dedupe")
    a = P("Quantitative fat fraction mapping of thigh muscles in dystrophy", ("Smith J",), None, False, "europepmc")
    b = P("Quantitative fat-fraction mapping of thigh muscles in dystrophy.", ("John Smith",), None, False, "openalex", "2")
    dedupe([a, b])
    assert "near-duplicate merged" in caplog.text and "fat" in caplog.text


# ---- against earlier runs

def test_candidates_already_seen_in_earlier_runs_are_dropped(tmp_path):
    st = State(tmp_path / "s.db")
    used = P("Deep learning reconstruction of knee MRI: a multicenter study", ("Smith J", "Wang L"), "10.1101/abc", True)
    st.record(used, "AI MRI", "used", "r1")
    st.record(P("Unrelated older paper about liver elastography", ("Doe A",), "10.1/q"), "AI MRI", "screened", "r1")
    st.record(P("Deep learning reconstruction of knee MRI: a multicenter study", ("Smith J",), "10.1/other"), "Other topic", "used", "r1")
    summaries = st.known_summaries("AI MRI")
    assert len(summaries) == 2 and {s["doi"] for s in summaries} == {"10.1101/abc", "10.1/q"}

    published_version = P("Deep learning reconstruction for knee MRI - a multi-center study", ("Jane Smith", "Li Wang"), "10.1016/xyz")
    fresh = P("Self-supervised T1 mapping of skeletal muscle", ("Roe B",), "10.1/new")
    assert drop_seen_duplicates([published_version, fresh], summaries) == [fresh]
    assert drop_seen_duplicates([published_version, fresh], []) == [published_version, fresh]


def test_index_stays_fast_with_thousands_of_titles():
    rng = random.Random(1)
    vocab = [f"w{n:04d}x" for n in range(1500)] + ["learning", "model", "network", "imaging", "analysis"]
    titles = [" ".join(rng.sample(vocab, 9)) for _ in range(4000)]
    index = TitleIndex()
    for t in titles:
        index.add(Sig.build(t, ["Smith J"], None, False))
    start = time.time()
    hits = [index.find(Sig.build(t, ["Smith J"], None, False)) for t in titles[:300]]
    assert time.time() - start < 5
    assert all(h is not None for h in hits)  # each known title is found again
    assert index.find(Sig.build("completely unrelated zebra giraffe okapi title", ["Smith J"], None, False)) is None
