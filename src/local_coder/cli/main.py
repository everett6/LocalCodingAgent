"""CLI for the local coding agent."""
import asyncio
import os
import uuid
from pathlib import Path
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.markup import escape
from local_coder import __version__
from local_coder.cli import ui

console = Console()

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


def _event_handler(event):
    """Handle agent events for display (routed to the live progress view)."""
    ui.render_event(console, event)


class _RequestGroup(click.Group):
    """Lets `local-coder "Add OAuth login"` run a request directly: click
    would otherwise look the request up as a subcommand name and fail. A
    single word that isn't a command is still reported as an unknown
    command, so a typo like `local-coder staus` doesn't start a run."""

    def resolve_command(self, ctx, args):
        if args and self.get_command(ctx, args[0]) is None and (len(args) > 1 or " " in args[0].strip()):
            return "run", self.get_command(ctx, "run"), args
        return super().resolve_command(ctx, args)


@click.group(cls=_RequestGroup, invoke_without_command=True, context_settings={"allow_extra_args": True})
@click.option("--config", "-c", type=click.Path(), help="Config file path")
@click.option("--project", "-p", type=click.Path(), help="Project root directory")
@click.option("--model", help="Use this configured model for every agent in the run")
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option(
    "--resume", "-r", is_flag=True,
    help="Continue the most recent session: pick up its unfinished request, or add a follow-up request to it.",
)
@click.option(
    "--session", "-s", "session_id", metavar="ID",
    help="Run in this session (created if new); resumes its unfinished request when no request is given.",
)
@click.option(
    "--yolo", is_flag=True,
    help="Auto-approve risky actions (shell commands, git commits/checkouts) without prompting. Dangerous.",
)
@click.version_option(version=__version__, prog_name="local-coder")
@click.pass_context
def cli(ctx, config, project, model, debug, resume, session_id, yolo):
    """Local Coding Agent - AI-powered local code assistant.

    Run with a request to execute it:

        local-coder "Add OAuth login"

    Or use subcommands:

    \b
        local-coder plan "Refactor auth"
        local-coder review
        local-coder security
        local-coder security-lessons
        local-coder validate-finding "SQL injection in ..."
        local-coder test

    Every run is saved as a session, so an interrupted or failed run can
    pick up where it stopped, and later requests can build on earlier ones:

    \b
        local-coder --resume
        local-coder --resume "now add tests for it"
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["project_root"] = project or _get_project_root()
    ctx.obj["debug"] = debug
    ctx.obj["model"] = model
    ctx.obj["yolo"] = yolo
    ctx.obj["resume"] = resume
    ctx.obj["session_id"] = session_id

    if ctx.invoked_subcommand is None:
        if ctx.args:
            # Direct execution: local-coder "Add OAuth login"
            request_str = " ".join(ctx.args)
            _run_request(request_str, ctx.obj)
        elif resume or session_id:
            _resume_or_interactive(ctx.obj)
        else:
            # Interactive mode
            _interactive_mode(ctx.obj)


@cli.command()
@click.argument("request", nargs=-1, required=True)
@click.pass_context
def run(ctx, request):
    """Execute a coding request, or a custom command such as "/fix-issue 42"."""
    request_str = " ".join(request)
    if not (request_str.startswith("/") and _run_extension_command(request_str, ctx.obj)):
        _run_request(request_str, ctx.obj)


@cli.command()
@click.argument("request", nargs=-1, required=True)
@click.pass_context
def plan(ctx, request):
    """Create a plan without executing."""
    request_str = " ".join(request)
    _run_plan(request_str, ctx.obj)


@cli.command()
@click.pass_context
def review(ctx):
    """Review current uncommitted changes."""
    _run_review(ctx.obj)


@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--focus", help="What to concentrate on, e.g. 'auth' or 'injection in the API handlers'")
@click.option("--batch-chars", type=int, default=None,
              help="Review scopes larger than this many characters of source in batches (default: twice the context window)")
@click.pass_context
def security(ctx, paths, focus, batch_chars):
    """Red/blue team security review of the project (read-only).

    Loads SECURITY_LESSONS.md, records findings in a ledger that survives
    context compaction, and ends by proposing new lessons for you to review
    with `local-coder security-lessons`.
    """
    _run_security(ctx.obj, list(paths), focus, batch_chars)


@cli.group(name="security-lessons", invoke_without_command=True)
@click.pass_context
def security_lessons(ctx):
    """Review what the security review learned (SECURITY_LESSONS.md).

    With no subcommand, lists accepted lessons and pending proposals.
    """
    if ctx.invoked_subcommand is None:
        _show_security_lessons(ctx.obj["project_root"])


@security_lessons.command(name="accept")
@click.argument("ids", nargs=-1)
@click.option("--all", "accept_all", is_flag=True, help="Accept every pending proposal")
@click.pass_context
def security_lessons_accept(ctx, ids, accept_all):
    """Move proposed lessons into SECURITY_LESSONS.md."""
    _decide_security_lessons(ctx.obj["project_root"], ids, accept_all, accept=True)


@security_lessons.command(name="reject")
@click.argument("ids", nargs=-1)
@click.option("--all", "reject_all", is_flag=True, help="Reject every pending proposal")
@click.pass_context
def security_lessons_reject(ctx, ids, reject_all):
    """Discard proposed lessons."""
    _decide_security_lessons(ctx.obj["project_root"], ids, reject_all, accept=False)


@security_lessons.command(name="suppress")
@click.argument("rule")
@click.argument("path_glob")
@click.option("--reason", required=True, help="Why this is a false positive")
@click.pass_context
def security_lessons_suppress(ctx, rule, path_glob, reason):
    """Mark RULE findings under PATH_GLOB as a known false positive."""
    from local_coder.security.lessons import Lesson, LessonStore

    added = LessonStore(ctx.obj["project_root"]).add(Lesson("suppress", reason, rule, path_glob))
    console.print("[green]Added to SECURITY_LESSONS.md[/green]" if added else "[yellow]Already in SECURITY_LESSONS.md[/yellow]")


@cli.command(name="validate-finding")
@click.argument("finding", nargs=-1, required=True)
@click.option("--path", "paths", multiple=True, help="File(s) the finding is in (repeatable)")
@click.pass_context
def validate_finding(ctx, finding, paths):
    """Reproduce an already-identified security finding in this repo as a local PoC test.

    Red-team companion to `security`: pass a finding it reported (with its file:line) to
    confirm the vulnerability is real and get a test that fails once it is fixed.
    Operates only on this project's own code.
    """
    _run_validate_finding(ctx.obj, " ".join(finding), list(paths))


@cli.command()
@click.pass_context  
def test(ctx):
    """Run tests and report results."""
    _run_tests(ctx.obj)


@cli.command()
@click.pass_context
def status(ctx):
    """Show system status."""
    _run_status(ctx.obj)


@cli.command()
@click.pass_context
def agents(ctx):
    """Show configured agents and models."""
    _show_agents(ctx.obj)


@cli.command()
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
@click.argument("session_id", required=False)
@click.pass_context
def sessions(ctx, session_id):
    """List saved sessions, or show one session's requests."""
    if session_id:
        _show_session(session_id, ctx.obj)
    else:
        _list_sessions(ctx.obj)


