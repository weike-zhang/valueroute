from __future__ import annotations

import re

from valueroute.routing.models import RequestBoundaryDecision, RoutingRequestEnvelope

_AMENDMENT_MARKERS = (
    "改",
    "扩展",
    "变更",
    "加个",
    "增加",
    "补充",
    "amendment",
    "change the",
    "also",
    "additionally",
    "scope",
    "范围",
    "调整",
)


_CONTINUATION_MARKERS = (
    "继续",
    "接着",
    "continuation",
    "continue",
    "keep going",
    "接着上次",
    "下一步",
    "next step",
)


_CONTROL_MARKERS = (
    "取消",
    "暂停",
    "恢复",
    "cancel",
    "pause",
    "resume",
    "停止",
    "状态",
    "status",
    "查看",
    "what",
    "进度",
    "progress",
)


_CLARIFICATION_MARKERS = (
    "?" ,
    "？",
    "是什么",
    "如何",
    "为什么",
    "clarif",
    "explain",
    "疑问",
    "确认",
)


def classify_boundary(envelope: RoutingRequestEnvelope) -> RequestBoundaryDecision:
    """Classify the request boundary with rule-based evidence.

    The host's declared type wins when present and unambiguous.  Otherwise a
    conservative keyword rule set is used; if nothing matches, the boundary is
    ``new_task`` with low confidence so the host can override.
    """

    declared = envelope.host_declared_request_type
    if declared is not None:
        return RequestBoundaryDecision(
            request_type=declared,
            confidence=1.0,
            method="host_declared",
            rationale=f"host declared request type {declared}",
        )

    text = envelope.user_text.lower()

    def _scores(markers: tuple[str, ...]) -> int:
        return sum(1 for marker in markers if marker in text)

    scores = {
        "material_amendment": _scores(_AMENDMENT_MARKERS),
        "continuation": _scores(_CONTINUATION_MARKERS),
        "clarification": _scores(_CLARIFICATION_MARKERS),
        "control": _scores(_CONTROL_MARKERS),
    }
    best = max(scores, key=scores.get)
    best_score = scores[best]
    if best_score > 0:
        confidence = min(1.0, 0.4 + 0.15 * best_score)
        return RequestBoundaryDecision(
            request_type=best,
            confidence=round(confidence, 3),
            method="rule_based",
            rationale=f"matched {best_score} marker(s) for {best}",
        )

    # The user asked to do something new without any marker: new_task.
    if re.search(r"[\u4e00-\u9fff]|\w", text):
        return RequestBoundaryDecision(
            request_type="new_task",
            confidence=0.5,
            method="rule_based",
            rationale="no marker matched; conservative new_task fallback with low confidence",
        )

    return RequestBoundaryDecision(
        request_type="new_task",
        confidence=0.3,
        method="rule_based",
        rationale="empty or unparseable input; host should provide request_type",
    )


__all__ = ["classify_boundary"]
