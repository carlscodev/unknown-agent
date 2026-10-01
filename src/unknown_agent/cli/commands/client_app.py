"""``ua client`` — serve the installable desktop/mobile PWA and sync API."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from unknown_agent.config.paths import resolve_paths
from unknown_agent.web.auth import WebAuthStore

console = Console()


def client_command(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8790, "--port", min=1, max=65535),
    ssl_certfile: Path | None = typer.Option(None, "--ssl-certfile"),
    ssl_keyfile: Path | None = typer.Option(None, "--ssl-keyfile"),
) -> None:
    """Start the encrypted installable client with enrollment and sync APIs."""
    if (ssl_certfile is None) != (ssl_keyfile is None):
        raise typer.BadParameter("--ssl-certfile and --ssl-keyfile must be supplied together")
    if ssl_certfile and (
        not ssl_certfile.is_file() or not ssl_keyfile or not ssl_keyfile.is_file()
    ):
        raise typer.BadParameter("TLS certificate or private key file does not exist")
    try:
        import uvicorn
    except ImportError:
        console.print("[red]client server dependency missing: uvicorn[/red]")
        raise typer.Exit(1) from None

    from unknown_agent.web.server import create_app

    paths = resolve_paths()
    paths.ensure_user_dirs()
    auth = WebAuthStore(paths.config_dir / "web_auth.json")
    auth.ensure_owner_token()
    static_dir = Path(__file__).resolve().parent.parent.parent / "client_app" / "static"
    if not (static_dir / "index.html").is_file():
        console.print("[red]packaged client assets are missing[/red]")
        raise typer.Exit(1)
    scheme = "https" if ssl_certfile else "http"
    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"{scheme}://{shown_host}:{port}"
    warning = (
        ""
        if ssl_certfile or shown_host in {"127.0.0.1", "localhost"}
        else "\n[yellow]Use TLS for remote/mobile enrollment.[/yellow]"
    )
    console.print(
        Panel(
            f"[bold]client[/bold]   {url}\n"
            f"[bold]enroll[/bold]   ua gateway device-enroll-issue{warning}",
            title="Unknown Agent client",
            border_style="cyan",
            expand=False,
        )
    )
    application = create_app(auth=auth, paths=paths, static_dir=static_dir)
    try:
        uvicorn.run(
            application,
            host=host,
            port=port,
            log_level="warning",
            ssl_certfile=str(ssl_certfile) if ssl_certfile else None,
            ssl_keyfile=str(ssl_keyfile) if ssl_keyfile else None,
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        console.print("\n[dim]client stopped[/dim]")


__all__ = ["client_command"]
