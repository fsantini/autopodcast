from datetime import date
from types import SimpleNamespace as NS

import pytest

from podcast.config import NotebookLMConfig, Topic
from podcast.models import Paper
from podcast.notebook import build_episode_with, notebook_title, parse_show_notes, sniff_audio_ext

TOPIC = Topic(name="Muscle MRI", keywords=["k"])
ANSWER = """**TITLE:** Fat fractions and fibrosis [1]
SUMMARY: Papers show MRI fat fraction tracks disease [2-3].
It also predicts function.
TAKEAWAYS:
- Smith 2026: fat fraction rises 2%/year
* Doe 2026: T2 mapping is reproducible
"""


def test_parse_show_notes_cleans_markup_and_citations():
    title, summary, items = parse_show_notes(ANSWER)
    assert title == "Fat fractions and fibrosis"
    assert summary.startswith("Papers show MRI fat fraction tracks disease") and "It also predicts function." in summary
    assert "[" not in summary
    assert items == ["Smith 2026: fat fraction rises 2%/year", "Doe 2026: T2 mapping is reproducible"]


def test_parse_show_notes_free_text_falls_back_to_summary():
    title, summary, items = parse_show_notes("Just some prose about the papers.")
    assert title is None and summary == "Just some prose about the papers." and items == []


@pytest.mark.parametrize("head,ext", [(b"\x00\x00\x00\x20ftypM4A ", ".m4a"), (b"ID3\x04\x00\x00", ".mp3"),
                                      (b"\xff\xfb\x90\x00", ".mp3"), (b"OggS\x00", ".ogg"), (b"RIFF\x00\x00\x00\x00WAVE", ".wav")])
def test_sniff_audio_ext(tmp_path, head, ext):
    f = tmp_path / "a.audio"
    f.write_bytes(head + b"\x00" * 64)
    assert sniff_audio_ext(f) == ext


class FakeClient:
    def __init__(self, reject=(), gen_fail=0, delete_error=False, chat_error=False):
        self.deleted, self.reject, self.gen_fail, self.generated = [], set(reject), gen_fail, 0
        self.delete_error, self.chat_error = delete_error, chat_error
        done = NS(is_failed=False, is_rate_limited=False, is_complete=True, error=None, task_id="t", status="completed")
        bad = NS(is_failed=True, is_rate_limited=False, is_complete=False, error="boom", task_id="t", status="failed")

        async def create(title):
            self.title = title
            return NS(id="nb1")

        async def add_file(nb, path, **kw):
            if path in self.reject:
                raise RuntimeError("unsupported")
            return NS(id="src-" + path)

        async def generate_audio(nb, **kw):
            self.generated += 1
            self.kw = kw
            return bad if self.generated <= self.gen_fail else done

        async def wait_for_completion(nb, task_id, **kw):
            return done

        async def download_audio(nb, path, **kw):
            open(path, "wb").write(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100)
            return path

        async def list_audio(nb):
            return [NS(duration_seconds=1234.56)]

        async def ask(nb, q, **kw):
            if self.chat_error:
                raise RuntimeError("chat down")
            return NS(answer=ANSWER)

        async def get_summary(nb):
            return "Automatic notebook summary."

        async def delete(nb):
            if self.delete_error:
                raise RuntimeError("nope")
            self.deleted.append(nb)

        self.notebooks = NS(create=create, delete=delete, get_summary=get_summary)
        self.sources = NS(add_file=add_file)
        self.artifacts = NS(generate_audio=generate_audio, wait_for_completion=wait_for_completion,
                            download_audio=download_audio, list_audio=list_audio)
        self.chat = NS(ask=ask)


def papers(n=3):
    return [Paper(source="x", source_id=str(i), title=f"T{i}", authors=["A"], pdf_path=f"/p{i}.pdf") for i in range(n)]


def cfg(**kw):
    return NotebookLMConfig(**{"min_sources": 2, **kw})


async def test_success_builds_episode_and_deletes_notebook(tmp_path):
    c = FakeClient()
    ep = await build_episode_with(c, cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert c.title == notebook_title(TOPIC, date(2026, 10, 13)) == "Podcast · Muscle MRI · 2026-10-13"
    assert ep.title == "Fat fractions and fibrosis" and ep.duration_seconds == 1234.56 and len(ep.papers) == 3
    assert ep.audio_path.endswith("episode.m4a") and (tmp_path / "episode.m4a").exists()
    assert c.deleted == ["nb1"]
    assert c.kw["source_ids"] == ["src-/p0.pdf", "src-/p1.pdf", "src-/p2.pdf"] and c.kw["language"] == "en"


async def test_delete_is_optional(tmp_path):
    c = FakeClient()
    await build_episode_with(c, cfg(delete_notebook=False), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert c.deleted == []


async def test_rejected_source_is_dropped_but_episode_builds(tmp_path):
    c = FakeClient(reject={"/p1.pdf"})
    ep = await build_episode_with(c, cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert [p.source_id for p in ep.papers] == ["0", "2"]


async def test_too_few_sources_fails_but_still_deletes_notebook(tmp_path):
    c = FakeClient(reject={"/p0.pdf", "/p1.pdf"})
    with pytest.raises(RuntimeError, match="only 1 source"):
        await build_episode_with(c, cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert c.deleted == ["nb1"]


async def test_delete_failure_does_not_lose_episode(tmp_path):
    ep = await build_episode_with(FakeClient(delete_error=True), cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert ep.title


async def test_chat_failure_falls_back_to_notebook_summary(tmp_path):
    ep = await build_episode_with(FakeClient(chat_error=True), cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert ep.summary == "Automatic notebook summary." and ep.title.startswith("Muscle MRI – week of")


async def test_generation_retries_then_succeeds(tmp_path, monkeypatch):
    async def no_sleep(_):
        pass
    monkeypatch.setattr("podcast.notebook.asyncio.sleep", no_sleep)
    c = FakeClient(gen_fail=1)
    await build_episode_with(c, cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert c.generated == 2
    c2 = FakeClient(gen_fail=5)
    with pytest.raises(RuntimeError, match="audio generation failed"):
        await build_episode_with(c2, cfg(), TOPIC, papers(), tmp_path, date(2026, 10, 13))
    assert c2.deleted == ["nb1"]