@cli.command()
@click.argument("session_id", required=False)
@click.pass_context
def resume(ctx, session_id):
    """Pick up a session's unfinished request (the latest session by default)."""
    from local_coder.orchestrator.sessions import SessionError, SessionStore

    if session_id:
        try:
            SessionStore(ctx.obj["project_root"]).open(session_id)
        except SessionError as exc:
            raise click.ClickException(str(exc)) from exc
        ctx.obj["session_id"] = session_id
    else:
        ctx.obj["resume"] = True
    _run_request(None, ctx.obj)


@cli.command()
@click.pass_context
def init(ctx):
    """Create a .local-coder project configuration."""
    config_path = Path(ctx.obj["project_root"]) / ".local-coder" / "config.yaml"
    if config_path.exists():
        raise click.ClickException(f"Configuration already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
    console.print(f"Created [bold]{config_path}[/bold]")
    console.print("Edit the model_id and base_url, then run [bold]local-coder[/bold].")


@cli.command()
@click.pass_context
def checkpoint(ctx):
    """Create a local checkpoint of the current working tree."""
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx.obj["project_root"]).create()
        console.print(f"Created checkpoint [bold]{saved.checkpoint_id}[/bold]")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="checkpoints")
@click.pass_context
def checkpoints(ctx):
    """List local working-tree checkpoints."""
    from local_coder.git import CheckpointManager

    try:
        saved = CheckpointManager(ctx.obj["project_root"]).list()
        if not saved:
            console.print("No checkpoints found.")
            return
        for item in saved:
            console.print(f"{item.checkpoint_id}  {item.created_at}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("checkpoint_id")
@click.pass_context
def rollback(ctx, checkpoint_id):
    """Restore a local checkpoint by ID."""
    from local_coder.git import CheckpointManager

    try:
        restored = CheckpointManager(ctx.obj["project_root"]).rollback(checkpoint_id)
        console.print(f"Restored checkpoint [bold]{restored.checkpoint_id}[/bold]")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="map")
