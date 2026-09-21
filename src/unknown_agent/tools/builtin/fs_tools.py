"""Filesystem tools: read, write, patch, create, delete, list, search, tree.

Design notes that matter for agent quality:

* every tool is path-policy aware (containment + denied globs) - the executor
  enforces policy, the tools resolve paths through the shared resolver;
* ``read_file`` returns line-numbered content so the model can cite lines and
  build correct patches without a second round-trip;
* ``write_file`` is diff-aware: it returns how many lines changed, backs up the
  previous revision, and refuses to create a file when ``create`` was not set -
  a silent overwrite is the most common way a coding agent destroys work;
* ``apply_patch`` accepts a unified diff *or* search/replace blocks and applies it
  atomically: if any hunk fails, nothing is written;
* ``search_code`` and ``glob_files`` respect ignore rules (gitignore + project
  config) so results reflect the project's real source, not build output.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pathspec

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit",
    "coverage", ".turbo", ".cache", ".idea", ".vscode", "vendor", "Pods", ".unknown",
}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".dylib", ".dll", ".exe", ".pyc", ".class", ".woff", ".woff2", ".mp4", ".mp3", ".sqlite", ".db"}


def _resolver(ctx: ToolContext) -> Any:
    resolver = ctx.extra.get("path_resolver")
    if resolver is None:
        from unknown_agent.tools.policy import PolicyEngine

        resolver = PolicyEngine(ctx.settings, project_root=ctx.project_root)
        ctx.extra["path_resolver"] = resolver
    return resolver


def resolve(ctx: ToolContext, raw: str) -> Path:
    return _resolver(ctx).resolve_path(raw)


def relative(ctx: ToolContext, path: Path) -> str:
    try:
        return str(path.relative_to(ctx.project_root))
    except ValueError:
        return str(path)


def _ignore_spec(ctx: ToolContext) -> pathspec.PathSpec:
    """Build a gitignore+config matcher, cached per project root."""

    key = f"ignore_spec:{ctx.project_root}"
    cached = ctx.extra.get(key)
    if cached is not None:
        return cached
    patterns: list[str] = []
    for name in (".gitignore", ".uaignore", ".dockerignore"):
        path = ctx.project_root / name
        if path.is_file():
            try:
                patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:  # pragma: no cover
                pass
    patterns.extend(f"{item.strip('/')}/" for item in DEFAULT_IGNORE_DIRS)
    patterns.extend(ctx.settings.project.ignore)
    spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    ctx.extra[key] = spec
    return spec


def iter_files(ctx: ToolContext, root: Path, *, max_files: int = 20_000) -> list[Path]:
    spec = _ignore_spec(ctx)
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in DEFAULT_IGNORE_DIRS
            and not spec.match_file(str((current / name).relative_to(root).as_posix()) + "/")
        ]
        for filename in filenames:
            candidate = current / filename
            relative_path = candidate.relative_to(root).as_posix()
            if spec.match_file(relative_path):
                continue
            results.append(candidate)
            if len(results) >= max_files:
                return results
    return results


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file with line numbers. Use offset/limit for large files. "
        "Returns an error for binary files instead of garbage."
    )
    category = "filesystem"
    risk = RiskLevel.READ
    tags = ("inspect", "context")
    parameters = (
        ParameterSpec("path", "string", "File path relative to the project root.", required=True, examples=("src/app.py",)),
        ParameterSpec("offset", "integer", "1-based first line to return.", default=1, minimum=1),
        ParameterSpec("limit", "integer", "Maximum number of lines to return.", default=400, minimum=1, maximum=5000),
    )
    examples = ("ua tools call read_file --arg path=README.md",)

    async def run(self, ctx: ToolContext, path: str, offset: int = 1, limit: int = 400) -> ToolResult:
        target = resolve(ctx, path)
        if not target.exists():
            candidates = _suggest_paths(ctx, path)
            return ToolResult.failure(
                f"File not found: {relative(ctx, target)}",
                output="\n".join(candidates) or "No similar path found in the project.",
            )
        if target.is_dir():
            return ToolResult.failure(f"{relative(ctx, target)} is a directory. Use list_files or project_tree instead.")
        if target.suffix.lower() in BINARY_SUFFIXES:
            size = target.stat().st_size
            return ToolResult.success(f"[binary file: {relative(ctx, target)}, {size} bytes]", path=relative(ctx, target), binary=True, size=size)

        max_bytes = ctx.settings.tools.read_max_bytes
        raw = target.read_bytes()
        truncated_bytes = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[max(0, offset - 1) : max(0, offset - 1) + limit]
        numbered = "\n".join(f"{offset + index:>6}\t{line}" for index, line in enumerate(selected))
        total = len(lines)
        footer = ""
        if offset - 1 + limit < total or truncated_bytes:
            footer = f"\n... showing lines {offset}-{offset + len(selected) - 1} of {total}{' (byte-truncated)' if truncated_bytes else ''}"
        ctx.emit("observation", f"read {relative(ctx, target)} ({total} lines)")
        return ToolResult.success(
            numbered + footer,
            path=relative(ctx, target),
            lines=total,
            shown=len(selected),
            truncated=bool(footer),
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write or overwrite a text file. Creates missing parent directories. "
        "Set create=false to refuse overwriting an existing file. Reports a diff summary "
        "and keeps a backup of the previous revision."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    tags = ("edit",)
    parameters = (
        ParameterSpec("path", "string", "File path relative to the project root.", required=True),
        ParameterSpec("content", "string", "Full new file content.", required=True),
        ParameterSpec("create", "boolean", "Allow creating a new file when it does not exist.", default=True),
        ParameterSpec("overwrite", "boolean", "Allow overwriting an existing file.", default=True),
    )
    examples = ('ua tools call write_file --arg path=notes.md --arg content="hello"',)

    async def run(self, ctx: ToolContext, path: str, content: str, create: bool = True, overwrite: bool = True) -> ToolResult:
        target = resolve(ctx, path)
        existed = target.exists()
        if existed and not overwrite:
            return ToolResult.failure(f"Refusing to overwrite {relative(ctx, target)} (overwrite=false).")
        if not existed and not create:
            return ToolResult.failure(f"Refusing to create {relative(ctx, target)} (create=false).")
        if len(content.encode("utf-8")) > ctx.settings.policy.max_write_bytes:
            return ToolResult.failure(
                f"Content exceeds policy.max_write_bytes ({ctx.settings.policy.max_write_bytes} bytes).",
                output="Split the change into several smaller writes or raise the limit in config.",
            )
        previous = ""
        if existed:
            previous = target.read_text(encoding="utf-8", errors="replace")
            shutil.copy2(target, ctx.backup_path(relative(ctx, target)))
        target.parent.mkdir(parents=True, exist_ok=True)
        if not content.endswith("\n") and content:
            content += "\n"
        target.write_text(content, encoding="utf-8")
        added, removed = _diff_stats(previous, content)
        action = "updated" if existed else "created"
        ctx.emit("observation", f"{action} {relative(ctx, target)} (+{added}/-{removed})")
        return ToolResult.success(
            f"{action} {relative(ctx, target)} (+{added} lines, -{removed} lines, {len(content.splitlines())} total)",
            path=relative(ctx, target),
            created=not existed,
            lines_added=added,
            lines_removed=removed,
            bytes=len(content.encode("utf-8")),
        )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a surgical edit to an existing file. Accepts either a unified diff "
        "(@@ hunks) or search/replace blocks. Atomic: if any hunk fails to match, "
        "nothing is written. Prefer this over write_file for targeted changes."
    )
    category = "filesystem"
    risk = RiskLevel.WRITE
    tags = ("edit", "primary")
    parameters = (
        ParameterSpec("path", "string", "File to patch.", required=True),
        ParameterSpec("patch", "string", "Unified diff or search/replace block.", required=True),
        ParameterSpec("replace_all", "boolean", "Replace every occurrence of a search block.", default=False),
    )
    examples = (
        'ua tools call apply_patch --arg path=src/app.py --arg patch="<<<<<<< SEARCH\\nold\\n=======\\nnew\\n>>>>>>> REPLACE"',
    )

    async def run(self, ctx: ToolContext, path: str, patch: str, replace_all: bool = False) -> ToolResult:
        target = resolve(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"Cannot patch missing file: {relative(ctx, target)}")
        original = target.read_text(encoding="utf-8", errors="replace")
        try:
            if "<<<<<<<" in patch or ">>>>>>>" in patch:
                updated, applied = _apply_search_replace(original, patch, replace_all=replace_all)
            else:
                updated, applied = _apply_unified(original, patch)
        except ValueError as exc:
            return ToolResult.failure(str(exc), output=_patch_failure_help(original, patch))
        if not applied:
            return ToolResult.failure("Patch did not change the file (no matching context).", output=_patch_failure_help(original, patch))
        shutil.copy2(target, ctx.backup_path(relative(ctx, target)))
        target.write_text(updated, encoding="utf-8")
        added, removed = _diff_stats(original, updated)
        ctx.emit("observation", f"patched {relative(ctx, target)} ({applied} hunk(s), +{added}/-{removed})")
        return ToolResult.success(
            f"patched {relative(ctx, target)}: {applied} hunk(s) applied, +{added} lines, -{removed} lines",
            path=relative(ctx, target),
            hunks=applied,
            lines_added=added,
            lines_removed=removed,
        )


class CreateFileTool(Tool):
    name = "create_file"
    description = "Create a new file, failing if it already exists. Use write_file to overwrite."
    category = "filesystem"
    risk = RiskLevel.WRITE
    parameters = (
        ParameterSpec("path", "string", "New file path relative to the project root.", required=True),
        ParameterSpec("content", "string", "Initial content.", default=""),
    )

    async def run(self, ctx: ToolContext, path: str, content: str = "") -> ToolResult:
        target = resolve(ctx, path)
        if target.exists():
            return ToolResult.failure(f"{relative(ctx, target)} already exists. Use apply_patch or write_file to modify it.")
        tool = WriteFileTool()
        return await tool.run(ctx, path=path, content=content, create=True, overwrite=False)


class DeletePathTool(Tool):
    name = "delete_path"
    description = (
        "Delete a file (or an empty directory) after copying it into .unknown/backups. "
        "Requires the destructive policy to be enabled."
    )
    category = "filesystem"
    risk = RiskLevel.DESTRUCTIVE
    parameters = (
        ParameterSpec("path", "string", "File or empty directory to delete.", required=True),
        ParameterSpec("recursive", "boolean", "Delete directory trees recursively.", default=False),
        ParameterSpec("reason", "string", "Why the deletion is needed (recorded in the journal).", default=""),
    )

    async def run(self, ctx: ToolContext, path: str, recursive: bool = False, reason: str = "") -> ToolResult:
        target = resolve(ctx, path)
        if not target.exists():
            return ToolResult.failure(f"Nothing to delete at {relative(ctx, target)}")
        if target == ctx.project_root:
            return ToolResult.failure("Refusing to delete the project root.")
        size = _path_size(target)
        if size > ctx.settings.policy.max_delete_bytes:
            return ToolResult.failure(
                f"Refusing to delete {size} bytes (policy.max_delete_bytes={ctx.settings.policy.max_delete_bytes}).",
                output="Raise the limit deliberately in config if this is intended.",
            )
        backup = ctx.backup_path(relative(ctx, target).replace("/", "__"))
        if target.is_dir():
            if any(target.iterdir()) and not recursive:
                return ToolResult.failure(f"{relative(ctx, target)} is not empty; pass recursive=true to delete the tree.")
            shutil.copytree(target, backup, symlinks=True)
            shutil.rmtree(target)
        else:
            shutil.copy2(target, backup)
            target.unlink()
        ctx.emit("observation", f"deleted {relative(ctx, target)} (backup: {backup.name})")
        return ToolResult.success(
            f"deleted {relative(ctx, target)} ({size} bytes). Backup kept at .unknown/backups/{backup.name}",
            path=relative(ctx, target),
            backup=str(backup),
            reason=reason,
        )


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files and directories at a path, one level deep, with sizes and types."
    category = "filesystem"
    risk = RiskLevel.READ
    concurrency_safe = True
    parameters = (
        ParameterSpec("path", "string", "Directory relative to the project root.", default="."),
        ParameterSpec("include_hidden", "boolean", "Include dotfiles.", default=False),
        ParameterSpec("limit", "integer", "Maximum entries.", default=200, minimum=1, maximum=2000),
    )

    async def run(self, ctx: ToolContext, path: str = ".", include_hidden: bool = False, limit: int = 200) -> ToolResult:
        target = resolve(ctx, path)
        if not target.exists():
            return ToolResult.failure(f"Path not found: {relative(ctx, target)}")
        if target.is_file():
            return ToolResult.success(f"{relative(ctx, target)} ({target.stat().st_size} bytes)")
        spec = _ignore_spec(ctx)
        entries = []
        for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            name = entry.name
            if not include_hidden and name.startswith("."):
                continue
            if spec.match_file(str(entry.relative_to(ctx.project_root).as_posix()) + ("/" if entry.is_dir() else "")):
                continue
            if entry.is_dir():
                entries.append(f"[dir]  {name}/")
            else:
                entries.append(f"[file] {name}  ({entry.stat().st_size} bytes)")
            if len(entries) >= limit:
                entries.append(f"... (limit {limit} reached)")
                break
        header = f"{relative(ctx, target)}: {len(entries)} entries"
        return ToolResult.success("\n".join([header, *entries]), path=relative(ctx, target), entries=len(entries))


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Regex search across the project's source files (gitignore-aware). Returns "
        "file:line:content matches, grouped by file. Use this to find definitions, "
        "usages and configuration without reading whole trees."
    )
    category = "search"
    risk = RiskLevel.READ
    tags = ("inspect", "context", "primary")
    parameters = (
        ParameterSpec("pattern", "string", "Python regular expression.", required=True),
        ParameterSpec("path", "string", "Directory or file to search.", default="."),
        ParameterSpec("glob", "string", "Only search files matching this glob (e.g. '*.py').", default=""),
        ParameterSpec("case_sensitive", "boolean", "Match case sensitively.", default=False),
        ParameterSpec("max_results", "integer", "Maximum matches to return.", default=80, minimum=1, maximum=1000),
        ParameterSpec("context_lines", "integer", "Lines of context around each match.", default=0, minimum=0, maximum=6),
    )
    examples = ('ua tools call search_code --arg pattern="def main" --arg glob="*.py"',)

    async def run(self, ctx: ToolContext, pattern: str, path: str = ".", glob: str = "", case_sensitive: bool = False, max_results: int = 80, context_lines: int = 0) -> ToolResult:
        root = resolve(ctx, path)
        if not root.exists():
            return ToolResult.failure(f"Search path not found: {relative(ctx, root)}")
        try:
            regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return ToolResult.failure(f"Invalid regular expression: {exc}")
        files = [root] if root.is_file() else iter_files(ctx, root)
        if glob:
            files = [item for item in files if fnmatch.fnmatch(item.name, glob) or fnmatch.fnmatch(str(item), glob)]
        matches: list[str] = []
        files_hit = 0
        for file_path in files:
            if file_path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            lines = text.splitlines()
            hits: list[str] = []
            for index, line in enumerate(lines, start=1):
                if regex.search(line):
                    rel = relative(ctx, file_path)
                    if context_lines:
                        start = max(1, index - context_lines)
                        end = min(len(lines), index + context_lines)
                        block = "\n".join(f"    {number:>5}: {lines[number - 1]}" for number in range(start, end + 1))
                        hits.append(f"  {rel}:{index}\n{block}")
                    else:
                        hits.append(f"  {rel}:{index}: {line.strip()[:200]}")
                    if len(matches) + len(hits) >= max_results:
                        break
            if hits:
                files_hit += 1
                matches.append(f"{relative(ctx, file_path)} ({len(hits)} match{'es' if len(hits) != 1 else ''})")
                matches.extend(hits)
            if len(matches) >= max_results:
                break
        if not matches:
            return ToolResult.success(f"No matches for /{pattern}/ in {relative(ctx, root)}", matches=0)
        body = "\n".join(matches[: max_results + files_hit])
        if len(matches) > max_results:
            body += f"\n... ({len(matches) - max_results} more matches not shown)"
        return ToolResult.success(body, matches=len(matches), files=files_hit)


class GlobFilesTool(Tool):
    name = "glob_files"
    description = "Find files by glob pattern (e.g. '**/*test*.py', 'src/**/index.ts')."
    category = "search"
    risk = RiskLevel.READ
    parameters = (
        ParameterSpec("pattern", "string", "Glob pattern relative to the project root.", required=True),
        ParameterSpec("limit", "integer", "Maximum paths returned.", default=200, minimum=1, maximum=2000),
    )

    async def run(self, ctx: ToolContext, pattern: str, limit: int = 200) -> ToolResult:
        spec = _ignore_spec(ctx)
        matches: list[str] = []
        for path in sorted(ctx.project_root.glob(pattern)):
            relative_path = path.relative_to(ctx.project_root).as_posix()
            if spec.match_file(relative_path):
                continue
            matches.append(f"{relative_path}{'/' if path.is_dir() else ''}")
            if len(matches) >= limit:
                break
        if not matches:
            return ToolResult.success(f"No files match {pattern!r}", matches=0)
        return ToolResult.success("\n".join(matches), matches=len(matches))


class ProjectTreeTool(Tool):
    name = "project_tree"
    description = "Print an indented directory tree of the project, ignoring build output and dependencies."
    category = "search"
    risk = RiskLevel.READ
    tags = ("inspect",)
    parameters = (
        ParameterSpec("path", "string", "Subdirectory to start from.", default="."),
        ParameterSpec("depth", "integer", "Maximum depth.", default=3, minimum=1, maximum=8),
        ParameterSpec("limit", "integer", "Maximum entries.", default=300, minimum=10, maximum=3000),
    )

    async def run(self, ctx: ToolContext, path: str = ".", depth: int = 3, limit: int = 300) -> ToolResult:
        root = resolve(ctx, path)
        spec = _ignore_spec(ctx)
        lines: list[str] = [f"{relative(ctx, root)}/"]
        count = 0

        def walk(directory: Path, level: int, prefix: str) -> None:
            nonlocal count
            if level > depth or count >= limit:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError:
                return
            for entry in entries:
                if count >= limit:
                    return
                if entry.name in DEFAULT_IGNORE_DIRS or (entry.name.startswith(".") and entry.name not in {".github", ".env.example"}):
                    continue
                relative_path = entry.relative_to(ctx.project_root).as_posix()
                if spec.match_file(relative_path + ("/" if entry.is_dir() else "")):
                    continue
                count += 1
                connector = "├── " if entry.is_dir() else "└── "
                lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
                if entry.is_dir():
                    walk(entry, level + 1, prefix + ("│   " if entry.is_dir() else "    "))

        walk(root, 1, "")
        if count >= limit:
            lines.append(f"... (limit {limit} entries reached)")
        return ToolResult.success("\n".join(lines), entries=count)


class MovePathTool(Tool):
    name = "move_path"
    description = "Move or rename a file/directory inside the project."
    category = "filesystem"
    risk = RiskLevel.WRITE
    parameters = (
        ParameterSpec("source", "string", "Existing path.", required=True),
        ParameterSpec("destination", "string", "New path.", required=True),
    )

    async def run(self, ctx: ToolContext, source: str, destination: str) -> ToolResult:
        src = resolve(ctx, source)
        dst = resolve(ctx, destination)
        if not src.exists():
            return ToolResult.failure(f"Source not found: {relative(ctx, src)}")
        if dst.exists():
            return ToolResult.failure(f"Destination already exists: {relative(ctx, dst)}")
        for decision_path in (src, dst):
            decision = _resolver(ctx).check_path(decision_path)
            if not decision.allowed:
                return ToolResult.failure(f"Denied by policy: {decision.reason}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        ctx.emit("observation", f"moved {relative(ctx, src)} -> {relative(ctx, dst)}")
        return ToolResult.success(f"moved {relative(ctx, src)} -> {relative(ctx, dst)}", source=relative(ctx, src), destination=relative(ctx, dst))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _diff_stats(before: str, after: str) -> tuple[int, int]:
    import difflib

    added = removed = 0
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _apply_search_replace(original: str, patch: str, *, replace_all: bool = False) -> tuple[str, int]:
    """Parse ``<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE`` blocks."""

    blocks = re.findall(
        r"<{5,9}\s*SEARCH\s*\n(.*?)\n?={5,9}\s*\n(.*?)\n?>{5,9}\s*REPLACE",
        patch,
        flags=re.DOTALL,
    )
    if not blocks:
        raise ValueError(
            "No valid search/replace block found.",
        )
    updated = original
    applied = 0
    for search, replace in blocks:
        if search and search not in updated:
            raise ValueError(
                "Search block not found in the file:\n" + "\n".join(search.splitlines()[:6]),
            )
        if not search:
            updated = updated + ("\n" if not updated.endswith("\n") else "") + replace
        elif replace_all:
            applied += updated.count(search)
            updated = updated.replace(search, replace)
        else:
            applied += 1
            updated = updated.replace(search, replace, 1)
    return updated, applied


def _apply_unified(original: str, patch: str) -> tuple[str, int]:
    """Apply a unified diff by locating each hunk's removal block in the file."""

    patch_lines = patch.splitlines(keepends=True)
    if not patch_lines:
        raise ValueError("Empty patch.")
    return _apply_hunks(original.splitlines(keepends=True), patch_lines)


