"""Remove duplicate papers: exact DOI matches, then conservative fuzzy matching of titles and authors.

A false merge silently drops a good paper, so the fuzzy rules lean towards keeping both records:
  * the required title similarity depends on the evidence (authors, preprint vs published);
  * two published articles with different DOIs are never merged;
  * titles differing by a number, "Part II", or a single swapped word ("children" vs "adults") are never merged.
Every fuzzy merge is logged with both titles so it can be audited.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import Paper

log = logging.getLogger(__name__)

STOP = frozenset("a an the of for in on and with to by from using via based".split())
SERIES = frozenset("part phase i ii iii iv v vi vii viii ix".split())
MIN_JACCARD = 0.5  # cheap pre-filter on the word sets, before the string comparison

# minimum title similarity, by (number of preprints in the pair, author evidence)
REQUIRED_RATIO = {
    (0, "same"): 0.90, (0, "unknown"): 0.95, (0, "different"): 0.98,
    (1, "same"): 0.80, (1, "unknown"): 0.93, (1, "different"): 0.98,  # preprint vs its published version: rewording is normal
    (2, "same"): 0.88, (2, "unknown"): 0.95, (2, "different"): 0.98,
}


def _richness(p: Paper) -> tuple:
    return (p.license is not None, len(p.pdf_urls), len(p.abstract), not p.is_preprint)


def merge(a: Paper, b: Paper) -> Paper:
    best, other = (a, b) if _richness(a) >= _richness(b) else (b, a)
    best = best.model_copy(deep=True)
    best.doi = best.doi or other.doi
    best.license = best.license or other.license
    best.landing_url = best.landing_url or other.landing_url
    best.journal = best.journal or other.journal
    best.authors = best.authors or other.authors
    for u in other.pdf_urls:
        if u not in best.pdf_urls:
            best.pdf_urls.append(u)
    best.found_in = sorted(set(best.found_in + other.found_in + [a.source, b.source]))
    return best


# ---------------------------------------------------------------- signatures

def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def surname(name: str) -> str:
    """Surname from the formats the sources use: 'Smith J', 'John Smith', 'Smith, J.', 'Smith JK'."""
    name = re.sub(r"\bet al\.?", "", name, flags=re.I).strip()
    if "," in name:
        s = name.split(",")[0]
    else:
        parts = name.replace(".", " ").split()
        if not parts:
            return ""
        initials = lambda w: re.fullmatch(r"[A-Z]{1,3}", w) is not None  # noqa: E731
        s = parts[0] if len(parts) > 1 and initials(parts[-1]) and not initials(parts[0]) else parts[-1]
    return re.sub(r"[^a-z]", "", _fold(s))


@dataclass
class Sig:
    title: str
    tokens: list[str]
    tokset: frozenset
    keys: frozenset  # longer, informative words: used to find candidate pairs quickly
    surnames: list[str]
    doi: str | None
    preprint: bool

    @classmethod
    def build(cls, title: str, authors: list[str], doi: str | None, preprint: bool) -> "Sig":
        tokens = re.findall(r"[a-z0-9]+", _fold(title))
        keys = frozenset(t for t in tokens if len(t) >= 4 and t not in STOP)
        names = [s for s in (surname(a) for a in authors[:10]) if s]
        return cls(title, tokens, frozenset(tokens), keys, names, doi.lower() if doi else None, bool(preprint))

    @classmethod
    def of(cls, p: Paper) -> "Sig":
        return cls.build(p.title, p.authors, p.doi, p.is_preprint)


def author_relation(a: Sig, b: Sig) -> str:
    """'same' | 'different' | 'unknown' from the surnames of the first (up to 10) authors."""
    if not a.surnames or not b.surnames:
        return "unknown"
    if a.surnames[0] == b.surnames[0]:
        return "same"
    shared = set(a.surnames) & set(b.surnames)
    if len(shared) >= min(2, len(a.surnames), len(b.surnames)):
        return "same"
    return "different" if not shared else "unknown"


def _swapped_single_word(sa: frozenset, sb: frozenset) -> bool:
    """Titles identical except for one different content word ('... in children' vs '... in adults')."""
    ua = [t for t in sa - sb if t not in STOP and len(t) > 2]
    ub = [t for t in sb - sa if t not in STOP and len(t) > 2]
    return len(ua) == 1 and len(ub) == 1 and SequenceMatcher(None, ua[0], ub[0]).ratio() < 0.8  # allow spelling variants


def likely_same_work(a: Sig, b: Sig) -> bool:
    if a.doi and b.doi and a.doi == b.doi:
        return True
    if not a.tokset or not b.tokset:
        return False
    if len(a.tokset & b.tokset) / len(a.tokset | b.tokset) < MIN_JACCARD:
        return False
    if any(t.isdigit() or t in SERIES for t in a.tokset ^ b.tokset):
        return False  # "Part I" / "Part II", different years or numbers
    preprints = int(a.preprint) + int(b.preprint)
    if preprints == 0 and a.doi and b.doi:
        return False  # two published articles with different DOIs are two works
    if _swapped_single_word(a.tokset, b.tokset):
        return False
    relation = author_relation(a, b)
    if relation == "different" and min(len(a.tokens), len(b.tokens)) < 6:
        return False  # short generic titles ("Editorial") by different authors
    ratio = SequenceMatcher(None, " ".join(a.tokens), " ".join(b.tokens)).ratio()
    return ratio >= REQUIRED_RATIO[(preprints, relation)]


class TitleIndex:
    """Finds candidate duplicates through shared informative words instead of comparing every pair."""

    def __init__(self) -> None:
        self.sigs: list[Sig] = []
        self._by_word: dict[str, list[int]] = defaultdict(list)

    def add(self, sig: Sig) -> int:
        self.sigs.append(sig)
        self._index(len(self.sigs) - 1, sig)
        return len(self.sigs) - 1

    def replace(self, i: int, sig: Sig) -> None:
        self.sigs[i] = sig
        self._index(i, sig)  # old words stay in the index; harmless, every hit is re-checked against the current signature

    def _index(self, i: int, sig: Sig) -> None:
        for w in sig.keys:
            self._by_word[w].append(i)

    def find(self, sig: Sig) -> int | None:
        if not sig.keys:
            return None
        crowded = max(50, len(self.sigs) // 5)  # words in a fifth of all titles ("learning", "model") say little
        hits = Counter(i for w in sig.keys if len(self._by_word.get(w, ())) <= crowded for i in self._by_word.get(w, ()))
        need = min(2, len(sig.keys))
        for i, n in hits.most_common():
            if n < need:
                break
            if likely_same_work(self.sigs[i], sig):
                return i
        return None


# ---------------------------------------------------------------- public API

def dedupe(papers: list[Paper], fuzzy: bool = True) -> list[Paper]:
    """Merge duplicates: same DOI first, then (fuzzy=True) near-identical titles with supporting evidence,
    or (fuzzy=False) identical normalized titles."""
    by_key: dict[str, Paper] = {}
    title_to_key: dict[str, str] = {}
    for p in papers:
        existing_key = p.key if p.key in by_key else (None if fuzzy else title_to_key.get(p.title_key))
        if existing_key is None:
            p.found_in = sorted(set(p.found_in + [p.source]))
            by_key[p.key] = p
            title_to_key[p.title_key] = p.key
            continue
        merged = merge(by_key.pop(existing_key), p)
        by_key[merged.key] = merged
        title_to_key[p.title_key] = title_to_key[merged.title_key] = merged.key
    unique = list(by_key.values())
    if not fuzzy:
        return unique

    kept: list[Paper] = []
    index = TitleIndex()
    for p in unique:
        sig = Sig.of(p)
        j = index.find(sig)
        if j is None:
            index.add(sig)
            kept.append(p)
            continue
        log.info("near-duplicate merged: %r ~ %r", kept[j].title[:80], p.title[:80])
        kept[j] = merge(kept[j], p)
        index.replace(j, Sig.of(kept[j]))
    return kept


def known_index(summaries: list[dict]) -> TitleIndex:
    """Index of papers seen in earlier runs: dicts with title, authors, doi, is_preprint."""
    index = TitleIndex()
    for s in summaries:
        index.add(Sig.build(s["title"], s.get("authors") or [], s.get("doi"), s.get("is_preprint", False)))
    return index


def drop_seen_duplicates(candidates: list[Paper], summaries: list[dict]) -> list[Paper]:
    """Remove candidates that are the same work as a paper from an earlier run (e.g. the journal version of a preprint
    already screened or used)."""
    index = known_index(summaries)
    kept = []
    for p in candidates:
        j = index.find(Sig.of(p))
        if j is None:
            kept.append(p)
        else:
            log.info("already seen in an earlier run: %r ~ %r", p.title[:80], index.sigs[j].title[:80])
    return kept
