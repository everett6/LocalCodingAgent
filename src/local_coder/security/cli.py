"""Explicit local finding review and isolated regression commands."""
import asyncio
import json
from pathlib import Path

import click

from local_coder.security.evidence import EvidenceStore
from local_coder.security.threat_model import build_threat_model, evaluate_findings


def emit(value):
    click.echo(json.dumps(value, indent=2))


@click.group()
def security_findings():
    """Review recorded local findings and verify fixes with isolated regressions."""


@security_findings.command(name="list")
@click.pass_context
def list_findings(ctx):
    store = EvidenceStore(ctx.obj["project_root"])
    store.sync()
    emit(store.list())


@security_findings.command(name="threat-model")
@click.option("--path", "paths", multiple=True)
@click.pass_context
def threat_model(ctx, paths):
    emit(build_threat_model(ctx.obj["project_root"], list(paths) or None))


def agent_action(ctx, identity, action):
    from local_coder.cli.main import _load_config, _build_coordinator
    coordinator = _build_coordinator(_load_config(ctx.obj), ctx.obj)
    method = coordinator.review_security_finding if action == "review" else coordinator.prepare_security_regression
    try:
        emit(asyncio.run(method(identity)))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@security_findings.command()
@click.argument("identity")
@click.pass_context
def review(ctx, identity):
    """Challenge an existing finding using a separate read-only reviewer context."""
    agent_action(ctx, identity, "review")


@security_findings.command()
@click.argument("identity")
@click.pass_context
def prepare(ctx, identity):
    """Generate an offline safe-behavior regression without executing it."""
    agent_action(ctx, identity, "prepare")


@security_findings.command()
@click.argument("identity")
@click.option("--phase", type=click.Choice(["before", "after"]), required=True)
@click.option("--test", "test_path", required=True)
@click.option("--image", required=True, help="Already-installed trusted image containing Python, pytest and project dependencies")
@click.option("--backend", type=click.Choice(["docker", "podman"]), default="docker")
@click.option("--timeout", "timeout_seconds", type=click.IntRange(1, 600), default=60)
@click.option("--negative-control", "negative_control_ids", multiple=True, required=True,
              help="Exact pytest node ID of a safe control in the same regression file (repeatable)")
@click.pass_context
def verify(ctx, identity, **options):
    """Run one regression in an isolated container and record host-derived evidence."""
    try:
        options["negative_control_ids"] = list(options["negative_control_ids"])
        result = asyncio.run(EvidenceStore(ctx.obj["project_root"]).verify_controlled(identity, **options))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    emit(result)
    expected = "failed" if options["phase"] == "before" else "passed"
    if result["result"]["status"] != expected or (options["phase"] == "before" and result["state"] != "reproduced"):
        raise click.ClickException("Verification did not establish the requested evidence")


@security_findings.command()
@click.argument("identity")
@click.option("--reason", required=True)
@click.pass_context
def suppress(ctx, identity, reason):
    emit(EvidenceStore(ctx.obj["project_root"]).suppress(identity, reason))


@security_findings.command()
@click.argument("identity")
@click.pass_context
def reopen(ctx, identity):
    emit(EvidenceStore(ctx.obj["project_root"]).reopen(identity))


@security_findings.command()
@click.argument("dataset", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def metrics(dataset):
    """Score a labeled JSON dataset with predicted_ids and expected_ids lists."""
    if dataset.stat().st_size > 262144:
        raise click.ClickException("Dataset exceeds 256 KiB")
    try:
        data = json.loads(dataset.read_text())
        for name in ("predicted_ids", "expected_ids"):
            if not isinstance(data[name], list) or any(not isinstance(item, str) for item in data[name]):
                raise ValueError("IDs must be lists of strings")
        emit(evaluate_findings(data["predicted_ids"], data["expected_ids"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise click.ClickException("Dataset requires predicted_ids and expected_ids string lists") from exc


@security_findings.command(name="export")
@click.argument("identity")
@click.pass_context
def export_package(ctx, identity):
    """Export revision-bound evidence with explicit missing-control disposition."""
    try:
        emit(EvidenceStore(ctx.obj["project_root"]).export_package(identity))
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
