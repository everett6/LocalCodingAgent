import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from local_coder.orchestrator.config_loader import load_config
from local_coder.tools import create_tool_registry
from local_coder.tools.shell import RunCommandTool


def run(coro):
    return asyncio.run(coro)


def child_result(stdout=b"", stderr=b"", returncode=0):
    return SimpleNamespace(
        returncode=returncode, stdin=None,
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[stdout, b""])),
        stderr=SimpleNamespace(read=AsyncMock(side_effect=[stderr, b""])),
        wait=AsyncMock(return_value=returncode),
        communicate=AsyncMock(return_value=(stdout, stderr)),
    )


def test_sandboxed_command_uses_restricted_container_arguments(tmp_path):
    (tmp_path / "src").mkdir()
    process = child_result(b"ok\n")
    approve = AsyncMock(return_value=True)
    tool = RunCommandTool(str(tmp_path), approval_callback=approve)
    tool.sandbox_enabled = True
    tool.sandbox_runtime = "docker"
    tool.sandbox_image = "local/test-image:latest"

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
    assert "--entrypoint=sh" in argv
    assert argv[-3:] == ("local/test-image:latest", "-lc", "printf ok")
    assert "--env" not in argv and "--privileged" not in argv
    assert result.data["execution_outcome"] == "completed"
    assert result.data["exit_code"] == 0
    approve.assert_awaited_once()


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


@pytest.mark.parametrize("image", ["--privileged", "local/image --network=host", "\nlocal/image", "image," , "x" * 256])
def test_image_cannot_supply_runtime_options(tmp_path, image):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    tool.sandbox_image = image
    with patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"), \
            patch("local_coder.tools.shell.asyncio.create_subprocess_exec") as spawn:
        result = run(tool.execute("pwd"))
    assert not result.success
    assert result.data["execution_outcome"] == "not_started"
    spawn.assert_not_called()


def test_mount_delimiter_path_is_rejected_without_execution(tmp_path):
    root = tmp_path / "source,readonly=false"
    root.mkdir()
    tool = RunCommandTool(str(root))
    tool.sandbox_enabled = True
    with patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"), \
            patch("local_coder.tools.shell.asyncio.create_subprocess_exec") as spawn:
        result = run(tool.execute("pwd"))
    assert not result.success
    spawn.assert_not_called()


def test_root_uid_has_no_container_root_fallback(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    with patch("local_coder.tools.shell.os.getuid", return_value=0, create=True), \
            pytest.raises(ValueError, match="non-root"):
        tool._sandbox_argv("docker", "fixture", "pwd", tmp_path)


def test_sandbox_does_not_bypass_approval_or_block_policy(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    with patch("local_coder.tools.shell.asyncio.create_subprocess_exec") as spawn:
        assert "approval" in run(tool.execute("pip --version")).output
        assert "blocked" in run(tool.execute("rm -rf /")).output
    spawn.assert_not_called()


def test_binary_output_preserves_completed_result(tmp_path):
    child = child_result(b"done\xff")
    with patch("local_coder.tools.shell.asyncio.create_subprocess_shell", AsyncMock(return_value=child)):
        result = run(RunCommandTool(str(tmp_path)).execute("pwd"))
    assert result.success and result.output == "done\ufffd"
    assert result.data["execution_outcome"] == "completed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake-runtime process lifecycle")
@pytest.mark.parametrize("cancel", [False, True, "during-launch"])
def test_interrupted_container_cli_is_reaped_and_removal_requested(tmp_path, cancel):
    # This is a real disposable process lifecycle fixture, not a claim of real
    # Docker isolation. No supplied command is executed by this fake runtime.
    runtime = tmp_path / "fake-runtime"
    marker = tmp_path / "started.json"
    removed = tmp_path / "removed"
    runtime.write_text(f"#!{sys.executable}\n" +
        "import json, os, pathlib, sys, time\n" +
        f"marker = pathlib.Path({str(marker)!r})\n" +
        f"removed = pathlib.Path({str(removed)!r})\n" +
        "if sys.argv[1] == 'run':\n"
        "    marker.write_text(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}))\n"
        "    time.sleep(30)\n"
        "elif sys.argv[1:3] == ['rm', '--force']:\n"
        "    removed.write_text(sys.argv[3])\n")
    runtime.chmod(0o700)
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    tool._resolve_sandbox_runtime = lambda: str(runtime)

    async def scenario():
        task = asyncio.create_task(tool.execute("pwd", timeout=1))
        for _ in range(200):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        assert marker.exists()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert not result.success
            assert result.data["execution_outcome"] == "unknown"
            assert result.data["mutation_completion_unknown"] is True
            assert "before retrying" in result.output
    create_process = asyncio.create_subprocess_exec

    async def delayed_handle(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        if cancel == "during-launch" and args[1] == "run":
            # Simulate the process already existing before asyncio hands its
            # handle back. Cancellation must still acquire and reap it.
            await asyncio.sleep(.1)
        return process

    with patch("local_coder.tools.shell.asyncio.create_subprocess_exec", delayed_handle):
        run(scenario())
    record = json.loads(marker.read_text())
    assert removed.read_text() == record["argv"][record["argv"].index("--name") + 1]
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


def test_timeout_surfaces_cleanup_failure_without_echoing_runtime_error(tmp_path):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    child = SimpleNamespace(returncode=1)
    calls = AsyncMock(side_effect=[asyncio.TimeoutError(), (b"", b"private runtime diagnostic")])
    with patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"), \
            patch("local_coder.tools.shell.asyncio.create_subprocess_exec", AsyncMock(return_value=child)), \
            patch("local_coder.tools.shell.communicate", calls):
        result = run(tool.execute("pwd", timeout=1))
    assert not result.success
    assert "cleanup failed" in result.output
    assert "private runtime diagnostic" not in result.output
    assert result.data["mutation_completion_unknown"] is True


def test_cancelled_cleanup_failure_is_visible_without_swallowing_cancellation(tmp_path, caplog):
    tool = RunCommandTool(str(tmp_path))
    tool.sandbox_enabled = True
    calls = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"private runtime diagnostic")])
    with patch("local_coder.tools.shell.shutil.which", return_value="/usr/bin/docker"), \
            patch("local_coder.tools.shell.asyncio.create_subprocess_exec", AsyncMock(return_value=child_result(returncode=1))), \
            patch("local_coder.tools.shell.communicate", calls), \
            pytest.raises(asyncio.CancelledError) as error:
        run(tool.execute("pwd"))
    assert any("cleanup failed" in note for note in error.value.__notes__)
    assert "cleanup failed" in caplog.text
    assert "private runtime diagnostic" not in caplog.text


@pytest.mark.parametrize("timeout", [0, -1, 3601, True, 1.5])
def test_invalid_timeout_cannot_start_a_process(tmp_path, timeout):
    with patch("local_coder.tools.shell.asyncio.create_subprocess_shell") as spawn:
        result = run(RunCommandTool(str(tmp_path)).execute("pwd", timeout=timeout))
    assert not result.success
    assert result.data["execution_outcome"] == "not_started"
    spawn.assert_not_called()
