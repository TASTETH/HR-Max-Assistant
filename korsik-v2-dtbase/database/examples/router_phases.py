"""Two-phase Router integration contract. No MAX SDK or semantic model is mocked.
The classifier is injected by the real backend. Database handlers are in SQL.
"""
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class Context:
    employee_id: int
    revision: int
    snapshot: dict

@dataclass(frozen=True)
class PreparedDecision:
    context: Context
    route: str
    params: dict

class ContextChanged(Exception):
    pass


def prepare_decision(conn, employee_id: int, incoming: dict, classifier: Callable):
    with conn.transaction():
        row = conn.execute("SELECT id,context_revision,hr_id,location_tag,employee_type,communication_style,"
                           "registration_step,registration_revision,lifecycle FROM korsik.employees WHERE id=%s",
                           (employee_id,)).fetchone()
        if row is None:
            raise LookupError("Activated employee required")
        # A dict_row connection is expected. Do not keep its transaction open.
        snapshot = dict(row)
        snapshot["active_events"] = conn.execute("SELECT id,kind,status,current_question_code,interaction_revision,definition "
            "FROM korsik.plan_events WHERE employee_id=%s AND status IN ('queued','awaiting_answer','awaiting_comment')",
            (employee_id,)).fetchall()
        context = Context(employee_id, row["context_revision"], snapshot)
    # No DB transaction / row locks while the external classifier runs.
    route, params = classifier(context.snapshot, incoming)
    allowed = {"survey", "legacy_answer", "hr_request", "rag_question", "mixed_feedback",
               "plan_question", "clarification", "service", "stale_action"}
    if route not in allowed:
        raise ValueError("Classifier route is outside the closed contract")
    return PreparedDecision(context, route, params)


def apply_prepared(conn, decision: PreparedDecision, transactional_handler: Callable):
    """Use INSIDE the inbox application transaction, before marking it done.
    Handler verifies signed target, question revision, employee ownership and expiry.
    User validation failures must become a friendly response + inbox.done; model or
    database faults remain retryable errors. Do not reclassify stale buttons as RAG.
    """
    with conn.transaction():
        row = conn.execute("SELECT context_revision FROM korsik.employees WHERE id=%s FOR UPDATE",
                           (decision.context.employee_id,)).fetchone()
        if row is None or row["context_revision"] != decision.context.revision:
            raise ContextChanged("Recompute local routing or ask clarification")
        return transactional_handler(conn, decision)
