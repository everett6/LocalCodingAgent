"""Lossless, content-addressed local scan results, before prompt extraction."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path

from local_coder.statedir import state_path
from local_coder.types import ToolResult

MAX_SCAN_BYTES = 64 * 1024 * 1024
PREFIX = '.local-coder/security/scans/'


class ScanArchive:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def save(self, tool: str, arguments: dict, result: ToolResult) -> str:
        """Reject oversized records explicitly; never silently truncate evidence."""
        payload = {'version': 1, 'tool': tool, 'arguments': arguments,
                   'result': result.model_dump(mode='json')}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        if len(raw) > MAX_SCAN_BYTES:
            raise ValueError('Full scan exceeds the 64 MiB archive limit; narrow the scan')
        digest = hashlib.sha256(raw).hexdigest()
        reference = PREFIX + digest + '.json'
        path = state_path(self.root, reference)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            self.read(reference)
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return reference

    def save_capture(self, tool: str, arguments: dict, stdout: bytes, stderr: bytes, *,
                     returncode: int | None, stream_limit: int, complete: bool,
                     disposition: str) -> str:
        """Archive exact process bytes, independently of later JSON parsing.

        Each stream is at most 16 MiB; base64 keeps the two-stream archive below
        the existing 64 MiB record limit even for arbitrary diagnostic bytes.
        Incomplete captures are explicitly labeled and never parsed as scans.
        """
        if (type(stream_limit) is not int or not 64 <= stream_limit <= 16 * 1024 * 1024
                or not isinstance(stdout, bytes) or not isinstance(stderr, bytes)
                or len(stdout) > stream_limit or len(stderr) > stream_limit
                or type(complete) is not bool or disposition not in {"complete", "over_limit", "timeout"}):
            raise ValueError('Scanner capture exceeds its explicit bounded stream limit')
        result = ToolResult(success=complete, data={
            'stdout_base64': base64.b64encode(stdout).decode('ascii'),
            'stderr_base64': base64.b64encode(stderr).decode('ascii'),
            'returncode': returncode,
            'capture': {'complete': complete, 'disposition': disposition,
                        'encoding': 'base64', 'scope': 'raw_process_bytes_only',
                        'verification': 'not_assessed', 'stream_limit_bytes': stream_limit,
                        'total_limit_bytes': stream_limit * 2,
                        'stdout_bytes_retained': len(stdout), 'stderr_bytes_retained': len(stderr)},
        })
        return self.save(tool, arguments, result)

    def read(self, reference: str) -> dict:
        if (not isinstance(reference, str) or not reference.startswith(PREFIX)
                or len(reference) != len(PREFIX) + 69 or not reference.endswith('.json')):
            raise ValueError('Invalid scan archive reference')
        digest = reference[len(PREFIX):-5]
        if any(char not in '0123456789abcdef' for char in digest):
            raise ValueError('Invalid scan archive digest')
        path = state_path(self.root, reference)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_SCAN_BYTES:
                raise ValueError('Scan archive must be a bounded regular file')
            raw = stream.read(MAX_SCAN_BYTES + 1)
        if len(raw) > MAX_SCAN_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Scan archive digest mismatch')
        return json.loads(raw)
