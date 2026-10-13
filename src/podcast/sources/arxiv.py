from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import date

from ..http import get
from ..models import Paper

URL = "http://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


def parse_feed(xml: str) -> list[Paper]:
    papers = []
    for e in ET.fromstring(xml).findall("a:entry", NS):
        abs_url = e.findtext("a:id", "", NS)
        arxiv_id = re.sub(r"v\d+$", "", abs_url.rsplit("/abs/", 1)[-1])
        title = " ".join((e.findtext("a:title", "", NS)).split())
        abstract = " ".join((e.findtext("a:summary", "", NS)).split())
        if not title or not abstract:
            continue
        pub = e.findtext("a:published", "", NS)[:10]
        papers.append(Paper(
            source="arxiv", source_id=arxiv_id,
            doi=e.findtext("x:doi", None, NS) or None,
            title=title, abstract=abstract,
            authors=[a.findtext("a:name", "", NS) for a in e.findall("a:author", NS)],
            journal=e.findtext("x:journal_ref", None, NS),
            published=date.fromisoformat(pub) if pub else None,
            license=None,  # resolved lazily: see resolve_license
            pdf_urls=[f"https://arxiv.org/pdf/{arxiv_id}"],
            landing_url=f"https://arxiv.org/abs/{arxiv_id}",
            is_preprint=True,
        ))
    return papers


_ID_PATTERNS = (
    re.compile(r"arxiv\.org/(?:pdf|abs)/([^?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$", re.I),
    re.compile(r"^10\.48550/arxiv\.(.+?)(?:v\d+)?$", re.I),
)


def arxiv_id(paper: Paper) -> str | None:
    """arXiv id of a (possibly merged) paper: its own source id is not reliable after dedupe."""
    if paper.source == "arxiv":
        return paper.source_id
    for text in [*paper.pdf_urls, paper.landing_url or "", paper.doi or ""]:
        for pat in _ID_PATTERNS:
            if m := pat.search(text):
                return m.group(1)
    return None


def resolve_license(client, paper: Paper) -> str | None:
    """arXiv's API does not expose licenses; read the abs page (only for shortlisted papers)."""
    from ..licenses import normalize_license

    if not (aid := arxiv_id(paper)):
        return None
    html = get(client, f"https://arxiv.org/abs/{aid}").text
    m = re.search(r'href="(https?://(?:creativecommons\.org/(?:licenses|publicdomain)|arxiv\.org/licenses)/[^"]+)"', html)
    return normalize_license(m.group(1)) if m else None


class Arxiv:
    name = "arxiv"

    def search(self, client, topic, since, until, limit):
        kw = " OR ".join(f'all:"{k}"' for k in topic.keywords)
        q = f"({kw}) AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]"
        params = {"search_query": q, "start": 0, "max_results": min(limit, 100),
                  "sortBy": "submittedDate", "sortOrder": "descending"}
        papers: list[Paper] = []
        while len(papers) < limit:
            page = parse_feed(get(client, URL, params=params).text)
            papers += page
            if len(page) < params["max_results"]:
                break
            params["start"] += params["max_results"]
            time.sleep(3)  # arXiv API etiquette
        return [p for p in papers[:limit] if not any(x.lower() in (p.title + p.abstract).lower() for x in topic.exclude)]
