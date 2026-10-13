from __future__ import annotations

from datetime import date
from typing import Protocol

import httpx

from ..config import Topic
from ..models import Paper


class Source(Protocol):
    name: str

    def search(self, client: httpx.Client, topic: Topic, since: date, until: date, limit: int) -> list[Paper]: ...


def matches(topic: Topic, text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in topic.keywords) and not any(x.lower() in t for x in topic.exclude)
