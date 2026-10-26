import asyncio
import shlex
import sys

import pytest

from local_coder.tools.testing import RunTestsTool


def test_cancelling_test_tool_reaps_the_running_process(tmp_path, monkeypatch):
    processes = []
    real = asyncio.create_subprocess_exec
    async def track(*args, **kwargs):
        process = await real(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", track)
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n")
    async def scenario():
        tool = RunTestsTool(str(tmp_path))
        task = asyncio.create_task(tool.execute(framework=f'{shlex.quote(sys.executable)} {shlex.quote(str(script))}'))
        for _ in range(100):
            if processes:
                break
            await asyncio.sleep(.01)
        assert processes
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert processes[0].returncode is not None
        finally:
            if processes[0].returncode is None:
                processes[0].kill()
                await processes[0].wait()
    asyncio.run(scenario())