@click.argument("query", nargs=-1)
@click.option("--file", "-f", "files", multiple=True, help="File the work is about (repeatable)")
@click.option("--tokens", "-t", default=1024, show_default=True, type=int, help="Approximate map size")
@click.pass_context
def repo_map(ctx, query, files, tokens):
    """Print the ranked repo map agents see, optionally centered on QUERY."""
    from local_coder.context.repo_map import RepoMap

    text = RepoMap(ctx.obj["project_root"]).build(" ".join(query), list(files), tokens)
    click.echo(text or "No source files with definitions were found.")


@cli.group(name="local-server")
def local_server_group():
    """Start, stop, and choose models for this machine's local inference server(s)."""


@local_server_group.command(name="models")
def local_server_models():
    """List GGUF quantizations available on disk for the big model."""
    from local_coder import local_server

    available = local_server.list_available_models()
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
def local_server_status():
    """Show whether the big and draft model servers are running and healthy."""
    from local_coder import local_server

    state = local_server.status()
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
@click.option("--quant", default="q2_k", show_default=True, help="Which GGUF quant to load for the big model.")
@click.option("--n-ctx", default=65536, show_default=True, type=int, help="Context length in tokens.")
@click.option("--no-slot-cache", is_flag=True, help="Disable the disk-backed session cache (--slot-save-path).")
@click.option("--no-draft", is_flag=True, help="Skip starting the small draft model.")
@click.option("--wait/--no-wait", default=True, help="Wait for the server(s) to become healthy before returning.")
def local_server_start(quant, n_ctx, no_slot_cache, no_draft, wait):
    """Start the big model (and, by default, the draft model)."""
    from local_coder import local_server

    result = local_server.start_big_model(quant=quant, n_ctx=n_ctx, slot_cache=not no_slot_cache)
    console.print(f"Big model ({quant}): {result['status']} (pid {result.get('pid', '-')})")
    if not no_draft:
        try:
            draft_result = local_server.start_draft_model()
            console.print(f"Draft model: {draft_result['status']} (pid {draft_result.get('pid', '-')})")
        except FileNotFoundError as exc:
            console.print(f"[yellow]Draft model not started:[/yellow] {exc}")

    if wait:
        console.print("Waiting for the big model to warm up (can take 1-2 minutes)...")
        if local_server.wait_healthy("big"):
            console.print("[green]Big model is ready.[/green]")
        else:
            console.print(f"[red]Big model did not become healthy -- check {local_server.STATE_DIR / 'big_model.log'}[/red]")


@local_server_group.command(name="switch")
@click.option("--quant", required=True, help="Which GGUF quant to switch to.")
@click.option("--n-ctx", default=65536, show_default=True, type=int)
@click.option("--no-slot-cache", is_flag=True)
@click.option("--wait/--no-wait", default=True)
def local_server_switch(quant, n_ctx, no_slot_cache, wait):
    """Stop the big model and restart it with a different quant (llama-server can't hot-swap weights)."""
    from local_coder import local_server

    result = local_server.switch_big_model(quant=quant, n_ctx=n_ctx, slot_cache=not no_slot_cache)
    console.print(f"Big model ({quant}): {result['status']} (pid {result.get('pid', '-')})")
    if wait:
        console.print("Waiting for the big model to warm up (can take 1-2 minutes)...")
        if local_server.wait_healthy("big"):
            console.print("[green]Big model is ready.[/green]")
        else:
            console.print(f"[red]Big model did not become healthy -- check {local_server.STATE_DIR / 'big_model.log'}[/red]")


@local_server_group.command(name="stop")
@click.option("--big/--no-big", default=True)
@click.option("--draft/--no-draft", default=True)
def local_server_stop(big, draft):
    """Stop the running server(s)."""
    from local_coder import local_server

    if big:
        result = local_server.stop("big")
        console.print(f"Big model: {result['status']}")
    if draft:
        result = local_server.stop("draft")
        console.print(f"Draft model: {result['status']}")


