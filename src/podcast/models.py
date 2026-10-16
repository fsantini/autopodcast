from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field


class Screening(BaseModel):
    """Output of a decision model (no free text): numbers on 0-10 scales plus a study-type label."""

    relevance: float  # 0-10
    quality: float  # 0-10
    engaging: float  # probability 0-1 that it makes a good segment
    topic_check: float | None = None  # 0-10 answer to the topic's ai_screening question, if it has one
    study_type: str = ""
    study_type_confidence: float | None = None
    model: str | None = None  # resolved model snapshot that produced the answer
    keep: bool

    def summary(self) -> str:
        t = "" if self.topic_check is None else f" T{self.topic_check:>4.1f}"
        return f"R{self.relevance:>4.1f} Q{self.quality:>4.1f} E{self.engaging:.2f}{t} {self.study_type[:14]:<14}"

    @property
    def score(self) -> float:
        return 0.6 * self.relevance + 0.4 * self.quality + 2 * self.engaging


class Paper(BaseModel):
    source: str  # europepmc | openalex | arxiv | biorxiv | medrxiv
    source_id: str
    doi: str | None = None
    title: str
    abstract: str = ""
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    published: date | None = None
    license: str | None = None  # normalized, see licenses.normalize_license
    pdf_urls: list[str] = Field(default_factory=list)
    landing_url: str | None = None
    is_preprint: bool = False
    found_in: list[str] = Field(default_factory=list)
    screening: Screening | None = None
    pdf_path: str | None = None

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        return f"{self.source}:{self.source_id}"

    @property
    def title_key(self) -> str:
        return re.sub(r"[^a-z0-9]+", " ", self.title.lower()).strip()

    @property
    def citation(self) -> str:
        first = self.authors[0] if self.authors else "Unknown"
        et_al = " et al." if len(self.authors) > 1 else ""
        year = self.published.year if self.published else "n.d."
        return f"{first}{et_al} ({year}). {self.title}"


class Episode(BaseModel):
    """Everything the publish stage needs; written to episode.json next to the audio."""

    topic: str
    date: date
    title: str
    summary: str = ""  # from NotebookLM show notes
    takeaways: list[str] = Field(default_factory=list)
    audio_path: str
    duration_seconds: float | None = None
    notebook_id: str | None = None
    language: str = "en"
    papers: list[Paper]

    def metadata(self) -> dict:
        """Publishable metadata. Citations/licenses come from our own records, never from NotebookLM text."""
        return {
            "title": self.title, "topic": self.topic, "date": self.date.isoformat(), "language": self.language,
            "summary": self.summary, "takeaways": self.takeaways, "duration_seconds": self.duration_seconds,
            "disclaimer": "AI-generated audio overview (Google NotebookLM) of the open-access papers listed below; "
                          "it may contain errors. Please consult the original papers.",
            "papers": [{"title": p.title, "authors": p.authors, "journal": p.journal,
                        "published": p.published.isoformat() if p.published else None, "doi": p.doi,
                        "url": p.landing_url or (f"https://doi.org/{p.doi}" if p.doi else None),
                        "license": p.license or "unknown", "preprint": p.is_preprint,
                        "citation": p.citation} for p in self.papers],
        }
