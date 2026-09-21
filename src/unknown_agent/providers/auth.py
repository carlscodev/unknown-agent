"""Authentication modes: how a provider is *reached*, not just what key it takes.

The catalog used to encode exactly one idea of authentication — ``api_key_required:
bool`` plus an ``env_var``.  That is wrong for a growing share of real providers:

* **Codex** can run on a ChatGPT/Codex subscription (an OAuth session managed by the
  ``codex`` CLI) *or* on a direct OpenAI API key.  The two differ in billing, in what
  models are reachable, and in how they fail.
* **Anything routed** (OpenRouter in front of Anthropic, a company gateway, LiteLLM)
  is a different auth path to the same intelligence, with different headers and a
  different endpoint.
* **Azure / vLLM / self-hosted** need an endpoint the user owns before a credential
  means anything.

So a provider now declares a *list* of :class:`AuthMode`.  Each mode carries its own
kind (how the panel collects it), its own validation expectations, its own billing
story, and its own caveats.  The setup panel renders that list instead of assuming a
key prompt, and the saved profile records which mode is active so later code can
report it honestly.

Nothing here performs I/O.  Validation lives in :mod:`unknown_agent.providers.health`,
which consumes these declarations.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "AuthKind",
    "AuthMode",
    "AuthConflict",
    "API_KEY_MODE",
    "auth_modes_for",
    "resolve_auth_mode",
    "detect_auth_conflicts",
    "cli_session_state",
]


#: How the panel collects this mode, and what the health check must prove.
#:
#: ``api_key``      - a secret pasted into the masked dialog
#: ``subscription`` - an account session owned by an external CLI (Codex/ChatGPT)
#: ``routed``       - a third-party router in front of the real vendor
#: ``endpoint``     - a user-owned base URL, optionally plus a key
#: ``none``         - genuinely keyless (a local server)
AuthKind = Literal["api_key", "subscription", "routed", "endpoint", "none"]


@dataclass(frozen=True, slots=True)
class AuthMode:
    """One valid way to authenticate against a provider."""

    key: str
    label: str
    kind: AuthKind
    summary: str = ""
    #: What the user is billed through when this mode is active.  Shown verbatim in
    #: the panel because confusing subscription access with API billing is the single
    #: most expensive mistake a user can make here.
    billing: str = ""
    #: Environment variable that carries the credential, when the mode uses one.
    env_var: str = ""
    #: External command that owns the session, for ``subscription`` modes.
    cli_command: str = ""
    #: Argument vector that reports login state.  Must be read-only and fast.
    cli_status_args: tuple[str, ...] = ()
    #: Argument vector that starts an interactive login.
    cli_login_args: tuple[str, ...] = ()
    #: The user must supply a base URL before this mode can be used.
    requires_base_url: bool = False
    #: Base URL written when the user does not override it.
    default_base_url: str = ""
    #: Extra headers this mode needs on every request.
    extra_headers: dict[str, str] = field(default_factory=dict)
    #: Things that bite people.  Rendered as warnings in the panel, not buried in docs.
    caveats: tuple[str, ...] = ()
    #: Can models be listed over HTTP in this mode?
    supports_model_listing: bool = True
    #: Where to get the credential / start the signup.
    docs_url: str = ""
    #: Shown first in the selector.
    recommended: bool = False

    @property
    def needs_secret(self) -> bool:
        return self.kind in {"api_key", "routed"} or (self.kind == "endpoint" and bool(self.env_var))

    @property
    def needs_login(self) -> bool:
        return self.kind == "subscription"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "summary": self.summary,
            "billing": self.billing,
            "env_var": self.env_var,
            "cli_command": self.cli_command,
            "requires_base_url": self.requires_base_url,
            "default_base_url": self.default_base_url,
            "caveats": list(self.caveats),
            "supports_model_listing": self.supports_model_listing,
            "docs_url": self.docs_url,
            "recommended": self.recommended,
        }


# --------------------------------------------------------------------------- #
# per-provider declarations
# --------------------------------------------------------------------------- #
#: Codex is the motivating case: two genuinely different products behind one name.
CODEX_MODES: tuple[AuthMode, ...] = (
    AuthMode(
        key="chatgpt",
        label="Sign in with ChatGPT (subscription)",
        kind="subscription",
        summary=(
            "Uses the Codex session your ChatGPT plan already grants. The `codex` CLI owns "
            "the OAuth login; Unknown Agent reuses it rather than holding a second credential."
        ),
        billing="Included in your ChatGPT Plus/Pro/Team plan — no per-token API charge.",
        cli_command="codex",
        cli_status_args=("login", "status"),
        cli_login_args=("login",),
        caveats=(
            "OPENAI_API_KEY in your environment can silently take priority over the "
            "subscription session — Unknown Agent checks for this and warns.",
            "Plan limits apply; they are not the same as API rate limits.",
            "Requires the `codex` CLI to be installed and logged in.",
        ),
        supports_model_listing=False,
        docs_url="https://developers.openai.com/codex/cli/",
        recommended=True,
    ),
    AuthMode(
        key="api_key",
        label="OpenAI API key (direct billing)",
        kind="api_key",
        summary="Authenticate straight to the OpenAI API with a platform key.",
        billing="Billed per token to your OpenAI API account — separate from ChatGPT.",
        env_var="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        caveats=("Usage is charged to the API account, not your ChatGPT subscription.",),
        docs_url="https://platform.openai.com/api-keys",
    ),
    AuthMode(
        key="routed",
        label="Through a third-party router",
        kind="routed",
        summary="Reach Codex-class models via OpenRouter, a gateway, or a proxy you control.",
        billing="Billed by the router, at the router's rates.",
        env_var="OPENROUTER_API_KEY",
        default_base_url="https://openrouter.ai/api/v1",
        caveats=("Tool-calling fidelity depends on the router, not on OpenAI.",),
        docs_url="https://openrouter.ai/docs",
    ),
)

#: Vendors that also make sense behind a router or a self-hosted gateway.
def _standard_modes(
    *,
    env_var: str,
    base_url: str,
    docs_url: str,
    listing: bool = True,
    routed: bool = True,
) -> tuple[AuthMode, ...]:
    modes = [
        AuthMode(
            key="api_key",
            label="API key",
            kind="api_key",
            summary="The vendor's own key, pasted once and stored with 0600 permissions.",
            billing="Billed per token to the account that owns the key.",
            env_var=env_var,
            default_base_url=base_url,
            supports_model_listing=listing,
            docs_url=docs_url,
            recommended=True,
        ),
        AuthMode(
            key="endpoint",
            label="Custom endpoint (self-hosted / gateway)",
            kind="endpoint",
            summary="Same dialect, your URL: Azure, vLLM, LiteLLM, or a company proxy.",
            billing="Whatever the endpoint owner bills.",
            env_var=env_var,
            requires_base_url=True,
            supports_model_listing=listing,
            docs_url=docs_url,
        ),
    ]
    if routed:
        modes.append(
            AuthMode(
                key="routed",
                label="Through a third-party router",
                kind="routed",
                summary="Reach this vendor's models through OpenRouter or another gateway.",
                billing="Billed by the router, at the router's rates.",
                env_var="OPENROUTER_API_KEY",
                default_base_url="https://openrouter.ai/api/v1",
                caveats=("Model ids and tool support follow the router's catalog.",),
                docs_url="https://openrouter.ai/docs",
            )
        )
    return tuple(modes)


#: The fallback for a keyed provider that declares nothing special.
API_KEY_MODE = AuthMode(
    key="api_key",
    label="API key",
    kind="api_key",
    summary="The vendor's own key, stored with 0600 permissions.",
    billing="Billed per token to the account that owns the key.",
    recommended=True,
)

_KEYLESS_MODE = AuthMode(
    key="none",
    label="No credential required",
    kind="none",
    summary="This provider runs without authentication.",
    billing="Free — it runs on your machine.",
    recommended=True,
)

#: Explicit per-provider overrides.  Anything absent is derived from the catalog entry.
_DECLARED: dict[str, tuple[AuthMode, ...]] = {"codex": CODEX_MODES}


def auth_modes_for(entry: Any) -> tuple[AuthMode, ...]:
    """Every valid way to authenticate against ``entry``.

    Providers may declare modes explicitly (``_DECLARED``) or carry them on the
    catalog entry itself; otherwise a sensible set is derived so *every* provider
    gains custom-endpoint and routed options without 21 hand-written tables.
    """

    declared = getattr(entry, "auth_modes", None)
    if declared:
        return tuple(declared)
    key = getattr(entry, "key", "")
    if key in _DECLARED:
        return _DECLARED[key]

    if not getattr(entry, "api_key_required", True):
        # Local servers: keyless by default, but still reachable at another address.
        return (
            _KEYLESS_MODE,
            AuthMode(
                key="endpoint",
                label="Custom endpoint",
                kind="endpoint",
                summary="Point at another host running the same server.",
                billing="Free — you host it.",
                requires_base_url=True,
                docs_url=getattr(entry, "docs_url", ""),
            ),
        )

    return _standard_modes(
        env_var=getattr(entry, "env_var", ""),
        base_url=getattr(entry, "base_url", ""),
        docs_url=getattr(entry, "docs_url", ""),
        listing=getattr(entry, "model_listing", "openai") != "none",
        # A router in front of a router is noise.
        routed=key not in {"openrouter"},
    )


def resolve_auth_mode(entry: Any, mode_key: str | None) -> AuthMode:
    """The named mode, or the recommended one when nothing is named."""

    modes = auth_modes_for(entry)
    if mode_key:
        for mode in modes:
            if mode.key == mode_key:
                return mode
    for mode in modes:
        if mode.recommended:
            return mode
    return modes[0] if modes else API_KEY_MODE


# --------------------------------------------------------------------------- #
# conflict detection
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AuthConflict:
    """Something in the environment contradicts the configured auth mode."""

    severity: str            # "warn" | "error"
    title: str
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "title": self.title, "detail": self.detail, "fix": self.fix}


def detect_auth_conflicts(entry: Any, mode: AuthMode, *, env: dict[str, str] | None = None) -> list[AuthConflict]:
    """Find environment state that will quietly defeat the chosen mode.

    The headline case is Codex: an ``OPENAI_API_KEY`` exported in the shell can take
    precedence over a ChatGPT subscription session, so the user believes they are on
    their plan while being billed per token.  That must be surfaced at configuration
    time, not discovered on an invoice.
    """

    environ = env if env is not None else dict(os.environ)
    conflicts: list[AuthConflict] = []

    if mode.kind == "subscription":
        # Any API key for the same vendor family can override the session.
        for suspect in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            if environ.get(suspect):
                conflicts.append(
                    AuthConflict(
                        severity="warn",
                        title=f"${suspect} is set in this environment",
                        detail=(
                            f"You chose subscription sign-in, but ${suspect} is exported. Some tools prefer the "
                            "API key when both are present, which bills you per token instead of using your plan."
                        ),
                        fix=f"unset {suspect}   (or pick the API-key mode deliberately)",
                    )
                )
        if mode.cli_command and not shutil.which(mode.cli_command):
            conflicts.append(
                AuthConflict(
                    severity="error",
                    title=f"the `{mode.cli_command}` CLI is not installed",
                    detail=f"Subscription sign-in is managed by `{mode.cli_command}`, which is not on your PATH.",
                    fix=f"Install it, then run `{mode.cli_command} login` — or choose API-key mode instead.",
                )
            )

    if mode.kind in {"api_key", "routed"} and mode.env_var:
        # A *different* key in the environment than the one we stored is confusing,
        # because resolution order decides which wins.
        if environ.get(mode.env_var):
            conflicts.append(
                AuthConflict(
                    severity="warn",
                    title=f"${mode.env_var} is also set in this environment",
                    detail=(
                        "A stored credential and an environment variable both exist. The environment usually wins, "
                        "so the key you pasted may not be the one in use."
                    ),
                    fix=f"unset {mode.env_var} to use the stored key, or choose 'use $%s' during setup." % mode.env_var,
                )
            )

    return conflicts


# --------------------------------------------------------------------------- #
# subscription session probing
# --------------------------------------------------------------------------- #
def cli_session_state(mode: AuthMode, *, timeout: float = 8.0) -> tuple[str, str]:
    """Ask an external CLI whether its session is live.  Returns ``(state, detail)``.

    ``state`` is one of ``ok`` / ``login_required`` / ``missing`` / ``unknown``.  This
    shells out deliberately: the subscription session belongs to that CLI, and
    re-implementing its token storage would be both fragile and a security mistake.
    """

    if not mode.cli_command:
        return "unknown", "this mode has no session command to check"
    binary = shutil.which(mode.cli_command)
    if not binary:
        return "missing", f"`{mode.cli_command}` is not installed or not on PATH"
    if not mode.cli_status_args:
        return "unknown", f"`{mode.cli_command}` is installed but exposes no status command"

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, *mode.cli_status_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"could not run `{mode.cli_command}`: {exc}"

    blob = f"{completed.stdout}\n{completed.stderr}".strip()
    lowered = blob.lower()
    if completed.returncode == 0 and not any(
        marker in lowered for marker in ("not logged in", "no credentials", "please log in", "login required")
    ):
        first = next((line.strip() for line in blob.splitlines() if line.strip()), "session is active")
        return "ok", first[:200]
    if any(marker in lowered for marker in ("not logged in", "no credentials", "please log in", "login required")):
        return "login_required", f"`{mode.cli_command}` reports no active session"
    return "login_required", (blob.splitlines()[0][:200] if blob else f"`{mode.cli_command}` exited {completed.returncode}")
