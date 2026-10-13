from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .models import Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  key TEXT NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL,
  score REAL, run_id TEXT, data TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (key, topic)
);
CREATE TABLE IF NOT EXISTS episodes (
  run_id TEXT NOT NULL, topic TEXT NOT NULL, dir TEXT NOT NULL, status TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (run_id, topic)
);
"""


class State:
    """Tracks papers already screened/used so weekly runs never repeat work."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def known_keys(self, topic: str) -> set[str]:
        rows = self.db.execute("SELECT key FROM papers WHERE topic=?", (topic,))
        return {r[0] for r in rows}

    def record(self, paper: Paper, topic: str, status: str, run_id: str) -> None:
        score = paper.screening.score if paper.screening else None
        self.db.execute(
            "INSERT INTO papers (key, topic, status, score, run_id, data) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(key, topic) DO UPDATE SET status=excluded.status, score=excluded.score, "
            "run_id=excluded.run_id, data=excluded.data, updated=CURRENT_TIMESTAMP",
            (paper.key, topic, status, score, run_id, paper.model_dump_json()),
        )
        self.db.commit()

    def forget(self, keys: list[str], topic: str) -> None:
        """Make papers eligible again (e.g. after a failed episode build)."""
        self.db.executemany("DELETE FROM papers WHERE key=? AND topic=?", [(k, topic) for k in keys])
        self.db.commit()

    def record_episode(self, run_id: str, topic: str, directory: Path, status: str) -> None:
        self.db.execute(
            "INSERT INTO episodes (run_id, topic, dir, status) VALUES (?,?,?,?) "
            "ON CONFLICT(run_id, topic) DO UPDATE SET status=excluded.status, dir=excluded.dir, updated=CURRENT_TIMESTAMP",
            (run_id, topic, str(directory), status))
        self.db.commit()

    def pending_episodes(self) -> list[tuple[str, str, Path]]:
        """Episodes that were built but not published yet (oldest first): (run_id, topic, directory)."""
        rows = self.db.execute("SELECT run_id, topic, dir FROM episodes WHERE status='built' ORDER BY updated")
        return [(r[0], r[1], Path(r[2])) for r in rows]

    def mark_published(self, directory: Path) -> int:
        cur = self.db.execute("UPDATE episodes SET status='published', updated=CURRENT_TIMESTAMP WHERE dir=?",
                              (str(Path(directory).resolve()),))
        self.db.commit()
        return cur.rowcount

    def carryover(self, topic: str, max_age_days: int) -> list[Paper]:
        """Screened papers waiting for enough company to make an episode. Papers published (or, without a date,
        first stored) more than ``max_age_days`` ago expire; they stay in the table so they are never rediscovered."""
        today = date.today()
        waiting, expired = [], []
        for key, data, updated in self.db.execute(
                "SELECT key, data, updated FROM papers WHERE topic=? AND status='carryover'", (topic,)).fetchall():
            paper = Paper.model_validate_json(data)
            reference = paper.published or date.fromisoformat(updated[:10])
            (expired if (today - reference).days > max_age_days else waiting).append((key, paper))
        self.db.executemany("UPDATE papers SET status='expired' WHERE key=? AND topic=?", [(k, topic) for k, _ in expired])
        self.db.commit()
        return [p for _, p in waiting]
