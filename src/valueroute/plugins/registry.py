"""Public plugin registry (design section 18.3, FR-204).

Third-party plugins register under a stable role; the registry validates the
implementation against the corresponding contract before it becomes usable.
The registry is read-only for execution: registering a plugin never grants it
write, execution, or trust rights beyond the contract shape.
"""

from __future__ import annotations

import inspect
from typing import Any

from valueroute.plugins.contracts import (
    CONTRACT_VERSION,
    PLUGIN_ROLES,
    ControllerSelector,
    Framework,
    Profiler,
    Provider,
    Verifier,
    WorkerPolicy,
)

_ROLE_CONTRACTS: dict[str, type[Any]] = {
    "profiler": Profiler,
    "controller_selector": ControllerSelector,
    "worker_policy": WorkerPolicy,
    "provider": Provider,
    "framework": Framework,
    "verifier": Verifier,
}


class PluginRegistrationError(ValueError):
    """Raised when a plugin fails role/contract validation."""


class PluginRegistry:
    """Role-scoped registry that validates contracts at registration time."""

    def __init__(self, *, contract_version: str = CONTRACT_VERSION) -> None:
        self.contract_version = contract_version
        self._plugins: dict[str, dict[str, Any]] = {}

    def register(self, role: str, name: str, plugin: Any, *, contract_version: str | None = None) -> None:
        if role not in PLUGIN_ROLES:
            raise PluginRegistrationError(f"unknown plugin role: {role}")
        if not name.strip():
            raise PluginRegistrationError("plugin name must not be blank")
        if contract_version is not None and contract_version != self.contract_version:
            raise PluginRegistrationError(
                f"plugin {name} targets contract version {contract_version}; registry expects {self.contract_version}"
            )
        contract = _ROLE_CONTRACTS[role]
        if not isinstance(plugin, contract):
            raise PluginRegistrationError(f"plugin {name} does not satisfy the {role} contract")
        if role == "provider":
            complete = getattr(plugin, "complete", None)
            if complete is None or not inspect.iscoroutinefunction(complete):
                raise PluginRegistrationError(f"plugin {name} does not satisfy the provider contract: complete must be async")
        self._plugins.setdefault(role, {})[name] = plugin

    def get(self, role: str, name: str) -> Any:
        by_role = self._plugins.get(role, {})
        if name not in by_role:
            raise KeyError(f"no plugin {name!r} for role {role}")
        return by_role[name]

    def resolve(self, role: str, *, preferred: str | None = None) -> Any | None:
        by_role = self._plugins.get(role, {})
        if not by_role:
            return None
        if preferred is not None and preferred in by_role:
            return by_role[preferred]
        return next(iter(by_role.values()))

    def registered(self) -> dict[str, list[str]]:
        return {role: sorted(names) for role, names in self._plugins.items()}


__all__ = ["PluginRegistrationError", "PluginRegistry"]