async def _cli_approval_callback(description: str) -> bool:
    """Prompt the user in the terminal for a risky action. Runs inline in
    the same event loop as the agent -- blocking on input here is fine
    since a single interactive session has nothing else to do meanwhile."""
    with ui.paused():
        console.print(f"[bold yellow]Approval required:[/bold yellow] {description}")
        try:
            with ui.sigint_raises():
                return click.confirm("Allow this action?", default=False)
        except click.Abort:
            # Ctrl+C at the approval prompt cancels the whole request.
            console.print()
            raise KeyboardInterrupt from None


def _build_coordinator(config, ctx_obj: dict):
    """Construct a Coordinator wired for this CLI invocation: --yolo turns
    off approval prompts entirely, otherwise ASK-risk actions are routed
    to an interactive y/n prompt instead of being auto-denied."""
    from local_coder.orchestrator.coordinator import Coordinator

    if ctx_obj.get("yolo"):
        config.approval.require_approval_for_commands = False
        config.approval.require_approval_for_commits = False
        approval_callback = None
    else:
        approval_callback = _cli_approval_callback

    coordinator = Coordinator(
        config=config, project_root=ctx_obj["project_root"], approval_callback=approval_callback,
    )
    coordinator.on_event(_event_handler)
    _install_hooks(coordinator, ctx_obj)
    return coordinator


def _open_session(ctx_obj: dict):
    """The session this invocation runs in: the one named with --session
    (created if new), the latest one with --resume, otherwise a new one."""
    from local_coder.orchestrator.sessions import SessionError, SessionStore

    store = SessionStore(ctx_obj["project_root"])
    try:
        if ctx_obj.get("session_id"):
            return store.open_or_create(ctx_obj["session_id"])
        if ctx_obj.get("resume"):
            session = store.latest()
            if session is None:
                raise click.ClickException("No saved session to resume. Run a request first.")
            return session
        return store.create()
    except SessionError as exc:
        raise click.ClickException(str(exc)) from exc


def _resume_hint(session_id: str) -> str:
    return f"[dim]Session {session_id} -- continue it with: local-coder --session {session_id} [request][/dim]"


