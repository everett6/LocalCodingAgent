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


OUTPUT_TRUNCATION_MARKER = b"[output truncated; showing tail]\n"


async def communicate(
    proc: asyncio.subprocess.Process,
    timeout: float = 300,
    *,
    max_stream_bytes: int = 200_000,
    max_total_bytes: int = 400_000,
) -> tuple[bytes | None, bytes | None]:
    """Drain both pipes through EOF, retaining bounded tails including labels.

    Each active pipe gets an equal share of the combined budget, capped by
    the per-stream budget. Readers keep draining after reaching their limit
    so noisy children cannot block on a full pipe. Timeout/cancellation kill
    the process tree while these readers are still active.
    """
    for value in (max_stream_bytes, max_total_bytes):
        if type(value) is not int or value < 2 * len(OUTPUT_TRUNCATION_MARKER):
            raise ValueError("Output bounds must be integers large enough for truncation labels")
    streams = sum(stream is not None for stream in (proc.stdout, proc.stderr))
    limit = min(max_stream_bytes, max_total_bytes // max(streams, 1))

    async def drain(stream: asyncio.StreamReader | None) -> bytes | None:
        if stream is None:
            return None
        tail = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            tail.extend(chunk)
            if len(tail) > limit:
                truncated = True
                del tail[:-limit]
        if truncated:
            keep = limit - len(OUTPUT_TRUNCATION_MARKER)
            del tail[:-keep]
            # A byte cap may split the first UTF-8 character. Existing callers
            # decode valid textual child output strictly, so omit that partial
            # character without changing how arbitrary binary data is handled.
            while tail and tail[0] & 0xC0 == 0x80:
                del tail[0]
            return OUTPUT_TRUNCATION_MARKER + bytes(tail)
        return bytes(tail)

    async def collect() -> tuple[bytes | None, bytes | None]:
        if proc.stdin is not None:
            proc.stdin.close()
        stdout, stderr, _ = await asyncio.gather(drain(proc.stdout), drain(proc.stderr), proc.wait())
        return stdout, stderr

    capture = asyncio.create_task(collect())
    try:
        return await asyncio.wait_for(asyncio.shield(capture), timeout=timeout)
    except BaseException:
        try:
            await terminate_tree(proc)
        finally:
            # A descendant outside the process group might retain a pipe;
            # cleanup must still finish in bounded time after the tree dies.
            try:
                await asyncio.wait_for(capture, timeout=1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                capture.cancel()
        raise
