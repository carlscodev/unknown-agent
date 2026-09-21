"""Validation subsystem: quality gates, evidence, definition of done."""

from unknown_agent.validation.custom import build_custom_gates, preset_gates
from unknown_agent.validation.engine import EngineOptions, ValidationEngine
from unknown_agent.validation.gates import (
    BrowserGate,
    CommandGate,
    CustomGate,
    DiffGate,
    Gate,
    GateContext,
    SyntaxGate,
    default_gates,
)

__all__ = [
    "ValidationEngine",
    "EngineOptions",
    "Gate",
    "GateContext",
    "SyntaxGate",
    "CommandGate",
    "DiffGate",
    "BrowserGate",
    "CustomGate",
    "default_gates",
    "build_custom_gates",
    "preset_gates",
]
