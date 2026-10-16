from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]


class PodcastOptions(BaseModel):
    format: str = "deep_dive"  # deep_dive | brief | critique | debate
    length: str = "default"  # short | default | long
    language: str = "en"
    instructions: str = ""


class Topic(BaseModel):
    name: str
    keywords: list[str]  # any keyword may match; all words of a keyword must occur (any order), "quoted" = exact phrase
    exclude: list[str] = Field(default_factory=list)
    podcast: PodcastOptions = Field(default_factory=PodcastOptions)


class ScreeningConfig(BaseModel):
    model: str = "~perplexity/pplx-decider-latest"
    min_relevance: float = 6.0  # 0-10
    min_quality: float = 5.0  # 0-10
    min_engaging: float = 0.5  # probability
    top_n: int = 6
    concurrency: int = 4
    allow_preprints: bool = True


class NotebookLMConfig(BaseModel):
    profile: str | None = None  # notebooklm-py auth profile (default: active profile)
    source_timeout: float = 300  # per-PDF processing wait, seconds
    generation_timeout: float = 1800  # audio generation wait, seconds
    generation_attempts: int = 2
    min_sources: int = 2  # an episode needs at least this many accepted papers
    delete_notebook: bool = True  # delete the notebook once audio + description are saved (also if the build fails)


class SiteConfig(BaseModel):
    upload_url: str | None = None  # https://.../upload.php ; SITE_UPLOAD_URL in .env is the fallback
    chunk_mb: float = 8  # upper bound for one request; the server's advertised limit can lower it
    timeout: float = 120
    retries: int = 4  # attempts per request for network errors / 429 / 5xx

    @property
    def chunk_bytes(self) -> int:
        return int(self.chunk_mb * 1024 * 1024)

    def resolved_url(self) -> str:
        return self.upload_url or require_env("SITE_UPLOAD_URL")


class Config(BaseModel):
    window_days: int = 7
    licenses: list[str] | None = None  # None = any open-access paper; else e.g. [cc0, cc-by]
    sources: list[str] = Field(default_factory=lambda: ["europepmc", "openalex", "arxiv", "biorxiv"])
    max_results_per_source: int = 300
    fuzzy_dedupe: bool = True  # also merge near-identical titles (preprint vs published) and skip works already seen
    carryover_days: int = 28  # screened papers wait this long for an episode when too few were found; 0 = drop them
    contact_email: str | None = None
    data_dir: Path = ROOT / "data"
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    notebooklm: NotebookLMConfig = Field(default_factory=NotebookLMConfig)
    site: SiteConfig = Field(default_factory=SiteConfig)
    topics: list[Topic]


def load_config(path: Path | str | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config.yaml"
    cfg = Config.model_validate(yaml.safe_load(path.read_text()))
    cfg.contact_email = cfg.contact_email or os.environ.get("CONTACT_EMAIL")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing environment variable {name} (set it in .env)")
    return val
