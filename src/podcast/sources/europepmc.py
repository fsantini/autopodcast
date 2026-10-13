from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ..http import get
from ..licenses import normalize_license
from ..models import Paper

log = logging.getLogger(__name__)
REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
URL = f"{REST}/search"


def build_query(keywords: list[str], exclude: list[str], since: date, until: date) -> str:
    kw = " OR ".join(f'TITLE_ABS:"{k}"' for k in keywords)
    q = f"({kw}) AND OPEN_ACCESS:y AND FIRST_PDATE:[{since} TO {until}]"
    if exclude:
        q += " NOT (" + " OR ".join(f'TITLE_ABS:"{x}"' for x in exclude) + ")"
    return q


def pmc_s3_urls(pmcid: str, versions: tuple[int, ...] = (1, 2)) -> list[str]:
    """PDF locations in the PMC open-data bucket (key = <PMCID>.<version>/<PMCID>.<version>.pdf)."""
    return [f"https://pmc-oa-opendata.s3.amazonaws.com/{pmcid}.{v}/{pmcid}.{v}.pdf" for v in versions]


def abstract_from_jats(xml: str) -> str:
    """Main abstract of a JATS article (skips graphical/teaser abstracts when a plain one exists)."""
    root = ET.fromstring(xml)
    abstracts = root.findall(".//article-meta/abstract") or root.findall(".//abstract")
    plain = [a for a in abstracts if not a.get("abstract-type")]
    node = (plain or abstracts or [None])[0]
    if node is None:
        return ""
    text = " ".join(" ".join(node.itertext()).split())  # keep section titles apart from the next sentence
    return re.sub(r"\s+([.,;:)%])", r"\1", text).replace("( ", "(")


def parse(rec: dict, abstract: str | None = None) -> Paper | None:
    """``abstract`` overrides the record's own (new PMC records arrive without abstractText)."""
    title = (rec.get("title") or "").rstrip(".").strip()
    abstract = abstract or rec.get("abstractText") or ""
    if not title or not abstract:
        return None
    pdfs, landing = [], None
    for u in (rec.get("fullTextUrlList") or {}).get("fullTextUrl", []):
        if u.get("availability") in ("Open access", "Free") and u.get("documentStyle") == "pdf":
            pdfs.append(u["url"])
        if u.get("documentStyle") == "html" and not landing:
            landing = u["url"]
    if pmcid := rec.get("pmcid"):
        # 1st choice: NCBI's open-data bucket (no bot checks). Fallbacks: the publisher-independent
        # NCBI/Europe PMC renderers, which sit behind JS challenges and need the headless browser.
        pdfs = pmc_s3_urls(pmcid) + pdfs + [f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"]
    authors = [a.get("fullName", "") for a in (rec.get("authorList") or {}).get("author", []) if a.get("fullName")]
    pub = rec.get("firstPublicationDate")
    return Paper(
        source="europepmc",
        source_id=f"{rec.get('source')}:{rec.get('id')}",
        doi=(rec.get("doi") or None),
        title=title,
        abstract=abstract,
        authors=authors or ([rec["authorString"]] if rec.get("authorString") else []),
        journal=(rec.get("journalInfo") or {}).get("journal", {}).get("title") or rec.get("bookOrReportDetails", {}).get("publisher"),
        published=date.fromisoformat(pub) if pub else None,
        license=normalize_license(rec.get("license")),
        pdf_urls=pdfs,
        landing_url=landing,
        is_preprint=rec.get("source") == "PPR",
    )


class EuropePMC:
    name = "europepmc"

    @staticmethod
    def _abstracts(client, pmcids: list[str]) -> list[str]:
        def one(pmcid: str) -> str:
            try:
                return abstract_from_jats(get(client, f"{REST}/{pmcid}/fullTextXML").text)
            except Exception as e:
                log.warning("abstract fetch failed for %s: %s", pmcid, e)
                return ""
        with ThreadPoolExecutor(4) as pool:
            return list(pool.map(one, pmcids))

    def search(self, client, topic, since, until, limit):
        params = {"query": build_query(topic.keywords, topic.exclude, since, until),
                  "resultType": "core", "format": "json", "pageSize": min(limit, 100), "cursorMark": "*"}
        papers: list[Paper] = []
        while len(papers) < limit:
            data = get(client, URL, params=params).json()
            recs = data.get("resultList", {}).get("result", [])
            missing = [r for r in recs if not r.get("abstractText") and r.get("pmcid")]
            fetched = dict(zip((r["pmcid"] for r in missing), self._abstracts(client, [r["pmcid"] for r in missing])))
            for rec in recs:
                if p := parse(rec, fetched.get(rec.get("pmcid"))):
                    papers.append(p)
            nxt = data.get("nextCursorMark")
            if not nxt or nxt == params["cursorMark"] or not data["resultList"]["result"]:
                break
            params["cursorMark"] = nxt
        return papers[:limit]
