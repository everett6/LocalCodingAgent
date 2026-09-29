"""CLI for the local coding agent."""
import asyncio
import os
import sys
import uuid
from pathlib import Path
import click
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from local_coder import __version__
from local_coder.cli.output import JsonAwareGroup, console, emit, json_mode, json_option

_DEFAULT_CONFIG = """# Local Coder project configuration
models:
    coder:
        name: coder
        backend: openai_compatible
        model_id: your-model-name
        base_url: http://localhost:8090/v1
        context_length: 8192
        temperature: 0.2
        max_tokens: 4096
verification:
    run_tests_after_changes: true
    max_fix_iterations: 3
approval:
    require_approval_for_commands: true
    require_approval_for_commits: true
state_dir: .local-coder
"""


def _get_project_root() -> str:
    """Find project root (directory with .git, pyproject.toml, etc)."""
    path = os.getcwd()
    markers = [".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Makefile"]
    while path != "/":
        if any(os.path.exists(os.path.join(path, m)) for m in markers):
            return path
        path = os.path.dirname(path)
    return os.getcwd()


def _event_handler(event, ctx_obj: dict | None = None):
    """Handle agent events for display (and collect them in JSON mode)."""
    if json_mode(ctx_obj):
        ctx_obj.setdefault("events", []).append(event.model_dump(mode="json"))
    timestamp = event.timestamp.strftime("%H:%M:%S")
    source_colors = {
        "ORCHESTRATOR": "bold cyan",
        "EXPLORER": "bold green",
        "PLANNER": "bold yellow",
        "CODER": "bold blue",
        "DEBUGGER": "bold red",
        "TESTER": "bold magenta",
        "REVIEWER": "bold white",
    }
    color = source_colors.get(event.source, "white")
    console.print(f"[dim]{timestamp}[/dim] [{color}][{event.source}][/{color}] {event.message}")


@click.group(cls=JsonAwareGroup, invoke_without_command=True, context_settings={"allow_extra_args": True})
@click.option("--config", "-c", type=click.Path(), help="Config file path")
@click.option("--project", "-p", type=click.Path(), help="Project root directory")
@click.option("--model", help="Use this configured model for every agent in the run")
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option(
    "--yolo", is_flag=True,
    help="Auto-approve risky actions (shell commands, git commits/checkouts) without prompting. Dangerous.",
)
@click.version_option(version=__version__, prog_name="local-coder")
@json_option
@click.pass_context
def cli(ctx, config, project, model, debug, yolo):
    """Local Coding Agent - AI-powered local code assistant.

    Run with a request to execute it:

        local-coder "Add OAuth login"

    Or use subcommands:

        local-coder plan "Refactor auth"
        local-coder review
        local-coder test

    Add --json (before or after the subcommand) to get one JSON document on
    stdout instead of formatted text.
    """
    ctx.ensure_object(dict)
    # Rich output moves to stderr in JSON mode so stdout is only the JSON.
    console.stderr = json_mode(ctx.obj)
    ctx.obj["config_path"] = config
    ctx.obj["project_root"] = project or _get_project_root()
    ctx.obj["debug"] = debug
    ctx.obj["model"] = model
    ctx.obj["yolo"] = yolo
    
    if ctx.invoked_subcommand is None:
        if ctx.args:
            # Direct execution: local-coder "Add OAuth login"
            request_str = " ".join(ctx.args)
            ctx.obj["command"] = "run"
            _run_request(request_str, ctx.obj)
        else:
            # Interactive mode
            if json_mode(ctx.obj):
                raise click.UsageError("--json needs a subcommand or a request; interactive mode has no JSON output.")
            _interactive_mode(ctx.obj)


@cli.command()
@json_option
@click.argument("request", nargs=-1, required=True)
@click.pass_context
def run(ctx, request):
    """Execute a coding request."""
    request_str = " ".join(request)
    _run_request(request_str, ctx.obj)


@cli.command()
@json_option
@click.argument("request", nargs=-1, required=True)
@click.pass_context
def plan(ctx, request):
    """Create a plan without executing."""
    request_str = " ".join(request)
    _run_plan(request_str, ctx.obj)


@cli.command()
@json_option
@click.pass_context
def review(ctx):
    """Review current uncommitted changes."""
    _run_review(ctx.obj)


@cli.command()
@json_option
@click.pass_context  
def test(ctx):
    """Run tests and report results."""
    _run_tests(ctx.obj)


