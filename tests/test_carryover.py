from datetime import date, timedelta

import pytest

from podcast import pipeline
from podcast.config import Config, NotebookLMConfig, ScreeningConfig, Topic
from podcast.models import Episode, Paper, Screening
from podcast.state import State

TOPIC = Topic(name="Muscle MRI", keywords=["muscle"])
PDF = b"%PDF-1.7\n" + b"x" * 30_000 + b"\n%%EOF"


def mk(i, relevance=8.0, published=None, **kw):
    return Paper(source="x", source_id=str(i), title=f"Paper {i}", abstract="a", authors=["A"],
                 pdf_urls=[f"https://x/{i}.pdf"], published=published or date.today(),
                 screening=Screening(relevance=relevance, quality=8, engaging=0.9, keep=True), **kw)


def cfg(tmp_path, **kw):
    return Config(topics=[TOPIC], data_dir=tmp_path, notebooklm=NotebookLMConfig(min_sources=2),
                  screening=ScreeningConfig(top_n=3), **kw)


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "s.db")


def statuses(state):
    return dict(state.db.execute("SELECT key, status FROM papers").fetchall())


@pytest.fixture
def fake_pipeline(monkeypatch, tmp_path):
    """No network: discovery, screening and PDF download are replaced; builds produce a trivial episode."""
    box = type("Box", (), {})()
    box.new, box.built, box.fail_build = [], [], False

    monkeypatch.setattr(pipeline, "discover", lambda cfg, topic: list(box.new))
    monkeypatch.setattr(pipeline, "screen", lambda scfg, topic, papers: papers)  # papers arrive pre-screened

    def fake_download(client, paper, dest_dir, use_browser=True):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{paper.source_id}.pdf"
        path.write_bytes(PDF)
        box.downloaded = getattr(box, "downloaded", []) + [paper.source_id]
        return path

    def fake_build(ncfg, topic, papers, out_dir, day=None):
        if box.fail_build:
            raise RuntimeError("NotebookLM down")
        out_dir.mkdir(parents=True, exist_ok=True)
        box.built.append([p.source_id for p in papers])
        return Episode(topic=topic.name, date=date.today(), title="T", audio_path=str(out_dir / "episode.m4a"), papers=papers)

    monkeypatch.setattr(pipeline, "download_pdf", fake_download)
    monkeypatch.setattr(pipeline, "build_episode", fake_build)
    return box


def test_too_few_papers_are_saved_for_the_next_run(tmp_path, state, fake_pipeline):
    fake_pipeline.new = [mk(1)]
    _, chosen, episode = pipeline.run_topic(cfg(tmp_path), TOPIC, state, "run1")
    assert episode is None and [p.source_id for p in chosen] == ["1"]
    assert statuses(state) == {"x:1": "carryover"}
    assert fake_pipeline.built == []


def test_carried_paper_joins_a_new_one_and_is_not_downloaded_again(tmp_path, state, fake_pipeline):
    c = cfg(tmp_path)
    fake_pipeline.new = [mk(1)]
    pipeline.run_topic(c, TOPIC, state, "run1")
    assert fake_pipeline.downloaded == ["1"]

    fake_pipeline.new = [mk(2)]  # a week later: only one new paper, but the leftover completes the pair
    _, chosen, episode = pipeline.run_topic(c, TOPIC, state, "run2")
    assert episode is not None and sorted(fake_pipeline.built[0]) == ["1", "2"]
    assert fake_pipeline.downloaded == ["1", "2"]  # paper 1 was reused, not fetched again
    assert statuses(state) == {"x:1": "used", "x:2": "used"}


def test_carried_papers_are_not_screened_or_discovered_twice(tmp_path, state, fake_pipeline):
    c = cfg(tmp_path)
    fake_pipeline.new = [mk(1)]
    pipeline.run_topic(c, TOPIC, state, "run1")
    fake_pipeline.new = [mk(1)]  # the search finds the same paper again (still inside the window)
    _, chosen, episode = pipeline.run_topic(c, TOPIC, state, "run2")
    assert episode is None and [p.source_id for p in chosen] == ["1"]  # counted once, not twice
    assert statuses(state) == {"x:1": "carryover"}


def test_carryover_ranks_with_new_papers_and_unchosen_ones_keep_waiting(tmp_path, state, fake_pipeline):
    c = cfg(tmp_path)
    c.screening.top_n = 2
    fake_pipeline.new = [mk(1, relevance=5.0)]
    pipeline.run_topic(c, TOPIC, state, "run1")
    fake_pipeline.new = [mk(2, relevance=9.0), mk(3, relevance=8.0)]
    pipeline.run_topic(c, TOPIC, state, "run2")
    assert sorted(fake_pipeline.built[0]) == ["2", "3"]  # the weaker leftover lost out...
    assert statuses(state)["x:1"] == "carryover"  # ...but keeps waiting for next week


def test_old_carryover_expires_and_is_never_rediscovered(tmp_path, state, fake_pipeline):
    c = cfg(tmp_path, carryover_days=28)
    fake_pipeline.new = [mk(1, published=date.today() - timedelta(days=40))]
    pipeline.run_topic(c, TOPIC, state, "run1")
    assert statuses(state)["x:1"] == "carryover"
    fake_pipeline.new = [mk(2)]
    _, chosen, episode = pipeline.run_topic(c, TOPIC, state, "run2")
    assert [p.source_id for p in chosen] == ["2"] and episode is None  # the 40-day-old paper no longer counts
    assert statuses(state)["x:1"] == "expired"
    assert "x:1" in state.known_keys(TOPIC.name)  # still known, so a later search cannot resurrect it


def test_carryover_disabled_releases_papers(tmp_path, state, fake_pipeline):
    fake_pipeline.new = [mk(1)]
    pipeline.run_topic(cfg(tmp_path, carryover_days=0), TOPIC, state, "run1")
    assert statuses(state) == {}  # forgotten: eligible again only if the search finds them again


def test_failed_build_keeps_papers_for_the_retry(tmp_path, state, fake_pipeline):
    c = cfg(tmp_path)
    fake_pipeline.new = [mk(1), mk(2)]
    fake_pipeline.fail_build = True
    with pytest.raises(RuntimeError, match="NotebookLM down"):
        pipeline.run_topic(c, TOPIC, state, "run1")
    assert statuses(state) == {"x:1": "carryover", "x:2": "carryover"}
    fake_pipeline.fail_build = False
    fake_pipeline.new = []  # nothing new next time: the retry still produces the episode
    _, _, episode = pipeline.run_topic(c, TOPIC, state, "run2")
    assert episode is not None and sorted(fake_pipeline.built[0]) == ["1", "2"]


def test_state_expiry_uses_updated_date_when_paper_has_no_date(tmp_path, state):
    p = mk(1)
    p.published = None
    state.record(p, "T", "carryover", "r")
    assert [q.source_id for q in state.carryover("T", 28)] == ["1"]
    state.db.execute("UPDATE papers SET updated=?", ((date.today() - timedelta(days=60)).isoformat() + " 00:00:00",))
    assert state.carryover("T", 28) == []
