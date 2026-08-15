from datetime import datetime, timedelta, timezone

import pytest

from valueroute.execution.claims import AttemptRecord, WorkerClaimService

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_claim_issues_token_and_ttl_and_heartbeat_renews_it():
    service = WorkerClaimService(timedelta(seconds=30))
    claimed = service.claim(AttemptRecord("a1", "ws1"), now=NOW)
    assert claimed.status == "claimed"
    assert claimed.claim and claimed.claim.token
    assert claimed.claim.expires_at == NOW + timedelta(seconds=30)

    renewed = service.heartbeat(claimed, claimed.claim.token, now=NOW + timedelta(seconds=10))
    assert renewed.claim and renewed.claim.expires_at == NOW + timedelta(seconds=40)


def test_heartbeat_rejects_wrong_or_expired_token():
    service = WorkerClaimService(timedelta(seconds=30))
    claimed = service.claim(AttemptRecord("a1", "ws1"), now=NOW)
    with pytest.raises(ValueError, match="invalid or expired"):
        service.heartbeat(claimed, "wrong", now=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="invalid or expired"):
        service.heartbeat(claimed, claimed.claim.token, now=NOW + timedelta(seconds=30))


def test_startup_reclaims_safe_checkpoint_and_blocks_without_one():
    service = WorkerClaimService(timedelta(seconds=30))
    safe = service.claim(AttemptRecord("safe", "ws1", checkpoint_id="cp1"), now=NOW)
    unsafe = service.claim(AttemptRecord("unsafe", "ws2"), now=NOW)
    updated, decisions = service.reclaim_on_startup(
        [safe, unsafe], [type("CP", (), {"id": "cp1", "safe_to_resume": True})()]
    )
    assert [attempt.status for attempt in updated] == ["queued", "blocked"]
    assert [(d.attempt_id, d.action, d.checkpoint_id) for d in decisions] == [("safe", "requeue", "cp1"), ("unsafe", "block", None)]
    assert updated[0].claim is None and updated[1].claim is None


def test_startup_does_not_touch_terminal_attempts():
    service = WorkerClaimService(timedelta(seconds=30))
    attempt = AttemptRecord("done", "ws1", status="failed")
    updated, decisions = service.reclaim_on_startup([attempt])
    assert updated == [attempt]
    assert decisions == []


def test_recovery_creates_a_new_attempt_and_preserves_parent_reference():
    service = WorkerClaimService(timedelta(seconds=30))
    previous = AttemptRecord("failed-1", "ws1", status="failed")
    resumed = service.create_recovery_attempt(previous, checkpoint_id="cp1", attempt_id="attempt-2")
    assert resumed.id == "attempt-2"
    assert resumed.status == "queued"
    assert resumed.resumed_from_attempt_id == previous.id
    assert resumed.recovery_checkpoint_id == "cp1"
    assert previous.status == "failed"
