from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from .config import Config, Topic
from .dedupe import dedupe, drop_seen_duplicates
from .fetch_pdf import download_pdf, is_pdf_file
from .http import make_client
from .notebook import build_episode
from .publish import publish_episode
from .licenses import is_allowed
from .models import Episode, Paper
from .screen import screen
from .sources import REGISTRY
from .sources.arxiv import resolve_license
from .state import State

log = logging.getLogger(__name__)


def discover(cfg: Config, topic: Topic, today: date | None = None) -> list[Paper]:
    until = today or date.today()
    since = until - timedelta(days=cfg.window_days)
    papers: list[Paper] = []
    with make_client(cfg.contact_email) as client:
        for name in cfg.sources:
            try:
                found = REGISTRY[name].search(client, topic, since, until, cfg.max_results_per_source)
            except Exception as e:  # one broken source must not stop the run
                log.error("source %s failed: %s", name, e)
                continue
            log.info("%s: %d papers", name, len(found))
            papers += found
    return dedupe(papers, fuzzy=cfg.fuzzy_dedupe)


def filter_candidates(cfg: Config, papers: list[Paper]) -> list[Paper]:
    """Keep papers with a PDF and either an allowed license or an unresolved one (arXiv, resolved later)."""
    out = []
    for p in papers:
        if not p.pdf_urls:
            continue
        if p.license is None and "arxiv" in p.found_in and cfg.licenses is not None:
            out.append(p)  # license resolved after screening
        elif is_allowed(p.license, cfg.licenses):
            out.append(p)
    return out


def eligible(cfg: Config, papers: list[Paper]):
    """Yield screened-and-kept papers best first; arXiv licenses are resolved lazily, disallowed ones skipped."""
    kept = sorted((p for p in papers if p.screening and p.screening.keep),
                  key=lambda p: p.screening.score, reverse=True)
    with make_client(cfg.contact_email) as client:
        for p in kept:
            if p.license is None and "arxiv" in p.found_in:
                try:
                    p.license = resolve_license(client, p)
                except Exception as e:
                    log.warning("license lookup failed for %s: %s", p.key, e)
            if is_allowed(p.license, cfg.licenses):
                yield p


def acquire(cfg: Config, papers: list[Paper], pdf_dir: Path, use_browser: bool = True) -> list[Paper]:
    """Download PDFs best-first until top_n succeed; papers whose PDF cannot be obtained are skipped (backfill)."""
    chosen: list[Paper] = []
    with make_client(cfg.contact_email, timeout=120) as client:
        for p in eligible(cfg, papers):
            if len(chosen) >= cfg.screening.top_n:
                break
            if p.pdf_path and is_pdf_file(Path(p.pdf_path)):  # carried over from an earlier run: already downloaded
                chosen.append(p)
            elif path := download_pdf(client, p, pdf_dir, use_browser):
                p.pdf_path = str(path)
                chosen.append(p)
    return chosen


def run_search_and_screen(cfg: Config, topic: Topic, state: State, run_id: str, fetch: bool = False):
    """Stages 1-2 (+3 when fetch=True). Returns (screened, chosen); chosen is empty unless fetch=True."""
    known = state.known_keys(topic.name)
    candidates = [p for p in filter_candidates(cfg, discover(cfg, topic)) if p.key not in known]
    if cfg.fuzzy_dedupe:  # e.g. the journal version of a preprint that an earlier run already screened or used
        candidates = drop_seen_duplicates(candidates, state.known_summaries(topic.name))
    log.info("%d new candidates for %r", len(candidates), topic.name)
    screened = screen(cfg.screening, topic, candidates)
    # papers saved by an earlier run that found too few for an episode: already screened, PDFs usually downloaded
    carried = state.carryover(topic.name, cfg.carryover_days) if cfg.carryover_days > 0 else []
    if carried:
        log.info("%d paper(s) carried over from earlier runs for %r", len(carried), topic.name)
    pool = carried + screened
    chosen: list[Paper] = []
    if fetch:
        chosen = acquire(cfg, pool, cfg.data_dir / "pdfs" / run_id / topic_slug(topic))
    chosen_keys = {p.key for p in chosen}
    for p in screened:
        if p.screening is not None:  # failed screenings stay unrecorded -> retried next run
            state.record(p, topic.name, "selected" if p.key in chosen_keys else "screened", run_id)
    for p in carried:
        if p.key in chosen_keys:  # carried papers that were not chosen keep waiting
            state.record(p, topic.name, "selected", run_id)
    return pool, chosen


def topic_slug(topic: Topic) -> str:
    return re.sub(r"\W+", "_", topic.name).strip("_")


def keep_for_later(cfg: Config, state: State, topic: Topic, papers: list[Paper], run_id: str) -> None:
    """Save screened papers (with their PDFs) for the next run; with carryover disabled, release them instead."""
    if cfg.carryover_days > 0:
        for p in papers:
            state.record(p, topic.name, "carryover", run_id)
    else:
        state.forget([p.key for p in papers], topic.name)


def run_topic(cfg: Config, topic: Topic, state: State, run_id: str, until: str = "build"):
    """Run the pipeline for one topic up to ``until`` (screen | fetch | build).
    Returns (screened, chosen, episode-or-None)."""
    screened, chosen = run_search_and_screen(cfg, topic, state, run_id, fetch=until in ("fetch", "build"))
    if until != "build":
        return screened, chosen, None
    if len(chosen) < cfg.notebooklm.min_sources:
        log.warning("%r: only %d usable paper(s) (need %d) - no episode%s", topic.name, len(chosen),
                    cfg.notebooklm.min_sources,
                    f"; saved for the next run (kept up to {cfg.carryover_days} days)" if chosen and cfg.carryover_days > 0 else "")
        keep_for_later(cfg, state, topic, chosen, run_id)
        return screened, chosen, None
    out_dir = cfg.data_dir / "episodes" / run_id / topic_slug(topic)
    try:
        episode = build_episode(cfg.notebooklm, topic, chosen, out_dir)
    except Exception:
        keep_for_later(cfg, state, topic, chosen, run_id)  # let a retry pick the same papers up again
        raise
    (out_dir / "episode.json").write_text(json.dumps(episode.metadata(), indent=2, ensure_ascii=False))
    used = {p.key for p in episode.papers}
    for p in chosen:
        state.record(p, topic.name, "used" if p.key in used else "screened", run_id)
    state.record_episode(run_id, topic.name, out_dir.resolve(), "built")
    return screened, chosen, episode


def publish_dir(cfg: Config, state: State, directory: Path) -> dict:
    """Upload one built episode folder and mark it published in the state db."""
    result = publish_episode(cfg.site, directory)
    state.mark_published(directory)
    return result
