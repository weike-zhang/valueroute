from __future__ import annotations

from datetime import datetime, timedelta, timezone

from valueroute.application.service import overlaps
from valueroute.domain.errors import DomainError
from valueroute.domain.models import ResourceRegion, WriterLease, new_id
from valueroute.storage.store import Store


class LeaseManager:
    """Process-local lease application service; journal commit is the durable boundary."""

    def __init__(self, store: Store):
        self.store = store

    def acquire(self, child_task_id: str, owner_agent_id: str, region: ResourceRegion, *, ttl: timedelta = timedelta(seconds=60), acquired_at: datetime | None = None) -> WriterLease:
        acquired_at = acquired_at or datetime.now(timezone.utc)
        for lease in self.store.leases.values():
            if lease.status == "active" and overlaps(lease.region, region):
                raise DomainError("lease_overlap", "resource regions overlap")
        lease = WriterLease(id=new_id("lease"), child_task_id=child_task_id, owner_agent_id=owner_agent_id, region=region, acquired_at=acquired_at, expires_at=acquired_at + ttl)
        self.store.leases[lease.id] = lease
        self.store.commit({"type": "lease.acquired", "data": lease.model_dump(mode="json")})
        return lease

    def release(self, lease_id: str) -> WriterLease:
        lease = self.store.leases.get(lease_id)
        if not lease:
            raise DomainError("not_found", "lease not found", 404)
        if lease.status != "active":
            raise DomainError("invalid_transition", "lease is already terminal")
        released = lease.model_copy(update={"status": "released", "version": lease.version + 1})
        self.store.leases[lease_id] = released
        self.store.commit({"type": "lease.released", "data": {"id": lease_id}})
        return released

    def heartbeat(self, lease_id: str, *, ttl: timedelta = timedelta(seconds=60), at: datetime | None = None) -> WriterLease:
        lease = self.store.leases.get(lease_id)
        if not lease:
            raise DomainError("not_found", "lease not found", 404)
        if lease.status != "active":
            raise DomainError("invalid_transition", "lease is not active")
        at = at or datetime.now(timezone.utc)
        renewed = lease.model_copy(update={"expires_at": at + ttl, "version": lease.version + 1})
        self.store.leases[lease_id] = renewed
        self.store.commit({"type": "lease.heartbeat", "data": renewed.model_dump(mode="json")})
        return renewed

    def expire(self, *, at: datetime | None = None) -> list[WriterLease]:
        at = at or datetime.now(timezone.utc)
        expired: list[WriterLease] = []
        for lease in list(self.store.leases.values()):
            if lease.status == "active" and lease.expires_at is not None and lease.expires_at <= at:
                updated = lease.model_copy(update={"status": "expired", "version": lease.version + 1})
                self.store.leases[lease.id] = updated
                self.store.commit({"type": "lease.expired", "data": updated.model_dump(mode="json")})
                expired.append(updated)
        return expired