@cli.command()
@json_option
@click.pass_context
def status(ctx):
    """Show system status."""
    _run_status(ctx.obj)


@cli.command()
@json_option
@click.pass_context
def agents(ctx):
    """Show configured agents and models."""
    _show_agents(ctx.obj)


@cli.command()
@json_option
@click.pass_context
def models(ctx):
    """Show configured models."""
    _show_models(ctx.obj)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, show_default=True, type=int)
@click.option("--token", envvar="LOCAL_CODER_REMOTE_TOKEN", hide_input=True)
@click.pass_context
def serve(ctx, host, port, token):
    """Start the local HTTP control plane for remote sessions.

    The server has no interactive terminal to prompt with, so ASK-risk
    actions are denied by default unless the top-level --yolo flag is set
    when starting it (an explicit, opt-in autonomous mode).
    """
    from local_coder.remote import RemoteControlServer

    if ctx.obj.get("yolo"):
        console.print("[bold yellow]Warning:[/bold yellow] --yolo is set; risky actions will be auto-approved with no prompt.")
    console.print(f"Remote control listening on http://{host}:{port}")
    try:
        RemoteControlServer(
            ctx.obj["project_root"], ctx.obj["config_path"], ctx.obj.get("model"),
            yolo=ctx.obj.get("yolo", False), token=token,
        ).serve(host, port)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="sessions")
@json_option
@click.pass_context
def sessions(ctx):
    """List resumable agent sessions."""
    from local_coder.orchestrator.sessions import SessionStore

    saved = SessionStore(ctx.obj["project_root"]).list()
    if json_mode(ctx.obj):
        emit(ctx.obj, {"sessions": saved})
        return
    for session in saved:
        console.print(f"{session['session_id']}  {session.get('phase', 'unknown')}  {session.get('updated_at', '')}")


@cli.command()
@json_option
@click.argument("session_id")
@click.pass_context
def resume(ctx, session_id):
    """Resume a session's request from the local journal."""
    from local_coder.orchestrator.sessions import SessionStore

    session = SessionStore(ctx.obj["project_root"]).get(session_id)
    if not session or not session.get("request"):
        raise click.ClickException(f"Session not found or has no request: {session_id}")
    _run_request(session["request"], ctx.obj)


@cli.command()
@json_option
@click.pass_context
def init(ctx):
    """Create a .local-coder project configuration."""
    config_path = Path(ctx.obj["project_root"]) / ".local-coder" / "config.yaml"
    if config_path.exists():
        raise click.ClickException(f"Configuration already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    if json_mode(ctx.obj):
        emit(ctx.obj, {"config_path": str(config_path)})
        return
    console.print(f"Created [bold]{config_path}[/bold]")
    console.print("Edit the model_id and base_url, then run [bold]local-coder[/bold].")


@cli.command()
@json_option
@click.pass_context
def checkpoint(ctx):
    """Create a local checkpoint of the current working tree."""
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx.obj["project_root"]).create()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if json_mode(ctx.obj):
        emit(ctx.obj, {"checkpoint": _checkpoint_json(saved)})
        return
    console.print(f"Created checkpoint [bold]{saved.checkpoint_id}[/bold]")


@cli.command(name="checkpoints")
@json_option
@click.pass_context
def checkpoints(ctx):
    """List local working-tree checkpoints."""
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx.obj["project_root"]).list()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if json_mode(ctx.obj):
        emit(ctx.obj, {"checkpoints": [_checkpoint_json(item) for item in saved]})
        return
    if not saved:
        console.print("No checkpoints found.")
        return
    for item in saved:
        console.print(f"{item.checkpoint_id}  {item.created_at}")


@cli.command()
@json_option
@click.argument("checkpoint_id")
@click.pass_context
def rollback(ctx, checkpoint_id):
    """Restore a local checkpoint by ID."""
    from local_coder.git import CheckpointManager

    try:
        restored = CheckpointManager(ctx.obj["project_root"]).rollback(checkpoint_id)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    if json_mode(ctx.obj):
        emit(ctx.obj, {"checkpoint": _checkpoint_json(restored)})
        return
    console.print(f"Restored checkpoint [bold]{restored.checkpoint_id}[/bold]")


