"""Standalone, side-effect-free HR Intelligence module."""
from .analyzer import Analyzer, AnalyzerError, OllamaAnalyzer
from .decision import EscalationPolicy
from .schemas import HRInput, HRResult, Analysis, TicketDraft
from .service import HRService, process_hr_input
__all__ = [
    "Analyzer", "AnalyzerError", "OllamaAnalyzer", "EscalationPolicy",
    "HRInput", "HRResult", "Analysis", "TicketDraft", "HRService", "process_hr_input",
]
