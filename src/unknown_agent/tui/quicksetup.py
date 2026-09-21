"""The guided setup panel: provider → key → validate → models → save.

This is the *default* configuration experience.  It exists because the previous
path asked a first-time user to know commands (``ua setup --provider ... --api-key-env ...``,
then ``ua models list``, then ``ua models use ...``).  Nobody should have to
memorise flags to connect a provider.

The flow is four panels and nothing else:

1. :class:`ProviderScreen` — a visible, searchable list of every provider that
   has a real adapter.  Arrow keys + Enter.
2. :class:`ApiKeyScreen` — a modal dialog with a masked input.  Pressing Enter
   **immediately** validates the key against the provider and, on success,
   fetches the live model catalog (OpenRouter's ``/models``, OpenAI's ``/models``,
   Anthropic's ``/models``, Ollama's ``/api/tags`` …).  Failures are explained in
   the dialog with a retry, never as a raw traceback.
3. :class:`ModelScreen` — the fetched models in a selector with a filter box and
   the metadata that decides the choice (context window, tools, vision, price).
4. :class:`ReviewScreen` — one confirm step, then the config is written and a
   clean success panel is shown.

Everything network-facing runs in a Textual worker so the UI never blocks, and
the secret only ever travels from the masked input to the 0600 credential store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from unknown_agent.config.credentials import CredentialStore
from unknown_agent.observability.logging import get_logger
from unknown_agent.providers.catalog import (
    CATALOG,
    ProviderEntry,
    known_model_ids,
    profile_name_for,
    resolve_entry,
)
from unknown_agent.providers.discovery import (
    ModelInfo,
    ProviderProbeError,
    fetch_models,
    validate_credential,
)
from unknown_agent.providers.auth import AuthMode, auth_modes_for, cli_session_state, detect_auth_conflicts
from unknown_agent.tui.themes import apply_to_app
from unknown_agent.tui.widgets import format_context, format_cost, rank_models

log = get_logger("tui.quicksetup")

__all__ = [
    "QuickSetupResult",
    "SetupApp",
    "ProviderScreen",
    "ApiKeyScreen",
    "ModelScreen",
    "ReviewScreen",
    "run_setup_panel",
    "save_provider_choice",
]


# --------------------------------------------------------------------------- #
# result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class QuickSetupResult:
    """What the panel produced.  ``cancelled`` means nothing was written."""

    cancelled: bool = True
    provider: str = ""          # profile name in config
    provider_type: str = ""     # catalog key
    provider_label: str = ""
    #: Which declared auth mode was configured (``chatgpt``/``api_key``/``routed``/…).
    auth_mode: str = ""
    model: str = ""
    base_url: str = ""
    credential_ref: str = ""
    credential_stored: bool = False
    models: list[str] = field(default_factory=list)
    models_source: str = ""     # "live" | "catalog"
    config_path: str = ""
    credential_path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "cancelled" if self.cancelled else "configured",
            "provider": self.provider,
            "provider_type": self.provider_type,
            "provider_label": self.provider_label,
            "auth_mode": self.auth_mode,
            "model": self.model,
            "base_url": self.base_url,
            "credential_ref": self.credential_ref,
            "credential_stored": self.credential_stored,
            "models": self.models[:200],
            "models_source": self.models_source,
            "config_path": self.config_path,
            "credential_path": self.credential_path,
            "error": self.error,
        }


def _fingerprint(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:3]}{'•' * 8}{value[-4:]}"


def _safe_name(value: str) -> str:
    cleaned = "".join(char if (char.isalnum() or char in "-_") else "-" for char in (value or "").strip().lower())
    return (cleaned.strip("-") or "provider")[:48]


# --------------------------------------------------------------------------- #
# persistence (shared with the CLI so there is exactly one save path)
# --------------------------------------------------------------------------- #
def save_provider_choice(
    runtime: Any,
    *,
    entry: ProviderEntry,
    model: str,
    api_key: str = "",
    credential_ref: str = "",
    base_url: str = "",
    models: Sequence[ModelInfo] = (),
    profile_name: str = "",
    auth_mode: str = "",
    store: CredentialStore | None = None,
) -> QuickSetupResult:
    """Write the provider profile + credential and return the result.

    The secret goes to the credential store (0600); the config only ever holds a
    reference (``store:openrouter`` / ``env:OPENROUTER_API_KEY``).
    """

    from unknown_agent.config.models import ProviderConfig

    settings = runtime.load(force=True).settings
    store = store or CredentialStore()
    name = profile_name or profile_name_for(entry, dict(settings.providers or {}))

    values: dict[str, Any] = dict(entry.profile_defaults)
    values["model"] = model or values.get("model") or ""
    if base_url:
        values["base_url"] = base_url
    if auth_mode:
        # Recorded so later runs report the truth (subscription vs key) instead of
        # inferring it from whether a credential happens to be present.
        values["auth_mode"] = auth_mode

    chosen = next((item for item in models if item.id == model), None)
    if chosen is not None:
        if chosen.context_window:
            values["context_window"] = int(chosen.context_window)
        if chosen.max_output_tokens:
            values["max_output_tokens"] = int(chosen.max_output_tokens)
        if chosen.supports_tools is not None:
            values["supports_tools"] = bool(chosen.supports_tools)
        if chosen.supports_vision:
            values["supports_vision"] = True
        if chosen.input_cost_per_mtok is not None:
            values["input_cost_per_mtok"] = chosen.input_cost_per_mtok
        if chosen.output_cost_per_mtok is not None:
            values["output_cost_per_mtok"] = chosen.output_cost_per_mtok

    stored = False
    reference = credential_ref
    if api_key:
        secret_name = _safe_name(name or entry.key)
        store.set(secret_name, api_key)
        reference = f"store:{secret_name}"
        stored = True
    if reference:
        values["api_key"] = reference
    else:
        values.pop("api_key", None)

    profile = ProviderConfig(**values)
    providers = dict(settings.providers or {})
    providers[name] = profile
    # Drop the stock placeholder profile: after a real setup it is only noise.
    for stale in [key for key, item in providers.items() if key != name and getattr(item, "is_placeholder", False)]:
        providers.pop(stale, None)
    settings.providers = providers
    settings.provider = name

    config_path = ""
    writer = getattr(runtime, "save_settings", None)
    if callable(writer):
        config_path = str(writer(settings))

    log.info("setup panel wrote profile=%s provider=%s model=%s", name, entry.key, model)
    return QuickSetupResult(
        cancelled=False,
        provider=name,
        provider_type=entry.key,
        provider_label=entry.label,
        auth_mode=auth_mode,
        model=model,
        base_url=base_url,
        credential_ref=reference,
        credential_stored=stored,
        models=[item.id for item in models],
        config_path=config_path,
        credential_path=str(store.path) if stored else "",
    )


# --------------------------------------------------------------------------- #
# screen 1 — provider selector
# --------------------------------------------------------------------------- #
class ProviderScreen(Screen[None]):
    """A visible list of supported providers, filterable, Enter to choose."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+c", "cancel", "Quit", show=False),
    ]

    CSS = """
    ProviderScreen { align: center middle; }
    #provider-card {
        width: 96;
        max-width: 96%;
        height: auto;
        max-height: 92%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #setup-title { text-style: bold; color: $primary; height: 1; }
    #setup-sub { color: $text-muted; height: auto; margin-bottom: 1; }
    #provider-filter { margin-bottom: 1; }
    #provider-list { height: 16; border: round $panel-lighten-2 50%; background: $surface; }
    #provider-detail {
        height: auto; min-height: 3; margin-top: 1; padding: 0 1;
        color: $text-muted; border-left: thick $primary 60%;
    }
    """

    def __init__(self, setup_app: SetupApp) -> None:
        super().__init__()
        self.setup = setup_app
        self._entries: list[ProviderEntry] = list(CATALOG)

    def compose(self) -> ComposeResult:
        with Vertical(id="provider-card"):
            yield Static("Unknown Agent · setup", id="setup-title")
            yield Static(
                "Step 1 of 3 — choose your provider.  Type to filter, ↑↓ to move, Enter to select.",
                id="setup-sub",
            )
            yield Input(placeholder="filter providers …", id="provider-filter")
            yield OptionList(id="provider-list")
            yield Static("", id="provider-detail")
        yield Footer()

    def on_mount(self) -> None:
        self._reload("")
        self.query_one("#provider-filter", Input).focus()

    # -- data ------------------------------------------------------------- #
    def _reload(self, query: str) -> None:
        text = query.strip().lower()
        entries = [
            entry
            for entry in CATALOG
            if not text or text in entry.key.lower() or text in entry.label.lower() or text in entry.notes.lower()
        ]
        self._entries = entries
        listing = self.query_one("#provider-list", OptionList)
        listing.clear_options()
        for entry in entries:
            listing.add_option(Option(self._row(entry), id=entry.key))
        if entries:
            listing.highlighted = 0
            self._detail(entries[0])
        else:
            self.query_one("#provider-detail", Static).update("[yellow]no provider matches that filter[/]")

    @staticmethod
    def _row(entry: ProviderEntry) -> str:
        badges = []
        if entry.local:
            badges.append("[green]local[/]")
        if not entry.api_key_required:
            badges.append("[green]no key needed[/]")
        if entry.model_listing != "none":
            badges.append("[cyan]live model list[/]")
        if entry.supports_tools:
            badges.append("[dim]tools[/]")
        return f"[bold]{entry.label:<22}[/] " + "  ".join(badges)

    def _detail(self, entry: ProviderEntry) -> None:
        lines = [entry.notes or f"{entry.label} via the {entry.protocol} dialect."]
        lines.append(f"[dim]→[/] endpoint: {entry.base_url or 'you provide it'}")
        lines.append(
            f"[dim]→[/] key: {'not required' if not entry.api_key_required else entry.env_var}"
            f"   ·   models: {'fetched from the API' if entry.model_listing != 'none' else 'curated list'}"
        )
        if entry.docs_url:
            lines.append(f"[dim]→ get a key: {entry.docs_url}[/]")
        self.query_one("#provider-detail", Static).update("\n".join(lines))

    def _entry_at(self, option_id: str | None) -> ProviderEntry | None:
        return resolve_entry(str(option_id)) if option_id else None

    # -- events ------------------------------------------------------------ #
    @on(Input.Changed, "#provider-filter")
    def _filter(self, event: Input.Changed) -> None:
        self._reload(event.value)

    @on(Input.Submitted, "#provider-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        if self._entries:
            self._choose(self._entries[0])

    @on(OptionList.OptionHighlighted, "#provider-list")
    def _highlight(self, event: OptionList.OptionHighlighted) -> None:
        entry = self._entry_at(event.option.id)
        if entry is not None:
            self._detail(entry)

    @on(OptionList.OptionSelected, "#provider-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        entry = self._entry_at(event.option.id)
        if entry is not None:
            self._choose(entry)

    def _choose(self, entry: ProviderEntry) -> None:
        self.setup.entry = entry
        # Providers declare their own auth modes; never assume "paste a key".
        self.setup.begin_auth_mode()

    def action_cancel(self) -> None:
        self.setup.cancel()


# --------------------------------------------------------------------------- #
# screen 2 — API key dialog
# --------------------------------------------------------------------------- #
class ApiKeyScreen(ModalScreen[None]):
    """Masked key entry.  Enter → validate → fetch models, all in this dialog."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+c", "quit_setup", "Quit", show=False),
    ]

    CSS = """
    ApiKeyScreen { align: center middle; background: $background 70%; }
    #key-dialog {
        width: 82; max-width: 96%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    #key-title { text-style: bold; color: $primary; height: 1; }
    #key-help { color: $text-muted; height: auto; margin-bottom: 1; }
    #key-field { margin-bottom: 1; }
    #key-status { height: auto; min-height: 2; margin-bottom: 1; }
    #key-buttons Button { margin-right: 1; }
    #key-busy { height: 1; display: none; }
    #key-busy.active { display: block; }
    """

    def __init__(self, setup_app: SetupApp) -> None:
        super().__init__()
        self.setup = setup_app
        self.busy = False

    @property
    def entry(self) -> ProviderEntry:
        assert self.setup.entry is not None
        return self.setup.entry

    def compose(self) -> ComposeResult:
        entry = self.entry
        env_value = os.environ.get(entry.env_var, "") if entry.env_var else ""
        with Vertical(id="key-dialog"):
            yield Static(f"{entry.label} · API key", id="key-title")
            yield Static(
                "Step 2 of 3 — paste your key and press Enter.  It is validated straight away and the model list is "
                "fetched for you.  The key is masked, stored with 0600 permissions, and never written to config or logs."
                + (f"\nGet one at {entry.docs_url}" if entry.docs_url else ""),
                id="key-help",
            )
            yield Input(placeholder=f"paste your {entry.label} API key", password=True, id="key-field")
            yield LoadingIndicator(id="key-busy")
            yield Static(
                f"[dim]found ${entry.env_var} in your environment — press “use environment key” to keep it there[/]"
                if env_value
                else "",
                id="key-status",
            )
            with Horizontal(id="key-buttons"):
                yield Button("connect", id="key-connect", variant="primary")
                if env_value:
                    yield Button(f"use ${entry.env_var}", id="key-env", variant="success")
                yield Button("back", id="key-back")

    def on_mount(self) -> None:
        self.query_one("#key-field", Input).focus()

    # -- feedback ---------------------------------------------------------- #
    def _status(self, markup: str) -> None:
        self.query_one("#key-status", Static).update(markup)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.query_one("#key-busy", LoadingIndicator).set_class(busy, "active")
        for button in self.query(Button):
            button.disabled = busy
        self.query_one("#key-field", Input).disabled = busy

    # -- events ------------------------------------------------------------ #
    @on(Input.Submitted, "#key-field")
    def _submitted(self, event: Input.Submitted) -> None:
        self._connect(event.value.strip())

    @on(Button.Pressed, "#key-connect")
    def _connect_pressed(self, event: Button.Pressed) -> None:
        self._connect(self.query_one("#key-field", Input).value.strip())

    @on(Button.Pressed, "#key-env")
    def _use_env(self, event: Button.Pressed) -> None:
        value = os.environ.get(self.entry.env_var, "")
        if not value:
            self._status("[red]✕ that environment variable is empty now[/]")
            return
        self.setup.credential_ref = f"env:{self.entry.env_var}"
        self._connect(value, store_key=False)

    @on(Button.Pressed, "#key-back")
    def _back_pressed(self, event: Button.Pressed) -> None:
        self.action_back()

    def action_back(self) -> None:
        if not self.busy:
            self.setup.back_to_providers()

    def action_quit_setup(self) -> None:
        self.setup.cancel()

    # -- the one interaction that matters ---------------------------------- #
    def _connect(self, key: str, *, store_key: bool = True) -> None:
        if self.busy:
            return
        entry = self.entry
        key = key.strip().strip("'\"")
        if entry.api_key_required and not key:
            self._status("[red]✕ paste your API key first — nothing has been sent anywhere[/]")
            return
        if key and len(key) < 8:
            self._status(f"[red]✕ that is too short to be an API key ({len(key)} characters)[/]")
            return
        self.setup.api_key = key if store_key else ""
        self.setup.env_key = "" if store_key else key
        self._status(f"[dim]⟳ contacting {entry.label} …[/]")
        self._set_busy(True)
        self._validate_and_fetch(key)

    @work(exclusive=True)
    async def _validate_and_fetch(self, key: str) -> None:
        entry = self.entry
        base_url = self.setup.base_url or None
        try:
            result = await validate_credential(entry, api_key=key or None, base_url=base_url)
        except Exception as exc:  # noqa: BLE001 - never show a traceback to a user
            self._fail("Could not reach the provider.", f"{type(exc).__name__}: {exc}")
            return
        if not result.ok:
            self._fail(result.message, result.hint or "Copy the key again from the provider dashboard and retry.")
            return

        self._status(f"[green]✓ key accepted[/] · [dim]fetching the models {entry.label} offers …[/]")
        models: list[ModelInfo] = []
        source = "live"
        try:
            models = list(await fetch_models(entry, api_key=key or None, base_url=base_url))
        except ProviderProbeError as exc:
            log.debug("model listing failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.debug("model listing crashed: %s", exc)
        if not models:
            source = "catalog"
            models = [ModelInfo(id=mid, name=mid, provider=entry.key) for mid in known_model_ids(entry)]
        if not models:
            self._fail(
                f"{entry.label} accepted the key but returned no models.",
                "Pick a different provider, or set a base URL for your deployment.",
            )
            return

        self.setup.models = models
        self.setup.models_source = source
        self.setup.validation_message = result.message
        if key and self.setup.api_key:
            self.setup.credential_ref = ""  # will be written as store:<profile>
        self._set_busy(False)
        self.setup.show_models()

    def _fail(self, message: str, hint: str) -> None:
        self._set_busy(False)
        self._status(f"[bold red]✕ {message}[/]\n[dim]{hint}[/]\n[dim]Edit the key above and press Enter to try again.[/]")
        field = self.query_one("#key-field", Input)
        field.disabled = False
        field.focus()


# --------------------------------------------------------------------------- #
# screen 3 — model selector
# --------------------------------------------------------------------------- #
class ModelScreen(Screen[None]):
    """The fetched catalog, ranked and filterable, Enter to select."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+c", "cancel", "Quit", show=False),
    ]

    CSS = """
    ModelScreen { align: center middle; }
    #model-card {
        width: 100; max-width: 96%; height: auto; max-height: 92%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    #model-title { text-style: bold; color: $primary; height: 1; }
    #model-sub { color: $text-muted; height: auto; margin-bottom: 1; }
    #model-filter { margin-bottom: 1; }
    #model-list { height: 16; border: round $panel-lighten-2 50%; background: $surface; }
    #model-detail {
        height: auto; min-height: 3; margin-top: 1; padding: 0 1;
        color: $text-muted; border-left: thick $primary 60%;
    }
    """

    def __init__(self, setup_app: SetupApp) -> None:
        super().__init__()
        self.setup = setup_app
        self._shown: list[ModelInfo] = []

    def compose(self) -> ComposeResult:
        entry = self.setup.entry
        label = entry.label if entry else "provider"
        source = "fetched live from the provider" if self.setup.models_source == "live" else "from the built-in catalog"
        with Vertical(id="model-card"):
            yield Static(f"{label} · choose a model", id="model-title")
            yield Static(
                f"Step 3 of 3 — {len(self.setup.models)} models {source}.  "
                "Tool-capable models are listed first.  Type to filter, Enter to select.",
                id="model-sub",
            )
            yield Input(placeholder="filter models …", id="model-filter")
            yield OptionList(id="model-list")
            yield Static("", id="model-detail")
        yield Footer()

    def on_mount(self) -> None:
        self._reload("")
        self.query_one("#model-filter", Input).focus()

    def _reload(self, query: str) -> None:
        ranked = rank_models(self.setup.models, query=query)
        self._shown = ranked
        listing = self.query_one("#model-list", OptionList)
        listing.clear_options()
        for info in ranked[:400]:
            listing.add_option(Option(self._row(info), id=info.id))
        if ranked:
            listing.highlighted = 0
            self._detail(ranked[0])
        else:
            self.query_one("#model-detail", Static).update("[yellow]nothing matches that filter[/]")

    @staticmethod
    def _row(info: ModelInfo) -> str:
        bits = [format_context(info.context_window)]
        if info.supports_tools:
            bits.append("[green]tools[/]")
        if info.supports_vision:
            bits.append("[cyan]vision[/]")
        if info.supports_reasoning:
            bits.append("[magenta]reasoning[/]")
        price = format_cost(info.input_cost_per_mtok, info.output_cost_per_mtok)
        if price:
            bits.append(f"[dim]{price}[/]")
        return f"[bold]{info.id}[/]\n   [dim]" + "  ·  ".join(str(bit) for bit in bits if bit) + "[/]"

    def _detail(self, info: ModelInfo) -> None:
        lines = [info.name or info.id]
        if info.context_window:
            lines.append(f"[dim]→[/] context window: {info.context_window:,} tokens")
        if info.max_output_tokens:
            lines.append(f"[dim]→[/] max output: {info.max_output_tokens:,} tokens")
        lines.append(
            f"[dim]→[/] tools: {'yes' if info.supports_tools else 'unknown/no'}   ·   "
            f"vision: {'yes' if info.supports_vision else 'no'}"
        )
        self.query_one("#model-detail", Static).update("\n".join(lines))

    def _info(self, model_id: str | None) -> ModelInfo | None:
        return next((item for item in self.setup.models if item.id == model_id), None)

    @on(Input.Changed, "#model-filter")
    def _filter(self, event: Input.Changed) -> None:
        self._reload(event.value)

    @on(Input.Submitted, "#model-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        if self._shown:
            self._pick(self._shown[0].id)

    @on(OptionList.OptionHighlighted, "#model-list")
    def _highlight(self, event: OptionList.OptionHighlighted) -> None:
        info = self._info(str(event.option.id))
        if info is not None:
            self._detail(info)

    @on(OptionList.OptionSelected, "#model-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self._pick(str(event.option.id))

    def _pick(self, model_id: str) -> None:
        self.setup.model = model_id
        self.setup.show_review()

    def action_back(self) -> None:
        self.setup.begin_credentials()

    def action_cancel(self) -> None:
        self.setup.cancel()


# --------------------------------------------------------------------------- #
# screen 4 — review / save / success
# --------------------------------------------------------------------------- #
class ReviewScreen(Screen[None]):
    """Confirm, save, and show the final success state."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+c", "cancel", "Quit", show=False),
    ]

    CSS = """
    ReviewScreen { align: center middle; }
    #review-card {
        width: 88; max-width: 96%; height: auto;
        border: round $success; background: $surface; padding: 1 2;
    }
    #review-title { text-style: bold; color: $primary; height: 1; margin-bottom: 1; }
    #review-body { height: auto; margin-bottom: 1; }
    #review-buttons Button { margin-right: 1; }
    """

    def __init__(self, setup_app: SetupApp) -> None:
        super().__init__()
        self.setup = setup_app
        self.saved = False

    def compose(self) -> ComposeResult:
        with Vertical(id="review-card"):
            yield Static("Review and save", id="review-title")
            yield Static(self._summary(), id="review-body")
            with Horizontal(id="review-buttons"):
                yield Button("save and finish", id="review-save", variant="primary")
                yield Button("change model", id="review-back")
                yield Button("cancel", id="review-cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#review-save", Button).focus()

    def _summary(self) -> str:
        setup = self.setup
        entry = setup.entry
        key_line = "not required"
        if setup.api_key:
            key_line = f"stored securely (0600) · {_fingerprint(setup.api_key)}"
        elif setup.credential_ref.startswith("env:"):
            key_line = f"read from ${setup.credential_ref.split(':', 1)[1]} at call time"
        rows = [
            ("provider", entry.label if entry else "—"),
            ("model", setup.model or "—"),
            ("credential", key_line),
            ("models found", f"{len(setup.models)} ({setup.models_source})"),
        ]
        if setup.base_url:
            rows.insert(1, ("endpoint", setup.base_url))
        body = "\n".join(f"[dim]{name:<14}[/] [bold]{value}[/]" for name, value in rows)
        return body + "\n\n[dim]Press “save and finish” — the config is written and the agent is ready.[/]"

    @on(Button.Pressed, "#review-save")
    def _save(self, event: Button.Pressed) -> None:
        self.setup.save_and_exit()

    @on(Button.Pressed, "#review-back")
    def _back(self, event: Button.Pressed) -> None:
        self.setup.show_models()

    @on(Button.Pressed, "#review-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        self.setup.cancel()

    def action_back(self) -> None:
        self.setup.show_models()

    def action_cancel(self) -> None:
        self.setup.cancel()


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# screen 1b — how do you want to authenticate?
# --------------------------------------------------------------------------- #
class AuthModeScreen(Screen[None]):
    """Choose *how* to authenticate, from the modes this provider declares.

    This is the screen the old flow lacked entirely.  It assumed every provider was an
    API key, which is wrong for Codex (a ChatGPT subscription is a different product
    with different billing), for routed access, and for self-hosted endpoints.  Each
    mode shows what it bills, what it needs, and what commonly goes wrong with it, so
    the choice is made with the consequences visible.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+c", "cancel", "Quit", show=False),
    ]

    CSS = """
    AuthModeScreen { align: center middle; }
    #auth-card {
        width: 96; max-width: 96%; height: auto; max-height: 92%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    #auth-title { text-style: bold; color: $primary; height: 1; }
    #auth-sub { color: $text-muted; height: auto; margin-bottom: 1; }
    #auth-list { height: auto; max-height: 12; border: round $panel-lighten-2 50%; background: $surface; }
    #auth-detail {
        height: auto; min-height: 5; margin-top: 1; padding: 0 1;
        color: $text-muted; border-left: thick $primary 60%;
    }
    """

    def __init__(self, setup_app: "SetupApp") -> None:
        super().__init__()
        self.setup = setup_app
        self.modes: list[AuthMode] = list(auth_modes_for(setup_app.entry))

    def compose(self) -> ComposeResult:
        label = getattr(self.setup.entry, "label", "this provider")
        with Vertical(id="auth-card"):
            yield Static("Unknown Agent · setup", id="auth-title")
            yield Static(
                f"Step 2 — how should Unknown Agent authenticate with {label}?  "
                "↑↓ to move, Enter to choose, Esc to go back.",
                id="auth-sub",
            )
            yield OptionList(id="auth-list")
            yield Static("", id="auth-detail")
        yield Footer()

    def on_mount(self) -> None:
        listing = self.query_one("#auth-list", OptionList)
        for mode in self.modes:
            line = f"{mode.label}"
            if mode.recommended:
                line += "   (recommended)"
            listing.add_option(Option(line, id=mode.key))
        if self.modes:
            listing.highlighted = 0
            self._describe(self.modes[0])
        listing.focus()

    def _describe(self, mode: AuthMode) -> None:
        lines = [mode.summary]
        if mode.billing:
            lines.append(f"Billing: {mode.billing}")
        if mode.kind == "subscription" and mode.cli_command:
            state, detail = cli_session_state(mode)
            glyph = {"ok": "✓", "login_required": "○", "missing": "✕"}.get(state, "·")
            lines.append(f"{glyph} {detail}")
        conflicts = detect_auth_conflicts(self.setup.entry, mode)
        for conflict in conflicts:
            lines.append(f"{'✕' if conflict.severity == 'error' else '▲'} {conflict.title} — {conflict.detail}")
            if conflict.fix:
                lines.append(f"    fix: {conflict.fix}")
        for caveat in mode.caveats:
            lines.append(f"· {caveat}")
        self.query_one("#auth-detail", Static).update("\n".join(line for line in lines if line))

    @on(OptionList.OptionHighlighted, "#auth-list")
    def _highlight(self, event: OptionList.OptionHighlighted) -> None:
        index = event.option_index
        if 0 <= index < len(self.modes):
            self._describe(self.modes[index])

    @on(OptionList.OptionSelected, "#auth-list")
    def _select(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if 0 <= index < len(self.modes):
            self.setup.choose_auth_mode(self.modes[index])

    def action_back(self) -> None:
        self.setup.back_to_providers()

    def action_cancel(self) -> None:
        self.setup.action_hard_cancel()




# --------------------------------------------------------------------------- #
# screen 2b — subscription sign-in (Codex / ChatGPT)
# --------------------------------------------------------------------------- #
class SubscriptionScreen(Screen[None]):
    """Launch and verify an account login owned by an external CLI.

    There is no key to paste here.  The session belongs to the provider's own CLI, so
    this screen checks it, runs the login command for the user, and re-checks — and it
    refuses to continue on an unverified session, because "saved" must mean "tested".
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+c", "cancel", "Quit", show=False),
    ]

    CSS = """
    SubscriptionScreen { align: center middle; }
    #sub-card {
        width: 88; max-width: 96%; height: auto; border: round $primary;
        background: $surface; padding: 1 2;
    }
    #sub-title { text-style: bold; color: $primary; height: 1; }
    #sub-body { height: auto; color: $text-muted; margin-bottom: 1; }
    #sub-status { height: auto; min-height: 2; margin-bottom: 1; }
    #sub-buttons { height: auto; align-horizontal: right; }
    #sub-buttons Button { margin-left: 1; }
    """

    def __init__(self, setup_app: "SetupApp") -> None:
        super().__init__()
        self.setup = setup_app
        self.mode: AuthMode = setup_app.auth_mode  # type: ignore[assignment]
        self.session_ok = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sub-card"):
            yield Static("Unknown Agent · setup", id="sub-title")
            yield Static(
                f"{self.mode.label}\n{self.mode.summary}\nBilling: {self.mode.billing}",
                id="sub-body",
            )
            yield Static("", id="sub-status")
            with Horizontal(id="sub-buttons"):
                yield Button("check again", id="sub-check")
                yield Button("sign in", id="sub-login", variant="primary")
                yield Button("continue", id="sub-continue", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self._check()

    @work(thread=True, exclusive=True)
    def _check(self) -> None:
        """Probe the CLI session on a thread; it shells out and can be slow."""

        state, detail = cli_session_state(self.mode)
        conflicts = detect_auth_conflicts(self.setup.entry, self.mode)
        self.app.call_from_thread(self._render_check, state, detail, conflicts)

    def _render_check(self, state: str, detail: str, conflicts: list) -> None:
        self.session_ok = state == "ok"
        lines = []
        if state == "ok":
            lines.append(f"[green]✓ signed in[/] — {detail}")
        elif state == "missing":
            lines.append(f"[red]✕ {detail}[/]")
            lines.append("Install it, or press Esc and pick an API-key mode instead.")
        else:
            lines.append(f"[yellow]○ {detail}[/]")
            lines.append("Press [b]sign in[/b]; the provider's CLI will take over the terminal briefly.")
        for conflict in conflicts:
            colour = "red" if conflict.severity == "error" else "yellow"
            lines.append(f"[{colour}]▲ {conflict.title}[/] — {conflict.detail}")
            if conflict.fix:
                lines.append(f"    fix: {conflict.fix}")
        self.query_one("#sub-status", Static).update("\n".join(lines))
        self.query_one("#sub-continue", Button).disabled = not self.session_ok

    @on(Button.Pressed, "#sub-check")
    def _recheck(self) -> None:
        self.query_one("#sub-status", Static).update("checking…")
        self._check()

    @on(Button.Pressed, "#sub-login")
    def _login(self) -> None:
        """Hand the terminal to the provider's CLI for an interactive login."""

        import shutil
        import subprocess

        binary = shutil.which(self.mode.cli_command)
        if not binary:
            self.query_one("#sub-status", Static).update(f"[red]✕ `{self.mode.cli_command}` is not installed[/]")
            return
        with self.app.suspend():
            subprocess.run([binary, *(self.mode.cli_login_args or ("login",))], check=False)  # noqa: S603
        self.query_one("#sub-status", Static).update("checking the session…")
        self._check()

    @on(Button.Pressed, "#sub-continue")
    def _continue(self) -> None:
        if self.session_ok:
            # No credential to store: the CLI owns the session.
            self.setup.api_key = ""
            self.setup.credential_ref = ""
            self.setup.begin_credentials()

    def action_back(self) -> None:
        self.setup.begin_auth_mode()

    def action_cancel(self) -> None:
        self.setup.action_hard_cancel()



