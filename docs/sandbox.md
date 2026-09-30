# Sandbox profiles

Unknown Agent applies native subprocess containment to agent-authored shell commands and validation gates. The sandbox wraps the existing process runner; policy checks, process-group cancellation, bounded output, and the five-minute timeout ceiling remain in force.

## Profiles

| Profile | Filesystem | Network | Intended use |
| --- | --- | --- | --- |
| `trusted` | Host permissions | Allowed | Explicit operator-controlled commands only |
| `workspace` | Root read-only; project writable | Denied by default | Builds, tests, linters, and normal shell tools |
| `isolated` | Root read-only; project writable; ephemeral `/tmp` | Always denied | Untrusted or dependency-heavy execution |

Backends are selected by platform:

- Linux: Bubblewrap (`bwrap`) with PID, IPC, UTS, mount, and optional network namespaces.
- macOS: Seatbelt through `sandbox-exec`, with default-deny rules and project-scoped writes.
- Windows: the `unknown-agent-sandbox.exe` restricted-token/job-object broker. If it is not installed, availability is reported honestly.

`mode = "required"` fails closed when the native backend is unavailable. `auto` reports degradation and continues under the normal policy/process boundary; `off` is an explicit opt-out. The default is `auto` with the `workspace` profile and network disabled.

```toml
[sandbox]
mode = "required"                 # off | auto | required
default_profile = "workspace"
shell_profile = "workspace"
validation_profile = "isolated"
allow_network = false
```

Writable paths and the working directory must resolve inside the project. Symlink and `..` escapes are rejected by canonical path validation. Environment inheritance follows the central subprocess policy, and raw secrets should never be passed as command arguments.

## Inspect the effective boundary

```bash
ua sandbox status
ua sandbox explain python
```

Both commands emit JSON without terminal formatting. `status` reports backend availability and configuration. `explain` returns the exact launch decision without starting the command.

## Timeout ceiling

All process, tool, validation, hook, scheduler, RPC, and approval waits are capped at **300 seconds**. Lower per-operation limits remain supported. A value above 300 seconds is either rejected by typed configuration or clamped by the final process boundary, so an internal caller cannot bypass the ceiling.
