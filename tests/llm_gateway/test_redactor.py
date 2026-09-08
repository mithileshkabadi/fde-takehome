import re

from llm_gateway.redactor import REDACTED, PiiStreamRedactor
from mocks.llm_provider import NORMAL_CHUNKS, PII_CHUNKS

EMAIL_RE = re.compile(r"[\w.]+@[\w.]+\.\w+")
SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")
CC_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{4}")


def _feed_all(chunks: list[str]) -> tuple[str, list[str]]:
    """Feed every chunk through a fresh redactor, then flush.

    Returns (full reassembled text, list of the non-empty per-feed outputs)
    so tests can check both the end result and that no raw PII ever
    appeared in an intermediate emission.
    """
    redactor = PiiStreamRedactor()
    emissions = []
    for chunk in chunks:
        out = redactor.feed(chunk)
        if out:
            emissions.append(out)
    tail = redactor.flush()
    if tail:
        emissions.append(tail)
    return "".join(emissions), emissions


def test_plain_text_passes_through_unchanged():
    full_text, _ = _feed_all(NORMAL_CHUNKS)
    assert full_text == "".join(NORMAL_CHUNKS)


def test_pii_split_across_chunk_boundaries_is_fully_redacted():
    full_text, emissions = _feed_all(PII_CHUNKS)

    assert not EMAIL_RE.search(full_text)
    assert not SSN_RE.search(full_text)
    assert not CC_RE.search(full_text)
    assert full_text.count("[REDACTED]") == 3
    # Surrounding non-PII text must survive untouched.
    assert "Contact John at" in full_text
    assert "was charged. Thanks!" in full_text

    # No raw PII may appear in any single emission either — not just the
    # final concatenation. This is the actual point of the holdback design:
    # nothing unsafe should ever cross the wire, even transiently.
    for piece in emissions:
        assert not EMAIL_RE.search(piece)
        assert not SSN_RE.search(piece)
        assert not CC_RE.search(piece)


def test_redacted_marker_is_never_split_across_emissions():
    # Regression: a naive character-count split could land inside the
    # "[REDACTED]" marker itself (e.g. "...[REDA" | "CTED]..."). Not a PII
    # leak, but garbled for any consumer that doesn't reassemble first.
    partial_prefixes = [REDACTED[:i] for i in range(1, len(REDACTED))]
    partial_suffixes = [REDACTED[i:] for i in range(1, len(REDACTED))]

    _, emissions = _feed_all(PII_CHUNKS)
    for piece in emissions:
        assert not any(piece.endswith(p) for p in partial_prefixes)
        assert not any(piece.startswith(s) for s in partial_suffixes)


def test_pii_wholly_within_a_single_chunk_is_redacted():
    redactor = PiiStreamRedactor()
    text = "padding text so this exceeds holdback " * 3 + "a@b.co more padding text here"
    out = redactor.feed(text)
    out += redactor.flush()
    assert "a@b.co" not in out
    assert "[REDACTED]" in out


def test_short_content_is_fully_held_back_until_flush():
    redactor = PiiStreamRedactor()
    assert redactor.feed("short") == ""
    assert redactor.flush() == "short"


def test_feed_returns_empty_while_under_holdback_threshold():
    redactor = PiiStreamRedactor()
    # One char under the threshold: nothing should be safe to emit yet.
    text = "x" * (PiiStreamRedactor.HOLDBACK)
    assert redactor.feed(text) == ""
    assert redactor.flush() == text


def test_feed_emits_once_past_holdback_threshold():
    redactor = PiiStreamRedactor()
    text = "x" * (PiiStreamRedactor.HOLDBACK + 1)
    emitted = redactor.feed(text)
    assert emitted == "x"
    assert redactor.flush() == "x" * PiiStreamRedactor.HOLDBACK


def test_flush_on_empty_redactor_returns_empty_string():
    redactor = PiiStreamRedactor()
    assert redactor.flush() == ""
