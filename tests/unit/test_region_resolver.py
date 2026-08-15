from pathlib import Path

import pytest

from valueroute.domain.models import ResourceRegion, WriterLease
from valueroute.ownership.resolver import (
    RegionResolutionError,
    ResourceObservation,
    SemanticRegionResolverRegistry,
    resolve_changeset,
    resolve_observation,
)
from valueroute.workspaces.local import LocalWorkspaceAdapter


def lease(region: ResourceRegion, owner: str = "owner") -> WriterLease:
    return WriterLease(id="lease", child_task_id="child", owner_agent_id=owner, region=region)


def test_resolver_maps_every_change_to_one_declared_region(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "a.py").write_text("old")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "workers")
    snapshot = adapter.snapshot()
    owner = adapter.create_owner_workspace("owner", snapshot)
    (owner / "a.py").write_text("new")
    changeset = adapter.create_changeset("owner", owner, snapshot)
    region = ResourceRegion(resource_kind="file", resource_id="a.py", selector_type="whole_resource", selector_value="", base_revision=snapshot.revision)
    assert resolve_changeset(changeset, [lease(region)]) == (region,)


def test_resolver_rejects_unresolved_semantic_region(tmp_path: Path):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "a.py").write_text("old")
    adapter = LocalWorkspaceAdapter(canonical, tmp_path / "workers")
    snapshot = adapter.snapshot()
    owner = adapter.create_owner_workspace("owner", snapshot)
    (owner / "a.py").write_text("new")
    changeset = adapter.create_changeset("owner", owner, snapshot)
    symbol = ResourceRegion(resource_kind="file", resource_id="a.py", selector_type="symbol", selector_value="fn", base_revision=snapshot.revision)
    with pytest.raises(RegionResolutionError):
        resolve_changeset(changeset, [lease(symbol)])


def test_semantic_resolution_is_fail_closed_without_trusted_adapter():
    observation = ResourceObservation("database", "orders", "row_keys", [1], "db-r1")
    region = ResourceRegion(
        resource_kind="database", resource_id="orders", selector_type="row_keys",
        selector_value=[1], base_revision="db-r1",
    )
    with pytest.raises(RegionResolutionError, match="no trusted resolver"):
        resolve_observation(observation, [lease(region)])


def test_semantic_adapter_result_still_requires_one_active_matching_lease():
    observation = ResourceObservation("external", "billing", "provider_subresource", "invoice/1", "ext-r1")
    region = ResourceRegion(
        resource_kind="external", resource_id="billing", selector_type="provider_subresource",
        selector_value="invoice/1", base_revision="ext-r1",
    )

    class Adapter:
        def resolve(self, value: ResourceObservation) -> ResourceRegion:
            assert value == observation
            return region

    assert resolve_observation(observation, [lease(region)], Adapter()) == region
    with pytest.raises(RegionResolutionError, match="exactly one"):
        resolve_observation(observation, [lease(region), lease(region, "other")], Adapter())


def test_semantic_adapter_cannot_change_identity_or_revision():
    observation = ResourceObservation("database", "orders", "key_range", {"start": 1}, "db-r1")
    wrong = ResourceRegion(
        resource_kind="database", resource_id="orders", selector_type="key_range",
        selector_value={"start": 2}, base_revision="db-r1",
    )

    class Adapter:
        def resolve(self, value: ResourceObservation) -> ResourceRegion:
            return wrong

    with pytest.raises(RegionResolutionError, match="different selector"):
        resolve_observation(observation, [], Adapter())


def test_registry_requires_explicit_supported_registration_and_fail_closed_restart():
    observation = ResourceObservation("database", "orders", "row_keys", [1], "db-r1")
    region = ResourceRegion(
        resource_kind="database", resource_id="orders", selector_type="row_keys",
        selector_value=[1], base_revision="db-r1",
    )

    class Adapter:
        def resolve(self, value: ResourceObservation) -> ResourceRegion:
            return region

    registry = SemanticRegionResolverRegistry()
    registry.register("database", "row_keys", Adapter())
    assert resolve_observation(observation, [lease(region)], registry) == region
    with pytest.raises(RegionResolutionError, match="duplicate"):
        registry.register("database", "row_keys", Adapter())
    restarted = SemanticRegionResolverRegistry()
    with pytest.raises(RegionResolutionError, match="no trusted resolver"):
        resolve_observation(observation, [lease(region)], restarted)


def test_registry_rejects_unsupported_or_failing_resolvers():
    registry = SemanticRegionResolverRegistry()

    class Broken:
        def resolve(self, value: ResourceObservation) -> ResourceRegion:
            raise RuntimeError("provider unavailable")

    with pytest.raises(RegionResolutionError, match="unsupported"):
        registry.register("database", "json_pointer", Broken())
    registry.register("external", "provider_subresource", Broken())
    observation = ResourceObservation("external", "billing", "provider_subresource", "invoice/1", "r1")
    with pytest.raises(RegionResolutionError, match="trusted resolver failed"):
        registry.resolve(observation)
