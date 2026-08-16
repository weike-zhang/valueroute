import pytest

from valueroute.evidence.verifier import VerifierService
from valueroute.plugins import (
    CONTRACT_VERSION,
    PLUGIN_ROLES,
    ControllerSelector,
    PluginRegistrationError,
    PluginRegistry,
    Profiler,
    Verifier,
)
from valueroute.routing.manifest import ModelProfile
from valueroute.routing.profiler import Profiler as BuiltinProfiler
from valueroute.routing.rank import ControllerRank, ControllerRanker


def certified_profile() -> ModelProfile:
    return ModelProfile.model_validate({
        "provider_id": "openai",
        "model_id": "gpt-x",
        "measured_at": "2026-08-15T00:00:00Z",
        "protocol_status": "compatible",
        "worker_status": "candidate",
        "controller_status": "certified",
        "supported_modalities": ["text"],
        "supported_tools": [],
        "effort_mapping": {},
        "region": "test",
        "evidence_refs": [],
    })


def test_contracts_are_runtime_checkable_against_builtin_implementations():
    assert isinstance(BuiltinProfiler(), Profiler)
    assert isinstance(ControllerRanker(), ControllerSelector)
    assert isinstance(VerifierService("store", "ownership", "reviews"), Verifier)


def test_all_six_roles_are_public():
    assert {
        "profiler",
        "controller_selector",
        "worker_policy",
        "provider",
        "framework",
        "verifier",
    } == PLUGIN_ROLES
    assert CONTRACT_VERSION == "0.1.0"


def test_register_and_resolve_builtin_plugin():
    registry = PluginRegistry()
    registry.register("profiler", "builtin", BuiltinProfiler())
    registry.register("controller_selector", "builtin", ControllerRanker())
    assert registry.resolve("profiler") is not None
    assert registry.get("profiler", "builtin") is not None
    assert registry.registered() == {"profiler": ["builtin"], "controller_selector": ["builtin"]}


def test_register_rejects_unknown_role():
    registry = PluginRegistry()
    with pytest.raises(PluginRegistrationError, match="unknown plugin role"):
        registry.register("mystery", "x", object())


def test_register_rejects_blank_name():
    registry = PluginRegistry()
    with pytest.raises(PluginRegistrationError, match="must not be blank"):
        registry.register("profiler", "  ", BuiltinProfiler())


def test_register_rejects_contract_version_mismatch():
    registry = PluginRegistry()
    with pytest.raises(PluginRegistrationError, match="targets contract version"):
        registry.register("profiler", "future", BuiltinProfiler(), contract_version="9.9.9")


def test_register_rejects_object_that_does_not_satisfy_contract():
    registry = PluginRegistry()
    with pytest.raises(PluginRegistrationError, match="does not satisfy"):
        registry.register("profiler", "not-a-profiler", object())


def test_register_rejects_wrong_shape_for_provider():
    registry = PluginRegistry()

    class NotAProvider:
        def complete(self, task_id, input_text):
            return None  # missing keyword-only contract shape

    with pytest.raises(PluginRegistrationError, match="does not satisfy"):
        registry.register("provider", "bad", NotAProvider())


def test_resolve_uses_preferred_then_first():
    registry = PluginRegistry()

    class CustomProfiler:
        def profile(self, envelope):
            return None

    registry.register("profiler", "a", CustomProfiler())
    registry.register("profiler", "b", BuiltinProfiler())
    assert registry.resolve("profiler", preferred="a") is not None
    assert registry.resolve("profiler") is not None


def test_resolve_returns_none_for_unregistered_role():
    assert PluginRegistry().resolve("worker_policy") is None


def test_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        PluginRegistry().get("profiler", "missing")


def test_controller_selector_contract_matches_ranker_signature():
    ranker = ControllerRanker()
    selected = ranker.select([certified_profile()])
    assert isinstance(selected, ControllerRank)
    assert selected.profile.model_id == "gpt-x"
