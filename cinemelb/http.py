"""Polite, cache-first HTTP layer.

Every adapter talks to the network through :class:`Http`.  Two behaviours
matter for a scraper that fans out across ~60 venues:

* **Caching.**  Cinema sites are slow and change at most a few times a day.
  Responses are cached on disk keyed by URL + the headers that alter the
  response (notably the "is-ajax-call" header the Flicks API keys on).  A
  ``refresh`` flag on the caller bypasses the cache.
* **Failing loudly but not fatally.**  One dead cinema must not abort a
  refresh; :meth:`Http.get` raises :class:`FetchError` which the orchestrator
  records per-venue.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from .config import CACHE_DIR, USER_AGENT

#: Headers that change *what* a URL returns, and so belong in the cache key.
KEYED_HEADERS = ("is-ajax-call", "accept")


class FetchError(RuntimeError):
    """A request failed in a way the caller should surface per-venue."""

    def __init__(self, url: str, status: int | None, detail: str = "") -> None:
        self.url = url
        self.status = status
        super().__init__(f"{url} -> {status} {detail}".strip())


@dataclass(slots=True)
class Response:
    url: str
    status: int
    text: str
    content_type: str = ""
    from_cache: bool = False

    def json(self) -> Any:
        return json.loads(self.text)


class Http:
    """Thread-safe cached HTTP client."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        ttl: int = 1800,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()
        #: set True to ignore TTL and refetch (a "hard" refresh)
        self.force_refresh = False
        #: per-host politeness: minimum seconds between starts
        self.delay = float(os.environ.get("CINEMELB_DELAY", "0.35"))
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- internals ---------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "en-AU,en;q=0.9",
                }
            )
            self._local.session = s
        return s

    def _cache_path(self, url: str, headers: Mapping[str, str]) -> Path:
        key_bits = [url]
        for h in KEYED_HEADERS:
            v = {k.lower(): val for k, val in headers.items()}.get(h, "")
            key_bits.append(f"{h}={v}")
        key = hashlib.sha256("|".join(key_bits).encode()).hexdigest()[:32]
        return self.cache_dir / key[0:2] / f"{key}.json"

    def _throttle(self, url: str) -> None:
        host = url.split("/")[2] if "//" in url else url
        with self._lock:
            last = self._last_hit.get(host, 0.0)
        wait = self.delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._last_hit[host] = time.monotonic()

    # -- public ------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        fresh: bool = False,
        allow_status: tuple[int, ...] = (),
    ) -> Response:
        headers = dict(headers or {})
        if params:
            from urllib.parse import urlencode

            sep = "&" if "?" in url else "?"
            url = url + sep + urlencode(params, doseq=True)

        path = self._cache_path(url, headers)
        if not fresh and not self.force_refresh and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < self.ttl:
                try:
                    blob = json.loads(path.read_text(encoding="utf-8"))
                    return Response(
                        url=url,
                        status=blob["status"],
                        text=blob["text"],
                        content_type=blob.get("content_type", ""),
                        from_cache=True,
                    )
                except (json.JSONDecodeError, KeyError):
                    path.unlink(missing_ok=True)

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            try:
                r = self._session().get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:  # network-level
                last_err = exc
                time.sleep(0.6 * (attempt + 1) + random.random() * 0.3)
                continue

            if r.status_code in allow_status or r.ok:
                resp = Response(
                    url=url,
                    status=r.status_code,
                    text=r.text,
                    content_type=r.headers.get("Content-Type", ""),
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {"url": url, "status": resp.status, "text": resp.text,
                         "content_type": resp.content_type}
                    ),
                    encoding="utf-8",
                )
                return resp

            if r.status_code in (403, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(1.0 * (attempt + 1) + random.random())
                last_err = FetchError(url, r.status_code)
                continue

            raise FetchError(url, r.status_code, r.text[:200])

        raise FetchError(url, None, str(last_err))

    def get_json(self, url: str, **kw: Any) -> Any:
        resp = self.get(url, headers={"Accept": "application/json", **kw.pop("headers", {})}, **kw)
        return resp.json()


_DEFAULT: Http | None = None


def default_http() -> Http:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Http()
    return _DEFAULT
