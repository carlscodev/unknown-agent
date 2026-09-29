"""Persistent Language Server Protocol intelligence for coding agents.

AST editing answers "what is written here?". LSP answers the harder questions:
"what does this name resolve to across the project?", "who references it?", and
"what does the compiler already know is wrong?".

A client is kept alive per project/language in ``ToolContext.extra``. Requests
are correlated by JSON-RPC id, diagnostics arrive asynchronously, stderr is
bounded, and a dead or wedged server is evicted instead of poisoning the rest of
the mission. No package is installed automatically: availability is discovered
from PATH and the exact installation remedy is returned to the agent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

__all__ = [
    "LspError",
    "ServerSpec",
    "SERVER_SPECS",
    "LspClient",
    "LspPool",
    "detect_server",
    "LspStatusTool",
    "LspSymbolsTool",
    "LspDefinitionTool",
    "LspReferencesTool",
    "LspHoverTool",
    "LspDiagnosticsTool",
    "LspRenamePreviewTool",
    "LspStopTool",
    "build_tools",
]

_POOL_KEY = "lsp:pool"


class LspError(RuntimeError):
    """Protocol, transport or server error with a safe message."""


@dataclass(frozen=True, slots=True)
class ServerSpec:
    language: str
    suffixes: tuple[str, ...]
    candidates: tuple[tuple[str, ...], ...]
    install_hint: str


SERVER_SPECS: tuple[ServerSpec, ...] = (
    ServerSpec(
        "python",
        (".py", ".pyi"),
        (("pyright-langserver", "--stdio"), ("pylsp",)),
        "install pyright (`npm install -g pyright`) or python-lsp-server",
    ),
    ServerSpec(
        "typescript",
        (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
        (("typescript-language-server", "--stdio"),),
        "install typescript-language-server and typescript globally",
    ),
    ServerSpec("rust", (".rs",), (("rust-analyzer",),), "install rust-analyzer"),
    ServerSpec("go", (".go",), (("gopls",),), "install gopls (`go install golang.org/x/tools/gopls@latest`)"),
    ServerSpec(
        "c-family",
        (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"),
        (("clangd",),),
        "install clangd",
    ),
)


def detect_server(path: Path, *, language: str = "") -> tuple[ServerSpec, list[str]]:
    """Return the first installed server matching a file or explicit language."""

    spec = next(
        (
            item
            for item in SERVER_SPECS
            if (language and item.language == language) or (not language and path.suffix.lower() in item.suffixes)
        ),
        None,
    )
    if spec is None:
        supported = ", ".join(sorted({suffix for item in SERVER_SPECS for suffix in item.suffixes}))
        raise LspError(f"no LSP mapping for {path.suffix or path.name}; supported: {supported}")
    for candidate in spec.candidates:
        executable = shutil.which(candidate[0])
        if executable:
            return spec, [executable, *candidate[1:]]
    names = ", ".join(candidate[0] for candidate in spec.candidates)
    raise LspError(f"no {spec.language} language server found ({names}); {spec.install_hint}")


@dataclass(slots=True)
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Any = None


class LspClient:
    """Thread-safe JSON-RPC/LSP client over one long-lived stdio process."""

    def __init__(
        self,
        command: list[str],
        *,
        root: Path,
        language: str,
        request_timeout: float = 20.0,
    ) -> None:
        self.command = list(command)
        self.root = root.resolve()
        self.language = language
        self.request_timeout = request_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._opened: dict[str, int] = {}
        self._stderr: deque[str] = deque(maxlen=200)
        self._notifications: deque[dict[str, Any]] = deque(maxlen=500)
        self.capabilities: dict[str, Any] = {}
        self.started_at = 0.0

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self.alive:
            return
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=str(self.root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise LspError(f"could not start {self.command[0]}: {exc}") from exc
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, name=f"lsp-{self.language}-stdout", daemon=True)
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name=f"lsp-{self.language}-stderr", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()
        try:
            result = self.request(
                "initialize",
                {
                    "processId": None,
                    "clientInfo": {"name": "unknown-agent", "version": "1"},
                    "rootUri": self.root.as_uri(),
                    "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
                    "capabilities": {
                        "workspace": {"workspaceFolders": True, "symbol": {"dynamicRegistration": False}},
                        "textDocument": {
                            "definition": {"dynamicRegistration": False},
                            "references": {"dynamicRegistration": False},
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                            "rename": {"prepareSupport": True},
                            "publishDiagnostics": {"relatedInformation": True},
                        },
                    },
                },
                timeout=30.0,
            )
            if isinstance(result, dict):
                self.capabilities = dict(result.get("capabilities") or {})
            self.notify("initialized", {})
        except Exception:
            self.stop()
            raise

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise LspError("language server is not running")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            try:
                process.stdin.write(frame)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LspError(f"language server transport closed: {exc}") from exc

    def request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        if not self.alive and method != "initialize":
            raise LspError("language server exited: " + self.stderr_tail())
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _Pending()
            self._pending[request_id] = pending
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        wait_for = timeout if timeout is not None else self.request_timeout
        if not pending.event.wait(wait_for):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise LspError(f"language server timed out after {wait_for:.1f}s on {method}")
        if pending.error is not None:
            raise LspError(f"{method} failed: {json.dumps(pending.error, ensure_ascii=False)}")
        return pending.result

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open_document(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_file():
            raise LspError(f"{resolved} is not a file")
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise LspError(f"{resolved} is outside LSP workspace {self.root}") from exc
        uri = resolved.as_uri()
        text = resolved.read_text(encoding="utf-8", errors="replace")
        version = self._opened.get(uri, 0) + 1
        if uri in self._opened:
            self.notify(
                "textDocument/didChange",
                {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
            )
        else:
            self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self.language,
                        "version": version,
                        "text": text,
                    }
                },
            )
        self._opened[uri] = version
        return uri

    def position_params(self, path: Path, line: int, column: int) -> dict[str, Any]:
        uri = self.open_document(path)
        return {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, column - 1)},
        }

    def diagnostics(self, path: Path, *, settle_ms: int = 500) -> list[dict[str, Any]]:
        uri = self.open_document(path)
        deadline = time.monotonic() + max(0, settle_ms) / 1000.0
        while time.monotonic() < deadline and uri not in self._diagnostics:
            time.sleep(0.025)
        return list(self._diagnostics.get(uri, ()))

    def _read_loop(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            return
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    raw = stream.readline()
                    if not raw:
                        self._fail_pending("language server closed stdout")
                        return
                    if raw in {b"\r\n", b"\n"}:
                        break
                    key, separator, value = raw.decode("ascii", errors="replace").partition(":")
                    if separator:
                        headers[key.strip().lower()] = value.strip()
                try:
                    length = int(headers.get("content-length", "0"))
                except ValueError:
                    continue
                if length <= 0:
                    continue
                body = stream.read(length)
                if len(body) != length:
                    self._fail_pending("language server returned a truncated frame")
                    return
                try:
                    message = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except Exception as exc:  # noqa: BLE001 - reader must wake all pending callers
            self._fail_pending(f"LSP reader failed: {exc}")

    def _dispatch(self, message: dict[str, Any]) -> None:
        response_id = message.get("id")
        if isinstance(response_id, int) and ("result" in message or "error" in message):
            with self._state_lock:
                pending = self._pending.pop(response_id, None)
            if pending is not None:
                pending.result = message.get("result")
                pending.error = message.get("error")
                pending.event.set()
            return
        method = str(message.get("method") or "")
        params = message.get("params")
        if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
            uri = str(params.get("uri") or "")
            diagnostics = params.get("diagnostics")
            if isinstance(diagnostics, list):
                self._diagnostics[uri] = [item for item in diagnostics if isinstance(item, dict)]
        self._notifications.append(message)
        # Servers occasionally send requests to the client. Respond explicitly
        # instead of leaving them hanging forever.
        if response_id is not None and method:
            self._send({"jsonrpc": "2.0", "id": response_id, "result": None})

    def _fail_pending(self, message: str) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = {"code": -32099, "message": message}
            item.event.set()

    def _stderr_loop(self) -> None:
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            self._stderr.append(raw.decode("utf-8", errors="replace").rstrip())

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(list(self._stderr)[-lines:])

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                self.request("shutdown", None, timeout=3.0)
                self.notify("exit", None)
            except LspError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
        self._fail_pending("language server stopped")
        self._process = None

    def describe(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "command": self.command,
            "root": str(self.root),
            "pid": self.pid,
            "alive": self.alive,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "opened_documents": len(self._opened),
            "diagnostic_documents": len(self._diagnostics),
            "capabilities": sorted(self.capabilities),
            "stderr_tail": self.stderr_tail(5),
        }


class LspPool:
    """One reusable language server per workspace/language."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._clients: dict[str, LspClient] = {}
        self._lock = threading.Lock()

    @staticmethod
    def for_context(ctx: ToolContext) -> "LspPool":
        current = ctx.extra.get(_POOL_KEY)
        if isinstance(current, LspPool) and current.root == ctx.project_root.resolve():
            return current
        pool = LspPool(ctx.project_root)
        ctx.extra[_POOL_KEY] = pool
        return pool

    def client_for(self, path: Path, *, language: str = "") -> LspClient:
        spec, command = detect_server(path, language=language)
        with self._lock:
            client = self._clients.get(spec.language)
            if client is not None and client.alive:
                return client
            if client is not None:
                client.stop()
            client = LspClient(command, root=self.root, language=spec.language)
            client.start()
            self._clients[spec.language] = client
            return client

    def clients(self) -> list[LspClient]:
        return list(self._clients.values())

    def stop(self, language: str = "") -> int:
        with self._lock:
            selected = [
                (name, client)
                for name, client in self._clients.items()
                if not language or name == language
            ]
            for name, client in selected:
                client.stop()
                self._clients.pop(name, None)
            return len(selected)


