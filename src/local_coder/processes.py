"""Keep agent-owned process trees bounded by their task lifecycle."""
from __future__ import annotations
import asyncio
import os
import shlex
import signal
import subprocess


def process_options():
    return ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
            else {"start_new_session": True})


def split_command(command):
    if os.name != "nt":
        return shlex.split(command)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'" else part
            for part in shlex.split(command, posix=False)]


async def terminate_tree(proc):
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(killer.wait(), 5)
        except asyncio.TimeoutError:
            killer.kill()
            await killer.wait()
            if proc.returncode is None:
                proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def communicate(proc, timeout=300):
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        await terminate_tree(proc)
        raise