def _apply_hunks(original_lines: list[str], patch_lines: list[str]) -> tuple[str, int]:
    """Apply unified-diff hunks with fuzzy context matching (exact first, then trimmed)."""

    hunks: list[tuple[int, list[str]]] = []
    current: list[str] = []
    start_line = 0
    for line in patch_lines:
        if line.startswith("@@"):
            if current:
                hunks.append((start_line, current))
            match = re.search(r"@@\s*-(\d+)(?:,\d+)?\s*\+(\d+)(?:,\d+)?\s*@@", line)
            start_line = int(match.group(1)) if match else 1
            current = []
            continue
        if line.startswith(("---", "+++")):
            continue
        if line[:1] in {" ", "-", "+", "\\"}:
            current.append(line)
    if current:
        hunks.append((start_line, current))
    if not hunks:
        raise ValueError("Patch contains no hunks.")

    lines = list(original_lines)
    offset = 0
    for start_line, hunk in hunks:
        removals = [line[1:] for line in hunk if line.startswith(("-", " "))]
        additions = [line[1:] for line in hunk if line.startswith(("+", " "))]
        if not removals:
            index = max(0, start_line - 1 + offset)
            lines[index:index] = [line[1:] for line in hunk if line.startswith("+")]
            offset += len([line for line in hunk if line.startswith("+")])
            continue
        index = _find_block(lines, removals, start_line - 1 + offset)
        if index is None:
            raise ValueError(
                "Hunk context does not match the file near line "
                f"{start_line}. Re-read the file and rebuild the patch from current content."
            )
        lines[index : index + len(removals)] = additions
        offset += len(additions) - len(removals)
    return "".join(lines), len(hunks)