class SetupApp(App[QuickSetupResult]):
    """Four panels, one flow, zero commands."""

    TITLE = "Unknown Agent · setup"
    SUB_TITLE = "provider → key → model"

    BINDINGS = [Binding("ctrl+c", "hard_cancel", "Quit", priority=True, show=False)]

    def __init__(self, runtime: Any = None, *, theme_key: str | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.theme_key = theme_key
        self.entry: ProviderEntry | None = None
        #: Which declared auth mode the user picked; drives every screen after it.
        self.auth_mode: AuthMode | None = None
        self.api_key: str = ""
        self.env_key: str = ""
        self.credential_ref: str = ""
        self.base_url: str = ""
        self.models: list[ModelInfo] = []
        self.models_source: str = ""
        self.model: str = ""
        self.validation_message: str = ""
        self.result = QuickSetupResult()

    def on_mount(self) -> None:
        try:
            apply_to_app(self, self.theme_key)
        except Exception:  # pragma: no cover - theme is cosmetic, never fatal
            log.debug("could not apply theme %r", self.theme_key)
        self.push_screen(ProviderScreen(self))

    # -- navigation -------------------------------------------------------- #
    def back_to_providers(self) -> None:
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if len(self.screen_stack) > 1 and not isinstance(self.screen, ProviderScreen):
            self.pop_screen()

    def begin_auth_mode(self) -> None:
        """After a provider is chosen, ask *how* to authenticate.

        A provider with exactly one mode is not worth a screen, so that case skips
        straight through — but the mode is still recorded, so the config always says
        which path is in use instead of leaving it implied.
        """

        entry = self.entry
        if entry is None:
            return
        modes = auth_modes_for(entry)
        if len(modes) <= 1:
            self.auth_mode = modes[0] if modes else None
            self.begin_credentials()
            return
        self._replace_with(AuthModeScreen(self))

    def choose_auth_mode(self, mode: AuthMode) -> None:
        """Record the mode and route to the setup flow that mode actually needs."""

        self.auth_mode = mode
        if mode.default_base_url and not self.base_url:
            self.base_url = mode.default_base_url
        if mode.kind == "subscription":
            self._replace_with(SubscriptionScreen(self))
            return
        self.begin_credentials()

    def begin_credentials(self) -> None:
        entry = self.entry
        mode = self.auth_mode
        if entry is None:
            return
        if mode is not None and mode.requires_base_url and not self.base_url:
            self.push_screen(BaseUrlScreen(self))
            return
        if mode is not None and mode.kind == "none":
            self.api_key = ""
            self.credential_ref = ""
            self._fetch_keyless()
            return
        if entry.requires_base_url and not self.base_url:
            # Resource-specific endpoints (Azure, self-hosted) are asked for in the
            # same dialog family rather than as a separate command.
            self.push_screen(BaseUrlScreen(self))
            return
        if not entry.api_key_required:
            self.api_key = ""
            self.credential_ref = ""
            self._fetch_keyless()
            return
        self._replace_with(ApiKeyScreen(self))

    def show_models(self) -> None:
        self._replace_with(ModelScreen(self))

    def show_review(self) -> None:
        self._replace_with(ReviewScreen(self))

    def _replace_with(self, screen: Screen[None]) -> None:
        # Keep the provider screen at the bottom of the stack so Escape always has
        # somewhere honest to go back to.
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen)

    # -- keyless providers (Ollama, offline) -------------------------------- #
    @work(exclusive=True)
    async def _fetch_keyless(self) -> None:
        entry = self.entry
        if entry is None:
            return
        models: list[ModelInfo] = []
        try:
            models = list(await fetch_models(entry, base_url=self.base_url or None))
            self.models_source = "live"
        except Exception:  # noqa: BLE001 - local servers are often not running yet
            models = []
        if not models:
            models = [ModelInfo(id=mid, name=mid, provider=entry.key) for mid in known_model_ids(entry)]
            self.models_source = "catalog"
        self.models = models
        self.show_models()

    # -- exits -------------------------------------------------------------- #
    def save_and_exit(self) -> None:
        entry = self.entry
        if entry is None or not self.model:
            self.cancel()
            return
        if self.runtime is None:
            self.result = QuickSetupResult(
                cancelled=False,
                provider=entry.key,
                provider_type=entry.key,
                provider_label=entry.label,
                model=self.model,
                base_url=self.base_url,
                credential_ref=self.credential_ref,
                models=[item.id for item in self.models],
                models_source=self.models_source,
            )
            self.exit(self.result)
            return
        try:
            result = save_provider_choice(
                self.runtime,
                entry=entry,
                model=self.model,
                api_key=self.api_key,
                credential_ref=self.credential_ref,
                base_url=self.base_url,
                models=self.models,
                auth_mode=self.auth_mode.key if self.auth_mode else "",
            )
        except Exception as exc:  # noqa: BLE001 - surface it in the panel, not as a crash
            log.warning("setup save failed: %s", exc)
            self.result = QuickSetupResult(cancelled=True, error=str(exc))
            self.exit(self.result)
            return
        result.models_source = self.models_source
        self.result = result
        self.exit(result)

    def cancel(self) -> None:
        self.result = QuickSetupResult(cancelled=True)
        self.exit(self.result)

    def action_hard_cancel(self) -> None:
        self.cancel()


