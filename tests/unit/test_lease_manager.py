import pytest
from datetime import datetime, timedelta, timezone

from valueroute.domain.models import ResourceRegion
from valueroute.ownership.manager import LeaseManager
from valueroute.application.service import DomainError
from valueroute.storage.journal import LocalJournal
from valueroute.storage.store import Store


def test_lease_manager_rejects_overlap_and_releases(tmp_path):
    journal = LocalJournal(tmp_path)
    try:
        manager = LeaseManager(Store(journal))
        first = manager.acquire("ct_a", "owner_a", ResourceRegion(resource_kind="file", resource_id="a.py", selector_type="symbol", selector_value="run", base_revision="r1"))
        with pytest.raises(DomainError, match="overlap"):
            manager.acquire("ct_b", "owner_b", ResourceRegion(resource_kind="file", resource_id="a.py", selector_type="symbol", selector_value="run", base_revision="r1"))
        assert manager.release(first.id).status == "released"
    finally:
        journal.close()


def test_lease_heartbeat_and_expiry_are_durable(tmp_path):
    journal = LocalJournal(tmp_path)
    try:
        store = Store(journal)
        manager = LeaseManager(store)
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        region = ResourceRegion(resource_kind="file", resource_id="a.py", selector_type="whole_resource", selector_value="", base_revision="r1")
        lease = manager.acquire("child", "owner", region, ttl=timedelta(seconds=5), acquired_at=moment)
        renewed = manager.heartbeat(lease.id, ttl=timedelta(seconds=10), at=moment + timedelta(seconds=1))
        assert renewed.expires_at == moment + timedelta(seconds=11)
        assert manager.expire(at=moment + timedelta(seconds=6)) == []
        expired = manager.expire(at=moment + timedelta(seconds=12))
        assert expired[0].status == "expired"
    finally:
        journal.close()
