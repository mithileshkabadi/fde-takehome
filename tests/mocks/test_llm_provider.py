import json
import re
import time

from fastapi.testclient import TestClient

from mocks.llm_provider import NORMAL_CHUNKS, PII_CHUNKS, app

client = TestClient(app)

EMAIL_RE = re.compile(r"[\w.]+@[\w.]+\.\w+")
SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
CC_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{4}")


def _collect_sse_events(response) -> list[dict | str]:
    events: list[dict | str] = []
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _reassemble_content(events: list[dict | str]) -> str:
    text = ""
    for event in events:
        if event == "[DONE]":
            continue
        for choice in event["choices"]:
            text += choice.get("delta", {}).get("content", "")
    return text


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_normal_stream_reassembles_to_expected_text():
    with client.stream("POST", "/v1/completions", json={"prompt": "hi"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _collect_sse_events(resp)
    assert events[-1] == "[DONE]"
    assert _reassemble_content(events) == "".join(NORMAL_CHUNKS)


def test_rate_limited_via_header_returns_429_without_streaming():
    resp = client.post(
        "/v1/completions", json={"prompt": "hi"}, headers={"X-Mock-Behavior": "rate_limited"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"
    assert "Retry-After" in resp.headers


def test_rate_limited_via_query_param_returns_429():
    resp = client.post("/v1/completions?behavior=rate_limited", json={"prompt": "hi"})
    assert resp.status_code == 429


def test_header_behavior_takes_precedence_over_query_param():
    resp = client.post(
        "/v1/completions?behavior=normal",
        json={"prompt": "hi"},
        headers={"X-Mock-Behavior": "rate_limited"},
    )
    assert resp.status_code == 429


def test_slow_behavior_delays_first_chunk_past_requested_delay():
    delay_ms = 200
    start = time.perf_counter()
    with client.stream(
        "POST", f"/v1/completions?behavior=slow&delay_ms={delay_ms}", json={"prompt": "hi"}
    ) as resp:
        assert resp.status_code == 200
        first_line = next(line for line in resp.iter_lines() if line.startswith("data: "))
    elapsed = time.perf_counter() - start
    assert first_line.startswith("data: ")
    assert elapsed >= delay_ms / 1000


def test_pii_stream_contains_email_ssn_and_credit_card_split_across_chunks():
    with client.stream("POST", "/v1/completions?behavior=pii", json={"prompt": "hi"}) as resp:
        assert resp.status_code == 200
        events = _collect_sse_events(resp)

    full_text = _reassemble_content(events)
    assert EMAIL_RE.search(full_text)
    assert SSN_RE.search(full_text)
    assert CC_RE.search(full_text)

    # Confirm the split is real: no single delta chunk contains a whole
    # pattern, so a per-chunk (non-buffering) matcher would miss all three.
    chunk_texts = [
        choice.get("delta", {}).get("content", "")
        for event in events
        if event != "[DONE]"
        for choice in event["choices"]
    ]
    assert chunk_texts == PII_CHUNKS
    for chunk in chunk_texts:
        assert not EMAIL_RE.search(chunk)
        assert not SSN_RE.search(chunk)
        assert not CC_RE.search(chunk)


def test_unknown_behavior_returns_400():
    resp = client.post("/v1/completions?behavior=bogus", json={"prompt": "hi"})
    assert resp.status_code == 400
