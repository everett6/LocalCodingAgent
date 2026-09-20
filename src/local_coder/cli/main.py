"""CLI for the local coding agent."""
import asyncio
import os
import sys
import click
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.markdown import Markdown
from rich import print as rprint

console = Console()


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
    """Handle agent events for display."""
    from rich.text import Text
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


@click.group(invoke_without_command=True)
@click.argument("request", nargs=-1, required=False)
@click.option("--config", "-c", type=click.Path(), help="Config file path")
@click.option("--project", "-p", type=click.Path(), help="Project root directory")
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, request, config, project, debug):
    """Local Coding Agent - AI-powered local code assistant.
    
    Run with a request to execute it:
    
        local-coder "Add OAuth login"
    
    Or use subcommands:
    
        local-coder plan "Refactor auth"
        local-coder review
        local-coder test
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["project_root"] = project or _get_project_root()
    ctx.obj["debug"] = debug
    
    if ctx.invoked_subcommand is None:
        if request:
            # Direct execution: local-coder "Add OAuth login"
            request_str = " ".join(request)
            _run_request(request_str, ctx.obj)
        else:
            # Interactive mode
            _interactive_mode(ctx.obj)


@cli.command()
@click.argument("request", nargs=-1, required=True)
@click.pass_context
def run(ctx, request):
    """Execute a coding request."""
    request_str = " ".join(request)
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


def _run_request(request: str, ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.coordinator import Coordinator
    
    console.print(Panel(f"Running request: [bold]{request}[/bold]", title="Local Coder", border_style="cyan"))
    
    config = load_config(ctx_obj["config_path"])
    
    async def _run():
        coordinator = Coordinator(config=config, project_root=ctx_obj["project_root"])
        coordinator.add_event_listener(_event_handler)
        
        try:
            result = await coordinator.run(request)
            console.print(Panel(Markdown(result), title="Result", border_style="green"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")
            if ctx_obj.get("debug"):
                import traceback
                traceback.print_exc()

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
                elif cmd == "/help":
                    console.print("""
Available commands:
  /quit, /exit, /q : Exit the interactive mode
  /status          : Show system status
  /plan <request>  : Create a plan without executing
  /review          : Review current uncommitted changes
  /test            : Run tests and report results
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
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.coordinator import Coordinator
    
    console.print(Panel(f"Planning request: [bold]{request}[/bold]", title="Local Coder - Plan", border_style="yellow"))
    
    config = load_config(ctx_obj["config_path"])
    
    async def _run():
        coordinator = Coordinator(config=config, project_root=ctx_obj["project_root"])
        coordinator.add_event_listener(_event_handler)
        
        try:
            result = await coordinator.run(f"Create a detailed plan for: {request}")
            console.print(Panel(Markdown(result), title="Plan", border_style="yellow"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")

    asyncio.run(_run())


def _run_review(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.coordinator import Coordinator
    
    console.print(Panel("Reviewing current changes", title="Local Coder - Review", border_style="white"))
    
    config = load_config(ctx_obj["config_path"])
    
    async def _run():
        coordinator = Coordinator(config=config, project_root=ctx_obj["project_root"])
        coordinator.add_event_listener(_event_handler)
        try:
            result = await coordinator.run("Review current uncommitted changes")
            console.print(Panel(Markdown(result), title="Review", border_style="white"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")

    asyncio.run(_run())


def _run_tests(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    from local_coder.orchestrator.coordinator import Coordinator
    
    console.print(Panel("Running tests", title="Local Coder - Test", border_style="magenta"))
    
    config = load_config(ctx_obj["config_path"])
    
    async def _run():
        coordinator = Coordinator(config=config, project_root=ctx_obj["project_root"])
        coordinator.add_event_listener(_event_handler)
        try:
            result = await coordinator.run("Run project tests and report results")
            console.print(Panel(Markdown(result), title="Test Results", border_style="magenta"))
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {str(e)}")

    asyncio.run(_run())


def _run_status(ctx_obj: dict):
    import subprocess
    from local_coder.orchestrator.config_loader import load_config
    
    console.print(Panel("System Status", title="Local Coder", border_style="cyan"))
    
    try:
        config = load_config(ctx_obj["config_path"])
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


def _show_agents(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    try:
        config = load_config(ctx_obj["config_path"])
    except Exception as e:
        console.print(f"[red]Failed to load config:[/red] {e}")
        return
        
    table = Table(title="Configured Agents")
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("Model Name", style="magenta")
    table.add_column("System Prompt", style="green")
    
    for agent_name, agent_config in config.agents.items():
        sys_prompt = agent_config.system_prompt[:50] + "..." if len(agent_config.system_prompt) > 50 else agent_config.system_prompt
        table.add_row(agent_name, agent_config.model_name, sys_prompt)
        
    console.print(table)


def _show_models(ctx_obj: dict):
    from local_coder.orchestrator.config_loader import load_config
    try:
        config = load_config(ctx_obj["config_path"])
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
            model.backend, 
            model.model_id, 
            str(model.context_length)
        )
        
    console.print(table)