@cli.command(name="tools")
@json_option
@click.pass_context
def tools_command(ctx):
    """List the agent's tools, their arguments, and which roles may use them."""
    from local_coder.types import AgentRole

    registry = _tool_registry(ctx.obj)
    listed = [
        {
            "name": tool.name.value,
            "description": tool.description,
            "parameters": tool.parameters,
            "roles": [role.value for role in AgentRole if registry.has_permission(role, tool.name)],
        }
        for tool in registry.get_tools_for_role(AgentRole.ORCHESTRATOR)
    ]
    if json_mode(ctx.obj):
        emit(ctx.obj, {"tools": listed})
        return
    table = Table(title="Tools")
    table.add_column("Tool", style="cyan", no_wrap=True)
    table.add_column("Arguments", style="magenta")
    table.add_column("Description", style="green")
    for item in listed:
        table.add_row(item["name"], ", ".join(item["parameters"].get("properties", {})), item["description"])
    console.print(table)


@cli.command(name="tool")
@json_option
@click.argument("name")
@click.option("--arg", "-a", "pairs", multiple=True, metavar="KEY=VALUE",
              help="A tool argument (repeatable). VALUE is parsed as JSON when it can be, else kept as a string.")
@click.option("--args", "args_json", metavar="JSON", help="All tool arguments as one JSON object.")
@click.pass_context
def tool_command(ctx, name, pairs, args_json):
    """Run one agent tool directly, with no model involved.

        local-coder tool run_tests --json

        local-coder tool grep -a pattern=TODO --json

    Risky tools (run_command, git_commit, git_checkout) still ask for
    approval unless --yolo is set. Exits 1 when the tool reports failure.
    """
    from local_coder.types import AgentRole

    arguments = _parse_tool_arguments(pairs, args_json)
    registry = _tool_registry(ctx.obj)
    known = [tool.name.value for tool in registry.get_tools_for_role(AgentRole.ORCHESTRATOR)]
    if name not in known:
        raise click.BadParameter(f"unknown tool {name!r}; available: {', '.join(known)}", param_hint="NAME")
    result = asyncio.run(registry.execute_tool(AgentRole.ORCHESTRATOR, name, arguments))
    if json_mode(ctx.obj):
        data = {"tool": name, "arguments": arguments, **result.model_dump(mode="json", exclude={"tool_call_id"})}
        details = _TOOL_DETAILS.get(name)
        data["details"] = details(result) if details else None
        emit(ctx.obj, data, ok=result.success)
        return
    click.echo(result.output)
    if result.error:
        click.echo(result.error, err=True)
    if not result.success:
        ctx.exit(1)


