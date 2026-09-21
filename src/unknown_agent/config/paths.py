"""Platform-correct filesystem locations.

Linux is the primary target, but every path decision goes through
:mod:`platformdirs` so that Windows (``%APPDATA%``/``%LOCALAPPDATA%``) and macOS
(``~/Library/...``) work without special cases.  Nothing in the product may
hardcode ``~/.config`` or ``/home``.

Layout (Linux, XDG):

* config ``~/.config/unknown-agent``      - ``config.toml``, procedures, plugins
* data   ``~/.local/share/unknown-agent`` - sessions, memory database, knowledge
* state  ``~/.local/state/unknown-agent`` - run journals, logs
* cache  ``~/.cache/unknown-agent``       - repo maps, provider capability cache

Per-project state lives in ``<repo>/.unknown/`` and is intentionally separate
from the user-level state so a project can be archived, shared, or deleted
without touching global memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "unknown-agent"
PROJECT_DIRNAME = ".unknown"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved user-level and project-level locations."""

    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    cwd: Path

    # -- user level ---------------------------------------------------------
    @property
    def user_config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def procedures_dir(self) -> Path:
        return self.config_dir / "procedures"

    @property
    def user_plugins_dir(self) -> Path:
        return self.config_dir / "plugins"

    @property
    def user_skills_dir(self) -> Path:
        """User-scope skills: installed once, active in every project."""

        return self.config_dir / "skills"

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def sessions_db(self) -> Path:
        return self.state_dir / "sessions.db"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "logs" / "unknown-agent.log"

    @property
    def cache_root(self) -> Path:
        return self.cache_dir

    # -- project level ------------------------------------------------------
    @property
    def project_dir(self) -> Path:
        return self.cwd / PROJECT_DIRNAME

    @property
    def project_config_file(self) -> Path:
        return self.project_dir / "config.toml"

    @property
    def project_memory_dir(self) -> Path:
        return self.project_dir / "memory"

    @property
    def runs_dir(self) -> Path:
        return self.project_dir / "runs"

    @property
    def artifacts_dir(self) -> Path:
        return self.project_dir / "artifacts"

    @property
    def backups_dir(self) -> Path:
        return self.project_dir / "backups"

    @property
    def project_cache_dir(self) -> Path:
        return self.project_dir / "cache"

    @property
    def project_logs_dir(self) -> Path:
        return self.project_dir / "logs"

    @property
    def project_plugins_dir(self) -> Path:
        return self.project_dir / "plugins"

    @property
    def project_skills_dir(self) -> Path:
        """Project-scope skills: arrive with a clone, so they are *untrusted*.

        Untrusted is a label, not a verdict — see ``SkillSet.untrusted()``.  Their
        tool grants are still requests that policy decides on; the difference is
        that the UI reports their origin and ``[skills]
        allow_project_skills = false`` turns them off entirely.
        """

        return self.project_dir / "skills"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def ensure_user_dirs(self) -> None:
        for path in (self.config_dir, self.data_dir, self.state_dir, self.cache_dir, self.procedures_dir):
            path.mkdir(parents=True, exist_ok=True)

    def ensure_project_dirs(self) -> None:
        for path in (
            self.project_dir,
            self.project_memory_dir,
            self.runs_dir,
            self.artifacts_dir,
            self.backups_dir,
            self.project_cache_dir,
            self.project_logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def as_dict(self) -> dict[str, str]:
        return {
            "config_dir": str(self.config_dir),
            "data_dir": str(self.data_dir),
            "state_dir": str(self.state_dir),
            "cache_dir": str(self.cache_dir),
            "cwd": str(self.cwd),
            "project_dir": str(self.project_dir),
            "user_config_file": str(self.user_config_file),
            "project_config_file": str(self.project_config_file),
            "log_file": str(self.log_file),
            "memory_db": str(self.memory_db),
            "sessions_db": str(self.sessions_db),
        }


def resolve_paths(cwd: Path | None = None, *, project_dirname: str = PROJECT_DIRNAME) -> AppPaths:
    """Build :class:`AppPaths` for ``cwd`` using platform-appropriate roots."""

    dirs = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)
    base = Path(cwd or Path.cwd()).expanduser().resolve()
    return AppPaths(
        config_dir=Path(dirs.user_config_dir),
        data_dir=Path(dirs.user_data_dir),
        state_dir=Path(dirs.user_state_dir),
        cache_dir=Path(dirs.user_cache_dir),
        cwd=base,
    )


def find_project_root(start: Path, *, markers: tuple[str, ...] = (PROJECT_DIRNAME, ".git"), max_levels: int = 12) -> Path:
    """Walk up from ``start`` looking for a project root.

    A directory containing ``.unknown/`` is authoritative; ``.git`` is the
    fallback so the agent can operate on repositories that were never
    initialised with ``ua init``.
    """

    current = start.resolve()
    for _ in range(max_levels):
        for marker in markers:
            if (current / marker).exists():
                return current
        if current.parent == current:
            break
        current = current.parent
    return start.resolve()
