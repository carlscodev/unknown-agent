# Security Policy

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability). Do not open public issues for security
problems. You will get an acknowledgement within a week.

## Scope notes for reviewers

Unknown Agent executes tools against real repositories, so its security
posture is explicit and testable:

- **Path policy**: deny-by-default filesystem access outside the project root.
- **Shell policy**: no-shell process execution by default; process-group
  timeouts; `shell_run` withheld in read-only sessions.
- **Secrets**: config stores *references* (`env:`, `file:`, `keyring:`), never
  values; output is piped through a redactor.
- **MCP trust ladder**: unannotated remote tools are treated as DESTRUCTIVE;
  `readOnlyHint` is believed only at `full` trust.
- **Vision exfiltration guard**: tool-initiated remote image URLs are blocked
  by default (`[vision] allow_remote_urls_in_tools = false`).
- **Computer use**: off by default; closed action vocabulary; budgets, host
  allowlist, denied keys, confirmation ladder that fails closed; every action
  journalled.
- **Installer**: no sudo, no writes outside `$HOME`, designed for
  download-inspect-run with release checksums (`SHA256SUMS`).

## Supported versions

The latest minor release line receives security fixes.