def _parse_tool_arguments(pairs: tuple[str, ...], args_json: str | None) -> dict:
    import json

    arguments: dict = {}
    if args_json:
        try:
            arguments = json.loads(args_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--args") from exc
        if not isinstance(arguments, dict):
            raise click.BadParameter("must be a JSON object", param_hint="--args")
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise click.BadParameter(f"expected KEY=VALUE, got {pair!r}", param_hint="--arg")
        try:
            arguments[key] = json.loads(value)
        except json.JSONDecodeError:
            arguments[key] = value
    return arguments


def _tool_registry(ctx_obj: dict):
    """The same tool registry the agents get, with the CLI's approval wiring."""
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.tools import create_tool_registry
    from local_coder.types import ApprovalConfig

    try:
        approval = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"]).approval
    except Exception:
        approval = ApprovalConfig()
    if ctx_obj.get("yolo"):
        approval.require_approval_for_commands = False
        approval.require_approval_for_commits = False
        callback = None
    else:
        callback = _json_approval_callback if json_mode(ctx_obj) else _cli_approval_callback
    return create_tool_registry(ctx_obj["project_root"], approval, callback)


def _run_tests_details(result) -> dict:
    from local_coder.verification.failures import parse_failures

    return {"failures": [{"test": f.test, "message": f.message} for f in parse_failures(result.output)]}


# Extra structure pulled out of a tool's text output for `tool NAME --json`.
_TOOL_DETAILS = {
    "run_tests": _run_tests_details,
}


def _checkpoint_json(item) -> dict:
    from dataclasses import asdict

    return asdict(item)


@cli.group(name="local-server")
@json_option
def local_server_group():
    """Start, stop, and choose models for this machine's local inference server(s)."""


@local_server_group.command(name="models")
@json_option
@click.pass_context
def local_server_models(ctx):
    """List GGUF quantizations available on disk for the big model."""
    from dataclasses import asdict
    from local_coder import local_server

    available = local_server.list_available_models()
    if json_mode(ctx.obj):
        emit(ctx.obj, {"models": [asdict(info) for info in available]})
        return
    if not available:
        console.print(f"[red]No quants found under {local_server.AI2_DIR}/models/quants/[/red]")
        return
    table = Table(title="Available local models")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Size", style="magenta")
    table.add_column("Notes", style="green")
    for info in available:
        table.add_row(info.name, f"{info.size_gb} GB", info.note)
    console.print(table)
    console.print("Switch with [bold]local-coder local-server start --quant <name>[/bold]")


@local_server_group.command(name="status")
@json_option
@click.pass_context
def local_server_status(ctx):
    """Show whether the big and draft model servers are running and healthy."""
    from local_coder import local_server

    state = local_server.status()
    if json_mode(ctx.obj):
        emit(ctx.obj, {"servers": state})
        return
    table = Table(title="Local server status")
    table.add_column("Server", style="cyan")
    table.add_column("PID", style="magenta")
    table.add_column("Port", style="blue")
    table.add_column("Healthy", style="green")
    for which, info in state.items():
        healthy = "[green]yes[/green]" if info["healthy"] else "[red]no[/red]"
        table.add_row(which, str(info["pid"] or "-"), str(info["port"]), healthy)
    console.print(table)


@local_server_group.command(name="start")
@json_option
@click.option("--quant", default="q2_k", show_default=True, help="Which GGUF quant to load for the big model.")
@click.option("--n-ctx", default=65536, show_default=True, type=int, help="Context length in tokens.")
@click.option("--no-slot-cache", is_flag=True, help="Disable the disk-backed session cache (--slot-save-path).")
@click.option("--no-draft", is_flag=True, help="Skip starting the small draft model.")
@click.option("--wait/--no-wait", default=True, help="Wait for the server(s) to become healthy before returning.")
@click.pass_context
def local_server_start(ctx, quant, n_ctx, no_slot_cache, no_draft, wait):
    """Start the big model (and, by default, the draft model)."""
    from local_coder import local_server

    data = {"big": None, "draft": None, "draft_error": None, "healthy": None}
    result = local_server.start_big_model(quant=quant, n_ctx=n_ctx, slot_cache=not no_slot_cache)
    data["big"] = result
    console.print(f"Big model ({quant}): {result['status']} (pid {result.get('pid', '-')})")
    if not no_draft:
        try:
            draft_result = local_server.start_draft_model()
            data["draft"] = draft_result
            console.print(f"Draft model: {draft_result['status']} (pid {draft_result.get('pid', '-')})")
        except FileNotFoundError as exc:
            data["draft_error"] = str(exc)
            console.print(f"[yellow]Draft model not started:[/yellow] {exc}")

    if wait:
        data["healthy"] = _wait_for_big_model(local_server)
    if json_mode(ctx.obj):
        emit(ctx.obj, data, ok=data["healthy"] is not False)


def _wait_for_big_model(local_server) -> bool:
    console.print("Waiting for the big model to warm up (can take 1-2 minutes)...")
    if local_server.wait_healthy("big"):
        console.print("[green]Big model is ready.[/green]")
        return True
    console.print(f"[red]Big model did not become healthy -- check {local_server.STATE_DIR / 'big_model.log'}[/red]")
    return False


@local_server_group.command(name="switch")
@json_option
@click.option("--quant", required=True, help="Which GGUF quant to switch to.")
@click.option("--n-ctx", default=65536, show_default=True, type=int)
@click.option("--no-slot-cache", is_flag=True)
@click.option("--wait/--no-wait", default=True)
@click.pass_context
def local_server_switch(ctx, quant, n_ctx, no_slot_cache, wait):
    """Stop the big model and restart it with a different quant (llama-server can't hot-swap weights)."""
    from local_coder import local_server

    result = local_server.switch_big_model(quant=quant, n_ctx=n_ctx, slot_cache=not no_slot_cache)
    console.print(f"Big model ({quant}): {result['status']} (pid {result.get('pid', '-')})")
    healthy = _wait_for_big_model(local_server) if wait else None
    if json_mode(ctx.obj):
        emit(ctx.obj, {"big": result, "healthy": healthy}, ok=healthy is not False)


@local_server_group.command(name="stop")
@json_option
@click.option("--big/--no-big", default=True)
@click.option("--draft/--no-draft", default=True)
@click.pass_context
def local_server_stop(ctx, big, draft):
    """Stop the running server(s)."""
    from local_coder import local_server

    data = {"big": None, "draft": None}
    if big:
        data["big"] = local_server.stop("big")
        console.print(f"Big model: {data['big']['status']}")
    if draft:
        data["draft"] = local_server.stop("draft")
        console.print(f"Draft model: {data['draft']['status']}")
    if json_mode(ctx.obj):
        emit(ctx.obj, data)


async def _cli_approval_callback(description: str) -> bool:
    """Prompt the user in the terminal for a risky action. Runs inline in
    the same event loop as the agent -- blocking on input here is fine
    since a single interactive session has nothing else to do meanwhile."""
    console.print(f"[bold yellow]Approval required:[/bold yellow] {description}")
    return click.confirm("Allow this action?", default=False)


async def _json_approval_callback(description: str) -> bool:
    """Approval prompt for --json runs: the prompt goes to stderr so stdout
    stays pure JSON, and with no terminal to ask (a script piping stdin)
    the action is denied rather than aborting the whole run."""
    console.print(f"[bold yellow]Approval required:[/bold yellow] {description}")
    if not sys.stdin.isatty():
        console.print("[yellow]Denied: no terminal to confirm on (use --yolo to auto-approve).[/yellow]")
        return False
    return click.confirm("Allow this action?", default=False, err=True)


def _build_coordinator(config, ctx_obj: dict):
    """Construct a Coordinator wired for this CLI invocation: --yolo turns
    off approval prompts entirely, otherwise ASK-risk actions are routed
    to an interactive y/n prompt instead of being auto-denied."""
    from local_coder.orchestrator.coordinator import Coordinator

    if ctx_obj.get("yolo"):
        config.approval.require_approval_for_commands = False
        config.approval.require_approval_for_commits = False
        approval_callback = None
    elif json_mode(ctx_obj):
        approval_callback = _json_approval_callback
    else:
        approval_callback = _cli_approval_callback

    coordinator = Coordinator(
        config=config, project_root=ctx_obj["project_root"], approval_callback=approval_callback,
    )
    coordinator.on_event(lambda event: _event_handler(event, ctx_obj))
    return coordinator


def _agent_run_status(events: list[dict]) -> str:
    """"completed" or "failed" from the coordinator's final event."""
    for event in reversed(events):
        if event["source"] == "ORCHESTRATOR" and event["event_type"] in ("task_completed", "task_failed"):
            return "completed" if event["event_type"] == "task_completed" else "failed"
    return "failed"


def _run_request(request: str, ctx_obj: dict):
    _run_agent(
        ctx_obj, request, request=request, phase="run",
        heading=f"Running request: [bold]{request}[/bold]", title="Local Coder",
        result_title="Result", border="cyan", result_border="green",
    )


def _run_agent(
    ctx_obj: dict, prompt: str, *, request: str, phase: str, heading: str, title: str,
    result_title: str, border: str, result_border: str | None = None, save_session: bool = True,
):
    """Run one coordinator request and show (or, with --json, emit) the result."""
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionStore

    console.print(Panel(heading, title=title, border_style=border))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
    if ctx_obj.get("model"):
        from local_coder.types import AgentRole
        config.agentic.role_models = {role.value: ctx_obj["model"] for role in AgentRole}

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)
        try:
            result = await coordinator.run(prompt)
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            if ctx_obj.get("debug"):
                import traceback
                traceback.print_exc()
            raise click.ClickException(str(e)) from e

        session_id = None
        if save_session:
            session_id = f"local-{uuid.uuid4().hex[:8]}"
            SessionStore(ctx_obj["project_root"]).save(session_id, request=request, phase=phase, result=result)
        if json_mode(ctx_obj):
            events = ctx_obj.get("events", [])
            status = _agent_run_status(events)
            errors = [e for e in events if e["event_type"].endswith("error")]
            emit(ctx_obj, {
                "request": request,
                "phase": phase,
                "status": status,
                "session_id": session_id,
                "result": result,
                "errors": errors,
                "events": events,
            }, ok=status == "completed" and not any(e["event_type"] == "model_error" for e in errors))
            return
        console.print(Panel(Markdown(result), title=result_title, border_style=result_border or border))

    asyncio.run(_run())


