# Contributing

## Development setup

```bash
git clone https://github.com/OWNER/REPO.git
cd unknown-agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q          # 550+ tests, all offline by default
```

Integration tests (real MCP stdio servers) are marked and opt-in:

```bash
pip install "mcp>=1.2"
python -m pytest -q -m integration
```

## Ground rules

- **Honesty over claims**: a feature exists when a test proves it. Never
  fabricate output, never mark a gate green without evidence.
- **Capability gates before advertising**: tools that cannot run in the
  current session are withheld with a reason, not registered to fail.
- **No secrets anywhere**: not in code, tests, fixtures, examples, or commit
  history. Config references secrets (`env:NAME`, `keyring:...`), never values.
- Lint/format with `ruff`, type-check with `mypy` (config in `pyproject.toml`).
- One behaviour change = one test pinning it.

## Pull requests

- Target `main`. CI must pass (tests on 3.11–3.13 + Windows, installer smoke,
  build check).
- Update `CHANGELOG.md` under *Unreleased*.
- Version bumps happen in release PRs only (`src/unknown_agent/version.py`).

## Security

Found a vulnerability? Do not open a public issue — see `SECURITY.md`.
