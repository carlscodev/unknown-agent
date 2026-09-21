"""Custom gates declared in configuration.

Two ways to extend validation without writing Python:

```toml
[validation.custom_gate_commands]
"api-contract" = "python scripts/check_openapi.py"
"migration-order" = "python manage.py showmigrations --plan | grep -q '\\[ \\]' && exit 1 || exit 0"
```

Plus type-aware presets useful for the stacks the product specialises in (web,
mobile, backend).  Presets only ever *suggest*; they run only when the project
actually contains the tool.
"""

from __future__ import annotations

from pathlib import Path

from unknown_agent.config.models import Settings
from unknown_agent.validation.gates import CustomGate, Gate

#: (gate name, project marker that must exist, command template)
PRESETS: list[tuple[str, str, str]] = [
    ("openapi", "openapi.yaml", "python -c \"import yaml,sys; yaml.safe_load(open('openapi.yaml'))\""),
    ("openapi", "openapi.json", "python -c \"import json,sys; json.load(open('openapi.json'))\""),
    ("docker-build", "Dockerfile", "docker build --check ."),
    ("compose-config", "docker-compose.yml", "docker compose config --quiet"),
    ("compose-config", "compose.yaml", "docker compose config --quiet"),
    ("alembic-heads", "alembic.ini", "python -m alembic heads"),
    ("prisma-validate", "prisma/schema.prisma", "npx --no-install prisma validate"),
    ("terraform-fmt", "main.tf", "terraform fmt -check -recursive"),
    ("k8s-dry-run", "k8s", "kubectl apply --dry-run=client -f k8s/"),
]


def build_custom_gates(settings: Settings) -> list[Gate]:
    gates: list[Gate] = []
    for name, command in (settings.validation.custom_gate_commands or {}).items():
        setting = settings.validation.gate(name)
        if setting.enabled:
            gates.append(CustomGate(name=name, command=command, timeout_s=setting.timeout_s))
    return gates


def preset_gates(project_root: Path, settings: Settings, *, only_existing: bool = True) -> list[Gate]:
    """Preset gates for the tooling actually present in this project."""

    root = Path(project_root)
    gates: list[Gate] = []
    seen: set[str] = set()
    for name, marker, command in PRESETS:
        if name in seen or name in (settings.validation.custom_gate_commands or {}):
            continue
        if only_existing and not (root / marker).exists():
            continue
        seen.add(name)
        gates.append(CustomGate(name=name, command=command, hint=f"Declared preset gate for {marker}."))
    return gates


__all__ = ["build_custom_gates", "preset_gates", "PRESETS"]