def _path(ctx: ToolContext, raw: str) -> Path:
    from unknown_agent.tools.builtin.fs_tools import resolve

    return resolve(ctx, raw)


def _location(value: Any, root: Path) -> Any:
    """Convert file URIs to project-relative paths recursively for model clarity."""

    if isinstance(value, list):
        return [_location(item, root) for item in value]
    if not isinstance(value, dict):
        return value
    out = {key: _location(item, root) for key, item in value.items()}
    uri = out.get("uri") or out.get("targetUri")
    if isinstance(uri, str) and uri.startswith("file:"):
        parsed = urlparse(uri)
        file_path = Path(unquote(parsed.path))
        try:
            out["path"] = str(file_path.relative_to(root))
        except ValueError:
            out["path"] = str(file_path)
    return out


def _render(value: Any, *, max_chars: int = 12000) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... [LSP result truncated]"


def _query(
    ctx: ToolContext,
    path: str,
    method: str,
    *,
    line: int = 1,
    column: int = 1,
    extra: dict[str, Any] | None = None,
) -> tuple[LspClient, Any]:
    target = _path(ctx, path)
    client = LspPool.for_context(ctx).client_for(target)
    params = client.position_params(target, line, column)
    if extra:
        params.update(extra)
    result = client.request(method, params)
    return client, _location(result, ctx.project_root.resolve())


