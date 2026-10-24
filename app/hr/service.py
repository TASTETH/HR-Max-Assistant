"""Public call: HRInput -> Ollama -> validated signals -> deterministic rules -> HRResult."""
from __future__ import annotations
from .analyzer import Analyzer, AnalyzerError, OllamaAnalyzer
from .decision import EscalationPolicy, decide_escalation
from .schemas import Analysis, AnalysisError, ErrorCode, HRInput, HRResult, LLMAnalysis, TicketDraft
from .validator import InvalidLLMResponse, validate_analysis

CATEGORY_ORDER = (
    "harassment", "intent_to_leave", "access_problem", "unclear_tasks",
    "conflict", "lack_of_support", "overload", "poor_communication", "hr_request",
)

def _make_ticket(data: HRInput, analysis: LLMAnalysis | None, priority: str) -> TicketDraft:
    found = {s.category for s in analysis.signals} if analysis else set()
    category = next((c for c in CATEGORY_ORDER if c in found), "general_hr_request")
    return TicketDraft(
        category=category,
        description=data.text or "Сотрудник запросил связь с HR",
        priority=priority,
        status="new",
    )

class HRService:
    def __init__(self, analyzer: Analyzer | None = None, policy: EscalationPolicy | None = None) -> None:
        self.analyzer = analyzer
        self.policy = policy or EscalationPolicy()

    async def process(self, data: HRInput | dict) -> HRResult:
        validated = HRInput.model_validate(data.model_dump(mode="python") if isinstance(data, HRInput) else data)
        if not validated.text and validated.score is None:
            # Explicit button press must not be lost, even if no text/LLM available.
            return HRResult(
                event_id=validated.event_id, employee_id=validated.employee_id,
                status="no_data", ticket=_make_ticket(validated, None, "low") if validated.source == "hr_request" else None,
            )
        if validated.text:
            try:
                analyzer = self.analyzer if self.analyzer is not None else OllamaAnalyzer.from_env()
            except (ValueError, TypeError):
                return self._failure(validated, "configuration_error", "Configure OLLAMA_MODEL")
            try:
                raw = await analyzer.analyze(validated)
                extracted = validate_analysis(raw, validated)
            except AnalyzerError as error:
                return self._failure(validated, error.code, str(error))
            except InvalidLLMResponse:
                return self._failure(validated, "invalid_llm_output", "LLM output did not pass validation")
        else:
            extracted = LLMAnalysis(
                sentiment="unknown", summary="Числовая оценка без текстового комментария", signals=[]
            )
        decision = decide_escalation(validated, extracted, self.policy)
        return HRResult(
            event_id=validated.event_id, employee_id=validated.employee_id, status="ok",
            analysis=Analysis(
                sentiment=extracted.sentiment, summary=extracted.summary,
                signals=extracted.signals, priority=decision.priority, requires_hr=decision.requires_hr,
            ),
            ticket=_make_ticket(validated, extracted, decision.priority) if decision.requires_hr else None,
        )

    @staticmethod
    def _failure(data: HRInput, code: ErrorCode, message: str) -> HRResult:
        return HRResult(
            event_id=data.event_id, employee_id=data.employee_id, status="error",
            error=AnalysisError(code=code, message=message),
            ticket=_make_ticket(data, None, "low") if data.source == "hr_request" else None,
        )

async def process_hr_input(
    data: dict | HRInput, *, analyzer: Analyzer | None = None, policy: EscalationPolicy | None = None
) -> HRResult:
    return await HRService(analyzer=analyzer, policy=policy).process(data)