def _interactive_mode(ctx_obj: dict):
    console.print(Panel("[bold cyan]Local Coding Agent[/bold cyan]\nType /help for commands, /quit to exit.", border_style="cyan"))
    
    while True:
        try:
            user_input = console.input("[bold green]> [/bold green]").strip()
            if not user_input:
                continue
                
            if user_input.startswith("/"):
                cmd = user_input.split()[0].lower()
                if cmd in ("/quit", "/exit", "/q"):
                    break
                elif cmd == "/status":
                    _run_status(ctx_obj)
                elif cmd == "/plan":
                    req = user_input[len("/plan"):].strip()
                    if req:
                        _run_plan(req, ctx_obj)
                    else:
                        console.print("[red]Please provide a request to plan.[/red]")
                elif cmd == "/review":
                    _run_review(ctx_obj)
                elif cmd == "/test":
                    _run_tests(ctx_obj)
                elif cmd == "/checkpoint":
                    _run_checkpoint(ctx_obj)
                elif cmd == "/checkpoints":
                    _run_checkpoints(ctx_obj)
                elif cmd == "/rollback":
                    parts = user_input.split(maxsplit=1)
                    if len(parts) == 2:
                        _run_rollback(parts[1], ctx_obj)
                    else:
                        console.print("[red]Please provide a checkpoint ID.[/red]")
                elif cmd == "/help":
                    console.print("""
Available commands:
  /quit, /exit, /q : Exit the interactive mode
  /status          : Show system status
  /plan <request>  : Create a plan without executing
  /review          : Review current uncommitted changes
  /test            : Run tests and report results
    /checkpoint      : Save the current working tree
    /checkpoints     : List saved checkpoints
    /rollback <id>   : Restore a checkpoint
  /help            : Show this help message
                    """)
                else:
                    console.print(f"[red]Unknown command:[/red] {cmd}")
            else:
                _run_request(user_input, ctx_obj)
                
        except KeyboardInterrupt:
            break
        except EOFError:
            break


