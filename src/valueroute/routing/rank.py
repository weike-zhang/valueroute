from __future__ import annotations

from valueroute.domain.models import new_id
from valueroute.routing.manifest import ModelProfile


class ControllerRank:
    """A ranked, certified controller candidate for the ``automatic`` mode.

    Rank order is role-specific: it uses only the ``controller_status`` field
    and never a single aggregate score shared with the Worker role.
    """

    def __init__(self, profile: ModelProfile, *, score: float, reason: str):
        self.profile = profile
        self.score = score
        self.reason = reason

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.profile.provider_id,
            "model_id": self.profile.model_id,
            "controller_status": self.profile.controller_status,
            "region": self.profile.region,
            "score": self.score,
            "reason": self.reason,
        }


class ControllerRanker:
    """Deterministic, role-specific ranking of controller candidates.

    Rules (design section 15.1 and 15.3):
    - only ``protocol_status == compatible`` profiles are considered;
    - only ``controller_status == certified`` profiles are eligible; suspended
      and candidate profiles are excluded from automatic selection;
    - preference order is deterministic and documented in the reason field so
      a host can override by pinning an explicit controller epoch.

    The ranker is pure: it reads only the provided profiles and never inspects
    controller state or grants execution rights.
    """

    def __init__(self, *, prefer: str = "latency") -> None:
        if prefer not in {"latency", "cost"}:
            raise ValueError("prefer must be 'latency' or 'cost'")
        self.prefer = prefer

    def rank(self, profiles: list[ModelProfile]) -> list[ControllerRank]:
        eligible = [profile for profile in profiles if profile.eligible_for_controller()]
        # Deterministic tie-break by (provider_id, model_id) so the rank is
        # stable across calls and restarts.
        eligible.sort(key=lambda profile: (profile.provider_id, profile.model_id))
        ranks = [self._rank(profile) for profile in eligible]
        ranks.sort(key=lambda rank: rank.score, reverse=True)
        return ranks

    def _rank(self, profile: ModelProfile) -> ControllerRank:
        # Preference model: latency mode favors a lower stated latency proxy.
        # Without measured latency evidence we keep a deterministic neutral
        # ordering; role status is the hard gate, not the score.
        if self.prefer == "cost":
            score = 1.0
            reason = "controller certified; deterministic preference (cost) without measured ranking evidence"
        else:
            score = 1.0
            reason = "controller certified; deterministic preference (latency) without measured ranking evidence"
        return ControllerRank(profile, score=score, reason=reason)

    def select(self, profiles: list[ModelProfile]) -> ControllerRank | None:
        ranks = self.rank(profiles)
        return ranks[0] if ranks else None


def build_controller_epoch(*, controller_session_id: str, rank: ControllerRank, reasoning_effort: str, version: int = 1) -> dict[str, object]:
    """Construct a ControllerEpoch dict from a selected controller candidate."""
    from valueroute.domain.models import ControllerEpoch

    return ControllerEpoch(
        id=new_id("ce"),
        version=version,
        controller_session_id=controller_session_id,
        provider_id=rank.profile.provider_id,
        model_id=rank.profile.model_id,
        reasoning_effort=reasoning_effort,
    ).model_dump(mode="json")


__all__ = ["ControllerRank", "ControllerRanker", "build_controller_epoch"]
