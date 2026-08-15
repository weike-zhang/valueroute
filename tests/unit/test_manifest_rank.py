import json

import pytest

from valueroute.routing.manifest import ModelProfile, load_model_manifests
from valueroute.routing.rank import ControllerRanker, build_controller_epoch


def profile(**overrides):
    base = {
        "provider_id": "openai",
        "model_id": "gpt-5-6-mini",
        "measured_at": "2026-08-15T00:00:00Z",
        "protocol_status": "compatible",
        "worker_status": "candidate",
        "controller_status": "candidate",
        "supported_modalities": ["text"],
        "supported_tools": [],
        "effort_mapping": {},
        "region": "test-region",
        "evidence_refs": [],
    }
    base.update(overrides)
    return ModelProfile.model_validate(base)


class TestModelProfile:
    def test_eligible_for_controller_requires_certified_and_compatible(self):
        assert profile(controller_status="certified").eligible_for_controller() is True
        assert profile(controller_status="candidate").eligible_for_controller() is False
        assert profile(controller_status="suspended").eligible_for_controller() is False
        assert profile(controller_status="certified", protocol_status="incompatible").eligible_for_controller() is False

    def test_eligible_for_worker_is_role_independent(self):
        worker = profile(worker_status="certified", controller_status="candidate")
        assert worker.eligible_for_worker() is True
        assert worker.eligible_for_controller() is False

    def test_blank_provider_or_model_rejected(self):
        with pytest.raises(ValueError):
            profile(provider_id="   ")
        with pytest.raises(ValueError):
            profile(model_id="   ")


class TestLoadModelManifests:
    def test_loads_all_profiles_and_skips_schema(self, tmp_path):
        (tmp_path / "model-manifest.schema.json").write_text("{}")
        for name, status in (("a.json", "certified"), ("b.json", "candidate")):
            (tmp_path / name).write_text(
                json.dumps({"model_profile": {
                    "provider_id": "openai", "model_id": name.split(".")[0],
                    "measured_at": "2026-08-15T00:00:00Z", "protocol_status": "compatible",
                    "worker_status": status, "controller_status": status,
                    "supported_modalities": ["text"], "supported_tools": [], "effort_mapping": {},
                    "region": "test", "evidence_refs": [],
                }})
            )
        profiles = load_model_manifests(tmp_path)
        assert len(profiles) == 2
        assert {p.model_id for p in profiles} == {"a", "b"}

    def test_skips_invalid_manifest_without_crashing(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not json")
        (tmp_path / "good.json").write_text(
            json.dumps({"model_profile": {
                "provider_id": "openai", "model_id": "good",
                "measured_at": "2026-08-15T00:00:00Z", "protocol_status": "compatible",
                "worker_status": "certified", "controller_status": "certified",
                "supported_modalities": ["text"], "supported_tools": [], "effort_mapping": {},
                "region": "test", "evidence_refs": [],
            }})
        )
        profiles = load_model_manifests(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].model_id == "good"

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_model_manifests(tmp_path / "nope")


class TestControllerRanker:
    def test_only_certified_controllers_are_selected(self):
        profiles = [
            profile(provider_id="p1", model_id="m1", controller_status="certified"),
            profile(provider_id="p2", model_id="m2", controller_status="candidate"),
            profile(provider_id="p3", model_id="m3", controller_status="suspended"),
        ]
        ranks = ControllerRanker().rank(profiles)
        assert len(ranks) == 1
        assert ranks[0].profile.model_id == "m1"

    def test_select_returns_none_when_no_certified_controller(self):
        ranks = ControllerRanker().rank([profile(controller_status="candidate")])
        assert ranks == []
        assert ControllerRanker().select([profile(controller_status="candidate")]) is None

    def test_incompatible_protocol_excluded_even_if_certified(self):
        profile_incompatible = profile(controller_status="certified", protocol_status="incompatible")
        assert ControllerRanker().select([profile_incompatible]) is None

    def test_rank_is_deterministic_across_calls(self):
        profiles = [
            profile(provider_id="b", model_id="m2", controller_status="certified"),
            profile(provider_id="a", model_id="m1", controller_status="certified"),
        ]
        first = ControllerRanker().rank(profiles)
        second = ControllerRanker().rank(profiles)
        assert [r.profile.model_id for r in first] == [r.profile.model_id for r in second]
        assert [r.as_dict()["provider_id"] for r in first] == sorted(r.as_dict()["provider_id"] for r in first)

    def test_build_controller_epoch_from_rank(self):
        rank = ControllerRanker().select([profile(controller_status="certified", model_id="gpt-x")])
        epoch = build_controller_epoch(controller_session_id="cs_1", rank=rank, reasoning_effort="low")
        assert epoch["provider_id"] == "openai"
        assert epoch["model_id"] == "gpt-x"
        assert epoch["controller_session_id"] == "cs_1"
        assert epoch["reasoning_effort"] == "low"
        assert epoch["status"] == "active"