def _run_plan(request: str, ctx_obj: dict):
    _run_agent(
        ctx_obj, f"Create a detailed plan for: {request}", request=request, phase="plan",
        heading=f"Planning request: [bold]{request}[/bold]", title="Local Coder - Plan",
        result_title="Plan", border="yellow",
    )


def _run_review(ctx_obj: dict):
    request = "Review current uncommitted changes"
    _run_agent(
        ctx_obj, request, request=request, phase="review",
        heading="Reviewing current changes", title="Local Coder - Review",
        result_title="Review", border="white",
    )


def _run_tests(ctx_obj: dict):
    request = "Run project tests and report results"
    _run_agent(
        ctx_obj, request, request=request, phase="test",
        heading="Running tests", title="Local Coder - Test",
        result_title="Test Results", border="magenta", save_session=False,
    )


def _run_checkpoint(ctx_obj: dict):
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx_obj["project_root"]).create()
        console.print(f"Created checkpoint [bold]{saved.checkpoint_id}[/bold]")
    except Exception as exc:
        console.print(f"[red]Checkpoint failed:[/red] {exc}")


def _run_checkpoints(ctx_obj: dict):
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx_obj["project_root"]).list()
        for item in saved:
            console.print(f"{item.checkpoint_id}  {item.created_at}")
        if not saved:
            console.print("No checkpoints found.")
    except Exception as exc:
        console.print(f"[red]Could not list checkpoints:[/red] {exc}")


def _run_rollback(checkpoint_id: str, ctx_obj: dict):
    from local_coder.git import CheckpointManager

    try:
        restored = CheckpointManager(ctx_obj["project_root"]).rollback(checkpoint_id)
        console.print(f"Restored checkpoint [bold]{restored.checkpoint_id}[/bold]")
    except Exception as exc:
        console.print(f"[red]Rollback failed:[/red] {exc}")


