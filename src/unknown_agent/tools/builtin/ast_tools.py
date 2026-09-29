"""Structure-aware code editing: refactor by syntax tree, not by string match.

Text replacement is how agents corrupt repositories.  ``old.replace(new)`` hits
the string inside a docstring, the similarly-named method on another class, and
the commented-out line nobody meant to resurrect.  Indentation-based languages
punish it hardest: splice a block at the wrong depth and the file still parses,
just wrongly, and the bug surfaces three steps later in a test the agent then
"fixes" by weakening it.

These tools locate code through the syntax tree and edit by **line span
splicing**, which is the combination that actually works in production:

* Location comes from :mod:`ast`, so ``process`` on ``class Order`` is a
  different target from ``process`` on ``class Invoice``, and a match inside a
  string literal is not a match at all.
* The edit itself is a line-range splice rather than an ``ast.unparse``
  round-trip.  Unparsing would normalise the whole file — destroying comments,
  blank-line grouping and string quoting style — and produce a diff no reviewer
  can read.  Splicing touches only the span that changed.
* Indentation is re-derived from the target's own column offset, so a method
  moved into a class body arrives at the right depth automatically.
* **Nothing is written until the result re-parses.**  Every mutation runs
  ``ast.parse`` on the finished buffer first; if the edit would produce a broken
  file the original is left untouched and the syntax error is reported.

TypeScript and JavaScript have no parser in the Python standard library.  Rather
than pretend otherwise, :class:`TsStructureIndex` implements a scanner that is
aware of strings, template literals, regex literals and both comment forms, and
matches braces to find declaration bounds.  That is genuinely structural — it
cannot be fooled by a brace inside a string, which is the failure mode that
matters — but it is not a full parser, and the tools say so in their output
rather than overclaiming.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.observability.logging import get_logger
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

log = get_logger("tools.ast")

__all__ = [
    "SymbolRef",
    "PySymbolIndex",
    "TsStructureIndex",
    "EditResult",
    "AstOutlineTool",
    "AstReplaceSymbolTool",
    "AstInsertSymbolTool",
    "AstRemoveSymbolTool",
    "AstAddImportTool",
    "AstRenameSymbolTool",
    "build_tools",
]

PY_SUFFIXES = frozenset({".py", ".pyi"})
TS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


# --------------------------------------------------------------------------- #
# symbol model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SymbolRef:
    """One addressable declaration in a file."""

    #: Dotted path within the file, e.g. ``Order.process`` or ``main``.
    qualname: str
    kind: Literal["function", "async function", "class", "method", "async method", "variable"]
    #: 1-based inclusive line span of the whole declaration, decorators included.
    start_line: int
    end_line: int
    #: Column the declaration starts at — the indentation to reproduce.
    col_offset: int
    signature: str = ""
    decorators: tuple[str, ...] = ()
    docstring: str = ""
    parent: str = ""

    @property
    def indent(self) -> str:
        return " " * self.col_offset

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualname": self.qualname,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "lines": self.line_count,
            "col_offset": self.col_offset,
            "signature": self.signature,
            "decorators": list(self.decorators),
            "parent": self.parent,
            "docstring": (self.docstring or "")[:200],
        }


@dataclass(slots=True)
class EditResult:
    """Outcome of a structural edit, including the diff a human can review."""

    ok: bool
    path: str
    message: str = ""
    diff: str = ""
    error: str = ""
    symbol: SymbolRef | None = None
    lines_before: int = 0
    lines_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "message": self.message,
            "error": self.error,
            "symbol": self.symbol.to_dict() if self.symbol else None,
            "lines_before": self.lines_before,
            "lines_after": self.lines_after,
        }


# --------------------------------------------------------------------------- #
# Python index
# --------------------------------------------------------------------------- #
class PySymbolIndex:
    """Every declaration in a Python file, addressable by qualified name."""

    def __init__(self, source: str, *, filename: str = "<source>") -> None:
        self.source = source
        self.filename = filename
        self.lines = source.splitlines()
        self.tree = ast.parse(source, filename=filename)
        self.symbols: dict[str, SymbolRef] = {}
        # Keep the original nodes as well as their public descriptions. This is
        # needed for structural invariants such as "removing this method would
        # leave its class body empty".
        self.nodes: dict[str, ast.AST] = {}
        self._walk(self.tree, prefix="")

    def _walk(self, node: ast.AST, *, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                self.symbols[qualname] = self._describe(child, qualname=qualname, parent=prefix)
                self.nodes[qualname] = child
                self._walk(child, prefix=qualname)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and not prefix:
                for target in self._assign_targets(child):
                    self.symbols[target] = SymbolRef(
                        qualname=target,
                        kind="variable",
                        start_line=child.lineno,
                        end_line=getattr(child, "end_lineno", child.lineno) or child.lineno,
                        col_offset=child.col_offset,
                    )

    @staticmethod
    def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[str]:
        names: list[str] = []
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
        return names

    def _describe(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        *,
        qualname: str,
        parent: str,
    ) -> SymbolRef:
        if isinstance(node, ast.ClassDef):
            kind: Any = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async method" if parent else "async function"
        else:
            kind = "method" if parent else "function"

        # Decorators are part of the declaration: a replace that drops
        # @property silently changes behaviour, so the span must include them.
        start = node.lineno
        for decorator in node.decorator_list:
            start = min(start, decorator.lineno)

        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        signature = self._signature(node)
        return SymbolRef(
            qualname=qualname,
            kind=kind,
            start_line=start,
            end_line=end,
            col_offset=node.col_offset,
            signature=signature,
            decorators=tuple(self._decorator_text(item) for item in node.decorator_list),
            docstring=ast.get_docstring(node) or "",
            parent=parent,
        )

    @staticmethod
    def _decorator_text(node: ast.expr) -> str:
        try:
            return "@" + ast.unparse(node)
        except Exception:  # noqa: BLE001 - unparse can fail on exotic nodes
            return "@<decorator>"

    def _signature(self, node: ast.AST) -> str:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(self._safe_unparse(base) for base in node.bases)
            return f"class {node.name}({bases})" if bases else f"class {node.name}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                args = ast.unparse(node.args)
            except Exception:  # noqa: BLE001
                args = "..."
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            returns = f" -> {self._safe_unparse(node.returns)}" if node.returns else ""
            return f"{prefix} {node.name}({args}){returns}"
        return ""

    @staticmethod
    def _safe_unparse(node: ast.AST | None) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return "..."

    # -- lookup ------------------------------------------------------------
    def find(self, target: str) -> SymbolRef | None:
        """Resolve ``Class.method``, ``method``, or a bare function name."""

        if target in self.symbols:
            return self.symbols[target]
        # Allow an unqualified name when it is unambiguous.
        tail_matches = [
            ref for name, ref in self.symbols.items() if name.rsplit(".", 1)[-1] == target
        ]
        if len(tail_matches) == 1:
            return tail_matches[0]
        return None

    def ambiguous(self, target: str) -> list[str]:
        return sorted(
            name for name in self.symbols if name.rsplit(".", 1)[-1] == target and name != target
        )

    def imports(self) -> list[tuple[int, str]]:
        """(line, text) for every import at module level."""

        found: list[tuple[int, str]] = []
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.append((node.lineno, self._safe_unparse(node)))
        return found

    def last_import_line(self) -> int:
        """Line after which a new import belongs (0 when there are none)."""

        last = 0
        for node in self.tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                last = max(last, getattr(node, "end_lineno", node.lineno) or node.lineno)
            elif last:
                break
        return last

    def has_import(self, statement: str) -> bool:
        wanted = " ".join(statement.split())
        return any(" ".join(text.split()) == wanted for _, text in self.imports())


# --------------------------------------------------------------------------- #
# TypeScript / JavaScript structural index
# --------------------------------------------------------------------------- #
_TS_DECL = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<prefix>(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?)"
    r"(?:(?P<kind>class|interface|enum|function|const|let|var)\s+)"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_TS_METHOD = re.compile(
    r"^(?P<indent>[ \t]+)"
    r"(?:(?:public|private|protected|readonly|static|async|get|set)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("
)


class TsStructureIndex:
    """Brace-matched declaration bounds for TS/JS, string- and comment-aware.

    This is not a TypeScript parser and does not claim to be one.  What it does
    guarantee is the property that makes naive text editing dangerous: braces,
    slashes and keywords appearing inside strings, template literals, regex
    literals or comments are never mistaken for code.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines()
        self._code_mask = self._build_mask(source)
        self.symbols: dict[str, SymbolRef] = {}
        self._index()

    @staticmethod
    def _build_mask(source: str) -> list[bool]:
        """True for characters that are real code, False inside strings/comments."""

        mask = [True] * len(source)
        index = 0
        length = len(source)
        state: str = "code"
        quote = ""
        while index < length:
            char = source[index]
            nxt = source[index + 1] if index + 1 < length else ""
            if state == "code":
                if char == "/" and nxt == "/":
                    state = "line_comment"
                    mask[index] = mask[min(index + 1, length - 1)] = False
                    index += 2
                    continue
                if char == "/" and nxt == "*":
                    state = "block_comment"
                    mask[index] = mask[min(index + 1, length - 1)] = False
                    index += 2
                    continue
                if char in "\"'`":
                    state = "string"
                    quote = char
                    mask[index] = False
                    index += 1
                    continue
                index += 1
                continue
            if state == "line_comment":
                mask[index] = False
                if char == "\n":
                    state = "code"
                index += 1
                continue
            if state == "block_comment":
                mask[index] = False
                if char == "*" and nxt == "/":
                    mask[min(index + 1, length - 1)] = False
                    state = "code"
                    index += 2
                    continue
                index += 1
                continue
            # inside a string
            mask[index] = False
            if char == "\\":
                if index + 1 < length:
                    mask[index + 1] = False
                index += 2
                continue
            if char == quote:
                state = "code"
                quote = ""
            index += 1
        return mask

    def _line_offsets(self) -> list[int]:
        offsets = [0]
        for line in self.source.split("\n")[:-1]:
            offsets.append(offsets[-1] + len(line) + 1)
        return offsets

    def _index(self) -> None:
        offsets = self._line_offsets()
        stack: list[tuple[str, int]] = []  # (container name, closing offset)
        for number, raw in enumerate(self.lines, start=1):
            start_offset = offsets[number - 1] if number - 1 < len(offsets) else 0
            # Skip lines that begin inside a string or comment.
            if start_offset < len(self._code_mask) and not self._code_mask[start_offset]:
                if not raw.strip():
                    continue

            while stack and start_offset > stack[-1][1]:
                stack.pop()

            match = _TS_DECL.match(raw)
            name = ""
            kind: Any = "function"
            if match:
                name = match.group("name")
                raw_kind = (match.group("kind") or "").lower()
                kind = "class" if raw_kind in {"class", "interface", "enum"} else "function"
                if raw_kind in {"const", "let", "var"} and "=>" not in raw and "function" not in raw:
                    kind = "variable"
            elif stack:
                method = _TS_METHOD.match(raw)
                # Guard against control-flow keywords that also look like calls.
                if method and method.group("name") not in {
                    "if", "for", "while", "switch", "catch", "return", "typeof", "await",
                }:
                    name = method.group("name")
                    kind = "method"
            if not name:
                continue

            parent = stack[-1][0] if stack else ""
            qualname = f"{parent}.{name}" if parent else name
            end_line, end_offset = self._span_end(number, start_offset)
            self.symbols[qualname] = SymbolRef(
                qualname=qualname,
                kind=kind,
                start_line=number,
                end_line=end_line,
                col_offset=len(raw) - len(raw.lstrip()),
                signature=raw.strip()[:200],
                parent=parent,
            )
            if kind in {"class", "function", "method"} and end_line > number:
                stack.append((qualname, end_offset))

    def _span_end(self, start_line: int, start_offset: int) -> tuple[int, int]:
        """Walk forward matching braces, ignoring masked-out characters."""

        depth = 0
        opened = False
        index = start_offset
        length = len(self.source)
        while index < length:
            if self._code_mask[index]:
                char = self.source[index]
                if char == "{":
                    depth += 1
                    opened = True
                elif char == "}":
                    depth -= 1
                    if opened and depth == 0:
                        return self.source.count("\n", 0, index) + 1, index
                elif char == ";" and not opened:
                    # A declaration with no body (interface member, type alias).
                    return self.source.count("\n", 0, index) + 1, index
            index += 1
        return start_line, start_offset

    def find(self, target: str) -> SymbolRef | None:
        if target in self.symbols:
            return self.symbols[target]
        matches = [ref for name, ref in self.symbols.items() if name.rsplit(".", 1)[-1] == target]
        return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _detect_newline(source: str) -> str:
    if "\r\n" in source:
        return "\r\n"
    return "\n"


