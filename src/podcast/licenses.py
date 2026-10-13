from __future__ import annotations

import re

# Canonical names: cc0, cc-by, cc-by-sa, cc-by-nc, cc-by-nd, cc-by-nc-sa, cc-by-nc-nd


def normalize_license(raw: str | None) -> str | None:
    """Map free-form license strings / URLs to a canonical Creative Commons name."""
    if not raw:
        return None
    s = raw.lower().strip()
    if "arxiv.org/licenses" in s:
        return "arxiv-" + s.rstrip("/").rsplit("/", 2)[-2].replace("-distrib", "")  # e.g. arxiv-nonexclusive
    if "publicdomain/zero" in s or re.search(r"\bcc[\s_-]?0\b", s) or "cc zero" in s:
        return "cc0"
    m = re.search(r"creativecommons\.org/licen[sc]es/([a-z-]+)", s)
    if m:
        parts = m.group(1)
    else:
        m = re.match(r"^cc[\s_-]?(by[\s_a-z-]*?)(?:[\s_-]?\d(?:\.\d)?)?(?:\s.*)?$", s)
        if not m:
            return None
        parts = m.group(1)
    parts = re.sub(r"[\s_]+", "-", parts.strip("-"))
    if not parts.startswith("by"):
        return None
    toks = [t for t in parts.split("-") if t in {"by", "nc", "nd", "sa"}]
    return "cc-" + "-".join(toks) if toks else None


def is_allowed(license_: str | None, allowed: list[str] | None) -> bool:
    """``allowed=None`` accepts any license, including unknown (the paper only has to be open access)."""
    return allowed is None or (license_ is not None and license_ in allowed)
