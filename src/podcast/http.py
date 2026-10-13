from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

UA = "auto-scientific-podcast/0.1 (+mailto:{email})"


def make_client(email: str | None = None, timeout: float = 60) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA.format(email=email or "unknown")},
        timeout=timeout,
        follow_redirects=True,
    )


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=2, max=30), reraise=True)
def get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    r = client.get(url, **kwargs)
    r.raise_for_status()
    return r
