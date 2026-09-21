"""The single entry point for configuring a provider.

Everything in the product that needs a configured provider calls
:func:`launch_setup`.  There is deliberately *one* function, because there is
deliberately one experience:

* a real terminal + ``textual`` installed  → the interactive setup panel
  (:mod:`unknown_agent.tui.quicksetup`): provider list → key dialog → automatic
  validation → automatic model fetch → model selector → save;
* no TTY, ``--json``, CI, or ``textual`` missing → the Rich guided fallback
  (:mod:`unknown_agent.onboarding.flow`), which asks exactly the same questions
  in the same order without needing a full-screen UI.

Flags (``--provider``, ``--api-key-env``, ``--model``) still exist for scripts,
and when any of them is present the scripted path is used directly.  For a human
typing ``ua``, no flag and no extra command is ever required.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from unknown_agent.observability.logging import get_logger
from unknown_agent.ui.console import output

log = get_logger("onboarding.panel")

__all__ = ["launch_setup", "panel_available", "SetupOutcome"]


class SetupOutcome:
    """Normalised result shared by the panel and the fallback flow."""

    def __init__(self, *, ok: bool, payload: dict[str, Any] | None = None, error: str = "") -> None:
        self.ok = ok
        self.payload = payload or {}
        self.error = error

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.payload)
        data.setdefault("status", "configured" if self.ok else "cancelled")
        if self.error:
            data["error"] = self.error
        return data


def panel_available(runtime: Any = None) -> bool:
    """Can the full-screen panel run here?  (TTY on both ends + textual.)"""

    if runtime is not None and getattr(runtime, "non_interactive", False):
        return False
    if output.is_machine or not output.interactive:
        return False
    try:
        stdin_tty = sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):  # pragma: no cover - closed stdin
        stdin_tty = False
    if not stdin_tty:
        return False
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def _theme_key(runtime: Any) -> str | None:
    try:
        theme = runtime.load().settings.appearance.theme
    except Exception:  # noqa: BLE001 - appearance is cosmetic
        return None
    return None if not theme or theme == "auto" else str(theme)


def launch_setup(runtime: Any, *, scripted: Any = None, force_fallback: bool = False) -> SetupOutcome:
    """Configure a provider.  Panel first, guided prompts as the fallback.

    ``scripted`` is an :class:`~unknown_agent.onboarding.flow.OnboardingOptions`
    carrying CLI flags; when it holds any explicit value the scripted path runs so
    automation keeps working unchanged.
    """

    from unknown_agent.onboarding.flow import OnboardingOptions, run_onboarding

    options: OnboardingOptions = scripted or OnboardingOptions()
    has_flags = any(
        [options.provider, options.model, options.api_key, options.api_key_env, options.base_url, options.profile_name]
    )

    if not has_flags and not force_fallback and panel_available(runtime):
        from unknown_agent.tui.quicksetup import run_setup_panel

        try:
            result = asyncio.run(run_setup_panel(runtime, theme_key=_theme_key(runtime)))
        except KeyboardInterrupt:
            return SetupOutcome(ok=False, error="interrupted")
        except Exception as exc:  # noqa: BLE001 - never strand the user in a broken UI
            log.warning("setup panel failed (%s); falling back to guided prompts", exc)
            output.print(f"[ua.muted]the setup panel could not start ({exc}); continuing with guided prompts[/ua.muted]")
            return launch_setup(runtime, scripted=options, force_fallback=True)
        if result.cancelled:
            return SetupOutcome(ok=False, payload=result.to_dict(), error=result.error)
        _render_success(result)
        return SetupOutcome(ok=True, payload=result.to_dict())

    outcome = asyncio.run(run_onboarding(runtime, options=options, prompter=getattr(runtime, "prompter", None)))
    if outcome is None:
        return SetupOutcome(ok=False)
    return SetupOutcome(ok=True, payload=outcome.to_dict())


def _render_success(result: Any) -> None:
    """The clean final state, drawn after the full-screen app has exited."""

    from unknown_agent.onboarding import screens

    output.print(
        screens.success_panel(
            result.provider_label,
            result.model,
            models=len(result.models),
            config_path=result.config_path,
            next_steps=(
                'ua "build a landing page for my API"   # just start working',
                "ua setup            # change provider or model, same panel",
                "ua doctor           # verify everything end to end",
            ),
        )
    )
