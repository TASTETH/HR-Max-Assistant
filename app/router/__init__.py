"""Persistent MAX routing. Call Router.handle() only from an authenticated adapter."""
from .classifier import Classifier, HttpRagBackend
from .repository import PostgresRepository
from .rules import decide_route
from .schemas import (Action, Attachment, Button, Choice, Context, Decision, IncomingEvent,
                      Intent, OutgoingMessage, Question, QuestionSettings, RagAnswer,
                      RagBackend, RagRequest, RejectedBeforeAcceptance, Route, RouterResult, Sender)


def __getattr__(name):
    if name == "Router":
        from .core import Router
        return Router
    raise AttributeError(name)

__all__ = ["Router", "PostgresRepository", "IncomingEvent", "Context", "Decision", "Intent", "Route",
           "Action", "Attachment", "Button", "Choice", "Question", "QuestionSettings", "RouterResult",
           "OutgoingMessage", "Classifier", "decide_route", "RagBackend", "RagRequest", "RagAnswer",
           "HttpRagBackend", "Sender", "RejectedBeforeAcceptance"]
