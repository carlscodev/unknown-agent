"""Advanced hyperagent tools: architecture, health, dependencies, scaffolding,
planning helpers, MCP discovery and skill distillation.

These are the tools that separate a coding *hyperagent* from a file editor.
They share three design rules:

1. **Deterministic first.**  Everything here computes facts (imports, layouts,
   lockfiles, journals) without a model call, so results are cheap, cacheable
   and honest.  Model judgement belongs in the loop, not inside a tool.
2. **Structured out.**  Each result carries machine-usable ``data`` next to the
   human ``output``, because these tools feed planners and CI as much as chat.
3. **Read-mostly.**  Only ``app_scaffold`` and ``skill_distill`` write, and both
   say exactly what they created.
"""

from __future__ import annotations

import ast
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from unknown_agent.core.models import RiskLevel, ToolResult
from unknown_agent.host.process import run_process
from unknown_agent.tools.base import ParameterSpec, Tool, ToolContext

__all__ = ["build_tools"]

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build", ".next",
    ".nuxt", "target", ".tox", ".unknown", "coverage",
}


def _iter_source(root: Path, suffixes: tuple[str, ...], *, limit: int = 4000) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack and len(found) < limit:
        current = stack.pop()
        try:
            for entry in sorted(current.iterdir()):
                if entry.name.startswith(".") and entry.name not in {".github"}:
                    continue
                if entry.is_dir():
                    if entry.name not in _SKIP_DIRS:
                        stack.append(entry)
                elif entry.suffix in suffixes:
                    found.append(entry)
        except OSError:
            continue
    return found


