from __future__ import annotations

from datetime import date

from ..http import get
from ..licenses import normalize_license
from ..models import Paper
from .base import matches

URL = "https://api.biorxiv.org/details/{server}/{since}/{until}/{cursor}"


def parse(rec: dict, server: str) -> Paper | None:
    if not rec.get("abstract") or not rec.get("title"):
        return None
    doi, ver = rec["doi"], rec.get("version", "1")
    authors = [a.strip() for a in rec.get("authors", "").split(";") if a.strip()]
    return Paper(
        source=server, source_id=doi, doi=doi, title=rec["title"].strip(), abstract=rec["abstract"].strip(),
        authors=authors, journal=server, published=date.fromisoformat(rec["date"]),
        license=normalize_license(rec.get("license")),
        pdf_urls=[f"https://www.{server}.org/content/{doi}v{ver}.full.pdf"],
        landing_url=f"https://www.{server}.org/content/{doi}v{ver}", is_preprint=True,
    )


class BioMedRxiv:
    """Both servers; the API has no keyword search so we filter client-side."""

    name = "biorxiv"

    def search(self, client, topic, since, until, limit):
        out: list[Paper] = []
        for server in ("biorxiv", "medrxiv"):
            cursor = 0
            while True:
                data = get(client, URL.format(server=server, since=since, until=until, cursor=cursor)).json()
                recs = data.get("collection", [])
                for rec in recs:
                    p = parse(rec, server)
                    if p and matches(topic, p.title + " " + p.abstract):
                        out.append(p)
                cursor += len(recs)
                total = int((data.get("messages") or [{}])[0].get("total", 0))
                if not recs or cursor >= total:
                    break
        # keep only the latest version of each DOI
        latest: dict[str, Paper] = {}
        for p in out:
            latest[p.doi] = p
        return list(latest.values())[:limit]
