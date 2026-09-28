import asyncio
import io
from datetime import datetime

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from local_coder.cli import ui
from local_coder.cli.main import cli
from local_coder.types import AgentEvent


def _console(**kwargs):
    return Console(file=io.StringIO(), width=100, color_system=None, **kwargs)


def _text(console):
    return console.file.getvalue()


# --- command registry / completion / suggestions -----------------------------

def test_command_names_include_aliases():
    names = ui.command_names()
    for expected in ("/plan", "/review", "/test", "/help", "/quit", "/exit", "/q", "/rollback"):
        assert expected in names
    assert len(names) == len(set(names))


def test_complete_command_prefixes():
    assert ui.complete_command("/pl") == ["/plan"]
    assert ui.complete_command("/check") == ["/checkpoint", "/checkpoints"]
    assert ui.complete_command("/") == sorted(ui.command_names())
    assert ui.complete_command("/PL") == ["/plan"]
    assert ui.complete_command("plan") == []
    assert ui.complete_command("/nope") == []


@pytest.mark.parametrize("typo,expected", [
    ("/pln", "/plan"),
    ("/hlep", "/help"),
    ("/reviw", "/review"),
    ("/tset", "/test"),
    ("/rollbak", "/rollback"),
    ("/stat", "/status"),
])
def test_suggest_command_finds_closest(typo, expected):
    assert ui.suggest_command(typo) == expected


def test_suggest_command_returns_none_for_garbage():
    assert ui.suggest_command("/xyzzyqwerty") is None


def test_unknown_command_message_mentions_suggestion():
    assert "Did you mean [bold]/plan[/bold]?" in ui.unknown_command_message("/pln")
    assert "Did you mean" not in ui.unknown_command_message("/xyzzyqwerty")


def test_help_table_lists_every_command_with_description():
    console = _console()
    console.print(ui.build_help_table())
    out = _text(console)
    for command in ui.COMMANDS:
        assert command.name in out
        assert command.help in out
    assert "/plan <request>" in out
    assert "/rollback <id>" in out
    assert "/quit, /exit, /q" in out
    # Descriptions line up in a single column.
    columns = {line.index(c.help) for c in ui.COMMANDS for line in out.splitlines() if c.help in line}
    assert len(columns) == 1


# --- history / prompt ---------------------------------------------------------

def test_history_path_is_under_state_dir(tmp_path):
    assert ui.history_path(tmp_path) == tmp_path / ".local-coder" / "history"
    assert ui.history_path(str(tmp_path)) == tmp_path / ".local-coder" / "history"


def test_save_history_fails_silently_when_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / ".local-coder"
    blocker.write_text("not a directory")
    session = ui.PromptSession(_console(), tmp_path, input_func=lambda p: "")
    session.readline_enabled = True  # force the write path
    if ui._readline is None:
        monkeypatch.setattr(ui, "_readline", type("R", (), {"write_history_file": staticmethod(lambda p: None)}))
    session.save_history()  # must not raise


def _scripted(*items):
    items = list(items)

    def fake_input(prompt):
        item = items.pop(0)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return item
    return fake_input


def test_prompt_single_ctrl_c_shows_hint_and_continues(tmp_path):
    console = _console()
    session = ui.PromptSession(console, tmp_path, input_func=_scripted(KeyboardInterrupt, "hello"))
    assert session.read() == "hello"
    assert "Ctrl+C again" in _text(console)


def test_prompt_double_ctrl_c_exits(tmp_path):
    session = ui.PromptSession(_console(), tmp_path, input_func=_scripted(KeyboardInterrupt, KeyboardInterrupt))
    assert session.read() is None


def test_prompt_ctrl_c_counter_resets_after_input(tmp_path):
    session = ui.PromptSession(
        _console(), tmp_path, input_func=_scripted(KeyboardInterrupt, "a", KeyboardInterrupt, "b"),
    )
    assert session.read() == "a"
    assert session.read() == "b"


def test_prompt_ctrl_d_exits(tmp_path):
    session = ui.PromptSession(_console(), tmp_path, input_func=_scripted(EOFError))
    assert session.read() is None


def test_ansi_prompt_wraps_escape_codes_for_readline():
    console = Console(file=io.StringIO(), force_terminal=True, color_system="standard")
    rendered = ui._ansi_prompt(console, "[bold green]> [/bold green]")
    assert "> " in rendered
    # Every escape sequence is bracketed by readline's ignore markers.
    stripped = rendered
    while "\001" in stripped:
        start = stripped.index("\001")
        end = stripped.index("\002", start)
        stripped = stripped[:start] + stripped[end + 1:]
    assert "\x1b" not in stripped
    assert stripped == "> "


# --- banner -------------------------------------------------------------------