# --------------------------------------------------------------------------- #
# architecture_map
# --------------------------------------------------------------------------- #
class ArchitectureMapTool(Tool):
    name = "architecture_map"
    description = (
        "Map the project's internal architecture: modules, their import graph, "
        "fan-in/fan-out hotspots and layering violations (cycles). Python-deep; "
        "other languages get a directory-level structure map."
    )
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("architecture", "analysis", "advanced")
    timeout_s = 120.0
    parameters = (
        ParameterSpec("top", type="integer", description="How many hotspots to report.", default=10),
    )

    async def run(self, ctx: ToolContext, top: int = 10) -> ToolResult:
        root = ctx.project_root
        py_files = _iter_source(root, (".py",))
        if not py_files:
            structure = self._directory_map(root)
            return ToolResult.success(structure, python_modules=0)

        # module name <- path (best-effort: src-layout aware)
        modules: dict[str, Path] = {}
        for path in py_files:
            rel = path.relative_to(root)
            parts = list(rel.parts)
            if parts and parts[0] in {"src", "lib"}:
                parts = parts[1:]
            dotted = ".".join(parts)[: -len(".py")] if parts else ""
            if dotted.endswith(".__init__"):
                dotted = dotted[: -len(".__init__")]
            if dotted:
                modules[dotted] = path

        imports: dict[str, set[str]] = defaultdict(set)
        parse_errors = 0
        internal_roots = {name.split(".")[0] for name in modules}
        for dotted, path in modules.items():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                parse_errors += 1
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in internal_roots:
                            imports[dotted].add(alias.name)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    if node.module.split(".")[0] in internal_roots:
                        for alias in node.names:
                            # `from pkg import mod` names the submodule pkg.mod when
                            # one exists; otherwise credit the package itself.
                            candidate = f"{node.module}.{alias.name}"
                            imports[dotted].add(candidate if candidate in modules else node.module)

        fan_in: dict[str, int] = defaultdict(int)
        for source, targets in imports.items():
            for target in targets:
                # credit the closest known module prefix
                candidate = target
                while candidate and candidate not in modules:
                    candidate = candidate.rpartition(".")[0]
                if candidate and candidate != source:
                    fan_in[candidate] += 1

        cycles = self._find_cycles(imports, modules)
        hot_in = sorted(fan_in.items(), key=lambda item: -item[1])[:top]
        hot_out = sorted(((mod, len(targets)) for mod, targets in imports.items()), key=lambda item: -item[1])[:top]

        lines = [f"{len(modules)} python modules, {sum(len(t) for t in imports.values())} internal imports"]
        if parse_errors:
            lines.append(f"{parse_errors} file(s) did not parse")
        lines.append("\nmost depended-on (fan-in):")
        lines += [f"  {count:3d} <- {mod}" for mod, count in hot_in]
        lines.append("\nmost dependent (fan-out):")
        lines += [f"  {count:3d} -> {mod}" for mod, count in hot_out]
        if cycles:
            lines.append("\nimport cycles (layering violations):")
            lines += [f"  {' -> '.join(cycle)}" for cycle in cycles[:8]]
        else:
            lines.append("\nno import cycles detected")
        return ToolResult.success(
            "\n".join(lines),
            modules=len(modules),
            fan_in={mod: count for mod, count in hot_in},
            fan_out={mod: count for mod, count in hot_out},
            cycles=[list(cycle) for cycle in cycles[:20]],
        )

    @staticmethod
    def _find_cycles(imports: dict[str, set[str]], modules: dict[str, Path]) -> list[tuple[str, ...]]:
        graph: dict[str, set[str]] = defaultdict(set)
        for source, targets in imports.items():
            for target in targets:
                candidate = target
                while candidate and candidate not in modules:
                    candidate = candidate.rpartition(".")[0]
                if candidate and candidate != source:
                    graph[source].add(candidate)
        cycles: list[tuple[str, ...]] = []
        seen_cycles: set[frozenset[str]] = set()
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = defaultdict(int)
        stack: list[str] = []

        def visit(node: str) -> None:
            color[node] = GRAY
            stack.append(node)
            for neighbour in graph.get(node, ()):
                if color[neighbour] == GRAY:
                    start = stack.index(neighbour)
                    cycle = tuple(stack[start:] + [neighbour])
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
                elif color[neighbour] == WHITE:
                    visit(neighbour)
            stack.pop()
            color[node] = BLACK

        for node in sorted(graph):
            if color[node] == WHITE:
                visit(node)
        return cycles

    @staticmethod
    def _directory_map(root: Path) -> str:
        lines = ["non-Python project: directory structure map"]
        for entry in sorted(root.iterdir()):
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                children = sum(1 for _ in entry.rglob("*") if _.is_file())
                lines.append(f"  {entry.name}/  ({children} files)")
            else:
                lines.append(f"  {entry.name}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# dependency_audit
# --------------------------------------------------------------------------- #
class DependencyAuditTool(Tool):
    name = "dependency_audit"
    description = (
        "Audit declared dependencies: what is declared where (pyproject/requirements/"
        "package.json), duplicates across files, unpinned specs, and - for Python - "
        "declared-but-unimported and imported-but-undeclared packages."
    )
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("dependencies", "analysis", "advanced")
    timeout_s = 120.0
    parameters = ()

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        root = ctx.project_root
        declared: dict[str, list[str]] = {}
        problems: list[str] = []

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            import tomllib

            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                problems.append(f"pyproject.toml does not parse: {exc}")
                data = {}
            deps = list(data.get("project", {}).get("dependencies", []) or [])
            for group, extra in (data.get("project", {}).get("optional-dependencies", {}) or {}).items():
                deps.extend(f"{item}  [{group}]" for item in extra)
            if deps:
                declared["pyproject.toml"] = deps

        for name in ("requirements.txt", "requirements-dev.txt", "requirements.in"):
            req = root / name
            if req.is_file():
                entries = [
                    line.strip()
                    for line in req.read_text(encoding="utf-8", errors="replace").splitlines()
                    if line.strip() and not line.strip().startswith(("#", "-r", "--"))
                ]
                if entries:
                    declared[name] = entries

        package_json = root / "package.json"
        if package_json.is_file():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
                entries = [f"{pkg}@{version}" for pkg, version in (data.get("dependencies") or {}).items()]
                entries += [f"{pkg}@{version}  [dev]" for pkg, version in (data.get("devDependencies") or {}).items()]
                if entries:
                    declared["package.json"] = entries
            except json.JSONDecodeError as exc:
                problems.append(f"package.json does not parse: {exc}")

        # Python cross-check: imports vs declarations
        optional_names = {
            self._normalise(spec)
            for specs in declared.values()
            for spec in specs
            if spec.rstrip().endswith("]")  # "[group]"-tagged optional dependencies
        }
        py_names = {self._normalise(spec) for specs in declared.values() for spec in specs}
        undeclared: list[str] = []
        unused: list[str] = []
        py_files = _iter_source(root, (".py",), limit=1500)
        if py_files and (pyproject.is_file() or any("requirements" in key for key in declared)):
            stdlib = self._stdlib_names()
            local_roots = {path.stem for path in root.iterdir() if path.is_dir()} | {"src", "tests", "test"}
            local_roots |= {p.stem for p in (root / "src").iterdir() if (root / "src").is_dir() and p.is_dir()} if (root / "src").is_dir() else set()
            imported: set[str] = set()
            for path in py_files:
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        imported.add(node.module.split(".")[0])
            third_party = {name for name in imported if name not in stdlib and name not in local_roots}
            declared_normalised = {self._import_name(name) for name in py_names}
            undeclared = sorted(name for name in third_party if name.lower() not in declared_normalised)[:20]
            unused = sorted(
                spec for spec in py_names
                if self._import_name(spec) not in {name.lower() for name in third_party}
                and spec not in optional_names  # optional groups are situational by design
            )[:20]

        unpinned = [
            f"{source}: {spec}"
            for source, specs in declared.items()
            for spec in specs
            if source != "package.json" and not re.search(r"[<>=~!]|@", spec.split("[")[0])
        ]

        lines: list[str] = []
        for source, specs in declared.items():
            lines.append(f"{source}: {len(specs)} declared")
        if not declared:
            lines.append("no dependency manifests found")
        if unpinned:
            lines.append(f"\nunpinned ({len(unpinned)}):")
            lines += [f"  {item}" for item in unpinned[:15]]
        if undeclared:
            lines.append(f"\nimported but not declared ({len(undeclared)}):")
            lines += [f"  {name}" for name in undeclared]
        if unused:
            lines.append(f"\ndeclared but never imported ({len(unused)}) - may be runtime/plugins:")
            lines += [f"  {name}" for name in unused]
        lines += [f"\n{problem}" for problem in problems]
        return ToolResult.success(
            "\n".join(lines),
            declared={key: len(value) for key, value in declared.items()},
            unpinned=unpinned[:50],
            undeclared=undeclared,
            possibly_unused=unused,
            problems=problems,
        )

    @staticmethod
    def _normalise(spec: str) -> str:
        return re.split(r"[<>=~!\[; @]", spec.strip(), maxsplit=1)[0].strip().lower()

    @staticmethod
    def _import_name(spec: str) -> str:
        name = re.split(r"[<>=~!\[; @]", spec.strip(), maxsplit=1)[0].strip().lower()
        aliases = {
            "pyyaml": "yaml", "pillow": "pil", "beautifulsoup4": "bs4", "scikit-learn": "sklearn",
            "python-dotenv": "dotenv", "typing-extensions": "typing_extensions", "tomli-w": "tomli_w",
            "pydantic-settings": "pydantic_settings", "pytest-asyncio": "pytest_asyncio",
        }
        return aliases.get(name, name.replace("-", "_"))

    @staticmethod
    def _stdlib_names() -> set[str]:
        import sys

        return set(sys.stdlib_module_names)


# --------------------------------------------------------------------------- #
# repo_health
# --------------------------------------------------------------------------- #
class RepoHealthTool(Tool):
    name = "repo_health"
    description = (
        "Score repository health across hygiene axes: tests present, CI config, "
        "README quality, license, gitignore, lockfiles, TODO/FIXME debt, large files, "
        "and secrets-shaped strings in tracked files."
    )
    category = "analysis"
    risk = RiskLevel.READ
    tags = ("health", "analysis", "advanced")
    timeout_s = 120.0
    parameters = ()

    _SECRET_PATTERN = re.compile(
        r"(?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"][A-Za-z0-9+/_\-]{16,}['\"]", re.IGNORECASE
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        root = ctx.project_root
        checks: list[tuple[str, bool, str]] = []

        tests_dir = any((root / name).is_dir() for name in ("tests", "test")) or bool(list(root.glob("**/test_*.py"))[:1])
        checks.append(("tests", tests_dir, "a tests/ directory or test_*.py files exist"))
        ci = any((root / path).exists() for path in (".github/workflows", ".gitlab-ci.yml", ".circleci", "Jenkinsfile"))
        checks.append(("ci", ci, "CI configuration present"))
        readme = next((p for p in root.glob("README*") if p.is_file()), None)
        readme_ok = bool(readme and len(readme.read_text(encoding="utf-8", errors="replace")) > 300)
        checks.append(("readme", readme_ok, "README exists and is more than a stub"))
        checks.append(("license", any((root / name).is_file() for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING")), "a license file exists"))
        checks.append(("gitignore", (root / ".gitignore").is_file(), ".gitignore exists"))
        lock = any((root / name).is_file() for name in ("uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "requirements.txt"))
        checks.append(("lockfile", lock, "dependency lock/pin file exists"))

        todo_count = 0
        secret_hits: list[str] = []
        large_files: list[str] = []
        for path in _iter_source(root, (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".toml", ".yaml", ".yml", ".env"), limit=2000):
            try:
                size = path.stat().st_size
                if size > 1_000_000:
                    large_files.append(f"{path.relative_to(root)} ({size // 1024} KiB)")
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            todo_count += len(re.findall(r"\b(?:TODO|FIXME|HACK|XXX)\b", text))
            if path.suffix != ".env" and self._SECRET_PATTERN.search(text):
                secret_hits.append(str(path.relative_to(root)))

        passed = sum(1 for _, ok, _ in checks if ok)
        score = round(100 * passed / len(checks))
        lines = [f"health score: {score}/100 ({passed}/{len(checks)} hygiene checks pass)", ""]
        for name, ok, description in checks:
            lines.append(f"  {'✓' if ok else '✗'} {name}: {description}")
        lines.append(f"\nTODO/FIXME markers: {todo_count}")
        if large_files:
            lines.append(f"large tracked files ({len(large_files)}):")
            lines += [f"  {item}" for item in large_files[:10]]
        if secret_hits:
            lines.append(f"secrets-shaped strings in ({len(secret_hits)}) file(s) - review them:")
            lines += [f"  {item}" for item in secret_hits[:10]]
        return ToolResult.success(
            "\n".join(lines),
            score=score,
            checks={name: ok for name, ok, _ in checks},
            todo_count=todo_count,
            large_files=large_files[:50],
            secret_suspects=secret_hits[:50],
        )


# --------------------------------------------------------------------------- #
# app_scaffold
# --------------------------------------------------------------------------- #
_SCAFFOLDS: dict[str, dict[str, str]] = {
    "python-cli": {
        "pyproject.toml": """[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = ""\nrequires-python = ">=3.11"\ndependencies = ["typer>=0.12", "rich>=13"]\n\n[project.scripts]\n{name} = "{module}.cli:main"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n""",
        "src/{module}/__init__.py": "",
        "src/{module}/cli.py": """\"\"\"{name} command-line interface.\"\"\"\n\nimport typer\n\napp = typer.Typer(name="{name}")\n\n\n@app.command()\ndef hello(name: str = "world") -> None:\n    \"\"\"Say hello.\"\"\"\n    typer.echo(f"hello, {{name}}!")\n\n\ndef main() -> None:\n    app()\n\n\nif __name__ == "__main__":\n    main()\n""",
        "tests/test_cli.py": """from typer.testing import CliRunner\n\nfrom {module}.cli import app\n\n\ndef test_hello():\n    result = CliRunner().invoke(app, ["--help"])\n    assert result.exit_code == 0\n""",
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\ndist/\n.pytest_cache/\n",
    },
    "python-api": {
        "pyproject.toml": """[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = ""\nrequires-python = ">=3.11"\ndependencies = ["fastapi>=0.115", "uvicorn>=0.32", "pydantic>=2.8"]\n\n[project.optional-dependencies]\ndev = ["pytest>=8", "httpx>=0.27"]\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n""",
        "src/{module}/__init__.py": "",
        "src/{module}/app.py": """\"\"\"{name} API.\"\"\"\n\nfrom fastapi import FastAPI\n\napp = FastAPI(title="{name}")\n\n\n@app.get("/health")\ndef health() -> dict:\n    return {{"status": "ok"}}\n""",
        "tests/test_app.py": """from fastapi.testclient import TestClient\n\nfrom {module}.app import app\n\n\ndef test_health():\n    client = TestClient(app)\n    response = client.get("/health")\n    assert response.status_code == 200\n    assert response.json() == {{"status": "ok"}}\n""",
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\ndist/\n.pytest_cache/\n",
    },
    "python-lib": {
        "pyproject.toml": """[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n[project]\nname = "{name}"\nversion = "0.1.0"\ndescription = ""\nrequires-python = ">=3.11"\ndependencies = []\n\n[project.optional-dependencies]\ndev = ["pytest>=8"]\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n""",
        "src/{module}/__init__.py": "__version__ = \"0.1.0\"\n",
        "src/{module}/core.py": "\"\"\"Core functionality of {name}.\"\"\"\n\n\ndef ping() -> str:\n    return \"pong\"\n",
        "tests/test_core.py": "from {module}.core import ping\n\n\ndef test_ping():\n    assert ping() == \"pong\"\n",
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\ndist/\n.pytest_cache/\n",
    },
}


class AppScaffoldTool(Tool):
    name = "app_scaffold"
    description = (
        "Scaffold a new application skeleton with tests, packaging and hygiene files. "
        "Templates: python-cli, python-api, python-lib. Refuses to overwrite existing files."
    )
    category = "scaffold"
    risk = RiskLevel.WRITE
    tags = ("scaffold", "advanced")
    timeout_s = 60.0
    parameters = (
        ParameterSpec("template", description="python-cli | python-api | python-lib", required=True, enum=tuple(_SCAFFOLDS)),
        ParameterSpec("name", description="Project name (kebab-case).", required=True),
        ParameterSpec("directory", description="Target directory (relative to project root; default: ./<name>)."),
    )
    examples = ('app_scaffold(template="python-api", name="orders-service")',)

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        template_key: str = kwargs["template"]
        raw_name: str = kwargs["name"]
        template = _SCAFFOLDS.get(template_key)
        if template is None:
            return ToolResult.failure(f"unknown template {template_key!r}; templates: {', '.join(_SCAFFOLDS)}")
        name = re.sub(r"[^a-z0-9-]", "-", raw_name.strip().lower()).strip("-")
        if not name:
            return ToolResult.failure(f"cannot derive a project name from {raw_name!r}")
        module = name.replace("-", "_")
        base = ctx.project_root / (kwargs.get("directory") or name)
        collisions = []
        rendered: dict[Path, str] = {}
        for rel_template, body in template.items():
            rel = rel_template.format(name=name, module=module)
            target = base / rel
            if target.exists():
                collisions.append(str(target))
            rendered[target] = body.format(name=name, module=module)
        if collisions:
            return ToolResult.failure(
                "refusing to overwrite existing files:\n" + "\n".join(f"  {item}" for item in collisions)
            )
        for target, body in rendered.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        created = sorted(str(path.relative_to(ctx.project_root)) for path in rendered)
        return ToolResult.success(
            f"scaffolded {template_key} project '{name}' ({len(created)} files):\n" + "\n".join(f"  {item}" for item in created),
            template=template_key,
            name=name,
            files=created,
        )


# --------------------------------------------------------------------------- #
# doc_query
# --------------------------------------------------------------------------- #
class DocQueryTool(Tool):
    name = "doc_query"
    description = (
        "Search the project's own documentation (README, docs/, *.md, docstrings in "
        "__init__ files) for a topic and return the most relevant passages."
    )
    category = "docs"
    risk = RiskLevel.READ
    tags = ("docs", "advanced")
    timeout_s = 60.0
    parameters = (
        ParameterSpec("query", description="Topic to look up.", required=True),
        ParameterSpec("max_passages", type="integer", description="How many passages.", default=5),
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        query: str = kwargs["query"]
        limit = int(kwargs.get("max_passages") or 5)
        root = ctx.project_root
        terms = [term.lower() for term in re.findall(r"\w{3,}", query)]
        if not terms:
            return ToolResult.failure("query too short")
        candidates = list(root.glob("*.md"))
        if (root / "docs").is_dir():
            candidates += list((root / "docs").rglob("*.md"))
        scored: list[tuple[float, str, str]] = []
        for path in candidates[:200]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for block in re.split(r"\n{2,}", text):
                lowered = block.lower()
                score = sum(lowered.count(term) for term in terms)
                if score > 0:
                    scored.append((score / (1 + len(block) / 500), str(path.relative_to(root)), block.strip()))
        scored.sort(key=lambda item: -item[0])
        if not scored:
            return ToolResult.success(f"no documentation passages mention: {query}", passages=[])
        passages = scored[:limit]
        rendered = "\n\n---\n\n".join(f"[{source}]\n{block[:900]}" for _score, source, block in passages)
        return ToolResult.success(rendered, passages=[{"source": src, "text": block[:900]} for _s, src, block in passages])


# --------------------------------------------------------------------------- #
# plan_review
# --------------------------------------------------------------------------- #
class PlanReviewTool(Tool):
    name = "plan_review"
    description = (
        "Deterministically review a plan text for classic failure modes: no verification "
        "step, no rollback thought, vague steps ('improve', 'handle'), missing dependency "
        "ordering, and steps that touch everything at once."
    )
    category = "planning"
    risk = RiskLevel.READ
    tags = ("planning", "advanced")
    timeout_s = 30.0
    parameters = (
        ParameterSpec("plan", description="The plan text (one step per line or markdown list).", required=True),
    )

    _VAGUE = re.compile(r"\b(improve|handle|deal with|make better|optimi[sz]e|clean\s*up|various|etc)\b", re.IGNORECASE)
    _VERIFY = re.compile(r"\b(test|verify|assert|validate|gate|check|smoke)\b", re.IGNORECASE)
    _ROLLBACK = re.compile(r"\b(rollback|revert|backup|migration down|undo)\b", re.IGNORECASE)

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        plan: str = kwargs["plan"]
        steps = [line.strip("-*0123456789. \t") for line in plan.splitlines() if line.strip() and len(line.strip()) > 3]
        findings: list[str] = []
        if len(steps) < 2:
            findings.append("the plan has fewer than 2 concrete steps - is it a plan or a wish?")
        vague = [step for step in steps if self._VAGUE.search(step)]
        if vague:
            findings.append(f"{len(vague)} vague step(s) (e.g. {vague[0][:60]!r}) - vague steps hide unbounded work")
        if not self._VERIFY.search(plan):
            findings.append("no verification step - nothing in this plan would notice failure")
        touches_schema = re.search(r"\b(migration|schema|database|drop|alter)\b", plan, re.IGNORECASE)
        if touches_schema and not self._ROLLBACK.search(plan):
            findings.append("touches schema/data but has no rollback/backup step")
        huge = [step for step in steps if len(step) > 200]
        if huge:
            findings.append(f"{len(huge)} step(s) are paragraphs - split them until each is one action")
        verdict = "sound" if not findings else ("needs work" if len(findings) < 3 else "reject and re-plan")
        lines = [f"steps: {len(steps)}", f"verdict: {verdict}"]
        lines += [f"  - {finding}" for finding in findings] or ["  no structural problems found"]
        return ToolResult.success("\n".join(lines), verdict=verdict, findings=findings, steps=len(steps))


# --------------------------------------------------------------------------- #
# mcp_discover
# --------------------------------------------------------------------------- #
class McpDiscoverTool(Tool):
    name = "mcp_discover"
    description = (
        "Discover MCP capability available to this session: configured servers, their "
        "status, the tools currently exposed as mcp.<server>.<tool>, and catalog "
        "suggestions that are not yet configured."
    )
    category = "mcp"
    risk = RiskLevel.READ
    tags = ("mcp", "advanced")
    timeout_s = 60.0
    parameters = ()

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        from unknown_agent.mcp.catalog import MCP_CATALOG

        manager = (ctx.extra or {}).get("mcp_manager")
        configured = list(ctx.settings.mcp.servers)
        lines: list[str] = []
        servers: list[dict[str, Any]] = []
        if manager is not None:
            described = manager.describe()
            for item in described["servers"]:
                servers.append(item)
                status = "connected" if item["connected"] else ("error: " + item["error"] if item["error"] else "configured")
                lines.append(f"  {item['name']} [{item['transport']}, trust {item['trust']}] - {status}")
        elif configured:
            lines.append(f"  {len(configured)} server(s) configured but not active in this session: {', '.join(configured)}")
        else:
            lines.append("  none configured")
        exposed = [name for name in (ctx.extra or {}).get("registered_tools", []) if name.startswith("mcp.")]
        suggestions = [entry.key for entry in MCP_CATALOG if entry.recommended and entry.key not in configured]
        text = "MCP servers:\n" + "\n".join(lines)
        if exposed:
            text += "\n\nexposed tools:\n" + "\n".join(f"  {name}" for name in exposed)
        if suggestions:
            text += "\n\nrecommended but not configured: " + ", ".join(suggestions) + "\n(add with `ua mcp add <key>`)"
        return ToolResult.success(text, servers=servers, exposed=exposed, suggestions=suggestions)


# --------------------------------------------------------------------------- #
# skill_distill
# --------------------------------------------------------------------------- #
class SkillDistillTool(Tool):
    name = "skill_distill"
    description = (
        "Distill the current run's experience into a draft skill file: what worked, the "
        "commands used, the gates that mattered. Writes .unknown/skills/<id>.draft.toml "
        "for human review - a draft never activates itself."
    )
    category = "skills"
    risk = RiskLevel.WRITE
    tags = ("skills", "advanced")
    timeout_s = 30.0
    parameters = (
        ParameterSpec("skill_id", description="Id for the draft (kebab-case).", required=True),
        ParameterSpec("summary", description="One-line description of the expertise.", required=True),
        ParameterSpec("instructions", description="The distilled guidance (imperative voice).", required=True),
        ParameterSpec("category", description="craft | repair | verification | delivery", default="craft"),
        ParameterSpec("gates", type="array", description="Gates this skill considers part of done."),
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        skill_id = re.sub(r"[^a-z0-9-]", "-", str(kwargs["skill_id"]).lower()).strip("-")
        if not skill_id:
            return ToolResult.failure("cannot derive a skill id")
        target_dir = ctx.paths.project_skills_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{skill_id}.draft.toml"
        gates = [str(g) for g in (kwargs.get("gates") or [])]
        body_lines = [
            "# DRAFT skill distilled by the agent - review before renaming to .toml",
            f"# distilled: {time.strftime('%Y-%m-%d %H:%M')}  run: {ctx.run_id or 'unknown'}",
            f'id = "{skill_id}"',
            f'label = "{skill_id.replace("-", " ").title()}"',
            f'summary = {json.dumps(str(kwargs["summary"]))}',
            f'category = {json.dumps(str(kwargs.get("category") or "craft"))}',
            f"gates = {json.dumps(gates)}",
            "",
            "instructions = '''",
            str(kwargs["instructions"]).strip(),
            "'''",
        ]
        target.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
        return ToolResult.success(
            f"draft skill written to {target}\nIt is inert until a human renames it to {skill_id}.toml "
            "(the .draft suffix is skipped by the loader).",
            path=str(target),
            skill_id=skill_id,
        )


# --------------------------------------------------------------------------- #
# ui_validate — deterministic UI hygiene without a model
# --------------------------------------------------------------------------- #
class UiValidateTool(Tool):
    name = "ui_validate"
    description = (
        "Deterministic UI hygiene checks on rendered HTML (via the managed browser): "
        "missing alt text, empty buttons/links, missing form labels, heading order, "
        "viewport meta, and console errors. The scripted half of UI validation - "
        "use screenshot_analyze for the visual-judgement half."
    )
    category = "browser"
    risk = RiskLevel.NETWORK
    tags = ("browser", "ui", "advanced")
    requires_extra = "browser"
    concurrency_safe = False
    timeout_s = 120.0
    parameters = (
        ParameterSpec("url", description="URL to open (relative uses [browser] base_url).", required=True),
    )

    _CHECK_SCRIPT = """
() => {
  const problems = [];
  document.querySelectorAll('img:not([alt])').forEach(el => problems.push('img without alt: ' + (el.src || '').slice(0, 80)));
  document.querySelectorAll('button, a').forEach(el => {
    const text = (el.innerText || '').trim();
    const label = el.getAttribute('aria-label');
    if (!text && !label && !el.querySelector('img[alt]')) problems.push('empty ' + el.tagName.toLowerCase());
  });
  document.querySelectorAll('input:not([type=hidden]):not([type=submit])').forEach(el => {
    const id = el.id;
    const labelled = (id && document.querySelector('label[for="' + id + '"]')) || el.getAttribute('aria-label') || el.closest('label');
    if (!labelled) problems.push('unlabelled input: ' + (el.name || el.type || '?'));
  });
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(h => +h.tagName[1]);
  for (let i = 1; i < headings.length; i++) {
    if (headings[i] - headings[i-1] > 1) { problems.push('heading level jump: h' + headings[i-1] + ' -> h' + headings[i]); break; }
  }
  if (!document.querySelector('meta[name=viewport]')) problems.push('no viewport meta tag');
  if (document.querySelectorAll('h1').length === 0) problems.push('no h1 on the page');
  return problems.slice(0, 40);
}
"""

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if ctx.browser is None:
            return ToolResult.failure("browser tools need Playwright: `ua doctor --install-browsers`")
        url: str = kwargs["url"]
        try:
            session = await ctx.browser.session()
            await session.goto(url)
            problems: list[str] = await session.page.evaluate(self._CHECK_SCRIPT)
            console_errors = list(getattr(session, "console_errors", []) or [])
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"ui validation failed: {exc}")
        all_problems = problems + [f"console error: {err}" for err in console_errors[:10]]
        if not all_problems:
            return ToolResult.success(f"{url}: no UI hygiene problems detected", problems=[])
        return ToolResult(
            ok=True,  # detection succeeded; the findings are the payload
            output=f"{url}: {len(all_problems)} problem(s)\n" + "\n".join(f"  - {item}" for item in all_problems),
            data={"problems": all_problems},
        )


def build_tools() -> list[Tool]:
    return [
        ArchitectureMapTool(),
        DependencyAuditTool(),
        RepoHealthTool(),
        AppScaffoldTool(),
        DocQueryTool(),
        PlanReviewTool(),
        McpDiscoverTool(),
        SkillDistillTool(),
        UiValidateTool(),
    ]
