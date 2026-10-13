from __future__ import annotations

from .models import Paper


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


def dedupe(papers: list[Paper]) -> list[Paper]:
    """Merge duplicates across sources by DOI, then by normalized title."""
    by_key: dict[str, Paper] = {}
    title_to_key: dict[str, str] = {}
    for p in papers:
        existing_key = p.key if p.key in by_key else title_to_key.get(p.title_key)
        if existing_key is None:
            p.found_in = sorted(set(p.found_in + [p.source]))
            by_key[p.key] = p
            title_to_key[p.title_key] = p.key
            continue
        merged = merge(by_key.pop(existing_key), p)
        by_key[merged.key] = merged
        title_to_key[p.title_key] = title_to_key[merged.title_key] = merged.key
    return list(by_key.values())
