"""Stage 4: NotebookLM (unofficial notebooklm-py). Upload PDFs, generate + download the audio overview,
and ask the notebook itself for the episode title/summary (show notes)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from pathlib import Path

from notebooklm import AudioFormat, AudioLength, NotebookLMClient

from .config import NotebookLMConfig, Topic
from .models import Episode, Paper

log = logging.getLogger(__name__)

NOTEBOOK_PREFIX = "Podcast · "

SHOW_NOTES_PROMPT = """You are writing show notes for a research podcast episode that covers ALL the sources in this notebook.
Answer in exactly this plain-text format, with no markdown, no bold, and no citation numbers:
TITLE: <an engaging episode title, at most 80 characters>
SUMMARY: <2-3 sentences, at most 600 characters, on the main themes and findings across the papers, for a specialist audience>
TAKEAWAYS:
- <one line per paper: first author, year, and the key finding>"""


def notebook_title(topic: Topic, day: date) -> str:
    return f"{NOTEBOOK_PREFIX}{topic.name} · {day:%Y-%m-%d}"


def sniff_audio_ext(path: Path) -> str:
    head = path.read_bytes()[:12]
    if head[4:8] == b"ftyp":
        return ".m4a"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"RIFF":
        return ".wav"
    return ".m4a"


def _clean(text: str) -> str:
    text = re.sub(r"\[\d+(?:\s*[-–,]\s*\d+)*\]", "", text)  # citation markers like [1], [2-4]
    text = re.sub(r"[*_`#]+", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_show_notes(answer: str) -> tuple[str | None, str, list[str]]:
    """-> (title, summary, takeaways); tolerant of missing sections."""
    text = _clean(answer)
    title = re.search(r"^TITLE:\s*(.+)$", text, re.M | re.I)
    summary = re.search(r"^SUMMARY:\s*(.+?)(?=^\s*TAKEAWAYS:|\Z)", text, re.M | re.I | re.S)
    takeaways = re.search(r"^TAKEAWAYS:\s*(.+)\Z", text, re.M | re.I | re.S)
    items = [re.sub(r"^[-•]\s*", "", ln).strip() for ln in (takeaways.group(1).splitlines() if takeaways else [])]
    return (title.group(1).strip()[:120] if title else None,
            " ".join(summary.group(1).split()) if summary else (text if not title else ""),
            [i for i in items if i])


async def _get_show_notes(client, nb_id: str, source_ids: list[str]) -> tuple[str | None, str, list[str]]:
    try:
        res = await client.chat.ask(nb_id, SHOW_NOTES_PROMPT, source_ids=source_ids)
        title, summary, items = parse_show_notes(res.answer)
        if summary:
            return title, summary, items
    except Exception as e:
        log.warning("show-notes chat failed: %s", e)
    try:  # fallback: NotebookLM's automatic notebook summary
        return None, _clean(await client.notebooks.get_summary(nb_id)), []
    except Exception as e:
        log.warning("notebook summary failed: %s", e)
        return None, "", []


async def _add_sources(client, nb_id: str, papers: list[Paper], cfg: NotebookLMConfig) -> tuple[list[Paper], list[str]]:
    async def add(p: Paper):
        try:
            src = await client.sources.add_file(nb_id, p.pdf_path, wait=True,
                                                wait_timeout=cfg.source_timeout, title=p.title[:200])
            return p, src.id
        except Exception as e:
            log.warning("NotebookLM rejected %s: %s", p.key, e)
            return p, None

    results = await asyncio.gather(*(add(p) for p in papers))
    ok = [(p, sid) for p, sid in results if sid]
    return [p for p, _ in ok], [sid for _, sid in ok]


async def _generate_audio(client, nb_id: str, source_ids: list[str], topic: Topic, cfg: NotebookLMConfig):
    opts = topic.podcast
    last = None
    for attempt in range(1, cfg.generation_attempts + 1):
        status = await client.artifacts.generate_audio(
            nb_id, source_ids=source_ids, language=opts.language, instructions=opts.instructions or None,
            audio_format=AudioFormat[opts.format.upper()], audio_length=AudioLength[opts.length.upper()])
        if not (status.is_failed or status.is_rate_limited):
            status = await client.artifacts.wait_for_completion(nb_id, status.task_id, timeout=cfg.generation_timeout)
        if status.is_complete:
            return status
        last = status
        log.warning("audio generation attempt %d/%d ended with %s (%s)", attempt, cfg.generation_attempts,
                    getattr(status, "status", "?"), status.error)
        await asyncio.sleep(60 * attempt)
    raise RuntimeError(f"NotebookLM audio generation failed: {last.error if last else 'unknown'}")


async def build_episode_with(client, cfg: NotebookLMConfig, topic: Topic, papers: list[Paper],
                             out_dir: Path, day: date) -> Episode:
    out_dir.mkdir(parents=True, exist_ok=True)
    nb = await client.notebooks.create(notebook_title(topic, day))
    log.info("notebook %s created", nb.id)
    try:
        used, source_ids = await _add_sources(client, nb.id, papers, cfg)
        if len(used) < cfg.min_sources:
            raise RuntimeError(f"only {len(used)} source(s) accepted by NotebookLM (need {cfg.min_sources})")
        await _generate_audio(client, nb.id, source_ids, topic, cfg)
        raw = out_dir / "episode.audio"
        await client.artifacts.download_audio(nb.id, str(raw))
        audio = raw.rename(raw.with_suffix(sniff_audio_ext(raw)))
        duration = None
        try:
            arts = await client.artifacts.list_audio(nb.id)
            duration = next((a.duration_seconds for a in reversed(arts) if a.duration_seconds), None)
        except Exception as e:
            log.warning("could not read audio duration: %s", e)
        title, summary, takeaways = await _get_show_notes(client, nb.id, source_ids)
        return Episode(topic=topic.name, date=day, title=title or f"{topic.name} – week of {day:%d %b %Y}",
                       summary=summary, takeaways=takeaways, audio_path=str(audio), duration_seconds=duration,
                       notebook_id=nb.id, language=topic.podcast.language, papers=used)
    finally:
        # audio + show notes are already saved locally at this point (or the build failed): free the space
        if cfg.delete_notebook:
            try:
                await client.notebooks.delete(nb.id)
                log.info("notebook %s deleted", nb.id)
            except Exception as e:
                log.warning("could not delete notebook %s: %s", nb.id, e)


def build_episode(cfg: NotebookLMConfig, topic: Topic, papers: list[Paper], out_dir: Path,
                  day: date | None = None) -> Episode:
    async def run():
        async with NotebookLMClient.from_storage(profile=cfg.profile, timeout=60, keepalive=600) as client:
            return await build_episode_with(client, cfg, topic, papers, out_dir, day or date.today())
    return asyncio.run(run())
