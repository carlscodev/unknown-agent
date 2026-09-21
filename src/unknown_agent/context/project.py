"""Project intelligence: understand a repository before touching it.

Everything here is deterministic and offline - no model calls.  The result,
:class:`ProjectContext`, is injected into every prompt, used by the planner, and
printed by ``ua inspect``.  Detection covers languages, frameworks, package
manager, test/lint/build commands, entry points, layout, conventions and risks,
so the agent starts from facts instead of guesses.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unknown_agent.config.models import Settings
from unknown_agent.host.info import detect_host
from unknown_agent.observability.logging import get_logger

log = get_logger("context.project")

LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".tsx"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java", ".kt", ".kts"),
    "php": (".php",),
    "ruby": (".rb",),
    "csharp": (".cs",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".hpp", ".cc"),
    "swift": (".swift",),
    "sql": (".sql",),
    "shell": (".sh", ".bash", ".zsh"),
    "terraform": (".tf",),
}

FRAMEWORK_SIGNATURES: dict[str, tuple[str, ...]] = {
    "django": ("django",),
    "fastapi": ("fastapi",),
    "flask": ("flask",),
    "starlette": ("starlette",),
    "sqlalchemy": ("sqlalchemy",),
    "pydantic": ("pydantic",),
    "pytest": ("pytest",),
    "celery": ("celery",),
    "next.js": ("next",),
    "react": ("react",),
    "vue": ("vue",),
    "svelte": ("svelte",),
    "nuxt": ("nuxt",),
    "astro": ("astro",),
    "remix": ("@remix-run",),
    "express": ("express",),
    "nestjs": ("@nestjs",),
    "fastify": ("fastify",),
    "prisma": ("prisma",),
    "tailwind": ("tailwindcss",),
    "vite": ("vite",),
    "jest": ("jest",),
    "vitest": ("vitest",),
    "playwright": ("playwright", "@playwright/test"),
    "cypress": ("cypress",),
    "laravel": ("laravel/framework",),
    "rails": ("rails",),
    "spring": ("spring-boot", "org.springframework"),
    "gin": ("github.com/gin-gonic/gin",),
    "actix": ("actix-web",),
    "axum": ("axum",),
}

PACKAGE_MANAGER_MARKERS: list[tuple[str, str]] = [
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("Pipfile.lock", "pipenv"),
    ("requirements.txt", "pip"),
    ("pyproject.toml", "pip"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("package-lock.json", "npm"),
    ("composer.lock", "composer"),
    ("go.sum", "go"),
    ("Cargo.lock", "cargo"),
    ("Gemfile.lock", "bundler"),
]

ENTRY_POINT_CANDIDATES = (
    "main.py",
    "app.py",
    "manage.py",
    "server.py",
    "wsgi.py",
    "asgi.py",
    "src/main.py",
    "src/app.py",
    "src/index.ts",
    "src/index.js",
    "src/main.ts",
    "index.js",
    "server.js",
    "server.ts",
    "app/page.tsx",
    "pages/index.tsx",
    "cmd/main.go",
    "src/main.rs",
    "public/index.php",
    "artisan",
    "config/application.rb",
)

CONVENTION_MARKERS: dict[str, str] = {
    ".editorconfig": "editorconfig",
    "ruff.toml": "ruff",
    ".ruff.toml": "ruff",
    "setup.cfg": "flake8/setup.cfg",
    ".flake8": "flake8",
    "mypy.ini": "mypy",
    ".eslintrc": "eslint",
    ".eslintrc.js": "eslint",
    ".eslintrc.cjs": "eslint",
    ".eslintrc.json": "eslint",
    "eslint.config.js": "eslint",
    "eslint.config.mjs": "eslint",
    ".prettierrc": "prettier",
    "prettier.config.js": "prettier",
    "tsconfig.json": "typescript",
    ".pre-commit-config.yaml": "pre-commit",
    "Makefile": "make",
    "justfile": "just",
    "Taskfile.yml": "task",
    ".github/workflows": "github-actions",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
    "compose.yaml": "docker-compose",
    ".devcontainer": "devcontainer",
}

STYLE_DOCS = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "STYLE.md", "CONVENTIONS.md")


@dataclass(slots=True)
class ProjectContext:
    root: Path
    name: str
    kind: str = "unknown"
    languages: list[str] = field(default_factory=list)
    primary_language: str = ""
    frameworks: list[str] = field(default_factory=list)
    package_manager: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    entry_points: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    monorepo: bool = False
    workspaces: list[str] = field(default_factory=list)
    file_count: int = 0
    top_directories: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dev_url: str | None = None
    pointers: dict[str, str] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # -- rendering ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "name": self.name,
            "kind": self.kind,
            "languages": self.languages,
            "primary_language": self.primary_language,
            "frameworks": self.frameworks,
            "package_manager": self.package_manager,
            "commands": self.commands,
            "entry_points": self.entry_points,
            "conventions": self.conventions,
            "monorepo": self.monorepo,
            "workspaces": self.workspaces,
            "file_count": self.file_count,
            "top_directories": self.top_directories,
            "risks": self.risks,
            "notes": self.notes,
            "dev_url": self.dev_url,
            "pointers": self.pointers,
            "git": self.git,
        }

    def prompt_summary(self) -> str:
        """Compact, token-efficient description for the system prompt."""

        lines = [
            f"project: {self.name} ({self.kind})",
            f"root: {self.root.name}",
            f"languages: {', '.join(self.languages[:5]) or 'unknown'}"
            + (f" (primary: {self.primary_language})" if self.primary_language else ""),
        ]
        if self.frameworks:
            lines.append(f"frameworks: {', '.join(self.frameworks[:10])}")
        if self.package_manager:
            lines.append(f"package manager: {self.package_manager}")
        if self.commands:
            lines.append("commands: " + "; ".join(f"{key}={value}" for key, value in sorted(self.commands.items())))
        if self.entry_points:
            lines.append(f"entry points: {', '.join(self.entry_points[:6])}")
        if self.conventions:
            lines.append(f"tooling: {', '.join(self.conventions[:10])}")
        if self.monorepo:
            lines.append(f"monorepo workspaces: {', '.join(self.workspaces[:6])}")
        if self.dev_url:
            lines.append(f"dev url: {self.dev_url}")
        if self.pointers:
            lines.append("guidance files present: " + ", ".join(self.pointers))
        if self.risks:
            lines.append("risks: " + "; ".join(self.risks[:5]))
        return "\n".join(lines)

    def render(self) -> str:
        """Full human-readable report used by ``ua inspect`` and ``inspect_project``."""

        sections: list[tuple[str, list[str]]] = [
            ("Identity", [f"name: {self.name}", f"kind: {self.kind}", f"root: {self.root}"]),
            ("Languages", [f"{item}" for item in self.languages] or ["none detected"]),
            ("Frameworks", self.frameworks or ["none detected"]),
            ("Package manager", [self.package_manager] if self.package_manager else ["not detected"]),
            ("Commands", [f"{key}: {value}" for key, value in sorted(self.commands.items())] or ["none detected"]),
            ("Entry points", self.entry_points or ["none detected"]),
            ("Conventions and tooling", self.conventions or ["none detected"]),
            ("Layout", [f"{directory}/" for directory in self.top_directories] + [f"{self.file_count} tracked source files"]),
            ("Guidance files", [f"{key}: {value}" for key, value in self.pointers.items()] or ["none"]),
            ("Risks", self.risks or ["none detected"]),
        ]
        if self.git:
            sections.insert(1, ("Git", [f"{key}: {value}" for key, value in self.git.items()]))
        if self.workspaces:
            sections.append(("Workspaces", self.workspaces))
        blocks = []
        for title, entries in sections:
            blocks.append(f"{title}\n" + "\n".join(f"  {entry}" for entry in entries))
        return "\n\n".join(blocks)


def load_project_context(root: Path, settings: Settings, *, max_files: int = 20_000) -> ProjectContext:
    """Scan ``root`` and build the project intelligence report."""

    root = Path(root).resolve()
    context = ProjectContext(root=root, name=root.name)
    ignore = _ignore_spec(root, settings)

    counts: Counter[str] = Counter()
    top_dirs: list[str] = []
    files: list[Path] = []

    for entry in sorted(root.iterdir()) if root.exists() else []:
        if entry.name.startswith(".") and entry.name not in {".github", ".devcontainer"}:
            continue
        if entry.is_dir():
            top_dirs.append(entry.name)

    for dirpath, dirnames, filenames in _walk(root, ignore, max_files=max_files):
        for filename in filenames:
            path = Path(dirpath) / filename
            suffix = path.suffix.lower()
            for language, extensions in LANGUAGE_EXTENSIONS.items():
                if suffix in extensions:
                    counts[language] += 1
                    files.append(path)
                    break

    context.file_count = sum(counts.values())
    context.languages = [name for name, _count in counts.most_common()]
    context.primary_language = context.languages[0] if context.languages else ""
    context.top_directories = top_dirs[:14]
    context.package_manager = _detect_package_manager(root)
    context.frameworks = _detect_frameworks(root, context.languages)
    context.kind = _detect_kind(context, root)
    context.commands = _detect_commands(root, context, settings)
    context.entry_points = [candidate for candidate in ENTRY_POINT_CANDIDATES if (root / candidate).is_file()]
    context.conventions = [label for marker, label in CONVENTION_MARKERS.items() if (root / marker).exists()]
    context.monorepo, context.workspaces = _detect_workspaces(root)
    context.dev_url = settings.project.dev_url or settings.browser.base_url or _detect_dev_url(root)
    context.pointers = _read_pointers(root, settings)
    context.git = _git_facts(root)
    context.risks = _detect_risks(root, context, files)
    context.notes = _notes(context)
    return context


def _walk(root: Path, ignore: Any, *, max_files: int) -> Any:
    import os

    from unknown_agent.tools.builtin.fs_tools import DEFAULT_IGNORE_DIRS

    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in DEFAULT_IGNORE_DIRS and not name.startswith(".")]
        if ignore is not None:
            try:
                relative_dir = current.relative_to(root).as_posix()
            except ValueError:
                relative_dir = ""
            if relative_dir and ignore.match_file(relative_dir + "/"):
                dirnames[:] = []
                continue
        yield dirpath, dirnames, filenames
        seen += len(filenames)
        if seen > max_files:
            return


def _ignore_spec(root: Path, settings: Settings) -> Any:
    try:
        import pathspec
    except ImportError:  # pragma: no cover
        return None
    patterns: list[str] = []
    for name in (".gitignore", ".uaignore"):
        path = root / name
        if path.is_file():
            patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    patterns.extend(settings.project.ignore)
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _detect_package_manager(root: Path) -> str | None:
    for marker, manager in PACKAGE_MANAGER_MARKERS:
        if (root / marker).exists():
            return manager
    return None


def _dependency_blob(root: Path) -> str:
    blobs: list[str] = []
    for name in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile"):
        path = root / name
        if path.is_file():
            blobs.append(path.read_text(encoding="utf-8", errors="replace").lower())
    package_json = root / "package.json"
    if package_json.is_file():
        blobs.append(package_json.read_text(encoding="utf-8", errors="replace").lower())
    for name in ("go.mod", "Cargo.toml", "composer.json", "Gemfile", "pom.xml", "build.gradle"):
        path = root / name
        if path.is_file():
            blobs.append(path.read_text(encoding="utf-8", errors="replace").lower())
    return "\n".join(blobs)


def _detect_frameworks(root: Path, languages: list[str]) -> list[str]:
    blob = _dependency_blob(root)
    found: list[str] = []
    for framework, needles in FRAMEWORK_SIGNATURES.items():
        if any(needle in blob for needle in needles):
            found.append(framework)
    # Import-level hints for Python projects without declared dependencies.
    if "python" in languages:
        for path in list(root.glob("**/*.py"))[:200]:
            if any(part in {".venv", "venv", "node_modules", "__pycache__"} for part in path.parts):
                continue
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                continue
            for framework, needle in (("django", "from django"), ("fastapi", "fastapi"), ("flask", "from flask"), ("pytest", "import pytest")):
                if needle in head and framework not in found:
                    found.append(framework)
            if len(found) >= 12:
                break
    return found


def _detect_kind(context: ProjectContext, root: Path) -> str:
    frameworks = set(context.frameworks)
    languages = set(context.languages)
    if {"next.js", "react", "vue", "svelte", "nuxt", "astro"} & frameworks:
        return "web-frontend" if not ({"fastapi", "django", "flask", "express", "nestjs"} & frameworks) else "fullstack-web"
    if {"fastapi", "django", "flask", "express", "nestjs", "gin", "axum", "actix", "spring", "laravel", "rails"} & frameworks:
        return "backend-api"
    if "python" in languages:
        return "python-application"
    if (root / "package.json").is_file():
        return "node-application"
    if (root / "go.mod").is_file():
        return "go-module"
    if (root / "Cargo.toml").is_file():
        return "rust-crate"
    return "generic-project"


def _detect_commands(root: Path, context: ProjectContext, settings: Settings) -> dict[str, str]:
    commands: dict[str, str] = dict(settings.project.commands or {})
    manager = context.package_manager
    languages = set(context.languages)
    frameworks = set(context.frameworks)

    if (root / "package.json").is_file():
        try:
            payload = json.loads((root / "package.json").read_text(encoding="utf-8", errors="replace"))
            scripts = payload.get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        runner = {"pnpm": "pnpm", "yarn": "yarn", "bun": "bun"}.get(manager or "npm", "npm")
        for name, script in scripts.items():
            key = name.lower()
            if "test" in key and "test" not in commands:
                commands["test"] = f"{runner} test" if runner != "npm" else "npm test"
            if key == "build":
                commands.setdefault("build", f"{runner} run build")
            if "lint" in key:
                commands.setdefault("lint", f"{runner} run {name}")
            if key in {"dev", "start"}:
                commands.setdefault("dev", f"{runner} run {name}")
            if "typecheck" in key or "types" in key:
                commands.setdefault("typecheck", f"{runner} run {name}")

    if "python" in languages or (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or (root / "tests").is_dir():
        prefix = ""
        if manager == "uv":
            prefix = "uv run "
        elif manager == "poetry":
            prefix = "poetry run "
        elif manager == "pipenv":
            prefix = "pipenv run "
        if (root / "tests").is_dir() or (root / "test").is_dir() or (root / "pytest.ini").is_file() or "pytest" in frameworks:
            commands.setdefault("test", f"{prefix}pytest -q")
        if "ruff" in context.conventions or (root / "ruff.toml").exists():
            commands.setdefault("lint", f"{prefix}ruff check .")
            commands.setdefault("format", f"{prefix}ruff format .")
        if "mypy" in context.conventions:
            commands.setdefault("typecheck", f"{prefix}mypy .")

    if "go" in languages and (root / "go.mod").is_file():
        commands.setdefault("test", "go test ./...")
        commands.setdefault("build", "go build ./...")
    if "rust" in languages and (root / "Cargo.toml").is_file():
        commands.setdefault("test", "cargo test")
        commands.setdefault("build", "cargo build")
    if (root / "Makefile").is_file():
        makefile = (root / "Makefile").read_text(encoding="utf-8", errors="replace")
        for target in ("test", "lint", "build", "dev", "format"):
            if re.search(rf"^{target}\s*:", makefile, re.MULTILINE):
                commands.setdefault(target, f"make {target}")
    return {key: value for key, value in commands.items() if value}


def _detect_workspaces(root: Path) -> tuple[bool, list[str]]:
    workspaces: list[str] = []
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
            raw = payload.get("workspaces")
            if isinstance(raw, list):
                workspaces.extend(str(item) for item in raw)
            elif isinstance(raw, dict) and raw.get("packages"):
                workspaces.extend(str(item) for item in raw["packages"])
        except json.JSONDecodeError:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            if "tool" in payload and "uv" in payload["tool"] and "workspace" in payload["tool"]["uv"]:
                members = payload["tool"]["uv"]["workspace"].get("members") or []
                workspaces.extend(str(item) for item in members)
        except tomllib.TOMLDecodeError:
            pass
    for candidate in ("packages", "apps", "services", "libs"):
        directory = root / candidate
        if directory.is_dir() and any(child.is_dir() and (child / "package.json").is_file() for child in directory.iterdir()):
            return True, workspaces or [candidate]
    return bool(workspaces), workspaces


def _detect_dev_url(root: Path) -> str | None:
    for name, port in (("docker-compose.yml", 3000), ("compose.yaml", 3000)):
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'"(\d{2,5}):(\d{2,5})"', text) or re.search(r"-\s*\"?(\d{2,5}):(\d{2,5})", text)
            if match:
                return f"http://localhost:{match.group(1)}"
    if (root / "package.json").is_file():
        try:
            payload = json.loads((root / "package.json").read_text(encoding="utf-8", errors="replace"))
            scripts = (payload.get("scripts") or {}).get("dev", "")
            match = re.search(r"(?:--port|-p)\s+(\d{2,5})", scripts or "")
            if match:
                return f"http://localhost:{match.group(1)}"
        except json.JSONDecodeError:
            pass
    if "django" in _dependency_blob(root):
        return "http://127.0.0.1:8000"
    if "fastapi" in _dependency_blob(root) or "flask" in _dependency_blob(root):
        return "http://127.0.0.1:8000"
    return None


def _read_pointers(root: Path, settings: Settings) -> dict[str, str]:
    pointers: dict[str, str] = {}
    for name in settings.project.pointer_files:
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pointers[name] = text[:1600]
    return pointers


def _git_facts(root: Path) -> dict[str, Any]:
    if not (root / ".git").exists():
        return {}
    import subprocess

    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return ""

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    commit = run("rev-parse", "--short", "HEAD")
    dirty = bool(run("status", "--short"))
    return {
        "branch": branch or "(detached)",
        "head": commit,
        "dirty": dirty,
        "remotes": len(run("remote").splitlines()),
    }


def _detect_risks(root: Path, context: ProjectContext, files: list[Path]) -> list[str]:
    risks: list[str] = []
    if not any((root / name).exists() for name in ("tests", "test", "spec", "__tests__")) and "pytest" not in context.frameworks:
        risks.append("no test directory detected - verification depends on manual or browser checks")
    if context.package_manager in {"pip", None} and (root / "pyproject.toml").is_file():
        risks.append("Python dependency resolution may not be reproducible without a lockfile (uv.lock/poetry.lock)")
    if (root / "package.json").is_file() and not any((root / name).exists() for name in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "bun.lockb")):
        risks.append("package.json without a lockfile - installs are not reproducible for the agent's build checks")
    secret_files = [".env", ".env.local", ".env.production", "secrets.yaml", "credentials.json"]
    present = [name for name in secret_files if (root / name).exists()]
    if present:
        risks.append(f"secret-bearing files present ({', '.join(present)}) - do not print their contents")
    if context.file_count > 4000:
        risks.append(f"large repository ({context.file_count} source files) - prefer targeted search over full reads")
    if context.monorepo:
        risks.append("monorepo layout - confirm which workspace owns the change before editing")
    if not context.commands.get("test"):
        risks.append("no test command detected - consider adding tests as part of the work")
    return risks


def _notes(context: ProjectContext) -> list[str]:
    notes: list[str] = []
    host = detect_host()
    notes.append(f"host: {host.os_name}/{host.arch}, python {host.python_version}")
    if host.is_windows:
        notes.append("Windows host: use platform-neutral paths and commands; the shell tool avoids POSIX-only syntax.")
    if host.is_wsl:
        notes.append("WSL detected: filesystem performance differs across /mnt mounts.")
    if context.kind == "fullstack-web":
        notes.append("Full-stack web project: verify both API responses and rendered UI.")
    return notes


__all__ = ["ProjectContext", "load_project_context"]
