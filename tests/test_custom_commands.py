import pytest
from click.testing import CliRunner

from local_coder import custom_commands
from local_coder.cli import main as cli_main
from local_coder.cli import ui
from local_coder.cli.main import cli


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_HOME", str(tmp_path / "home"))
    yield
    ui.set_custom_commands(())


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _project_cmds(tmp_path):
    return tmp_path / "proj" / ".local-coder" / "commands"


def test_discover_names_front_matter_and_namespaces(tmp_path):
    cmds = _project_cmds(tmp_path)
    _write(cmds, "review-auth.md", "---\ndescription: Review auth\nargument-hint: <path>\nmode: plan\n---\nReview $ARGUMENTS\n")
    _write(cmds, "sec/Triage.md", "Triage the finding in $1\n")
    found, warnings = custom_commands.discover(tmp_path / "proj")

    assert warnings == []
    by_name = {c.name: c for c in found}
    assert set(by_name) == {"/review-auth", "/sec:triage"}
    assert by_name["/review-auth"].description == "Review auth"
    assert by_name["/review-auth"].argument_hint == "<path>"
    assert by_name["/review-auth"].mode == "plan"
    # No front matter: description falls back to the first line, mode to run.
    assert by_name["/sec:triage"].description == "Triage the finding in $1"
    assert by_name["/sec:triage"].mode == "run"


def test_project_command_overrides_user_command(tmp_path):
    _write(tmp_path / "home" / "commands", "explain.md", "user version")
    _write(tmp_path / "home" / "commands", "mine.md", "only mine")
    _write(_project_cmds(tmp_path), "explain.md", "project version")
    found, _ = custom_commands.discover(tmp_path / "proj")

    by_name = {c.name: c for c in found}
    assert by_name["/explain"].template == "project version"
    assert by_name["/explain"].scope == "project"
    assert by_name["/mine"].scope == "user"


def test_builtin_names_cannot_be_overridden(tmp_path):
    _write(_project_cmds(tmp_path), "quit.md", "rm -rf everything")
    found, warnings = custom_commands.discover(tmp_path / "proj", reserved=ui.builtin_names())

    assert found == []
    assert len(warnings) == 1 and "/quit is a built-in command" in warnings[0]


def test_unusable_files_are_skipped(tmp_path):
    cmds = _project_cmds(tmp_path)
    _write(cmds, "empty.md", "---\ndescription: nothing\n---\n   \n")
    _write(cmds, "bad name!.md", "hello")
    _write(cmds, "notes.txt", "not markdown")
    _write(cmds, "ok.md", "---\nmode: nonsense\n---\nbody")
    found, _ = custom_commands.discover(tmp_path / "proj")

    assert [c.name for c in found] == ["/ok"]
    assert found[0].mode == "run"


def _cmd(template):
    return custom_commands.CustomCommand(name="/x", template=template, path=None, scope="project")


def test_expand_arguments_and_positionals():
    cmd = _cmd("Fix issue $1 in $2. Full: $ARGUMENTS. Missing: [$3]")
    assert cmd.expand('42 "src/app.py"') == 'Fix issue 42 in src/app.py. Full: 42 "src/app.py". Missing: []'


def test_expand_appends_arguments_when_template_has_no_placeholder():
    assert _cmd("Explain this file.").expand("foo.py") == "Explain this file.\n\nfoo.py"
    assert _cmd("Explain this file.").expand("") == "Explain this file."


def test_expand_does_not_reexpand_typed_placeholders():
    assert _cmd("Say: $ARGUMENTS").expand("$1 and $ARGUMENTS") == "Say: $1 and $ARGUMENTS"


def test_expand_tolerates_unbalanced_quotes():
    assert _cmd("first=$1").expand('"oops') == 'first="oops'


def test_split_invocation():
    assert custom_commands.split_invocation("/Fix-Issue 42  fast ") == ("/fix-issue", "42  fast")
    assert custom_commands.split_invocation("/review") == ("/review", "")


# --- CLI / REPL integration ---------------------------------------------------

def test_custom_commands_appear_in_help_and_completion(tmp_path):
    _write(_project_cmds(tmp_path), "review-auth.md", "---\ndescription: Review auth\n---\nbody")
    found, _ = custom_commands.discover(tmp_path / "proj", reserved=ui.builtin_names())
    ui.set_custom_commands(found)

    assert "/review-auth" in ui.complete_command("/rev")
    assert ui.suggest_command("/review-aut") == "/review-auth"
    from rich.console import Console
    import io
    console = Console(file=io.StringIO(), width=100, color_system=None)
    console.print(ui.build_help_table())
    text = console.file.getvalue()
    assert "Custom" in text and "/review-auth" in text and "Review auth" in text
    assert text.index("/quit") < text.index("/review-auth")


def test_run_extension_command_expands_and_dispatches(tmp_path, monkeypatch):
    _write(_project_cmds(tmp_path), "fix.md", "Fix issue #$1")
    _write(_project_cmds(tmp_path), "design.md", "---\nmode: plan\n---\nDesign $ARGUMENTS")
    calls = []
    monkeypatch.setattr(cli_main, "_run_request", lambda prompt, ctx: calls.append(("run", prompt)))
    monkeypatch.setattr(cli_main, "_run_plan", lambda prompt, ctx: calls.append(("plan", prompt)))
    ctx_obj = {"project_root": str(tmp_path / "proj"), "config_path": None}

    assert cli_main._run_extension_command("/fix 17", ctx_obj) is True
    assert cli_main._run_extension_command("/design a cache", ctx_obj) is True
    assert cli_main._run_extension_command("/nope", ctx_obj) is False
    assert calls == [("run", "Fix issue #17"), ("plan", "Design a cache")]


def test_run_subcommand_runs_custom_command(tmp_path, monkeypatch):
    _write(_project_cmds(tmp_path), "fix.md", "Fix issue #$1")
    calls = []
    monkeypatch.setattr(cli_main, "_run_request", lambda prompt, ctx: calls.append(prompt))

    result = CliRunner().invoke(cli, ["--project", str(tmp_path / "proj"), "run", "/fix", "9"])
    assert result.exit_code == 0, result.output
    assert calls == ["Fix issue #9"]

    # Anything else starting with "/" is still an ordinary request.
    calls.clear()
    CliRunner().invoke(cli, ["--project", str(tmp_path / "proj"), "run", "/src/app.py", "crashes"])
    assert calls == ["/src/app.py crashes"]


def test_commands_subcommand_lists_files(tmp_path):
    _write(_project_cmds(tmp_path), "fix.md", "---\ndescription: Fix an issue\nargument-hint: <n>\n---\nFix $1")
    result = CliRunner().invoke(cli, ["--project", str(tmp_path / "proj"), "commands"])

    assert result.exit_code == 0
    assert "/fix <n>" in result.output and "Fix an issue" in result.output


def test_commands_subcommand_when_empty(tmp_path):
    (tmp_path / "proj").mkdir()
    result = CliRunner().invoke(cli, ["--project", str(tmp_path / "proj"), "commands"])
    assert result.exit_code == 0
    assert "No custom commands" in result.output
