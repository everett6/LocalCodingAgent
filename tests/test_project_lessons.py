import json

import pytest
from click.testing import CliRunner

from local_coder.cli.main import cli
from local_coder.memory.lessons import (
    MAX_LESSON_CHARS,
    MAX_LESSONS,
    LessonStoreError,
    ProjectLessonStore,
)
from local_coder.orchestrator.sessions import SessionStore


def test_lessons_normalize_deduplicate_list_and_delete(tmp_path):
    store = ProjectLessonStore(tmp_path)

    first = store.add("  Prefer   focused tests before   the full suite. ")
    duplicate = store.add("prefer focused tests before the full suite.")

    assert duplicate == first
    assert store.list() == [first]
    assert store.render() == "- Prefer focused tests before the full suite."
    assert store.delete(first["id"]) is True
    assert store.delete(first["id"]) is False
    assert store.render() == ""


def test_lessons_validate_length_and_capacity(tmp_path):
    store = ProjectLessonStore(tmp_path)
    with pytest.raises(LessonStoreError, match="cannot be empty"):
        store.add("  \n ")
    with pytest.raises(LessonStoreError, match="characters or fewer"):
        store.add("x" * (MAX_LESSON_CHARS + 1))

    for index in range(MAX_LESSONS):
        store.add(f"Lesson {index}")
    with pytest.raises(LessonStoreError, match="limit reached"):
        store.add("One too many")


def test_corrupt_lesson_data_fails_explicitly(tmp_path):
    store = ProjectLessonStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 1, "lessons": [{"id": "bad"}]}))

    with pytest.raises(LessonStoreError, match="Invalid lesson entry"):
        store.list()


def test_lesson_commands_add_list_and_remove(tmp_path):
    runner = CliRunner()
    options = ["--project", str(tmp_path)]

    added = runner.invoke(cli, [*options, "learn", "Use focused tests"])
    listed = runner.invoke(cli, [*options, "lessons"])
    lesson_id = ProjectLessonStore(tmp_path).list()[0]["id"]
    removed = runner.invoke(cli, [*options, "forget", lesson_id])

    assert added.exit_code == 0, added.output
    assert listed.exit_code == 0 and "Use focused tests" in listed.output
    assert removed.exit_code == 0 and "Removed project lesson" in removed.output


def test_builtin_skills_are_listed_and_reserved_from_custom_commands(tmp_path):
    from local_coder.cli import main as cli_main

    command_path = tmp_path / ".local-coder" / "commands" / "implement.md"
    command_path.parent.mkdir(parents=True)
    command_path.write_text("Replace the built-in skill")
    commands = cli_main._load_custom_commands({"project_root": str(tmp_path)}, warn=False)

    assert "/implement" in commands
    assert commands["/implement"].scope == "built-in skill"
    assert commands["/implement"].template.startswith("Implement this request:")
    assert "Debug this symptom: a reproducible issue" in commands["/debug"].expand("a reproducible issue")


def test_run_slash_lesson_command_uses_the_same_operator_workflow(tmp_path):
    from local_coder.cli import main as cli_main

    ctx = {"project_root": str(tmp_path)}
    assert cli_main._run_extension_command("/learn Keep changes focused", ctx)
    assert ProjectLessonStore(tmp_path).list()[0]["text"] == "Keep changes focused"


def test_progress_command_displays_saved_session_state(tmp_path):
    session = SessionStore(str(tmp_path)).create("tracked")
    turn = session.start_turn("add an API endpoint")
    session.checkpoint(turn).put("explore", "inspected routes")

    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "progress"])

    assert result.exit_code == 0, result.output
    assert "add an API endpoint" in result.output
    assert "explore" in result.output


def test_progress_command_reports_unknown_session(tmp_path):
    result = CliRunner().invoke(cli, ["--project", str(tmp_path), "progress", "missing"])

    assert result.exit_code != 0
    assert "Session not found: missing" in result.output
