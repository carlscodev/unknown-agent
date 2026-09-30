"""``ua release`` — reproducible SBOM and signed provenance commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from unknown_agent.cli.runtime import Runtime
from unknown_agent.errors import UsageError
from unknown_agent.release import (
    create_attestation,
    generate_spdx,
    inventory,
    verify_attestation,
)

app = typer.Typer(name="release", no_args_is_help=True)


def _runtime(ctx: typer.Context) -> Runtime:
    if not isinstance(ctx.obj, Runtime):
        raise UsageError("CLI runtime is not initialised.")
    return ctx.obj


@app.command("sbom")
def sbom(
    ctx: typer.Context, output: Path = typer.Option(Path("sbom.spdx.json"), "--output")
) -> None:
    """Generate an SPDX 2.3 software bill of materials."""
    root = _runtime(ctx).load().paths.cwd
    payload = generate_spdx(root)
    destination = output if output.is_absolute() else root / output
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _runtime(ctx).emit_json({"path": str(destination), "files": len(payload["files"])})


@app.command("attest")
def attest(
    ctx: typer.Context,
    key_ref: str = typer.Option(..., "--key-ref"),
    revision: str = typer.Option(..., "--revision"),
    output: Path = typer.Option(Path("provenance.intoto.jsonl"), "--output"),
    builder: str = typer.Option("https://unknown-agent.dev/builder/cli", "--builder"),
) -> None:
    """Sign an in-toto/SLSA provenance statement with Ed25519."""
    root = _runtime(ctx).load().paths.cwd
    envelope = create_attestation(
        inventory(root),
        private_key_ref=key_ref,
        builder_id=builder,
        source_uri="https://github.com/flormariap/unknown-agent",
        source_revision=revision,
    )
    destination = output if output.is_absolute() else root / output
    destination.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    _runtime(ctx).emit_json({"path": str(destination), "keyid": envelope["signatures"][0]["keyid"]})


@app.command("verify-attestation")
def verify_command(
    ctx: typer.Context,
    attestation: Path,
) -> None:
    """Verify a provenance signature and exact project artifact inventory."""
    root = _runtime(ctx).load().paths.cwd
    source = attestation if attestation.is_absolute() else root / attestation
    envelope = json.loads(source.read_text(encoding="utf-8"))
    try:
        generated_path = source.relative_to(root).as_posix()
    except ValueError:
        generated_path = ""
    artifacts = [item for item in inventory(root) if item.path != generated_path]
    statement = verify_attestation(envelope, artifacts)
    _runtime(ctx).emit_json(
        {
            "valid": True,
            "subjects": len(statement["subject"]),
            "predicateType": statement["predicateType"],
        }
    )
