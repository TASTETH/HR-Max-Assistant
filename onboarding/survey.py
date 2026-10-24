"""Чистая проверка ответа: без SQL, LLM и отправки сообщений.

Router проверяет принадлежность/версию вопроса и передаёт его конфигурацию сюда.
invalid_answer — нормальный результат, а не причина повторять входящий webhook.
Принятый ответ и переход опроса сохраняет общий backend атомарно через БД.
"""
from dataclasses import asdict, dataclass
from typing import Any, Optional

ANSWER_TYPES = frozenset({"yes_no", "rating", "choice", "multi_choice", "text", "attachment"})


class QuestionConfigurationError(ValueError):
    """Ошибка настройки сценария. Её нельзя выдавать за неверный ответ сотрудника."""


@dataclass(frozen=True)
class AnswerResult:
    status: str
    question_code: str
    answer_type: str
    value: Any = None
    label: Optional[str] = None
    comment_required: bool = False
    reason_code: Optional[str] = None
    reply_text: Optional[str] = None
    profile_update: Optional[dict] = None

    def as_dict(self):
        return asdict(self)


class _InputError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def validate_question(question):
    if not isinstance(question, dict):
        raise QuestionConfigurationError("Конфигурация вопроса должна быть объектом")
    code, kind = question.get("code"), question.get("type")
    if not isinstance(code, str) or not code.strip() or kind not in ANSWER_TYPES:
        raise QuestionConfigurationError("Нужны код вопроса и поддерживаемый тип ответа")
    if not isinstance(question.get("text"), str) or not question["text"].strip():
        raise QuestionConfigurationError("Нужна формулировка вопроса")
    if "comment_below" in question and (type(question["comment_below"]) is not int or not 1 <= question["comment_below"] <= 11):
        raise QuestionConfigurationError("Порог комментария должен быть целым числом от 1 до 11")
    if kind in ("choice", "multi_choice"):
        options = question.get("options", [])
        ids = [o.get("id") for o in options]
        if not options or any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
            raise QuestionConfigurationError("Варианты должны иметь уникальные строковые ID")
        if any(not isinstance(o.get("label"), str) or not o["label"].strip() for o in options):
            raise QuestionConfigurationError("Каждому варианту нужна подпись")
    return code, kind


def _parse(question, raw_value, attachment_ref):
    kind = question["type"]
    raw = raw_value.strip() if isinstance(raw_value, str) else raw_value
    if kind == "yes_no":
        if type(raw) is bool:
            return raw, "Да" if raw else "Нет"
        if isinstance(raw, str) and raw.casefold() in ("да", "yes", "true"):
            return True, "Да"
        if isinstance(raw, str) and raw.casefold() in ("нет", "no", "false"):
            return False, "Нет"
        raise _InputError("invalid_boolean", "Пожалуйста, выбери «Да» или «Нет». Если пока не уверен, можешь задать уточняющий вопрос.")
    if kind == "rating":
        if type(raw) is int and 1 <= raw <= 10:
            return raw, str(raw)
        if isinstance(raw, str) and raw in [str(i) for i in range(1, 11)]:
            return int(raw), raw
        raise _InputError("invalid_rating", "Введи целое число от 1 до 10.")
    if kind in ("choice", "multi_choice"):
        values = [raw] if kind == "choice" else (raw if isinstance(raw, list) else raw.split(",") if isinstance(raw, str) else [])
        values = [v.strip() if isinstance(v, str) else v for v in values]
        labels = {option["id"]: option["label"] for option in question["options"]}
        if not values or any(not isinstance(v, str) or v not in labels for v in values) or len(set(values)) != len(values):
            raise _InputError("invalid_option", "Выбери доступный вариант ответа.")
        if kind == "choice":
            return values[0], labels[values[0]]
        if len(values) > 1 and any(v in question.get("exclusive_values", []) for v in values):
            raise _InputError("exclusive_options", "Этот вариант нельзя сочетать с другими. Проверь выбранные ответы.")
        values = [o["id"] for o in question["options"] if o["id"] in values]
        return values, "; ".join(labels[v] for v in values)
    if kind == "attachment":
        if not isinstance(attachment_ref, str) or not attachment_ref.strip() or len(attachment_ref) > 500:
            raise _InputError("attachment_required", "Приложи файл или фотографию к сообщению.")
        return attachment_ref.strip(), "Вложение"
    if not isinstance(raw, str) or not raw or len(raw) > 5000:
        raise _InputError("invalid_text", "Напиши непустой ответ длиной до 5000 символов.")
    return raw, raw


def validate_answer(question, raw_value=None, *, attachment_ref=None, confirmed=False):
    """Не меняет состояние опроса. Для multi_choice нужен явный confirmed=True."""
    code, kind = validate_question(question)
    try:
        value, label = _parse(question, raw_value, attachment_ref)
    except _InputError as error:
        return AnswerResult("invalid_answer", code, kind, reason_code=error.reason, reply_text=str(error))
    if kind == "multi_choice" and not confirmed:
        return AnswerResult("pending_confirmation", code, kind, value=value, label=label,
                            reply_text="Подтверди выбранные варианты, когда закончишь выбор.")
    needs_comment = ((kind == "rating" and value < question.get("comment_below", 0))
                     or value in question.get("comment_on", []))
    profile = None
    if question.get("profile_field"):
        profile = {question["profile_field"]: question.get("profile_values", {}).get(str(value), value)}
    return AnswerResult("accepted", code, kind, value=value, label=label,
                        comment_required=needs_comment, profile_update=profile)


def validate_comment(question, raw_value):
    """Только для реплики, которую Router явно связал с ожидаемым комментарием."""
    code, _ = validate_question(question)
    if not isinstance(raw_value, str) or not raw_value.strip() or len(raw_value) > 5000:
        return AnswerResult("invalid_answer", code, "comment", reason_code="invalid_comment",
                            reply_text="Напиши непустой комментарий длиной до 5000 символов.")
    return AnswerResult("accepted", code, "comment", value=raw_value.strip(), label=raw_value.strip())
