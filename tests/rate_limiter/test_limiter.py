import asyncio

import pytest

from rate_limiter.limiter import TokenRateLimiter


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "rate_limiter_test.db")


def test_allows_requests_within_budget(db_path):
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)
    result = asyncio.run(limiter.check_and_record("tenant-a", 400))
    assert result.allowed is True
    assert result.used_tokens == 400


def test_rejects_request_that_would_exceed_budget(db_path):
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)
    asyncio.run(limiter.check_and_record("tenant-a", 700))
    result = asyncio.run(limiter.check_and_record("tenant-a", 400))
    assert result.allowed is False
    assert result.used_tokens == 700
    assert result.retry_after_seconds is not None
    assert result.retry_after_seconds > 0


def test_rejected_request_is_not_recorded(db_path):
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)
    asyncio.run(limiter.check_and_record("tenant-a", 700))
    asyncio.run(limiter.check_and_record("tenant-a", 900))  # rejected, must not count
    result = asyncio.run(limiter.check_and_record("tenant-a", 300))
    assert result.allowed is True
    assert result.used_tokens == 1000


def test_tenants_are_tracked_independently(db_path):
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)
    asyncio.run(limiter.check_and_record("tenant-a", 900))
    result_b = asyncio.run(limiter.check_and_record("tenant-b", 900))
    assert result_b.allowed is True


def test_usage_outside_the_window_is_evicted_and_frees_budget(db_path):
    clock = {"now": 1_000_000.0}
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0, clock=lambda: clock["now"])

    asyncio.run(limiter.check_and_record("tenant-a", 900))
    blocked = asyncio.run(limiter.check_and_record("tenant-a", 500))
    assert blocked.allowed is False

    clock["now"] += 61.0  # advance past the 60s window
    result = asyncio.run(limiter.check_and_record("tenant-a", 500))
    assert result.allowed is True
    assert result.used_tokens == 500  # the earlier 900 has aged out


def test_exactly_at_the_limit_is_allowed_one_over_is_not(db_path):
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)
    at_limit = asyncio.run(limiter.check_and_record("tenant-a", 1000))
    assert at_limit.allowed is True

    limiter2 = TokenRateLimiter(str(db_path) + "-b", limit=1000, window_seconds=60.0)
    over_limit = asyncio.run(limiter2.check_and_record("tenant-a", 1001))
    assert over_limit.allowed is False


def test_concurrent_requests_never_exceed_the_budget(db_path):
    """The core correctness property: check-then-record is a classic TOCTOU
    race under concurrency. Fire many concurrent requests that would sum to
    well over budget if unserialized, and confirm the accepted total never
    exceeds the limit."""
    limiter = TokenRateLimiter(db_path, limit=1000, window_seconds=60.0)

    async def _run():
        results = await asyncio.gather(
            *(limiter.check_and_record("tenant-a", 100) for _ in range(30))
        )
        return results

    results = asyncio.run(_run())
    accepted_count = sum(1 for r in results if r.allowed)
    assert accepted_count == 10  # exactly 1000 / 100
    assert sum(1 for r in results if not r.allowed) == 20
    final = asyncio.run(limiter.check_and_record("tenant-a", 1))
    assert final.used_tokens == 1000  # not more, despite 30 concurrent attempts
