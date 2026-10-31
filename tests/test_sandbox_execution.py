import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from local_coder.orchestrator.config_loader import load_config
from local_coder.tools import create_tool_registry
from local_coder.tools.shell import RunCommandTool
from local_coder.types import ApprovalConfig


def run(coro):
    return asyncio.run(coro)


def test_sandboxed_command_uses_restricted_container_arguments(tmp_path):
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"ok\n", b"")),
    )
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    tool.sandbox_runtime = "docker"
    tool.sandbox_image = "local/test-image:latest"
    tool.approval = ApprovalConfig(require_approval_for_commands=False)  # Bypass approval check

    with (
        patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"),
        patch("local_coder.tools.shell.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn,
    ):
        result = run(tool.execute("printf ok", working_dir="src"))

    assert result.success
    assert result.output == "ok"
    argv = spawn.await_args.args
    assert argv[:3] == ("/usr/bin/docker", "run", "--rm")
    assert "--pull=never" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert f"type=bind,source={tmp_path.resolve()},target=/workspace" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace/src"
    assert argv[-3:] == ("sh", "-lc", "printf ok")


def test_sandbox_runtime_auto_prefers_docker_then_podman(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    tool.sandbox_runtime = "auto"
    tool.sandbox_image = "local/image"

    with patch("local_coder.tools.shell.shutil.which", side_effect=lambda name: {
        "docker": None,
        "podman": "/usr/bin/podman",
    }.get(name)):
        assert tool._resolve_sandbox_runtime() == "/usr/bin/podman"


def test_sandbox_missing_runtime_fails_explicitly_without_running_command(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True

    with (
        patch("local_coder.tools.shell.shutil.which", return_value=None),
        patch("local_coder.tools.shell.asyncio.create_subprocess_shell") as shell,
        patch("local_coder.tools.shell.asyncio.create_subprocess_exec") as exec_process,
    ):
        result = run(tool.execute("pwd"))

    assert not result.success
    assert "runtime 'auto' is unavailable" in result.output
    shell.assert_not_called()
    exec_process.assert_not_called()


def test_configured_sandbox_is_wired_to_shell_tool(tmp_path):
    (tmp_path / ".local-coder.yaml").write_text(
        "tools:\n"
        "  command_sandbox: true\n"
        "  sandbox_runtime: podman\n"
        "  sandbox_image: local/project:dev\n"
    )
    config = load_config(project_root=str(tmp_path))

    registry = create_tool_registry(str(tmp_path))
    registry.apply_settings(config.tools)
    command_tool = registry.get_tool("run_command")

    assert command_tool.sandbox_enabled is True
    assert command_tool.sandbox_runtime == "podman"
    assert command_tool.sandbox_image == "local/project:dev"


def test_invalid_sandbox_runtime_is_rejected_by_config(tmp_path):
    (tmp_path / ".local-coder.yaml").write_text(
        "tools:\n  sandbox_runtime: unknown\n"
    )

    try:
        load_config(project_root=str(tmp_path))
    except ValueError as exc:
        assert "sandbox_runtime" in str(exc)
    else:
        raise AssertionError("invalid sandbox runtime should be rejected")


def test_sandbox_missing_image_fails_with_clear_message(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    tool.sandbox_runtime = "docker"
    tool.sandbox_image = "nonexistent/image:latest"

    with (
        patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"),
        patch("local_coder.tools.shell.asyncio.create_subprocess_exec") as exec_process,
    ):
        # image inspect returns non-zero (image not found)
        proc = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"Error: No such image")))
        exec_process.return_value = proc
        result = run(tool.execute("pwd"))

    assert not result.success
    assert "not found locally" in result.output
    exec_process.assert_called_once()
