from __future__ import annotations

import re
from datetime import date
from typing import Protocol

import httpx

from ..config import Topic
from ..models import Paper


class Source(Protocol):
    name: str

    def search(self, client: httpx.Client, topic: Topic, since: date, until: date, limit: int) -> list[Paper]: ...


def terms(keyword: str) -> list[str]:
    """The words of a keyword. All of them must occur (any order, anywhere); "double quoted" segments stay together
    as exact phrases. So `Deep learning MRI` needs deep, learning and MRI, while `"deep learning" MRI` needs the phrase."""
    return [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', keyword)]


def keyword_in(text_lower: str, keyword: str) -> bool:
    return all(re.search(r"(?<!\w)" + re.escape(t.lower()) + r"(?!\w)", text_lower) for t in terms(keyword))


def excluded(topic: Topic, text: str) -> bool:
    t = text.lower()
    return any(x.lower() in t for x in topic.exclude)


def matches(topic: Topic, text: str) -> bool:
    t = text.lower()
    return any(keyword_in(t, k) for k in topic.keywords) and not excluded(topic, text)