class BaseUrlScreen(ModalScreen[None]):
    """Providers whose endpoint is user-owned (Azure, vLLM, gateways)."""

    BINDINGS = [Binding("escape", "back", "Back")]

    CSS = """
    BaseUrlScreen { align: center middle; background: $background 70%; }
    #url-dialog { width: 78; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #url-title { text-style: bold; color: $primary; height: 1; }
    #url-help { color: $text-muted; height: auto; margin-bottom: 1; }
    """

    def __init__(self, setup_app: SetupApp) -> None:
        super().__init__()
        self.setup = setup_app

    def compose(self) -> ComposeResult:
        entry = self.setup.entry
        with Vertical(id="url-dialog"):
            yield Static(f"{entry.label if entry else 'Provider'} · endpoint", id="url-title")
            yield Static(
                "This provider is hosted on your own resource, so it needs your base URL.\n"
                "Example: https://my-resource.openai.azure.com/openai  ·  http://localhost:8000/v1",
                id="url-help",
            )
            yield Input(placeholder="https://…/v1", id="url-field")
            yield Label("", id="url-status")

    def on_mount(self) -> None:
        self.query_one("#url-field", Input).focus()

    @on(Input.Submitted, "#url-field")
    def _submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value.startswith(("http://", "https://")):
            self.query_one("#url-status", Label).update("[red]✕ that must start with http:// or https://[/]")
            return
        self.setup.base_url = value
        self.setup.begin_credentials()

    def action_back(self) -> None:
        self.setup.back_to_providers()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
