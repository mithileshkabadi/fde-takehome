import asyncio
import json
import re
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import app, get_upstream_client
from mocks.llm_provider import NORMAL_CHUNKS
from mocks.llm_provider import app as upstream_app

EMAIL_RE = re.compile(r"[\w.]+@[\w.]+\.\w+")
SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
CC_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{4}")


@pytest.fixture
def gateway_client() -> Iterator[TestClient]:
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app), base_url="http://upstream-mock"
    )
    app.dependency_overrides[get_upstream_client] = lambda: upstream_client
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        asyncio.run(upstream_client.aclose())


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


def test_health(gateway_client: TestClient):
    resp = gateway_client.get("/health")
    assert resp.status_code == 200


def test_normal_stream_passes_through_unredacted(gateway_client: TestClient):
    with gateway_client.stream(
        "POST", "/v1/completions?behavior=normal", json={"prompt": "hi"}
    ) as resp:
        assert resp.status_code == 200
        events = _collect_sse_events(resp)
    assert events[-1] == "[DONE]"
    assert _reassemble_content(events) == "".join(NORMAL_CHUNKS)


def test_pii_stream_is_fully_redacted_despite_upstream_chunk_boundary_splits(
    gateway_client: TestClient,
):
    url = "/v1/completions?behavior=pii"
    with gateway_client.stream("POST", url, json={"prompt": "hi"}) as resp:
        assert resp.status_code == 200
        events = _collect_sse_events(resp)

    assert events[-1] == "[DONE]"
    content_events = [e for e in events if e != "[DONE]"]
    # The mock splits three PII patterns across four chunks; redaction
    # should still emit progressively, not as one final blob.
    assert len(content_events) >= 2

    full_text = _reassemble_content(events)
    assert not EMAIL_RE.search(full_text)
    assert not SSN_RE.search(full_text)
    assert not CC_RE.search(full_text)
    assert full_text.count("[REDACTED]") == 3
    assert "Contact John at" in full_text
    assert "was charged. Thanks!" in full_text

    # No raw PII may appear in any single wire event either.
    for event in content_events:
        text = event["choices"][0]["delta"]["content"]
        assert not EMAIL_RE.search(text)
        assert not SSN_RE.search(text)
        assert not CC_RE.search(text)


def test_rate_limited_upstream_response_is_passed_through_as_is(gateway_client: TestClient):
    resp = gateway_client.post("/v1/completions?behavior=rate_limited", json={"prompt": "hi"})
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"


def test_mock_behavior_header_is_forwarded_to_upstream(gateway_client: TestClient):
    resp = gateway_client.post(
        "/v1/completions", json={"prompt": "hi"}, headers={"X-Mock-Behavior": "rate_limited"}
    )
    assert resp.status_code == 429
