"""Working memory: the short-lived, token-budgeted scratchpad of one execution.

Working memory is deliberately not a chat log.  It keeps:

* the task and its constraints,
* the active plan step and its acceptance criteria,
* the last N observations compressed into one-liners,
* open questions and pending hypotheses,
* an explicit token budget so context packing stays predictable.

The agent loop writes here and asks for a packed view when it builds the next
prompt, which keeps prompt construction in one place instead of scattered
f-string assembly.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

OBSERVATION_TEMPLATE = "{tool}: {summary}"


@dataclass(slots=True)
class Observation:
    tool: str
    summary: str
    ok: bool = True
    step: int = 0
    detail: str = ""
    keys: dict[str, Any] = field(default_factory=dict)

    def render(self, *, max_chars: int = 400) -> str:
        text = OBSERVATION_TEMPLATE.format(tool=self.tool, summary=self.summary.strip())
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        return text


class WorkingMemory:
    """Bounded, structured scratchpad for the current run."""

    def __init__(self, *, max_observations: int = 40, token_budget: int = 6000, observation_budget: int = 3000) -> None:
        self.max_observations = max_observations
        self.token_budget = token_budget
        self.observation_budget = observation_budget
        self.task: str = ""
        self.constraints: list[str] = []
        self.observations: deque[Observation] = deque(maxlen=max_observations)
        self.hypotheses: list[str] = []
        self.open_questions: list[str] = []
        self.facts_learned: list[str] = []
        self.files_touched: set[str] = set()
        self.commands_run: list[str] = []
        self.failure_signatures: dict[str, int] = {}

    # -- writes ------------------------------------------------------------
    def start(self, task: str, *, constraints: list[str] | None = None) -> None:
        self.task = task
        self.constraints = list(constraints or [])

    def record_observation(self, tool: str, summary: str, *, ok: bool = True, step: int = 0, detail: str = "", **keys: Any) -> None:
        self.observations.append(Observation(tool=tool, summary=summary, ok=ok, step=step, detail=detail, keys=keys))
        signature = _signature(summary)
        if not ok:
            self.failure_signatures[signature] = self.failure_signatures.get(signature, 0) + 1

    def note_file(self, path: str) -> None:
        if path:
            self.files_touched.add(path)

    def note_command(self, command: str) -> None:
        if command:
            self.commands_run.append(command)

    def add_hypothesis(self, text: str) -> None:
        if text and text not in self.hypotheses:
            self.hypotheses.append(text)
        self.hypotheses = self.hypotheses[-6:]

    def add_question(self, text: str) -> None:
        if text and text not in self.open_questions:
            self.open_questions.append(text)

    def resolve_question(self, text: str) -> None:
        self.open_questions = [item for item in self.open_questions if item != text]

    def learn(self, fact: str) -> None:
        if fact and fact not in self.facts_learned:
            self.facts_learned.append(fact)
        self.facts_learned = self.facts_learned[-12:]

    # -- reads -------------------------------------------------------------
    def repeated_failure(self, summary: str, *, threshold: int = 2) -> bool:
        return self.failure_signatures.get(_signature(summary), 0) >= threshold

    def recent(self, *, count: int = 8, max_chars: int = 2400) -> str:
        lines: list[str] = []
        for observation in list(self.observations)[-count:]:
            marker = "ok" if observation.ok else "FAILED"
            lines.append(f"[{marker}] step {observation.step} {observation.render(max_chars=300)}")
        packed: list[str] = []
        budget = max_chars
        for line in reversed(lines):
            budget -= len(line) + 1
            if budget < 0:
                break
            packed.append(line)
        return "\n".join(reversed(packed))

    def pack(self) -> str:
        """Token-budgeted context block handed to the model each step."""

        sections: list[str] = []
        if self.task:
            sections.append(f"Task: {self.task}")
        if self.constraints:
            sections.append("Constraints:\n" + "\n".join(f"- {item}" for item in self.constraints))
        if self.files_touched:
            sections.append("Files touched: " + ", ".join(sorted(self.files_touched)[:20]))
        if self.facts_learned:
            sections.append("Learned this run:\n" + "\n".join(f"- {item}" for item in self.facts_learned))
        if self.hypotheses:
            sections.append("Working hypotheses:\n" + "\n".join(f"- {item}" for item in self.hypotheses))
        if self.open_questions:
            sections.append("Open questions:\n" + "\n".join(f"- {item}" for item in self.open_questions))
        recent = self.recent(count=10, max_chars=self.observation_budget)
        if recent:
            sections.append("Recent observations:\n" + recent)
        return "\n\n".join(sections)

    def snapshot(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "constraints": self.constraints,
            "observations": [obs.render() for obs in self.observations],
            "files_touched": sorted(self.files_touched),
            "commands_run": self.commands_run,
            "hypotheses": self.hypotheses,
            "open_questions": self.open_questions,
            "facts_learned": self.facts_learned,
            "repeated_failures": {key: value for key, value in self.failure_signatures.items() if value > 1},
        }


def _signature(text: str) -> str:
    import re

    cleaned = re.sub(r"\d+", "#", (text or "").lower())
    return cleaned[:120]


__all__ = ["WorkingMemory", "Observation"]
