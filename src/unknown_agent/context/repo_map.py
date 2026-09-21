"""Repository map: a token-budgeted, symbol-level summary of the codebase.

The map gives the model structural awareness without reading whole files: which
modules exist, what they export, how big they are, and where the interesting
entry points live.  It is cached under ``.unknown/cache`` keyed by the git HEAD
plus the mtime of the file tree, so repeated runs are instant.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.observability.logging import get_logger

log = get_logger("context.repo_map")

TEXT_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".php", ".java", ".kt", ".cs", ".vue", ".svelte", ".sql"}
SYMBOL_RE = {
    ".ts": re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ".tsx": re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ".js": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|const)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ".jsx": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|const)\s+([A-Za-z_$][\w$]*)", re.MULTILINE),
    ".go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.MULTILINE),
    ".rs": re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.MULTILINE),
    ".rb": re.compile(r"^\s*(?:class|module|def)\s+([A-Za-z_:]\w*)", re.MULTILINE),
    ".php": re.compile(r"^\s*(?:final\s+|abstract\s+)?(?:class|interface|trait|function)\s+([A-Za-z_]\w*)", re.MULTILINE),
    ".java": re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:class|interface|enum|record)\s+([A-Za-z_]\w*)", re.MULTILINE),
}
HEAVY_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "coverage", "__pycache__", ".next", ".nuxt", ".venv", "vendor", "site-packages", ".mypy_cache", ".ruff_cache", "out"}


@dataclass(slots=True)
class FileEntry:
    path: str
    size: int
    lines: int
    symbols: list[str] = field(default_factory=list)
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "lines": self.lines, "symbols": self.symbols, "language": self.language}


@dataclass(slots=True)
class RepoMap:
    root: Path
    entries: list[FileEntry] = field(default_factory=list)
    directories: dict[str, int] = field(default_factory=dict)
    total_files: int = 0
    total_lines: int = 0
    generated_at: float = field(default_factory=time.time)
    fingerprint: str = ""

    # -- rendering ---------------------------------------------------------
    def render(self, *, max_files: int = 120, max_chars: int = 12_000) -> str:
        lines = [f"repository map ({self.total_files} source files, {self.total_lines} lines)"]
        for directory, count in sorted(self.directories.items(), key=lambda item: -item[1])[:20]:
            lines.append(f"  {directory}/  ({count} files)")
        lines.append("")
        lines.append("key files:")
        shown = 0
        for entry in sorted(self.entries, key=lambda item: (-len(item.symbols), item.path))[:max_files]:
            symbols = ", ".join(entry.symbols[:8]) + (" ..." if len(entry.symbols) > 8 else "")
            lines.append(f"  {entry.path}  [{entry.language}, {entry.lines} lines]" + (f"  exports: {symbols}" if symbols else ""))
            shown += 1
            if sum(len(line) for line in lines) > max_chars:
                lines.append(f"  ... ({len(self.entries) - shown} more files)")
                break
        return "\n".join(lines)

    def render_paths(self, *, limit: int = 400) -> str:
        return "\n".join(entry.path for entry in self.entries[:limit])

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "total_files": self.total_files,
            "total_lines": self.total_lines,
            "directories": self.directories,
            "generated_at": self.generated_at,
            "fingerprint": self.fingerprint,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def build_repo_map(root: Path, *, cache_dir: Path | None = None, max_files: int = 4000, refresh: bool = False) -> RepoMap:
    """Scan the repository and return a :class:`RepoMap`, using a cache when valid."""

    root = Path(root).resolve()
    fingerprint = _fingerprint(root)
    cache_file = (cache_dir / "repo-map.json") if cache_dir else None
    if cache_file and cache_file.is_file() and not refresh:
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if payload.get("fingerprint") == fingerprint:
                return _from_dict(root, payload)
        except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt cache
            log.debug("repo map cache unreadable; rebuilding")

    entries: list[FileEntry] = []
    directories: dict[str, int] = {}
    total_lines = 0
    scanned = 0
    for path in _iter_source_files(root, max_files=max_files):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        lines = text.count("\n") + 1
        total_lines += lines
        relative = path.relative_to(root).as_posix()
        top = relative.split("/")[0] if "/" in relative else "."
        directories[top] = directories.get(top, 0) + 1
        entries.append(
            FileEntry(
                path=relative,
                size=len(text),
                lines=lines,
                symbols=_extract_symbols(path, text)[:20],
                language=path.suffix.lstrip("."),
            )
        )

    repo_map = RepoMap(
        root=root,
        entries=entries,
        directories=directories,
        total_files=scanned,
        total_lines=total_lines,
        fingerprint=fingerprint,
    )
    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(repo_map.to_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:  # pragma: no cover
            log.debug("could not persist repo map cache")
    return repo_map


def _iter_source_files(root: Path, *, max_files: int) -> list[Path]:
    import os

    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in HEAVY_DIRS and not name.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in TEXT_SUFFIXES:
                results.append(path)
                if len(results) >= max_files:
                    return results
    return results


def _extract_symbols(path: Path, text: str) -> list[str]:
    if path.suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        symbols: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append(target.id)
        return symbols
    pattern = SYMBOL_RE.get(path.suffix.lower())
    if pattern is None:
        if path.suffix.lower() in {".vue", ".svelte"}:
            return re.findall(r"<(?:script|template)", text)[:3]
        return []
    return pattern.findall(text)


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    head = root / ".git" / "HEAD"
    if head.is_file():
        digest.update(head.read_bytes())
    count = 0
    for path in _iter_source_files(root, max_files=500):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8", errors="replace"))
        digest.update(str(int(stat.st_mtime)).encode())
        count += 1
        if count >= 500:
            break
    return digest.hexdigest()[:16]


def _from_dict(root: Path, payload: dict[str, Any]) -> RepoMap:
    return RepoMap(
        root=root,
        entries=[FileEntry(**entry) for entry in payload.get("entries", [])],
        directories=payload.get("directories", {}),
        total_files=int(payload.get("total_files", 0)),
        total_lines=int(payload.get("total_lines", 0)),
        fingerprint=payload.get("fingerprint", ""),
    )


__all__ = ["RepoMap", "FileEntry", "build_repo_map"]
