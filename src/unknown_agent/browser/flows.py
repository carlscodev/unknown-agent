"""Reusable browser flows and the flow file format.

Flows exist so verification is repeatable: a team writes the signup or checkout
journey once in ``.unknown/flows/*.yaml``, and every run (human or agent) replays
exactly the same steps.  The agent can also emit ad-hoc flows inline.

File format::

```yaml
name: checkout-happy-path
url: http://localhost:3000
steps:
  - action: goto
    url: /products
  - action: click
    selector: "[data-testid=add-to-cart]"
  - action: goto
    url: /cart
  - action: assert_text
    text: Total
  - action: click
    selector: button.checkout
  - action: assert_visible
    selector: "[data-testid=order-summary]"
  - action: assert_no_console_errors
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from unknown_agent.errors import ConfigError
from unknown_agent.observability.logging import get_logger

log = get_logger("browser.flows")

DEFAULT_FLOW_DIRS = ("flows", "browser-flows", "e2e")

SUPPORTED_ACTIONS = (
    "goto", "click", "fill", "type", "press", "select", "check", "wait_for", "wait_for_timeout", "wait",
    "assert_text", "assert_no_text", "assert_visible", "assert_hidden", "assert_url", "assert_status",
    "assert_no_console_errors", "screenshot", "eval", "reload", "scroll", "note", "noop",
)


@dataclass(slots=True)
class Flow:
    name: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    url: str | None = None
    description: str = ""
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "description": self.description, "steps": self.steps, "path": str(self.path) if self.path else None}


def load_flow(path: Path) -> Flow:
    """Parse a YAML/JSON flow file with validation and clear errors."""

    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Flow file not found: {path}", hint="Create it under .unknown/flows/ or pass --steps-file.")
    try:
        if path.suffix.lower() == ".json":
            import json

            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"Flow file {path} is not valid YAML/JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"Flow file {path} must contain a mapping with 'steps'.")

    raw_steps = payload.get("steps") or payload.get("flow") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConfigError(f"Flow file {path} has no steps.", hint="Add a 'steps:' list; see docs/browser.md.")
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps, start=1):
        if isinstance(step, str):
            steps.append({"action": step})
            continue
        if not isinstance(step, dict):
            raise ConfigError(f"Flow {path}: step {index} must be a mapping or action string.")
        action = str(step.get("action") or step.get("do") or "").lower()
        if not action:
            raise ConfigError(f"Flow {path}: step {index} has no 'action'.")
        if action not in SUPPORTED_ACTIONS:
            raise ConfigError(
                f"Flow {path}: step {index} uses unsupported action {action!r}.",
                hint=f"Supported actions: {', '.join(SUPPORTED_ACTIONS)}",
            )
        steps.append({**step, "action": action})
    return Flow(
        name=str(payload.get("name") or path.stem),
        steps=steps,
        url=payload.get("url") or payload.get("base_url"),
        description=str(payload.get("description") or ""),
        path=path,
    )


def discover_flows(project_root: Path, *, directory: str = ".unknown/flows") -> list[Flow]:
    """Find flow files in the project (``.unknown/flows`` plus common e2e dirs)."""

    root = Path(project_root)
    candidates: list[Path] = []
    primary = root / directory
    if primary.is_dir():
        candidates.extend(sorted(primary.glob("*.y*ml")) + sorted(primary.glob("*.json")))
    for extra in DEFAULT_FLOW_DIRS:
        directory_path = root / extra
        if directory_path.is_dir() and extra != directory:
            candidates.extend(sorted(directory_path.glob("*.y*ml"))[:20])
    flows: list[Flow] = []
    for path in candidates:
        try:
            flows.append(load_flow(path))
        except ConfigError as exc:
            log.warning("skipping flow file %s: %s", path, exc)
    return flows


def write_flow(project_root: Path, name: str, steps: list[dict[str, Any]], *, url: str | None = None) -> Path:
    """Persist a flow so it can be replayed by CI and future runs."""

    directory = Path(project_root) / ".unknown" / "flows"
    directory.mkdir(parents=True, exist_ok=True)
    slug = "".join(char if char.isalnum() or char in "-_" else "-" for char in name.lower()).strip("-") or "flow"
    path = directory / f"{slug}.yaml"
    payload: dict[str, Any] = {"name": name, "steps": steps}
    if url:
        payload["url"] = url
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def smoke_steps(url: str | None = None) -> list[dict[str, Any]]:
    """The default flow used when nothing is specified: load and check it renders."""

    steps: list[dict[str, Any]] = []
    if url:
        steps.append({"action": "goto", "url": url})
    steps.extend(
        [
            {"action": "assert_visible", "selector": "body"},
            {"action": "assert_no_console_errors"},
        ]
    )
    return steps


__all__ = ["Flow", "load_flow", "discover_flows", "write_flow", "smoke_steps", "SUPPORTED_ACTIONS"]