def _run_request(request: str | None, ctx_obj: dict):
    """Run a request in a persistent session. With request=None, pick the
    session's unfinished request back up from its last checkpoint."""
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionError

    session = _open_session(ctx_obj)
    # Later requests in this process (the interactive prompt) stay in it.
    ctx_obj["session_id"] = session.session_id

    if request is None:
        turn = session.unfinished_turn()
        if turn is None:
            raise click.ClickException(
                f"Nothing to resume in session {session.session_id}: its last request finished. "
                f"Add a new request to continue it: local-coder --session {session.session_id} \"...\""
            )
        done = ", ".join(turn["checkpoints"]) or "nothing yet"
        console.print(Panel(
            f"Resuming: [bold]{turn['request']}[/bold]\n[dim]Already done: {done}[/dim]",
            title=f"Local Coder - session {session.session_id}", border_style="cyan",
        ))
    else:
        if session.unfinished_turn() is not None:
            console.print("[yellow]The previous request in this session never finished; starting the new one instead.[/yellow]")
        console.print(Panel(f"Running request: [bold]{request}[/bold]", title="Local Coder", border_style="cyan"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
    if ctx_obj.get("model"):
        from local_coder.types import AgentRole
        config.agentic.role_models = {role.value: ctx_obj["model"] for role in AgentRole}

    try:
        session.acquire()
    except SessionError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        if request is None:
            turn = session.unfinished_turn()
            if turn is None:
                raise click.ClickException(f"Session {session.session_id} has nothing left to resume.")
            session.reopen_turn(turn)
        else:
            turn = session.start_turn(request)

        async def _run():
            coordinator = _build_coordinator(config, ctx_obj)
            coordinator.on_event(session.record_event)
            return await coordinator.run(
                turn["request"], checkpoint=session.checkpoint(turn), history=turn["context"],
            )

        try:
            with ui.progress(console, "request"):
                result = asyncio.run(_run())
        except KeyboardInterrupt:
            session.finish_turn(turn, "interrupted", error="Interrupted")
            console.print("[yellow]Interrupted. Finished phases are saved.[/yellow]")
            console.print(_resume_hint(session.session_id))
            raise
        except Exception as e:
            session.finish_turn(turn, "failed", error=str(e))
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            console.print(_resume_hint(session.session_id))
            if ctx_obj.get("debug"):
                import traceback
                traceback.print_exc()
            raise click.ClickException(str(e)) from e
        session.finish_turn(turn, "completed", report=result)
        console.print(Panel(Markdown(result), title="Result", border_style="green"))
        console.print(_resume_hint(session.session_id))
    finally:
        session.release()



def _resume_or_interactive(ctx_obj: dict):
    """--resume / --session with no request: pick up the session's
    unfinished request if it has one, otherwise open the interactive prompt
    attached to that session."""
    session = _open_session(ctx_obj)
    ctx_obj["session_id"] = session.session_id
    if session.unfinished_turn() is not None:
        _run_request(None, ctx_obj)
    else:
        console.print(f"[dim]Session {session.session_id} has nothing unfinished; new requests will continue it.[/dim]")
        _interactive_mode(ctx_obj)


def _list_sessions(ctx_obj: dict):
    from local_coder.orchestrator.sessions import SessionStore

    records = SessionStore(ctx_obj["project_root"]).list()
    if not records:
        console.print("No sessions yet.")
        return
    table = Table(title="Sessions")
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Status", style="magenta")
    table.add_column("Requests", style="blue")
    table.add_column("Last request", style="green")
    table.add_column("Updated", style="dim")
    for record in records:
        request = str(record.get("request", ""))
        table.add_row(
            record["session_id"],
            str(record.get("status") or record.get("phase", "")),
            str(record.get("turns", 1)),
            request[:60] + ("..." if len(request) > 60 else ""),
            str(record.get("updated_at", ""))[:19],
        )
    console.print(table)


def _show_session(session_id: str, ctx_obj: dict):
    from local_coder.orchestrator.sessions import SessionError, SessionStore

    try:
        session = SessionStore(ctx_obj["project_root"]).open(session_id)
    except SessionError as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title=f"Session {session_id}")
    table.add_column("#", style="cyan")
    table.add_column("Status", style="magenta")
    table.add_column("Request", style="green")
    table.add_column("Finished phases", style="dim")
    for turn in session.turns:
        table.add_row(str(turn["index"] + 1), turn["status"], turn["request"], ", ".join(turn["checkpoints"]))
    console.print(table)
    if session.unfinished_turn() is not None:
        console.print(f"Resume the unfinished request with: [bold]local-coder resume {session_id}[/bold]")


def _interactive_mode(ctx_obj: dict):
    console.print(Panel(ui.build_banner(ctx_obj, __version__), border_style="cyan", expand=False))
    session = ui.PromptSession(console, ctx_obj["project_root"])
    _setup_interactive_extensions(ctx_obj)

    while True:
        user_input = session.read()
        if user_input is None:  # Ctrl+D, or Ctrl+C twice at the prompt
            break
        user_input = user_input.strip()
        if not user_input:
            continue
        session.save_history()
        try:
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
                elif cmd == "/security":
                    focus = user_input[len("/security"):].strip() or None
                    _run_security(ctx_obj, [], focus, None)
                elif cmd == "/validate-finding":
                    _run_validate_finding(ctx_obj, user_input[len("/validate-finding"):].strip(), [])
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
                elif cmd == "/sessions":
                    _list_sessions(ctx_obj)
                elif cmd == "/new":
                    ctx_obj["session_id"] = None
                    ctx_obj["resume"] = False
                    console.print("Next request starts a new session.")
                elif cmd == "/resume":
                    parts = user_input.split(maxsplit=1)
                    ctx_obj["session_id"] = parts[1] if len(parts) == 2 else ctx_obj.get("session_id")
                    ctx_obj["resume"] = True
                    try:
                        _run_request(None, ctx_obj)
                    except click.ClickException as exc:
                        console.print(f"[red]{exc.message}[/red]")
                elif cmd == "/help":
                    console.print(ui.build_help_table())
                elif not _run_extension_command(user_input, ctx_obj):
                    console.print(ui.unknown_command_message(cmd))
            else:
                _run_request(user_input, ctx_obj)

        except BaseException as exc:
            if ui.is_cancellation(exc):
                console.print("[yellow]Request cancelled.[/yellow] Back at the prompt.")
            elif isinstance(exc, click.ClickException):
                pass  # already reported by the command; keep the REPL alive
            elif isinstance(exc, Exception):
                console.print(f"[bold red]Error:[/bold red] {exc}")
            else:
                raise
    session.save_history()


def _run_plan(request: str, ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionStore

    console.print(Panel(f"Planning request: [bold]{request}[/bold]", title="Local Coder - Plan", border_style="yellow"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)

        try:
            result = await coordinator.run(f"Create a detailed plan for: {request}")
            SessionStore(ctx_obj["project_root"]).save(
                f"local-{uuid.uuid4().hex[:8]}", request=request, phase="plan", result=result
            )
            console.print(Panel(Markdown(result), title="Plan", border_style="yellow"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            raise click.ClickException(str(e)) from e

    with ui.progress(console, "plan"):
        asyncio.run(_run())


def _run_review(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionStore

    console.print(Panel("Reviewing current changes", title="Local Coder - Review", border_style="white"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)
        try:
            result = await coordinator.run("Review current uncommitted changes")
            SessionStore(ctx_obj["project_root"]).save(
                f"local-{uuid.uuid4().hex[:8]}", request="Review current uncommitted changes", phase="review", result=result
            )
            console.print(Panel(Markdown(result), title="Review", border_style="white"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            raise click.ClickException(str(e)) from e

    with ui.progress(console, "review"):
        asyncio.run(_run())


def _run_security(ctx_obj: dict, paths: list[str], focus: str | None, batch_chars: int | None = None):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionStore

    scope = ", ".join(paths) if paths else "whole project"
    console.print(Panel(f"Security review: {scope}", title="Local Coder - Security", border_style="bright_red"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)
        try:
            result = await coordinator.security_review(paths, focus, batch_chars)
            SessionStore(ctx_obj["project_root"]).save(
                f"local-{uuid.uuid4().hex[:8]}", request=f"Security review: {focus or scope}", phase="security", result=result
            )
            console.print(Panel(Markdown(result), title="Security Review", border_style="bright_red"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            raise click.ClickException(str(e)) from e

    with ui.progress(console, "security review"):
        asyncio.run(_run())


def _show_security_lessons(project_root: str):
    from local_coder.security.lessons import LESSONS_FILE, LessonStore

    store = LessonStore(project_root)
    accepted, pending = store.load(), store.pending()
    if accepted:
        table = Table(title=LESSONS_FILE)
        table.add_column("Kind")
        table.add_column("Lesson")
        for lesson in accepted:
            table.add_row(lesson.kind, escape(lesson.render()[2:]))
        console.print(table)
    else:
        console.print(f"[dim]No {LESSONS_FILE} yet.[/dim]")
    if pending:
        table = Table(title="Proposed lessons (pending review)")
        table.add_column("Id", style="cyan")
        table.add_column("Kind")
        table.add_column("Lesson")
        for lesson in pending:
            table.add_row(lesson.id, lesson.kind, escape(lesson.render()[2:]))
        console.print(table)
        console.print("Accept with [bold]local-coder security-lessons accept ID...[/bold] (or --all), reject with [bold]reject[/bold].")
    else:
        console.print("[dim]No proposals waiting.[/dim]")


def _decide_security_lessons(project_root: str, ids: tuple[str, ...], everything: bool, accept: bool):
    from local_coder.security.lessons import LESSONS_FILE, LessonStore

    if not ids and not everything:
        raise click.UsageError("Pass proposal ids, or --all.")
    store = LessonStore(project_root)
    chosen = (store.accept if accept else store.reject)(None if everything else list(ids))
    missing = set(ids) - {lesson.id for lesson in chosen}
    verb = f"Accepted into {LESSONS_FILE}" if accept else "Rejected"
    for lesson in chosen:
        console.print(f"{verb}: {escape(lesson.render()[2:])}")
    if missing:
        console.print(f"[yellow]No pending proposal with id: {', '.join(sorted(missing))}[/yellow]")


def _run_validate_finding(ctx_obj: dict, finding: str, paths: list[str]):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.sessions import SessionStore

    if not finding.strip():
        console.print("[red]Provide the finding to validate, e.g. the file:line and what is wrong.[/red]")
        return

    console.print(Panel("Validating a security finding (local PoC)", title="Local Coder - Validate Finding", border_style="red"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)
        try:
            result = await coordinator.validate_finding(finding, paths)
            SessionStore(ctx_obj["project_root"]).save(
                f"local-{uuid.uuid4().hex[:8]}", request=f"Validate finding: {finding[:80]}", phase="validate-finding", result=result
            )
            console.print(Panel(Markdown(result), title="Finding Validation", border_style="red"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            raise click.ClickException(str(e)) from e

    with ui.progress(console, "finding validation"):
        asyncio.run(_run())


def _run_tests(ctx_obj: dict):
    from rich.text import Text
    from local_coder.orchestrator.config_loader import load_config

    console.print(Panel("Running tests", title="Local Coder - Test", border_style="magenta"))

    config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])

    async def _run():
        coordinator = _build_coordinator(config, ctx_obj)
        try:
            result = await coordinator.run_tests_only()
            # Plain text: the report's line layout matters and it may contain [brackets].
            console.print(Panel(Text(result), title="Test Results", border_style="magenta"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            raise click.ClickException(str(e)) from e

    with ui.progress(console, "tests"):
        asyncio.run(_run())


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
    
    console.print(Panel("System Status", title="Local Coder", border_style="cyan"))
    
    try:
        config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
        console.print(f"[bold]Models Configured:[/bold] {len(config.models)}")
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        
    project_root = ctx_obj["project_root"]
    console.print(f"[bold]Project Root:[/bold] {project_root}")
    
    try:
        branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=project_root, text=True).strip()
        console.print(f"[bold]Git Branch:[/bold] {branch}")
    except Exception:
        console.print("[bold]Git Branch:[/bold] Not a git repository or git not found")
        
    # Try to check Ollama connectivity
    console.print("[bold]Ollama Connectivity:[/bold] ", end="")
    try:
        import httpx
        with httpx.Client(timeout=2.0) as client:
            resp = client.get("http://localhost:11434/api/tags")
            if resp.status_code == 200:
                console.print("[green]OK[/green]")
            else:
                console.print(f"[red]Failed ({resp.status_code})[/red]")
    except Exception:
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
        CoderAgent, DebuggerAgent, ExplorerAgent, ExploitValidatorAgent, PlannerAgent, ReviewerAgent,
        SecurityAgent, TesterAgent,
    )
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.types import AgentRole

    try:
        config = load_config(ctx_obj["config_path"], project_root=ctx_obj["project_root"])
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        return

    agent_classes = {
        AgentRole.EXPLORER: ExplorerAgent,
        AgentRole.PLANNER: PlannerAgent,
        AgentRole.CODER: CoderAgent,
        AgentRole.DEBUGGER: DebuggerAgent,
        AgentRole.TESTER: TesterAgent,
        AgentRole.REVIEWER: ReviewerAgent,
        AgentRole.SECURITY: SecurityAgent,
        AgentRole.EXPLOIT_VALIDATOR: ExploitValidatorAgent,
    }

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
        console.print(f"[red]Failed to load config:[/red] {e}")
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


# === Custom commands and hooks =============================================

def _load_custom_commands(ctx_obj: dict, warn: bool = False):
    from local_coder import custom_commands

    commands, warnings = custom_commands.discover(ctx_obj["project_root"], reserved=ui.builtin_names())
    if warn:
        for warning in warnings:
            console.print(f"[yellow]Custom command skipped:[/yellow] {warning}")
    return {c.name: c for c in commands}


def _run_extension_command(user_input: str, ctx_obj: dict) -> bool:
    """Run a custom command or /hooks. False if ``user_input`` is neither."""
    from local_coder import custom_commands

    name, arguments = custom_commands.split_invocation(user_input)
    if name == "/hooks":
        _show_hooks(ctx_obj)
        return True
    # Re-read on every use so edits to a command file apply without a restart.
    command = _load_custom_commands(ctx_obj).get(name)
    if command is None:
        return False
    prompt = command.expand(arguments)
    if command.mode == "plan":
        _run_plan(prompt, ctx_obj)
    else:
        _run_request(prompt, ctx_obj)
    return True


def _setup_interactive_extensions(ctx_obj: dict) -> None:
    """Load custom commands into /help and Tab completion, and offer to
    trust this project's hooks if it has untrusted ones."""
    from local_coder import hooks

    ui.set_custom_commands(_load_custom_commands(ctx_obj, warn=True).values())

    config = hooks.load_hooks(ctx_obj.get("config_path"), ctx_obj["project_root"])
    for error in config.errors:
        console.print(f"[yellow]Hook skipped:[/yellow] {error}")
    if config.hooks and not hooks.is_trusted(config, ctx_obj["project_root"]):
        console.print(f"[bold yellow]{config.source} defines hooks that run shell commands:[/bold yellow]")
        console.print(_hooks_table(config))
        try:
            answer = click.confirm("Trust and run these hooks in this project?", default=False)
        except click.Abort:
            console.print()
            answer = False
        if answer:
            hooks.trust(config, ctx_obj["project_root"])
            console.print("[green]Hooks trusted.[/green] They will ask again if they change.")
        else:
            console.print("[dim]Hooks will not run. Trust them later with `local-coder hooks trust`.[/dim]")


def _install_hooks(coordinator, ctx_obj: dict) -> None:
    """Attach trusted hooks to the coordinator's tool registry."""
    from local_coder import hooks

    config = hooks.load_hooks(ctx_obj.get("config_path"), ctx_obj["project_root"])
    if not config.hooks:
        return
    if not hooks.is_trusted(config, ctx_obj["project_root"]):
        if not ctx_obj.get("_warned_untrusted_hooks"):
            console.print(
                f"[yellow]Hooks in {config.source} are not trusted, so they will not run.[/yellow] "
                "Review them with `local-coder hooks`, then run `local-coder hooks trust`."
            )
            ctx_obj["_warned_untrusted_hooks"] = True
        return
    hooks.install(coordinator.tool_registry, hooks.HookRunner(config, ctx_obj["project_root"]))


def _hooks_table(config) -> Table:
    table = Table(box=None, padding=(0, 2))
    table.add_column("Event", style="cyan", no_wrap=True)
    table.add_column("Tools", style="dim")
    table.add_column("Command")
    for hook in config.hooks:
        table.add_row(hook.event, ", ".join(sorted(hook.tools)) or "all", hook.command)
    return table


def _show_hooks(ctx_obj: dict) -> None:
    from local_coder import hooks

    config = hooks.load_hooks(ctx_obj.get("config_path"), ctx_obj["project_root"])
    for error in config.errors:
        console.print(f"[yellow]Hook skipped:[/yellow] {error}")
    if not config.hooks:
        where = config.source or "the project config"
        console.print(f"No hooks configured. Add a `hooks:` section to {where}.")
        return
    trusted = hooks.is_trusted(config, ctx_obj["project_root"])
    state = "[green]trusted[/green]" if trusted else "[yellow]not trusted, will not run[/yellow] (local-coder hooks trust)"
    console.print(f"Hooks from {config.source}: {state}")
    console.print(_hooks_table(config))


@cli.group(name="hooks", invoke_without_command=True)
@click.pass_context
def hooks_group(ctx):
    """Show, trust or untrust this project's lifecycle hooks."""
    if ctx.invoked_subcommand is None:
        _show_hooks(ctx.obj)


@hooks_group.command(name="trust")
@click.option("--yes", "-y", is_flag=True, help="Trust without asking")
@click.pass_context
def hooks_trust(ctx, yes):
    """Allow the hooks currently configured here to run."""
    from local_coder import hooks

    ctx_obj = ctx.obj
    config = hooks.load_hooks(ctx_obj.get("config_path"), ctx_obj["project_root"])
    if not config.hooks:
        console.print("No hooks configured, nothing to trust.")
        return
    console.print(_hooks_table(config))
    if not yes and not click.confirm(f"Trust these hooks from {config.source}?", default=False):
        return
    hooks.trust(config, ctx_obj["project_root"])
    console.print("[green]Hooks trusted.[/green] Changing them will require trusting them again.")


@hooks_group.command(name="untrust")
@click.pass_context
def hooks_untrust(ctx):
    """Stop running this project's hooks."""
    from local_coder import hooks

    if hooks.untrust(ctx.obj["project_root"]):
        console.print("Hooks for this project are no longer trusted.")
    else:
        console.print("This project's hooks were not trusted.")


@cli.command(name="commands")
@click.pass_context
def commands_cmd(ctx):
    """List custom slash commands from .local-coder/commands/."""
    from local_coder import custom_commands

    commands = _load_custom_commands(ctx.obj, warn=True)
    if not commands:
        console.print(
            f"No custom commands. Add Markdown files to {custom_commands.project_commands_dir(ctx.obj['project_root'])}"
            f" or {custom_commands.user_commands_dir()}."
        )
        return
    table = Table(title="Custom commands", title_justify="left")
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Mode", style="dim")
    table.add_column("File", style="dim")
    for c in commands.values():
        table.add_row(f"{c.name} {c.argument_hint}".rstrip(), c.description, c.mode, f"{c.path} ({c.scope})")
    console.print(table)
