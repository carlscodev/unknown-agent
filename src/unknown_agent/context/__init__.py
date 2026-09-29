"""Codebase and project context: intelligence, repo map, prompt assembly."""

from unknown_agent.context.builder import BuiltContext, ContextBudget, ContextBuilder, estimate_tokens
from unknown_agent.context.compaction import CompactionRecord, CompactionResult, CompactionStore, ContextCompactor
from unknown_agent.context.project import ProjectContext, load_project_context
from unknown_agent.context.repo_map import FileEntry, RepoMap, build_repo_map

__all__ = [
    "ProjectContext",
    "load_project_context",
    "RepoMap",
    "FileEntry",
    "build_repo_map",
    "ContextBuilder",
    "BuiltContext",
    "ContextBudget",
    "estimate_tokens",
    "CompactionRecord",
    "CompactionResult",
    "CompactionStore",
    "ContextCompactor",
]
