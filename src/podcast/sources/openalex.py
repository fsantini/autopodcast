from __future__ import annotations

import os
from datetime import date

import httpx

from ..http import get
from ..licenses import normalize_license
from ..models import Paper
from .base import excluded, terms

URL = "https://api.openalex.org/works"


def rebuild_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words: dict[int, str] = {}
    for w, positions in inv.items():
        for p in positions:
            words[p] = w
    return " ".join(words[i] for i in sorted(words))


def parse(rec: dict) -> Paper | None:
    abstract = rebuild_abstract(rec.get("abstract_inverted_index"))
    title = rec.get("title") or ""
    if not title or not abstract:
        return None
    best = rec.get("best_oa_location") or {}
    pdfs, lic = [], normalize_license(best.get("license"))
    for loc in [best] + (rec.get("locations") or []):
        if loc and loc.get("is_oa") and loc.get("pdf_url") and loc["pdf_url"] not in pdfs:
            pdfs.append(loc["pdf_url"])
        lic = lic or (normalize_license(loc.get("license")) if loc else None)
    doi = (rec.get("doi") or "").replace("https://doi.org/", "") or None
    pub = rec.get("publication_date")
    src = (best.get("source") or {}) if best else {}
    return Paper(
        source="openalex",
        source_id=rec["id"].rsplit("/", 1)[-1],
        doi=doi,
        title=title,
        abstract=abstract,
        authors=[a["author"]["display_name"] for a in rec.get("authorships", []) if a.get("author")],
        journal=src.get("display_name"),
        published=date.fromisoformat(pub) if pub else None,
        license=lic,
        pdf_urls=pdfs,
        landing_url=best.get("landing_page_url"),
        is_preprint=(rec.get("type") == "preprint"),
    )


def search_expr(keywords: list[str]) -> str:
    """OpenAlex boolean search: all words of a keyword (stemmed, any order), phrases only where the user quoted them."""
    one = lambda t: f'"{t}"' if " " in t else t  # noqa: E731
    return " OR ".join("(" + " AND ".join(one(t) for t in terms(k)) + ")" for k in keywords)


class OpenAlex:
    name = "openalex"

    def search(self, client, topic, since, until, limit):
        search = search_expr(topic.keywords)
        params = {
            "search": search,
            "filter": f"from_publication_date:{since},to_publication_date:{until},open_access.is_oa:true,has_abstract:true",
            # Ask for twice the limit (max 100): some records still lose their abstract in parse(), and a
            # second page needs cursor paging, which OpenAlex only allows with an API key.
            "per-page": min(max(limit * 2, 25), 100), "cursor": "*",
        }
        if key := os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = key
        if client.headers.get("User-Agent", "").find("mailto:") >= 0:
            params["mailto"] = client.headers["User-Agent"].split("mailto:")[1].rstrip(")")
        papers: list[Paper] = []
        while len(papers) < limit:
            try:
                data = get(client, URL, params=params).json()
            except httpx.HTTPStatusError as e:
                # httpx puts the full URL in the message, which would write the key to the logs
                key = params.get("api_key")
                raise RuntimeError(str(e).replace(key, "***") if key else str(e)) from None
            for rec in data.get("results", []):
                if p := parse(rec):
                    papers.append(p)
            nxt = data.get("meta", {}).get("next_cursor")
            if not nxt or not data.get("results"):
                break
            params["cursor"] = nxt
        return [p for p in papers[:limit] if not excluded(topic, p.title + ' ' + p.abstract)]
