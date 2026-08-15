"""Fail-closed mapping from observed workspace changes to declared regions."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from valueroute.domain.models import ResourceRegion, WriterLease
from valueroute.workspaces.local import ChangeSet


class RegionResolutionError(ValueError):
    code = "region_resolution_failed"


@dataclass(frozen=True)
class ResourceObservation:
    """Provider-neutral observation of one non-file resource change.

    Adapters for a database or external provider may translate their native
    change event into this value.  The resolver still owns the final lease and
    revision checks; an adapter cannot assert ownership by returning a region
    alone.
    """

    resource_kind: str
    resource_id: str
    selector_type: str
    selector_value: object
    base_revision: str


@runtime_checkable
class SemanticRegionResolver(Protocol):
    """Optional adapter boundary for database/external observations.

    Implementations return a *candidate* region.  ``resolve_observation``
    validates it against active leases before accepting it.
    """

    def resolve(self, observation: ResourceObservation) -> ResourceRegion: ...


ResolverKey = tuple[str, str]
_SUPPORTED_RESOLVER_KEYS: frozenset[ResolverKey] = frozenset(
    {
        ("database", "row_keys"),
        ("database", "key_range"),
        ("database", "partition"),
        ("external", "provider_subresource"),
    }
)


class SemanticRegionResolverRegistry:
    """In-process registry for explicitly trusted semantic resolvers.

    The registry is deliberately not serializable or auto-populated. A fresh
    instance after a process restart has no registrations and therefore fails
    closed until the host explicitly installs them again.
    """

    def __init__(self) -> None:
        self._resolvers: dict[ResolverKey, SemanticRegionResolver] = {}

    @property
    def registered_keys(self) -> frozenset[ResolverKey]:
        return frozenset(self._resolvers)

    def register(
        self,
        resource_kind: str,
        selector_type: str,
        resolver: SemanticRegionResolver,
    ) -> None:
        key = (resource_kind, selector_type)
        if key not in _SUPPORTED_RESOLVER_KEYS:
            raise RegionResolutionError(f"unsupported semantic resolver key: {resource_kind}/{selector_type}")
        if key in self._resolvers:
            raise RegionResolutionError(f"duplicate semantic resolver registration: {resource_kind}/{selector_type}")
        if not callable(getattr(resolver, "resolve", None)):
            raise RegionResolutionError("semantic resolver must provide callable resolve()")
        self._resolvers[key] = resolver

    def unregister(self, resource_kind: str, selector_type: str) -> None:
        self._resolvers.pop((resource_kind, selector_type), None)

    def clear(self) -> None:
        self._resolvers.clear()

    def resolve(self, observation: ResourceObservation) -> ResourceRegion:
        key = (observation.resource_kind, observation.selector_type)
        resolver = self._resolvers.get(key)
        if resolver is None:
            raise RegionResolutionError(f"no trusted resolver configured for {key[0]}/{key[1]}")
        try:
            candidate = resolver.resolve(observation)
        except Exception as exc:
            raise RegionResolutionError(f"trusted resolver failed for {key[0]}/{key[1]}") from exc
        if not isinstance(candidate, ResourceRegion):
            raise RegionResolutionError("trusted resolver returned an invalid region")
        return candidate


class FailClosedSemanticRegionResolver:
    """Default for DB/external resources until a trusted adapter is installed."""

    def resolve(self, observation: ResourceObservation) -> ResourceRegion:
        raise RegionResolutionError(
            f"no trusted resolver configured for "
            f"{observation.resource_kind}/{observation.selector_type}"
        )


def _validate_candidate(candidate: ResourceRegion, observation: ResourceObservation) -> None:
    if candidate.resource_kind != observation.resource_kind:
        raise RegionResolutionError("resolver returned a different resource kind")
    if candidate.resource_id != observation.resource_id or candidate.selector_type != observation.selector_type:
        raise RegionResolutionError("resolver returned a different resource identity")
    if candidate.selector_value != observation.selector_value:
        raise RegionResolutionError("resolver returned a different selector")
    if candidate.base_revision != observation.base_revision:
        raise RegionResolutionError("resolver returned a different base revision")


def resolve_observation(
    observation: ResourceObservation,
    leases: Iterable[WriterLease],
    resolver: SemanticRegionResolver | SemanticRegionResolverRegistry | None = None,
) -> ResourceRegion:
    """Resolve a DB/external observation without allowing an implicit pass."""
    if observation.resource_kind not in {"database", "external"}:
        raise RegionResolutionError("semantic resolver only accepts database/external observations")
    candidate = (resolver or FailClosedSemanticRegionResolver()).resolve(observation)
    _validate_candidate(candidate, observation)
    active = [lease.region for lease in leases if lease.status == "active"]
    # Reuse the domain overlap rules without making ownership import-time
    # dependent on the application service module.
    from valueroute.application.service import overlaps

    matches = [region for region in active if overlaps(region, candidate)]
    if len(matches) != 1:
        raise RegionResolutionError(
            f"expected exactly one active lease for semantic region, got {len(matches)}"
        )
    return matches[0]


def resolve_change_path(path: str, base_revision: str, leases: Iterable[WriterLease]) -> ResourceRegion:
    matches: list[ResourceRegion] = []
    for lease in leases:
        if lease.status != "active" or lease.region.base_revision != base_revision:
            continue
        region = lease.region
        if region.resource_kind == "file" and region.selector_type == "whole_resource" and region.resource_id == path:
            matches.append(region)
        elif region.resource_kind == "directory" and region.selector_type == "path_prefix":
            prefix = str(region.selector_value).rstrip("/")
            if path == prefix or path.startswith(prefix + "/"):
                matches.append(region)
        elif region.resource_kind in {"database", "external"} or region.selector_type in {"symbol", "ast_node", "row_keys", "key_range", "partition", "json_pointer", "provider_subresource"}:
            raise RegionResolutionError(f"cannot resolve workspace file change {path!r} against {region.resource_kind}/{region.selector_type}")
    if len(matches) != 1:
        raise RegionResolutionError(f"expected exactly one region for changed path {path!r}, got {len(matches)}")
    return matches[0]


def resolve_changeset(changeset: ChangeSet, leases: Iterable[WriterLease]) -> tuple[ResourceRegion, ...]:
    """Resolve every actual changed path; one failure rejects the whole ChangeSet."""
    lease_list = list(leases)
    return tuple(resolve_change_path(change.path, changeset.base_revision, lease_list) for change in changeset.files)
