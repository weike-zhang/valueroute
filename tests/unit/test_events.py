import pytest

from valueroute.observability.events import (
    EventStreamError,
    deduplicate_events,
    format_sse_frame,
    parse_last_event_id,
)


def test_parse_last_event_id_defaults_and_accepts_decimal_sequences():
    assert parse_last_event_id(None) == 0
    assert parse_last_event_id("") == 0
    assert parse_last_event_id(" 12 ") == 12


@pytest.mark.parametrize("value", ["-1", "+1", "1.0", "abc", True, False])
def test_parse_last_event_id_rejects_illegal_values(value):
    with pytest.raises(EventStreamError):
        parse_last_event_id(value)


def test_deduplicate_events_by_id_or_sequence_and_keep_order():
    events = [
        {"id": "a", "sequence": 1, "payload": {"n": 1}},
        {"id": "a", "sequence": 2, "payload": {"n": 2}},
        {"id": "b", "sequence": 2, "payload": {"n": 2}},
        {"id": "c", "sequence": 3, "payload": {"n": 3}},
    ]
    assert deduplicate_events(events) == [events[0], events[3]]


@pytest.mark.parametrize(
    "events",
    [
        [{"id": "a", "sequence": 0}],
        [{"id": "a", "sequence": 2}, {"id": "b", "sequence": 1}],
        [{"id": "a", "sequence": 1}],
    ],
)
def test_deduplicate_events_rejects_invalid_or_non_monotonic_records(events):
    if len(events) == 1:
        events[0].pop("id")
    with pytest.raises(EventStreamError):
        deduplicate_events(events)


def test_format_sse_frame_is_json_safe_and_has_terminal_blank_line():
    frame = format_sse_frame({"id": "evt-1", "sequence": 7, "payload": {"text": "你好"}})
    assert frame == 'id: 7\ndata: {"id":"evt-1","sequence":7,"payload":{"text":"你好"}}\n\n'