def _find_block(lines: list[str], block: list[str], hint: int) -> int | None:
    def normalise(items: list[str]) -> list[str]:
        return [item.strip() for item in items]

    target = normalise(block)
    window = [normalise(line) for line in lines]
    candidates = [hint] + [index for index in range(len(window))]
    for index in candidates:
        if index < 0 or index + len(target) > len(window):
            continue
        if window[index : index + len(target)] == target:
            return index
    return None


def _patch_failure_help(original: str, patch: str) -> str:
    preview = "\n".join(original.splitlines()[:20])
    return (
        "The patch could not be applied. Re-read the file and rebuild the patch with exact context.\n\n"
        f"First lines of the current file:\n{preview}"
    )


def _suggest_paths(ctx: ToolContext, missing: str) -> list[str]:
    import difflib

    target_name = Path(missing).name.lower()
    candidates = [
        relative(ctx, path)
        for path in iter_files(ctx, ctx.project_root, max_files=5000)
        if path.suffix in {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json", ".toml", ".yaml", ".yml"}
    ]
    close = difflib.get_close_matches(target_name, [Path(item).name.lower() for item in candidates], n=5, cutoff=0.5)
    suggestions = [item for item in candidates if Path(item).name.lower() in close]
    if not suggestions:
        suggestions = candidates[:8]
    return [f"  - {item}" for item in suggestions[:8]]


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:  # pragma: no cover
                continue
    return total


def build_tools() -> list[Tool]:
    return [
        ReadFileTool(),
        WriteFileTool(),
        ApplyPatchTool(),
        CreateFileTool(),
        DeletePathTool(),
        ListFilesTool(),
        SearchCodeTool(),
        GlobFilesTool(),
        ProjectTreeTool(),
        MovePathTool(),
    ]


__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "ApplyPatchTool",
    "CreateFileTool",
    "DeletePathTool",
    "ListFilesTool",
    "SearchCodeTool",
    "GlobFilesTool",
    "ProjectTreeTool",
    "MovePathTool",
    "build_tools",
    "iter_files",
    "resolve",
    "relative",
]