class LspStatusTool(Tool):
    name = "lsp_status"
    description = "Show persistent language servers, capabilities, opened files and stderr health."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "code-intelligence")
    parameters = (ParameterSpec("path", "string", "Optionally start/check the server for this file.", default=""),)

    async def run(self, ctx: ToolContext, path: str = "") -> ToolResult:
        pool = LspPool.for_context(ctx)
        try:
            if path:
                pool.client_for(_path(ctx, path))
            clients = [client.describe() for client in pool.clients()]
            return ToolResult.success(_render(clients), servers=clients)
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspSymbolsTool(Tool):
    name = "lsp_symbols"
    description = "Ask the compiler/indexer for document symbols, including types and ranges."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "code-intelligence", "primary")
    parameters = (ParameterSpec("path", "string", "Source file.", required=True),)

    async def run(self, ctx: ToolContext, path: str = "") -> ToolResult:
        try:
            target = _path(ctx, path)
            client = LspPool.for_context(ctx).client_for(target)
            uri = client.open_document(target)
            result = _location(
                client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}),
                ctx.project_root.resolve(),
            )
            return ToolResult.success(_render(result), symbols=result)
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspDefinitionTool(Tool):
    name = "lsp_definition"
    description = "Resolve the definition of the symbol at a 1-based line and column across the workspace."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "code-intelligence", "primary")
    parameters = (
        ParameterSpec("path", "string", "Source file.", required=True),
        ParameterSpec("line", "integer", "1-based line.", required=True, minimum=1),
        ParameterSpec("column", "integer", "1-based character column.", required=True, minimum=1),
    )

    async def run(self, ctx: ToolContext, path: str = "", line: int = 1, column: int = 1) -> ToolResult:
        try:
            _client, result = _query(ctx, path, "textDocument/definition", line=line, column=column)
            return ToolResult.success(_render(result), definitions=result)
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspReferencesTool(Tool):
    name = "lsp_references"
    description = "Find project-wide references to a symbol using compiler semantics, not text matching."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "code-intelligence", "primary")
    parameters = (
        *LspDefinitionTool.parameters,
        ParameterSpec("include_declaration", "boolean", "Include the definition itself.", default=True),
    )

    async def run(
        self,
        ctx: ToolContext,
        path: str = "",
        line: int = 1,
        column: int = 1,
        include_declaration: bool = True,
    ) -> ToolResult:
        try:
            _client, result = _query(
                ctx,
                path,
                "textDocument/references",
                line=line,
                column=column,
                extra={"context": {"includeDeclaration": include_declaration}},
            )
            return ToolResult.success(_render(result), references=result)
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspHoverTool(Tool):
    name = "lsp_hover"
    description = "Return inferred type, signature and documentation for the symbol under the cursor."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "code-intelligence")
    parameters = LspDefinitionTool.parameters

    async def run(self, ctx: ToolContext, path: str = "", line: int = 1, column: int = 1) -> ToolResult:
        try:
            _client, result = _query(ctx, path, "textDocument/hover", line=line, column=column)
            return ToolResult.success(_render(result), hover=result)
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspDiagnosticsTool(Tool):
    name = "lsp_diagnostics"
    description = "Open a file in its language server and collect compiler diagnostics asynchronously."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "diagnostics", "primary")
    parameters = (
        ParameterSpec("path", "string", "Source file.", required=True),
        ParameterSpec("settle_ms", "integer", "Wait for diagnostics to arrive.", default=700, minimum=0, maximum=10000),
    )

    async def run(self, ctx: ToolContext, path: str = "", settle_ms: int = 700) -> ToolResult:
        try:
            target = _path(ctx, path)
            client = LspPool.for_context(ctx).client_for(target)
            diagnostics = client.diagnostics(target, settle_ms=settle_ms)
            cleaned = _location(diagnostics, ctx.project_root.resolve())
            errors = sum(1 for item in diagnostics if int(item.get("severity", 0) or 0) == 1)
            warnings = sum(1 for item in diagnostics if int(item.get("severity", 0) or 0) == 2)
            return ToolResult.success(
                f"{path}: {errors} error(s), {warnings} warning(s), {len(diagnostics)} diagnostic(s)\n"
                + _render(cleaned),
                diagnostics=cleaned,
                errors=errors,
                warnings=warnings,
            )
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspRenamePreviewTool(Tool):
    name = "lsp_rename_preview"
    description = "Ask the language server for a project-wide rename edit without writing any file."
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("lsp", "refactor")
    parameters = (
        *LspDefinitionTool.parameters,
        ParameterSpec("new_name", "string", "Replacement identifier.", required=True),
    )

    async def run(
        self,
        ctx: ToolContext,
        path: str = "",
        line: int = 1,
        column: int = 1,
        new_name: str = "",
    ) -> ToolResult:
        try:
            _client, result = _query(
                ctx,
                path,
                "textDocument/rename",
                line=line,
                column=column,
                extra={"newName": new_name},
            )
            return ToolResult.success(
                "rename preview only — no files were modified\n" + _render(result), workspace_edit=result
            )
        except (LspError, OSError) as exc:
            return ToolResult.failure(str(exc))


class LspStopTool(Tool):
    name = "lsp_stop"
    description = "Gracefully stop persistent language servers for this session."
    category = "analysis"
    risk = RiskLevel.EXECUTE
    tags = ("lsp",)
    parameters = (ParameterSpec("language", "string", "Stop only this language; blank stops all.", default=""),)

    async def run(self, ctx: ToolContext, language: str = "") -> ToolResult:
        stopped = LspPool.for_context(ctx).stop(language)
        return ToolResult.success(f"stopped {stopped} language server(s)", stopped=stopped)


def build_tools() -> list[Tool]:
    return [
        LspStatusTool(),
        LspSymbolsTool(),
        LspDefinitionTool(),
        LspReferencesTool(),
        LspHoverTool(),
        LspDiagnosticsTool(),
        LspRenamePreviewTool(),
        LspStopTool(),
    ]