def _run_status(ctx_obj: dict):
    import subprocess
    from local_coder.orchestrator.config_loader import load_config

    as_json = json_mode(ctx_obj)
    data = {
        "project_root": ctx_obj["project_root"],
        "models_configured": None,
        "config_error": None,
        "git_branch": None,
        "ollama": {"reachable": False, "status_code": None},
    }
    if not as_json:
        console.print(Panel("System Status", title="Local Coder", border_style="cyan"))

    try:
        config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
        data["models_configured"] = len(config.models)
        if not as_json:
            console.print(f"[bold]Models Configured:[/bold] {len(config.models)}")
    except Exception as e:
        data["config_error"] = str(e)
        if not as_json:
            console.print(f"[red]Failed to load config:[/red] {e}")

    project_root = ctx_obj["project_root"]
    if not as_json:
        console.print(f"[bold]Project Root:[/bold] {project_root}")

    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=project_root, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        data["git_branch"] = branch
        if not as_json:
            console.print(f"[bold]Git Branch:[/bold] {branch}")
    except Exception:
        if not as_json:
            console.print("[bold]Git Branch:[/bold] Not a git repository or git not found")

    # Try to check Ollama connectivity
    try:
        import httpx
        with httpx.Client(timeout=2.0) as client:
            resp = client.get("http://localhost:11434/api/tags")
            data["ollama"] = {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception:
        pass

    if as_json:
        emit(ctx_obj, data)
        return
    console.print("[bold]Ollama Connectivity:[/bold] ", end="")
    if data["ollama"]["reachable"]:
        console.print("[green]OK[/green]")
    elif data["ollama"]["status_code"] is not None:
        console.print(f"[red]Failed ({data['ollama']['status_code']})[/red]")
    else:
        console.print("[red]Failed to connect (Is Ollama running?)[/red]")


def _resolve_model_name(config, role) -> str:
    """Mirror ModelManager.get_model's routing so this display is accurate."""
    role_name = role.value
    configured = config.agentic.role_models.get(role_name)
    if configured and configured in config.models:
        return configured
    if role_name in config.models:
        return role_name
    if "default" in config.models:
        return "default"
    if config.models:
        return next(iter(config.models))
    return "[not configured]"


def _show_agents(ctx_obj: dict):
    from local_coder.agents import (
        CoderAgent, DebuggerAgent, ExplorerAgent, PlannerAgent, ReviewerAgent, TesterAgent,
    )
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.types import AgentRole

    try:
        config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
    except Exception as e:
        if json_mode(ctx_obj):
            raise click.ClickException(f"Failed to load config: {e}") from e
        console.print(f"[red]Failed to load config:[/red] {e}")
        return

    agent_classes = {
        AgentRole.EXPLORER: ExplorerAgent,
        AgentRole.PLANNER: PlannerAgent,
        AgentRole.CODER: CoderAgent,
        AgentRole.DEBUGGER: DebuggerAgent,
        AgentRole.TESTER: TesterAgent,
        AgentRole.REVIEWER: ReviewerAgent,
    }

    if json_mode(ctx_obj):
        emit(ctx_obj, {"agents": [
            {
                "role": role.value,
                "model": model if (model := _resolve_model_name(config, role)) in config.models else None,
                "system_prompt": agent_cls.system_prompt.strip().splitlines()[0],
            }
            for role, agent_cls in agent_classes.items()
        ]})
        return

    table = Table(title="Configured Agents")
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("Model", style="magenta")
    table.add_column("System Prompt", style="green")

    for role, agent_cls in agent_classes.items():
        model_name = _resolve_model_name(config, role)
        first_line = agent_cls.system_prompt.strip().splitlines()[0]
        sys_prompt = first_line[:70] + "..." if len(first_line) > 70 else first_line
        table.add_row(role.value, model_name, sys_prompt)

    console.print(table)


def _show_models(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    try:
        config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
    except Exception as e:
        if json_mode(ctx_obj):
            raise click.ClickException(f"Failed to load config: {e}") from e
        console.print(f"[red]Failed to load config:[/red] {e}")
        return

    if json_mode(ctx_obj):
        emit(ctx_obj, {"models": [
            {
                "name": name,
                "backend": model.backend.value,
                "model_id": model.model_id,
                "base_url": model.base_url,
                "context_length": model.context_length,
                "temperature": model.temperature,
                "max_tokens": model.max_tokens,
            }
            for name, model in config.models.items()
        ]})
        return

    table = Table(title="Configured Models")
    table.add_column("Model Name", style="cyan", no_wrap=True)
    table.add_column("Backend", style="blue")
    table.add_column("Model ID", style="magenta")
    table.add_column("Context Length", style="green")

    for name, model in config.models.items():
        table.add_row(
            name,
            model.backend.value,
            model.model_id,
            str(model.context_length)
        )

    console.print(table)