def test_banner_survives_broken_config(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: [this is: not valid")
    console = _console()
    console.print(ui.build_banner({"project_root": str(tmp_path), "config_path": str(bad), "yolo": True}, "1.2.3"))
    out = _text(console)
    assert str(tmp_path) in out
    assert "--yolo" in out
    assert "1.2.3" in out


def test_banner_shows_model_override(tmp_path):
    console = _console()
    console.print(ui.build_banner({"project_root": str(tmp_path), "config_path": None, "model": "big"}))
    assert "big (--model)" in _text(console)


# --- live progress renderer ---------------------------------------------------

class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _event(source, message, event_type="phase"):
    return AgentEvent(source=source, event_type=event_type, message=message,
                      timestamp=datetime(2026, 1, 1, 12, 34, 56))


def test_role_label_and_role_of():
    assert ui.role_of("CODER-1") == "coder"
    assert ui.role_of("ORCHESTRATOR") == "orchestrator"
    assert ui.role_label("CODER-2") == "code2"
    assert ui.role_label("REVIEWER") == "review"
    assert ui.role_label("MYSTERY") == "myster"


def test_format_elapsed():
    assert ui.format_elapsed(3.21) == "3.2s"
    assert ui.format_elapsed(125) == "2m05s"
    assert ui.format_elapsed(3725) == "1h02m"


def test_renderer_state_transitions():
    console = _console()
    clock = FakeClock()
    renderer = ui.ProgressRenderer(console, "request", clock=clock)
    assert renderer.state == "idle"
    renderer.start()
    assert renderer.state == "running"

    renderer.handle(_event("EXPLORER", "Scanning files\nsecond line"))
    assert renderer.role == "explorer"
    assert renderer.activity == "Scanning files"
    renderer.handle(_event("CODER-1", "edit foo.py", "tool_called"))
    assert renderer.role == "coder"
    clock.now += 4.5
    assert "coder" in renderer.status_text().plain
    assert "4.5s" in renderer.status_text().plain

    renderer.pause()
    assert renderer.state == "paused"
    renderer.resume()
    assert renderer.state == "running"

    renderer.finish("done")
    assert renderer.state == "done"
    clock.now += 100  # elapsed frozen after finish
    assert renderer.elapsed == pytest.approx(4.5)
    out = _text(console)
    assert "12:34:56 explr" in out
    assert "code1" in out
    assert "request done in 4.5s" in out
    assert "2 events" in out


def test_pause_and_resume_are_noops_when_not_running():
    renderer = ui.ProgressRenderer(_console(), "x")
    renderer.pause()
    assert renderer.state == "idle"
    renderer.resume()
    assert renderer.state == "idle"


def test_progress_context_reports_outcomes():
    console = _console()
    with ui.progress(console, "plan") as renderer:
        assert ui.active_renderer() is renderer
        ui.render_event(console, _event("PLANNER", "planning"))
    assert ui.active_renderer() is None
    assert renderer.outcome == "done"

    with pytest.raises(RuntimeError):
        with ui.progress(console, "review") as renderer:
            raise RuntimeError("boom")
    assert renderer.outcome == "failed"

    for exc in (KeyboardInterrupt, asyncio.CancelledError, click.Abort):
        with pytest.raises(exc):
            with ui.progress(console, "tests") as renderer:
                raise exc()
        assert renderer.outcome == "cancelled"

    out = _text(console)
    assert "plan done" in out
    assert "review failed" in out
    assert "tests cancelled" in out


def test_paused_context_pauses_active_renderer():
    console = _console()
    with ui.progress(console, "request") as renderer:
        with ui.paused():
            assert renderer.state == "paused"
        assert renderer.state == "running"


def test_render_event_without_active_renderer_prints_plainly():
    console = _console()
    ui.render_event(console, _event("TESTER", "3 passed"))
    assert "test" in _text(console)
    assert "3 passed" in _text(console)


# --- interactive mode end to end (stdin is not a TTY under CliRunner) --------

def test_interactive_help_and_unknown_command(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path)], input="/hlep\n/help\n/quit\n")
    assert result.exit_code == 0
    assert "Did you mean /help?" in result.output
    assert "/rollback <id>" in result.output
    assert "Restore a checkpoint" in result.output


def test_interactive_request_error_does_not_kill_repl(tmp_path, monkeypatch):
    from local_coder.cli import main

    calls = []

    def fake_run_request(request, ctx_obj):
        calls.append(request)
        if request == "cancel me":
            raise KeyboardInterrupt
        raise click.ClickException("nope")

    monkeypatch.setattr(main, "_run_request", fake_run_request)
    result = CliRunner().invoke(cli, ["--project", str(tmp_path)], input="first\ncancel me\nthird\n")
    assert result.exit_code == 0
    assert calls == ["first", "cancel me", "third"]
    assert "Request cancelled" in result.output
