"""Ordered routing rules. No I/O and no mutations."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .schemas import Context, Decision, IncomingEvent, Intent, Route


def _decision(route: Route, reason: str, **kwargs) -> Decision:
    return Decision(route=route, reason=reason, **kwargs)


def _question_decision(context: Context, target: int, value: str = "") -> Decision:
    question = context.question(target)
    if not question or question.deadline_at <= datetime.now(timezone.utc):
        return _decision(Route.STALE, "question_expired_or_completed")
    comment = question.stage == "awaiting_comment" or question.status == "awaiting_comment"
    return _decision(Route.SURVEY_COMMENT if comment else Route.SURVEY_ANSWER,
                     "question_context", target_id=target, revision=question.revision, value=value)


def decide_route(event: IncomingEvent, context: Context, intent: Intent | None = None) -> Decision | None:
    """None requests semantic classification outside the database transaction."""
    if event.kind == "delivery":
        return _decision(Route.DELIVERY, "provider_receipt")
    if event.kind == "invitation":
        return _decision(Route.ACTIVATE, "invitation")
    if context.employee_id is None:
        return _decision(Route.ACTIVATE_REQUIRED, "unknown_account")
    command_match = re.match(r"^/([a-z_]+)(?:@\w+)?(?:\s|$)", event.text, re.IGNORECASE)
    if command_match and event.kind == "text":
        command = command_match[1].casefold()
        commands = {"hr": Route.HR_MENU, "help": Route.HELP, "start": Route.HELP,
                    "food": Route.CONTENT, "plan": Route.PLAN, "resume": Route.SURVEY_SHOW}
        return _decision(commands.get(command, Route.UNSUPPORTED), "explicit_command", value=command)
    if event.kind == "callback":
        if context.action_invalid or context.action is None:
            return _decision(Route.STALE, "invalid_callback")
        action = context.action
        if action.route in {Route.SURVEY_SHOW, Route.SURVEY_ANSWER, Route.SURVEY_COMMENT,
                            Route.SURVEY_TOGGLE, Route.SURVEY_SUBMIT}:
            question = context.question(action.target_id)
            if not question or question.deadline_at <= datetime.now(timezone.utc):
                return _decision(Route.STALE, "question_expired_or_completed")
            if action.revision is not None and action.revision != question.revision:
                return _decision(Route.STALE, "question_revision_changed")
        if action.route == Route.HR_FOLLOWUP and action.target_id not in context.open_cases:
            return _decision(Route.STALE, "case_no_longer_open")
        return _decision(action.route, "bound_action", target_id=action.target_id,
                         revision=action.revision, value=action.value, pending_id=action.pending_id)
    if context.reply_known:
        if context.reply_question_id:
            return _question_decision(context, context.reply_question_id, event.text)
        if context.reply_case_id:
            if context.reply_case_id in context.open_cases:
                return _decision(Route.HR_FOLLOWUP, "case_reply", target_id=context.reply_case_id, value=event.text)
            return _decision(Route.STALE, "case_no_longer_open")
    if event.kind == "attachment":
        if context.input_mode.get("kind") == "intro":
            return _decision(Route.PROFILE, "profile_attachment")
        return _decision(Route.CLARIFY, "attachment_requires_target")
    # A typed option is a structured answer even if its label describes a problem.
    mode = context.input_mode
    if mode.get("kind") not in {"comment", "hr", "registration", "intro"}:
        candidates = [q for q in context.questions if q.settings.kind == "choice" and q.stage == "draft"
            and (not mode.get("target_id") or mode["target_id"] == q.id)
            and any(event.text.casefold() in {o.code.casefold(), o.label.casefold()} for o in q.settings.options)]
        if len(candidates) == 1:
            if not mode and re.fullmatch(r"(?:10|[1-9]|да|yes)", event.text, re.IGNORECASE) and any(
                q.id != candidates[0].id and q.settings.kind in {"rating", "task"} for q in context.questions
            ):
                return _decision(Route.CLARIFY, "structured_answer_target_ambiguous")
            return _question_decision(context, candidates[0].id, event.text)
        if len(candidates) > 1:
            return _decision(Route.CLARIFY, "choice_target_ambiguous")
    if intent is None:
        return None
    # Explicitly established input modes can be interrupted by a new question.
    if mode and intent.name in {"answer", "unknown", "feedback"}:
        kind = mode.get("kind")
        if kind == "registration":
            return _decision(Route.REGISTRATION, "registration_input", revision=mode.get("revision"),
                             value=mode.get("field", ""))
        if kind in {"survey", "comment"}:
            return _question_decision(context, mode.get("target_id"), event.text)
        if kind == "hr" and mode.get("target_id") in context.open_cases:
            return _decision(Route.HR_FOLLOWUP, "case_input", target_id=mode["target_id"], value=event.text)
    if intent.name == "answer":
        if len(context.questions) == 1:
            q = context.questions[0]
            # Only unambiguous structured values may use a single available question.
            if q.settings.kind == "rating" and re.fullmatch(r"(?:10|[1-9])", event.text):
                return _question_decision(context, q.id, event.text)
            if q.settings.kind == "choice" and any(
                event.text.casefold() in {o.code.casefold(), o.label.casefold()} for o in q.settings.options
            ):
                return _question_decision(context, q.id, event.text)
        return _decision(Route.CLARIFY, "answer_target_ambiguous")
    mapping = {"question": Route.RAG, "plan": Route.PLAN, "feedback": Route.FEEDBACK,
               "mixed": Route.MIXED, "human": Route.HR_REQUEST, "greeting": Route.GREETING,
               "identity": Route.IDENTITY}
    return _decision(mapping.get(intent.name, Route.CLARIFY), "intent_" + intent.name)