async def run_setup_panel(runtime: Any = None, *, theme_key: str | None = None) -> QuickSetupResult:
    """Run the interactive setup panel and return its result."""

    app = SetupApp(runtime, theme_key=theme_key)
    result = await app.run_async()
    return result or app.result


# --------------------------------------------------------------------------- #
# changing your mind later: the same model panel, on its own
# --------------------------------------------------------------------------- #
class ModelPickerApp(App[str]):
    """Re-open just the model selector for an already-configured provider.

    Switching models should not mean remembering an id and typing
    ``ua models use meta-llama/llama-3.3-70b-instruct``.  This reuses the exact
    screen setup uses, so the experience is the same the second time.
    """

    TITLE = "Unknown Agent · model"

    BINDINGS = [Binding("ctrl+c", "cancel", "Quit", priority=True, show=False)]

    def __init__(
        self,
        entry: ProviderEntry,
        models: Sequence[ModelInfo],
        *,
        current: str = "",
        source: str = "live",
        theme_key: str | None = None,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.models = list(models)
        self.models_source = source
        self.model = current
        self.base_url = ""
        self.theme_key = theme_key
        self.picked = ""

    def on_mount(self) -> None:
        try:
            apply_to_app(self, self.theme_key)
        except Exception:  # pragma: no cover - cosmetic
            log.debug("could not apply theme %r", self.theme_key)
        self.push_screen(ModelScreen(self))

    # ``ModelScreen`` calls back into its owner with these three hooks.
    def show_review(self) -> None:
        self.picked = self.model
        self.exit(self.model)

    def begin_credentials(self) -> None:
        self.cancel()

    def cancel(self) -> None:
        self.picked = ""
        self.exit("")

    def action_cancel(self) -> None:
        self.cancel()


async def run_model_picker(
    entry: ProviderEntry,
    models: Sequence[ModelInfo],
    *,
    current: str = "",
    source: str = "live",
    theme_key: str | None = None,
) -> str:
    """Show the model selector alone.  Returns ``""`` when cancelled."""

    app = ModelPickerApp(entry, models, current=current, source=source, theme_key=theme_key)
    result = await app.run_async()
    return result or app.picked
