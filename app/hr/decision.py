"""Transparent rules: LLM cannot independently open or prioritize HR tickets."""
from __future__ import annotations
from dataclasses import dataclass
from .schemas import HRInput, LLMAnalysis, Priority, SignalName

@dataclass(frozen=True)
class EscalationPolicy:
    high_priority_signals: frozenset[SignalName] = frozenset({"intent_to_leave", "harassment"})
    low_score_threshold: int = 8
    # Automatically show specific concrete workplace problems to HR. HR makes the decision.
    auto_escalate_categories: frozenset[SignalName] = frozenset({
        "unclear_tasks", "access_problem", "conflict", "harassment",
    })
    escalate_medium: bool = False

    def __post_init__(self) -> None:
        if type(self.low_score_threshold) is not int or not 1 <= self.low_score_threshold <= 10:
            raise ValueError("low_score_threshold must be in 1..10")

@dataclass(frozen=True)
class Decision:
    priority: Priority
    requires_hr: bool

def decide_escalation(data: HRInput, analysis: LLMAnalysis, policy: EscalationPolicy | None = None) -> Decision:
    policy = policy or EscalationPolicy()
    categories = {signal.category for signal in analysis.signals}
    # Score is supported for satisfaction questions on a 1–10 scale, not arbitrary scoring.
    low_satisfaction = data.score is not None and data.score < policy.low_score_threshold
    if categories & policy.high_priority_signals:
        priority: Priority = "high"
    elif (categories - {"hr_request"}) or low_satisfaction or analysis.sentiment in {"negative", "mixed"}:
        priority = "medium"
    else:
        priority = "low"
    requires_hr = (
        data.source == "hr_request" or "hr_request" in categories or priority == "high"
        or bool(categories & policy.auto_escalate_categories)
        or (policy.escalate_medium and priority == "medium")
    )
    return Decision(priority=priority, requires_hr=requires_hr)
