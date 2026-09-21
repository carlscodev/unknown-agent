"""Layered memory: working, session, persistent project, procedural.

| Layer      | Lifetime    | Storage                          | Purpose                                   |
| ---------- | ----------- | -------------------------------- | ----------------------------------------- |
| working    | one step    | process memory                   | plan, observations, hypotheses, scratch   |
| session    | one run     | ``.unknown/runs/<id>/`` + index  | step journal, evidence, resume state       |
| project    | forever     | ``.unknown/memory/memory.db``    | conventions, architecture, decisions      |
| procedures | forever     | ``procedures/*.md``              | reusable workflows and behavioural rules  |
"""

from unknown_agent.memory.procedures import Procedure, ProcedureLibrary
from unknown_agent.memory.sessions import SessionRecord, SessionStore
from unknown_agent.memory.store import FACT_KINDS, MemoryStats, MemoryStore
from unknown_agent.memory.working import Observation, WorkingMemory

__all__ = [
    "WorkingMemory",
    "Observation",
    "MemoryStore",
    "MemoryStats",
    "FACT_KINDS",
    "SessionStore",
    "SessionRecord",
    "Procedure",
    "ProcedureLibrary",
]
