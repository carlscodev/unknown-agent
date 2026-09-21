"""``ua web`` — serve the browser control panel; ``ua approve device`` — pair a browser.

One command starts everything: it creates the owner token on first run, prints it,
and serves the Vue app plus the API.  Browsers without the token request access and
show a 6-digit code; this terminal approves them with ``ua approve device <code>``.
The handshake travels through the shared auth file, so approval works while the
server is running — no restart needed.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from unknown_agent.config.paths import resolve_paths
from unknown_agent.web.auth import WebAuthStore

approve_app = typer.Typer(
    help="Approve things from the terminal: pair a browser to the web panel.",
    no_args_is_help=True,
)

console = Console()


def _auth_store() -> WebAuthStore:
    paths = resolve_paths()
    paths.ensure_user_dirs()
    return WebAuthStore(paths.config_dir / "web_auth.json")


def web_command(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address. 0.0.0.0 reaches phones/LAN."),
    port: int = typer.Option(8787, "--port", help="TCP port for the panel."),
    show_token: bool = typer.Option(
        False, "--show-token", help="Print the owner token even if one already exists."
    ),
) -> None:
    """Start the web control panel: one command, token printed, ready to pair devices."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - deps are core since 1.2.0
        console.print(
            f"[red]web panel dependencies missing:[/red] {exc}\n"
            "Install with: pip install 'unknown-agent[dashboard]'"
        )
        raise typer.Exit(1) from None

    from unknown_agent.web.server import create_app

    paths = resolve_paths()
    paths.ensure_user_dirs()
    auth_path = paths.config_dir / "web_auth.json"
    first_run = not auth_path.exists()
    auth = WebAuthStore(auth_path)
    token = auth.ensure_owner_token()

    scheme = "http"
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"{scheme}://{display_host}:{port}"
    lines = [
        f"[bold]panel[/bold]   {url}",
        "[bold]login[/bold]   paste the token below, or pair from the browser",
        "[bold]pair[/bold]    ua approve device <code shown in the browser>",
    ]
    if first_run or show_token:
        lines.insert(1, f"[bold]token[/bold]   {token}")
    console.print(
        Panel("\n".join(lines), title="Unknown Agent web", border_style="cyan", expand=False)
    )
    if not first_run and not show_token:
        console.print("[dim]token hidden — reprint with: ua web --show-token[/dim]")

    static_dir = Path(__file__).resolve().parent.parent.parent / "web" / "static"
    app = create_app(auth=auth, paths=paths, static_dir=static_dir)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\n[dim]web panel stopped[/dim]")


@approve_app.command("device")
def approve_device(
    code: str = typer.Argument(
        None, help="The 6-digit code shown in the browser. Omit to list pending requests."
    ),
    name: str = typer.Option("", "--name", "-n", help="Friendly label for the device."),
) -> None:
    """Approve a browser's pairing code so it can use the web panel."""
    auth = _auth_store()
    if not code:
        pending = auth.pending()
        if not pending:
            console.print("[dim]no pending device requests[/dim]")
            raise typer.Exit(0)
        for c, meta in sorted(pending.items()):
            console.print(f"[bold]{c}[/bold]  [dim]{(meta or {}).get('label', '')[:60]}[/dim]")
        raise typer.Exit(0)
    if auth.approve_device(code, name=name):
        console.print(f"[green]approved[/green] device {code} — its browser is in on next poll")
    else:
        console.print(f"[red]no pending request[/red] for code {code}")
        raise typer.Exit(1)


__all__ = ["approve_app", "web_command"]
