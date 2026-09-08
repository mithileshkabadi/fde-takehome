"""Token-aware sliding-window rate limiter, backed by on-disk SQLite.

Sliding window *log*, not a fixed-window counter: every accepted request
records `(tenant_key, timestamp, tokens)`; a check sums tokens for that
tenant within the trailing `window_seconds` and evicts rows older than the
window on every call. That avoids the classic fixed-window bug where a
burst can land twice the limit's worth of tokens across a window boundary.

A fresh `sqlite3` connection is opened per call rather than shared across
threads — simplest way to stay correct under `asyncio.to_thread` (each call
may run on a different thread pool thread) without dealing with sqlite3's
same-thread restrictions. The real correctness requirement is elsewhere: the
check-then-record sequence is a classic TOCTOU race under concurrency (two
requests can each read "under budget" before either writes), so every call
for a given tenant is serialized through a per-tenant `asyncio.Lock` —
coarser global locking would be simpler but would serialize unrelated
tenants against each other for no reason.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    used_tokens: int
    limit: int
    retry_after_seconds: float | None = None


class TokenRateLimiter:
    def __init__(
        self,
        db_path: str,
        *,
        limit: int = 50_000,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = db_path
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_key TEXT NOT NULL,
                    ts REAL NOT NULL,
                    tokens INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_usage_tenant ON token_usage(tenant_key, ts)"
            )
            conn.commit()
        finally:
            conn.close()

    async def check_and_record(self, tenant_key: str, tokens: int) -> RateLimitResult:
        lock = self._locks[tenant_key]
        async with lock:
            return await asyncio.to_thread(self._check_and_record_sync, tenant_key, tokens)

    def _check_and_record_sync(self, tenant_key: str, tokens: int) -> RateLimitResult:
        now = self._clock()
        window_start = now - self._window
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM token_usage WHERE tenant_key = ? AND ts < ?",
                (tenant_key, window_start),
            )
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_key = ?",
                (tenant_key,),
            ).fetchone()

            if used + tokens > self._limit:
                (earliest,) = conn.execute(
                    "SELECT MIN(ts) FROM token_usage WHERE tenant_key = ?", (tenant_key,)
                ).fetchone()
                retry_after = (
                    max(0.0, earliest + self._window - now)
                    if earliest is not None
                    else self._window
                )
                conn.commit()
                return RateLimitResult(
                    allowed=False,
                    used_tokens=used,
                    limit=self._limit,
                    retry_after_seconds=retry_after,
                )

            conn.execute(
                "INSERT INTO token_usage (tenant_key, ts, tokens) VALUES (?, ?, ?)",
                (tenant_key, now, tokens),
            )
            conn.commit()
            return RateLimitResult(allowed=True, used_tokens=used + tokens, limit=self._limit)
        finally:
            conn.close()
