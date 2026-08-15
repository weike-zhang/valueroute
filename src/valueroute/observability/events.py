"""Small, side-effect-free helpers for journal-backed SSE events.

The journal remains the source of truth.  These helpers only validate and
shape records at the transport boundary; they do not modify a Store or the
journal itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


class EventStreamError(ValueError):
    """Raised when an event stream cursor or event record is unsafe."""


def parse_last_event_id(value: str | int | None) -> int:
    """Parse ``Last-Event-ID`` as a non-negative journal sequence.

    An absent or empty header means the beginning of the stream.  Booleans,
    floats, signs, and other non-decimal values are rejected deliberately so
    an invalid cursor cannot silently broaden or rewind a replay window.
    """

    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, bool):
        raise EventStreamError("last_event_id must be a non-negative integer")
    text = str(value).strip()
    if not text.isdecimal():
        raise EventStreamError("last_event_id must be a non-negative integer")
    sequence = int(text)
    if sequence < 0:
        raise EventStreamError("last_event_id must be a non-negative integer")
    return sequence


def _sequence(event: Mapping[str, Any]) -> int:
    value = event.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EventStreamError("event sequence must be a positive integer")
    return value


def deduplicate_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return first occurrences, deduplicated by event ``id`` or sequence.

    Records must be in journal order.  A repeated id/sequence is harmless and
    is ignored; a new record that moves backwards is rejected because it
    indicates a corrupt or incorrectly merged journal page.
    """

    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    sequences: set[int] = set()
    previous = 0
    for raw in events:
        if not isinstance(raw, Mapping):
            raise EventStreamError("event must be an object")
        event = dict(raw)
        sequence = _sequence(event)
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise EventStreamError("event id must be a non-empty string")
        if sequence < previous and sequence not in sequences:
            raise EventStreamError("event sequences must be monotonic")
        duplicate = event_id in ids or sequence in sequences
        ids.add(event_id)
        sequences.add(sequence)
        if duplicate:
            continue
        previous = sequence
        result.append(event)
    return result


def format_sse_frame(event: Mapping[str, Any]) -> str:
    """Encode one validated journal event as an SSE frame."""

    checked = deduplicate_events([event])
    encoded = json.dumps(checked[0], ensure_ascii=False, separators=(",", ":"))
    return f"id: {checked[0]['sequence']}\ndata: {encoded}\n\n"