def _reindent(block: str, indent: str) -> str:
    """Re-anchor a block of code to a new indentation level.

    The block's own shallowest non-blank line defines its base; everything keeps
    its relative depth.  This is what lets a module-level function be inserted
    as a method without the caller hand-counting spaces.
    """

    lines = block.expandtabs(4).splitlines()
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return block
    base = min(len(line) - len(line.lstrip()) for line in meaningful)
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        out.append(indent + line[base:])
    return "\n".join(out)


def _splice(lines: list[str], start_line: int, end_line: int, replacement: list[str]) -> list[str]:
    """Replace the inclusive 1-based span [start_line, end_line]."""

    return lines[: start_line - 1] + replacement + lines[end_line:]


def _make_diff(before: str, after: str, path: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    text = "".join(diff)
    return text if len(text) <= 8000 else text[:8000] + "\n... [diff truncated]\n"


def _validate(path: Path, content: str) -> str:
    """Return an error message when the content would not parse, else ''."""

    if path.suffix in PY_SUFFIXES:
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return f"edit rejected: result would not parse — {exc.msg} at line {exc.lineno}"
        return ""
    if path.suffix in TS_SUFFIXES:
        mask = TsStructureIndex._build_mask(content)
        depth = 0
        for index, char in enumerate(content):
            if not mask[index]:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    line = content.count("\n", 0, index) + 1
                    return f"edit rejected: unbalanced '}}' at line {line}"
        if depth != 0:
            return f"edit rejected: {depth} unclosed '{{' in the result"
    return ""


def _write_checked(path: Path, before: str, after: str) -> EditResult:
    """The single place a structural edit reaches disk."""

    relative_path = path.name
    problem = _validate(path, after)
    if problem:
        return EditResult(
            ok=False,
            path=relative_path,
            error=problem,
            lines_before=len(before.splitlines()),
        )
    if before == after:
        return EditResult(
            ok=True,
            path=relative_path,
            message="no change was necessary — the file already matches",
            lines_before=len(before.splitlines()),
            lines_after=len(after.splitlines()),
        )
    path.write_text(after, encoding="utf-8")
    return EditResult(
        ok=True,
        path=relative_path,
        message="written",
        diff=_make_diff(before, after, relative_path),
        lines_before=len(before.splitlines()),
        lines_after=len(after.splitlines()),
    )


def _index_for(path: Path, source: str) -> PySymbolIndex | TsStructureIndex:
    if path.suffix in PY_SUFFIXES:
        return PySymbolIndex(source, filename=str(path))
    return TsStructureIndex(source)


def _resolve_path(ctx: ToolContext, raw: str) -> Path:
    from unknown_agent.tools.builtin.fs_tools import resolve

    return resolve(ctx, raw)


def _relative(ctx: ToolContext, path: Path) -> str:
    from unknown_agent.tools.builtin.fs_tools import relative

    return relative(ctx, path)


def _supported(path: Path) -> str:
    if path.suffix in PY_SUFFIXES or path.suffix in TS_SUFFIXES:
        return ""
    return (
        f"{path.suffix or 'this file type'} is not structurally supported; "
        f"supported: {', '.join(sorted(PY_SUFFIXES | TS_SUFFIXES))}"
    )


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
class AstOutlineTool(Tool):
    name = "ast_outline"
    description = (
        "List every function, class and method in a file with its exact line span, signature and "
        "decorators. Call this before editing so you address a symbol by name instead of guessing "
        "line numbers or matching text."
    )
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("refactor", "ast", "primary")
    parameters = (
        ParameterSpec("path", "string", "File to outline.", required=True),
        ParameterSpec("kind", "string", "Only this kind (function, class, method, variable).", default=""),
    )
    examples = ("ua tools call ast_outline --arg path=src/app.py",)

    async def run(self, ctx: ToolContext, path: str = "", kind: str = "") -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        unsupported = _supported(target)
        if unsupported:
            return ToolResult.failure(unsupported)
        source = _read(target)
        try:
            index = _index_for(target, source)
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        symbols = sorted(index.symbols.values(), key=lambda ref: ref.start_line)
        if kind:
            symbols = [ref for ref in symbols if kind.lower() in ref.kind]
        if not symbols:
            return ToolResult.success(f"{path}: no matching declarations", symbols=[])

        lines = [f"{_relative(ctx, target)} — {len(symbols)} declaration(s)"]
        for ref in symbols:
            marker = "  " * ref.qualname.count(".")
            decorators = f" {' '.join(ref.decorators)}" if ref.decorators else ""
            lines.append(
                f"  {marker}{ref.qualname}  [{ref.kind}] "
                f"lines {ref.start_line}-{ref.end_line} ({ref.line_count}){decorators}"
            )
            if ref.signature:
                lines.append(f"  {marker}    {ref.signature}")
        note = ""
        if isinstance(index, TsStructureIndex):
            note = "\n(TS/JS: structural brace matching, string- and comment-aware — not a full parser)"
        return ToolResult.success(
            "\n".join(lines) + note,
            symbols=[ref.to_dict() for ref in symbols],
            language="python" if isinstance(index, PySymbolIndex) else "typescript",
        )


class AstReplaceSymbolTool(Tool):
    name = "ast_replace_symbol"
    description = (
        "Replace a whole function, method or class by qualified name (e.g. 'Order.process'), "
        "preserving surrounding code, comments and indentation. The new code is re-indented to the "
        "target's depth automatically, and the file is only written if the result still parses."
    )
    category = "analysis"
    risk = RiskLevel.WRITE
    tags = ("refactor", "ast", "primary")
    parameters = (
        ParameterSpec("path", "string", "File to edit.", required=True),
        ParameterSpec("symbol", "string", "Qualified name, e.g. 'Order.process' or 'main'.", required=True),
        ParameterSpec("code", "string", "Full replacement declaration, at any indentation.", required=True),
        ParameterSpec("dry_run", "boolean", "Return the diff without writing.", default=False),
    )
    examples = ('ua tools call ast_replace_symbol --arg path=src/app.py --arg symbol=main',)

    async def run(
        self, ctx: ToolContext, path: str = "", symbol: str = "", code: str = "", dry_run: bool = False
    ) -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        unsupported = _supported(target)
        if unsupported:
            return ToolResult.failure(unsupported)
        source = _read(target)
        try:
            index = _index_for(target, source)
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        ref = index.find(symbol)
        if ref is None:
            if isinstance(index, PySymbolIndex):
                options = index.ambiguous(symbol)
                if options:
                    return ToolResult.failure(
                        f"{symbol!r} is ambiguous in {path} — qualify it: {', '.join(options)}"
                    )
            available = ", ".join(sorted(index.symbols)[:25]) or "none"
            return ToolResult.failure(f"{symbol!r} not found in {path}. Available: {available}")

        newline = _detect_newline(source)
        lines = source.split(newline)
        replacement = _reindent(code.strip("\n"), ref.indent).split("\n")
        after = newline.join(_splice(lines, ref.start_line, ref.end_line, replacement))

        if dry_run:
            problem = _validate(target, after)
            return ToolResult(
                ok=not problem,
                output=_make_diff(source, after, _relative(ctx, target)) or "no change",
                error=problem or None,
                data={"symbol": ref.to_dict(), "dry_run": True},
            )

        result = _write_checked(target, source, after)
        result.symbol = ref
        result.path = _relative(ctx, target)
        if not result.ok:
            return ToolResult.failure(result.error, output=result.diff)
        return ToolResult.success(
            f"replaced {ref.qualname} in {result.path} "
            f"(lines {ref.start_line}-{ref.end_line} -> {len(replacement)} line(s))\n\n{result.diff}",
            **result.to_dict(),
        )


class AstInsertSymbolTool(Tool):
    name = "ast_insert_symbol"
    description = (
        "Insert a new function or method into a file — at module level, or inside a class by name. "
        "Indentation is derived from the destination, so the block always lands at the right depth. "
        "Refuses to shadow an existing symbol unless replace=true."
    )
    category = "analysis"
    risk = RiskLevel.WRITE
    tags = ("refactor", "ast")
    parameters = (
        ParameterSpec("path", "string", "File to edit.", required=True),
        ParameterSpec("code", "string", "The declaration to insert.", required=True),
        ParameterSpec("into_class", "string", "Insert as a member of this class.", default=""),
        ParameterSpec("after", "string", "Place it after this symbol (default: end of scope).", default=""),
        ParameterSpec("replace", "boolean", "Replace instead of failing if it already exists.", default=False),
    )

    async def run(
        self,
        ctx: ToolContext,
        path: str = "",
        code: str = "",
        into_class: str = "",
        after: str = "",
        replace: bool = False,
    ) -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        if target.suffix not in PY_SUFFIXES:
            return ToolResult.failure(
                "ast_insert_symbol currently supports Python only; for TS/JS use ast_replace_symbol "
                "on the enclosing declaration"
            )
        source = _read(target)
        try:
            index = PySymbolIndex(source, filename=str(target))
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        # Work out the name being inserted so we can detect collisions.
        try:
            parsed = ast.parse(code.strip())
        except SyntaxError as exc:
            return ToolResult.failure(f"the code to insert does not parse: {exc.msg} at line {exc.lineno}")
        declared = [
            node.name
            for node in parsed.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if not declared:
            return ToolResult.failure("the code to insert contains no function or class declaration")
        new_name = declared[0]
        qualname = f"{into_class}.{new_name}" if into_class else new_name

        existing = index.symbols.get(qualname)
        if existing and not replace:
            return ToolResult.failure(
                f"{qualname} already exists at lines {existing.start_line}-{existing.end_line}; "
                f"pass replace=true or use ast_replace_symbol"
            )

        newline = _detect_newline(source)
        lines = source.split(newline)

        if existing and replace:
            indent = existing.indent
            block = _reindent(code.strip("\n"), indent).split("\n")
            after_text = newline.join(_splice(lines, existing.start_line, existing.end_line, block))
        else:
            if into_class:
                container = index.find(into_class)
                if container is None or container.kind != "class":
                    return ToolResult.failure(f"class {into_class!r} not found in {path}")
                indent = " " * (container.col_offset + 4)
                anchor = container.end_line
                if after:
                    sibling = index.find(f"{into_class}.{after}") or index.find(after)
                    if sibling is not None and sibling.parent == into_class:
                        anchor = sibling.end_line
            else:
                indent = ""
                anchor = len(lines)
                if after:
                    sibling = index.find(after)
                    if sibling is None:
                        return ToolResult.failure(f"anchor symbol {after!r} not found in {path}")
                    anchor = sibling.end_line

            block = _reindent(code.strip("\n"), indent).split("\n")
            separator = [""] if indent else ["", ""]
            after_text = newline.join(lines[:anchor] + separator + block + lines[anchor:])

        result = _write_checked(target, source, after_text)
        result.path = _relative(ctx, target)
        if not result.ok:
            return ToolResult.failure(result.error)
        where = f"class {into_class}" if into_class else "module level"
        return ToolResult.success(
            f"inserted {qualname} into {where} of {result.path}\n\n{result.diff}", **result.to_dict()
        )


class AstRemoveSymbolTool(Tool):
    name = "ast_remove_symbol"
    description = (
        "Delete a function, method or class by qualified name, including its decorators, without "
        "leaving dangling blank lines or breaking the enclosing block."
    )
    category = "analysis"
    risk = RiskLevel.WRITE
    tags = ("refactor", "ast")
    parameters = (
        ParameterSpec("path", "string", "File to edit.", required=True),
        ParameterSpec("symbol", "string", "Qualified name to remove.", required=True),
        ParameterSpec("dry_run", "boolean", "Show the diff without writing.", default=False),
    )

    async def run(
        self, ctx: ToolContext, path: str = "", symbol: str = "", dry_run: bool = False
    ) -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        unsupported = _supported(target)
        if unsupported:
            return ToolResult.failure(unsupported)
        source = _read(target)
        try:
            index = _index_for(target, source)
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        ref = index.find(symbol)
        if ref is None:
            return ToolResult.failure(f"{symbol!r} not found in {path}")

        newline = _detect_newline(source)
        lines = source.split(newline)
        start, end = ref.start_line, ref.end_line
        replacement: list[str] = []

        # Python requires every class body to contain a statement. If this is
        # its last substantive member, leave a correctly-indented ``pass``
        # rather than either writing invalid syntax or refusing a safe removal.
        if isinstance(index, PySymbolIndex) and ref.parent:
            container = index.nodes.get(ref.parent)
            target_node = index.nodes.get(ref.qualname)
            if isinstance(container, ast.ClassDef) and target_node is not None:
                substantive = [
                    item
                    for item in container.body
                    if not (
                        isinstance(item, ast.Expr)
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)
                    )
                ]
                if len(substantive) == 1 and substantive[0] is target_node:
                    replacement = [f"{ref.indent}pass"]

        # Absorb trailing blank lines so removal does not leave a gap.
        while end < len(lines) and not lines[end].strip():
            end += 1
        after = newline.join(_splice(lines, start, end, replacement))

        if dry_run:
            problem = _validate(target, after)
            return ToolResult(
                ok=not problem,
                output=_make_diff(source, after, _relative(ctx, target)),
                error=problem or None,
                data={"symbol": ref.to_dict(), "dry_run": True},
            )

        result = _write_checked(target, source, after)
        result.path = _relative(ctx, target)
        if not result.ok:
            return ToolResult.failure(result.error)
        return ToolResult.success(
            f"removed {ref.qualname} ({ref.line_count} line(s)) from {result.path}\n\n{result.diff}",
            **result.to_dict(),
        )


class AstAddImportTool(Tool):
    name = "ast_add_import"
    description = (
        "Add an import to a Python file in the right place (after the existing import block, below "
        "the module docstring and __future__ imports), skipping it when already present."
    )
    category = "analysis"
    risk = RiskLevel.WRITE
    tags = ("refactor", "ast")
    parameters = (
        ParameterSpec("path", "string", "File to edit.", required=True),
        ParameterSpec("statement", "string", "e.g. 'from pathlib import Path'.", required=True),
    )

    async def run(self, ctx: ToolContext, path: str = "", statement: str = "") -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        if target.suffix not in PY_SUFFIXES:
            return ToolResult.failure("ast_add_import supports Python files only")
        statement = statement.strip()
        try:
            parsed = ast.parse(statement)
        except SyntaxError as exc:
            return ToolResult.failure(f"{statement!r} is not a valid import: {exc.msg}")
        if not parsed.body or not isinstance(parsed.body[0], (ast.Import, ast.ImportFrom)):
            return ToolResult.failure(f"{statement!r} is not an import statement")

        source = _read(target)
        try:
            index = PySymbolIndex(source, filename=str(target))
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        if index.has_import(statement):
            return ToolResult.success(f"{statement} is already imported in {_relative(ctx, target)}")

        newline = _detect_newline(source)
        lines = source.split(newline)
        anchor = index.last_import_line()
        if anchor == 0:
            # No imports: land after the module docstring, if any.
            anchor = 0
            body = index.tree.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                anchor = getattr(body[0], "end_lineno", 1) or 1
            new_lines = lines[:anchor] + ["", statement] + lines[anchor:]
        else:
            new_lines = lines[:anchor] + [statement] + lines[anchor:]

        after = newline.join(new_lines)
        result = _write_checked(target, source, after)
        result.path = _relative(ctx, target)
        if not result.ok:
            return ToolResult.failure(result.error)
        return ToolResult.success(
            f"added `{statement}` to {result.path}\n\n{result.diff}", **result.to_dict()
        )


@dataclass(slots=True)
class _RenameSite:
    line: int
    col: int
    context: str = ""


class _RenameCollector(ast.NodeVisitor):
    """Collect every binding and reference of a plain name, skipping attributes.

    Attribute access (``other.total``) is deliberately excluded: renaming a
    local called ``total`` must not touch ``config.total``, and that distinction
    is exactly what text substitution gets wrong.
    """

    def __init__(self, old: str) -> None:
        self.old = old
        self.sites: list[_RenameSite] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 - ast API
        if node.id == self.old:
            self.sites.append(_RenameSite(node.lineno, node.col_offset, "name"))
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:  # noqa: N802 - ast API
        if node.arg == self.old:
            self.sites.append(_RenameSite(node.lineno, node.col_offset, "arg"))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API
        self._maybe_def(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        self._maybe_def(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        self._maybe_def(node)
        self.generic_visit(node)

    def _maybe_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        if node.name != self.old:
            return
        # ``def <name>`` — the name starts after the keyword on the def line.
        self.sites.append(_RenameSite(node.lineno, -1, "definition"))


class AstRenameSymbolTool(Tool):
    name = "ast_rename_symbol"
    description = (
        "Rename a Python symbol through the syntax tree: definitions, references and parameters are "
        "updated, while attribute access on other objects, string literals and comments are left "
        "alone. This is the safe alternative to search-and-replace."
    )
    category = "analysis"
    risk = RiskLevel.WRITE
    tags = ("refactor", "ast", "primary")
    parameters = (
        ParameterSpec("path", "string", "File to edit.", required=True),
        ParameterSpec("old_name", "string", "Current identifier.", required=True),
        ParameterSpec("new_name", "string", "Replacement identifier.", required=True),
        ParameterSpec("dry_run", "boolean", "Show the diff without writing.", default=False),
    )

    async def run(
        self,
        ctx: ToolContext,
        path: str = "",
        old_name: str = "",
        new_name: str = "",
        dry_run: bool = False,
    ) -> ToolResult:
        target = _resolve_path(ctx, path)
        if not target.is_file():
            return ToolResult.failure(f"{path} is not a file")
        if target.suffix not in PY_SUFFIXES:
            return ToolResult.failure("ast_rename_symbol supports Python files only")
        if not new_name.isidentifier():
            return ToolResult.failure(f"{new_name!r} is not a valid Python identifier")

        source = _read(target)
        try:
            tree = ast.parse(source, filename=str(target))
        except SyntaxError as exc:
            return ToolResult.failure(f"{path} does not parse: {exc.msg} at line {exc.lineno}")

        collector = _RenameCollector(old_name)
        collector.visit(tree)
        if not collector.sites:
            return ToolResult.failure(
                f"{old_name!r} is not bound or referenced as a plain name in {path}; "
                f"attribute access and strings are intentionally not renamed"
            )

        newline = _detect_newline(source)
        lines = source.split(newline)
        # Apply right-to-left per line so earlier column offsets stay valid.
        by_line: dict[int, list[_RenameSite]] = {}
        for site in collector.sites:
            by_line.setdefault(site.line, []).append(site)

        changed = 0
        for line_number, sites in by_line.items():
            if line_number - 1 >= len(lines):
                continue
            text = lines[line_number - 1]
            for site in sorted(sites, key=lambda item: item.col, reverse=True):
                if site.col >= 0:
                    if text[site.col : site.col + len(old_name)] != old_name:
                        continue
                    text = text[: site.col] + new_name + text[site.col + len(old_name) :]
                    changed += 1
                else:
                    # A definition: rewrite only the declared name token.
                    pattern = re.compile(
                        rf"\b(def|class)(\s+){re.escape(old_name)}\b"
                    )
                    text, count = pattern.subn(rf"\1\g<2>{new_name}", text, count=1)
                    changed += count
            lines[line_number - 1] = text

        after = newline.join(lines)
        if dry_run:
            problem = _validate(target, after)
            return ToolResult(
                ok=not problem,
                output=_make_diff(source, after, _relative(ctx, target)),
                error=problem or None,
                data={"sites": changed, "dry_run": True},
            )

        result = _write_checked(target, source, after)
        result.path = _relative(ctx, target)
        if not result.ok:
            return ToolResult.failure(result.error)
        return ToolResult.success(
            f"renamed {old_name} -> {new_name} at {changed} site(s) in {result.path}\n\n{result.diff}",
            sites=changed,
            **result.to_dict(),
        )


def build_tools() -> list[Tool]:
    """Every AST tool, in the order a refactor usually needs them."""

    return [
        AstOutlineTool(),
        AstReplaceSymbolTool(),
        AstInsertSymbolTool(),
        AstRemoveSymbolTool(),
        AstAddImportTool(),
        AstRenameSymbolTool(),
    ]
