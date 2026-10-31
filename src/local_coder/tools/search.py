"""Bounded read-only filename and native-regex source-snapshot searches."""
from __future__ import annotations
import asyncio
import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import time
from typing import Any

from local_coder.context.privacy import private_read_path
from local_coder.context.source_snapshot import read_source_snapshot
from local_coder.processes import process_options, terminate_tree
from local_coder.tools.base import Tool
from local_coder.tools.scan_scope import IGNORED, _directory_entries
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace

SEARCH_SECONDS = 10.0
MAX_FILES = 512
MAX_ENTRIES = 10_000
MAX_DIRECTORIES = 512
MAX_DEPTH = 32
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CAPTURE_BYTES = 64_000
MAX_LIST_BYTES = 256_000
MAX_RESULT_BYTES = 64_000


def _coverage(engine):
    return {"status": "complete", "engine": engine, "files_considered": 0,
            "files_searched": 0, "bytes_read": 0, "entries_considered": 0, "skipped": {},
            "read_bytes_reserved": 0, "read_budget_used": 0,
            "result_bytes": 0,
            "enumeration_scope": "returned_file_records" if engine == "rg" else "walked_entries",
            "limits": {"files": MAX_FILES, "entries": MAX_ENTRIES, "file_bytes": MAX_FILE_BYTES,
                       "total_bytes": MAX_TOTAL_BYTES, "timeout_seconds": SEARCH_SECONDS},
            "gitignore_respected": engine == "rg"}


def _partial(coverage, reason):
    coverage["status"] = "partial"
    coverage["skipped"][reason] = coverage["skipped"].get(reason, 0) + 1


def _target(workspace, path):
    lexical = Path(os.path.abspath(workspace.root / (path or ".")))
    if private_read_path(lexical, workspace.root):
        raise ValueError("Private or out-of-workspace paths are not searched")
    resolved = workspace.resolve(lexical)
    if lexical != resolved:
        raise ValueError("Symbolic-link search targets are not supported")
    if any(part in IGNORED for part in resolved.relative_to(workspace.root).parts):
        raise ValueError("Dependency, generated and engine-state directories are excluded")
    return resolved


def _glob_matches(path, target, pattern):
    if not pattern:
        return True
    relative = path.relative_to(target) if target.is_dir() else Path(path.name)
    return relative.match(pattern) or (pattern.startswith("**/") and relative.match(pattern[3:]))


def _validate_glob(pattern):
    if (not isinstance(pattern, str) or not 1 <= len(pattern) <= 512 or "\0" in pattern
            or Path(pattern).is_absolute() or ".." in Path(pattern).parts):
        raise ValueError("File patterns must be bounded relative globs without parent traversal")


def _eligible(root, path, coverage):
    if private_read_path(path, root):
        _partial(coverage, "private_path")
        return False
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _partial(coverage, "nonregular_link_or_alias")
            return False
        return True
    except OSError:
        _partial(coverage, "unreadable")
        return False


def _walk(root, target, coverage, stop, deadline):
    """Prune before descent; descriptor-backed directory opens reject links."""
    info = target.lstat()
    if not stat.S_ISDIR(info.st_mode):
        return [target] if _eligible(root, target, coverage) else []
    found, pending, directories = [], [(target, 0)], 0
    while pending:
        directory, depth = pending.pop()
        if stop.is_set() or time.monotonic() >= deadline:
            _partial(coverage, "deadline_or_cancel")
            break
        if directories >= MAX_DIRECTORIES:
            _partial(coverage, "directory_limit")
            break
        directories += 1
        try:
            with _directory_entries(root, directory) as entries:
                for entry in entries:
                    if (stop.is_set() or time.monotonic() >= deadline or len(found) >= MAX_FILES
                            or coverage["entries_considered"] >= MAX_ENTRIES):
                        _partial(coverage, "enumeration_limit")
                        return sorted(found)
                    coverage["entries_considered"] += 1
                    path = directory / entry.name
                    if private_read_path(path, root):
                        _partial(coverage, "private_path")
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name in IGNORED:
                                continue
                            if depth >= MAX_DEPTH or len(pending) + directories >= MAX_DIRECTORIES:
                                _partial(coverage, "depth_or_directory_limit")
                            else:
                                pending.append((path, depth + 1))
                        elif _eligible(root, path, coverage):
                            found.append(path)
                    except OSError:
                        _partial(coverage, "unreadable")
        except (OSError, ValueError):
            _partial(coverage, "unreadable_directory")
    return sorted(found)


@dataclass
class _Capture:
    code: int | None
    stdout: bytes
    limited: bool
    diagnostic_limited: bool


async def _capture(argv, root, timeout, data=None, limit=MAX_CAPTURE_BYTES):
    """Feed bounded bytes while draining both pipes and reaping owned children."""
    if timeout <= 0:
        raise asyncio.TimeoutError
    environment = os.environ.copy()
    environment.pop("RIPGREP_CONFIG_PATH", None)
    environment.pop("GREP_OPTIONS", None)
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(root), env=environment,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **process_options())
    overflow = asyncio.Event()
    limited = {"stdout": False, "stderr": False}

    async def read(stream, name, cap):
        head, seen = bytearray(), 0
        while chunk := await stream.read(8192):
            seen += len(chunk)
            if name == "stdout":
                head.extend(chunk[:max(0, cap - len(head))])
            if seen > cap:
                limited[name] = True
                overflow.set()
        return bytes(head)

    async def feed():
        if data is None:
            return
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    stdout = asyncio.create_task(read(proc.stdout, "stdout", limit))
    stderr = asyncio.create_task(read(proc.stderr, "stderr", 4096))
    feeder = asyncio.create_task(feed())
    waiter = asyncio.create_task(proc.wait())
    exceeded = asyncio.create_task(overflow.wait())
    tasks = (stdout, stderr, feeder, waiter, exceeded)
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait((waiter, exceeded), return_when=asyncio.FIRST_COMPLETED)
            if overflow.is_set() and proc.returncode is None:
                await terminate_tree(proc)
            await asyncio.gather(stdout, stderr, feeder, waiter)
        return _Capture(proc.returncode, stdout.result(), limited["stdout"], limited["stderr"])
    except BaseException:
        if proc.returncode is None:
            await terminate_tree(proc)
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _candidates(root, target, file_pattern, has_rg, coverage, stop, deadline):
    if not has_rg:
        values = await asyncio.to_thread(_walk, root, target, coverage, stop, deadline)
        if file_pattern and file_pattern.startswith("!"):
            return [path for path in values if not _glob_matches(path, target, file_pattern[1:])]
        return [path for path in values if _glob_matches(path, target, file_pattern)]
    argv = ["rg", "--files", "--null", "--no-config"]
    if file_pattern:
        argv += ["-g", file_pattern]
    # Host exclusions follow caller globs so broad includes cannot re-enable them.
    for directory in sorted(IGNORED):
        argv += ["-g", f"!**/{directory}/**"]
    capture = await _capture([*argv, "--", str(target)], root,
                             deadline - time.monotonic(), limit=MAX_LIST_BYTES)
    if capture.diagnostic_limited or (capture.code not in (0, 1) and not capture.limited):
        raise ValueError("Native search file enumeration failed")
    if capture.limited:
        _partial(coverage, "file_list_output_limit")
    paths = []
    for record in capture.stdout.split(b"\0")[:-1]:
        if (len(paths) >= MAX_FILES or coverage["entries_considered"] >= MAX_ENTRIES
                or time.monotonic() >= deadline):
            _partial(coverage, "file_record_or_time_limit")
            break
        coverage["entries_considered"] += 1
        try:
            path = root / record.decode("utf-8")
            if _eligible(root, path, coverage):
                paths.append(path)
        except UnicodeError:
            _partial(coverage, "unsupported_filename_encoding")
    return sorted(paths)


def _display_path(path):
    return json.dumps(path) if any(char in path for char in ":\r\n\t") else path


class SearchFilesTool(Tool):
    name = ToolName.SEARCH_FILES
    description = "Find regular source files with a bounded, pruned filename search."
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Relative glob, e.g. '*.py'"},
        "path": {"type": "string", "description": "Optional search directory"},
        "max_results": {"type": "integer", "description": "Result cap, clamped to 1–200"}}, "required": ["pattern"]}

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def execute(self, pattern: str, path: str | None = None, max_results: int = 50,
                      **kwargs: Any) -> ToolResult:
        start, stop = time.monotonic(), threading.Event()
        coverage = _coverage("filename_walk")
        try:
            _validate_glob(pattern)
            if type(max_results) is not int:
                raise ValueError("max_results must be an integer")
            maximum = max(1, min(200, max_results))
            workspace = Workspace(self.project_root)
            target = _target(workspace, path)
            values = await asyncio.wait_for(asyncio.to_thread(_walk, workspace.root, target,
                coverage, stop, start + SEARCH_SECONDS), SEARCH_SECONDS)
            matches = [value.relative_to(workspace.root).as_posix() for value in values
                       if _eligible(workspace.root, value, coverage) and _glob_matches(value, target, pattern)]
            if len(matches) > maximum:
                _partial(coverage, "result_limit")
            retained = []
            for match in matches[:maximum]:
                size = len(json.dumps(match).encode("utf-8")) * 3
                if coverage["result_bytes"] + size > MAX_RESULT_BYTES:
                    _partial(coverage, "result_byte_limit")
                    break
                retained.append(match)
                coverage["result_bytes"] += size
            matches = retained
            coverage["files_considered"] = len(values)
            output = "\n".join(_display_path(value) for value in matches) or "No eligible files found."
            if coverage["status"] == "partial":
                output += "\n[Partial coverage; narrow the directory or pattern to inspect the remaining files.]"
            return ToolResult(success=True, output=output, data={"paths": matches, "coverage": coverage},
                              duration_ms=(time.monotonic() - start) * 1000)
        except asyncio.TimeoutError:
            _partial(coverage, "deadline")
            return ToolResult(success=False, output="Filename search deadline reached; coverage is partial.",
                              data={"coverage": copy.deepcopy(coverage)})
        except Exception as exc:
            coverage["status"] = "failed"
            return ToolResult(success=False, output=f"Filename search failed: {exc}",
                              data={"coverage": copy.deepcopy(coverage)})
        finally:
            stop.set()


class GrepTool(Tool):
    name = ToolName.GREP
    description = "Search bounded source snapshots with ripgrep or a native extended-regex fallback."
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Single-line native regex"},
        "path": {"type": "string", "description": "Optional search file or directory"},
        "file_pattern": {"type": "string", "description": "Optional file glob"},
        "max_results": {"type": "integer", "description": "Result cap, clamped to 1–200"}}, "required": ["pattern"]}

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def execute(self, pattern: str, path: str | None = None, file_pattern: str | None = None,
                      max_results: int = 50, **kwargs: Any) -> ToolResult:
        start = time.monotonic()
        deadline, stop = start + SEARCH_SECONDS, threading.Event()
        coverage = _coverage("rg" if shutil.which("rg") else "grep")
        matches, hashes = [], {}
        try:
            if not isinstance(pattern, str) or len(pattern) > 4096 or any(c in pattern for c in "\0\r\n"):
                raise ValueError("Regex must be at most 4096 characters without NUL or literal line breaks")
            if file_pattern:
                _validate_glob(file_pattern)
            if type(max_results) is not int:
                raise ValueError("max_results must be an integer")
            maximum = max(1, min(200, max_results))
            workspace = Workspace(self.project_root)
            target = _target(workspace, path)
            has_rg = coverage["engine"] == "rg"
            files = await _candidates(workspace.root, target, file_pattern, has_rg, coverage, stop, deadline)
            coverage["files_considered"] = len(files)
            for index, file in enumerate(files):
                if (len(matches) >= maximum or time.monotonic() >= deadline
                        or coverage["skipped"].get("result_byte_limit")):
                    _partial(coverage, "result_or_time_limit")
                    break
                remaining = MAX_TOTAL_BYTES - coverage["read_budget_used"]
                if remaining <= 0:
                    _partial(coverage, "read_byte_limit")
                    break
                allowance = min(MAX_FILE_BYTES, remaining)
                try:
                    snapshot = await asyncio.wait_for(asyncio.to_thread(read_source_snapshot,
                        workspace.root, file, max_bytes=allowance), max(.001, deadline - time.monotonic()))
                    coverage["bytes_read"] += snapshot.size
                    coverage["read_budget_used"] = coverage["bytes_read"] + coverage["read_bytes_reserved"]
                    if not _eligible(workspace.root, file, coverage):
                        continue
                except (OSError, ValueError):
                    coverage["read_bytes_reserved"] += allowance
                    coverage["read_budget_used"] = coverage["bytes_read"] + coverage["read_bytes_reserved"]
                    _partial(coverage, "unsafe_oversized_or_changed_source")
                    continue
                if b"\0" in snapshot.data:
                    _partial(coverage, "binary_source")
                    continue
                try:
                    snapshot.data.decode("utf-8")
                except UnicodeError:
                    _partial(coverage, "unsupported_source_encoding")
                    continue
                count = maximum - len(matches) + 1
                argv = (["rg", "--no-config", "--no-heading", "--color", "never", "-n", "-m", str(count)]
                        if has_rg else ["grep", "-nE", "-m", str(count)])
                capture = await _capture([*argv, "--", pattern], workspace.root,
                                         deadline - time.monotonic(), snapshot.data)
                if capture.diagnostic_limited or (capture.code not in (0, 1) and not capture.limited):
                    raise ValueError("Native search expression was rejected or the engine failed")
                coverage["files_searched"] += 1
                if capture.limited:
                    _partial(coverage, "match_output_limit")
                # Provenance names the lexical path whose snapshot was read;
                # do not resolve a path that may have changed during the process.
                relative = file.relative_to(workspace.root).as_posix()
                source_lines = snapshot.data.split(b"\n")
                for record in capture.stdout.split(b"\n"):
                    number, separator, _ = record.partition(b":")
                    if not separator or not number.isdigit():
                        continue
                    line = int(number)
                    if not 1 <= line <= len(source_lines):
                        continue
                    if len(matches) >= maximum:
                        _partial(coverage, "result_limit")
                        break
                    text = source_lines[line - 1].decode("utf-8", errors="replace").rstrip("\r")
                    shown = text if len(text) <= 300 else text[:300] + f"...[{len(text)} chars]"
                    row = {"path": relative, "line": line, "text": shown}
                    size = 2 * len(json.dumps(row).encode("utf-8")) + 3 * len(json.dumps(relative).encode("utf-8")) + 64
                    if coverage["result_bytes"] + size > MAX_RESULT_BYTES:
                        _partial(coverage, "result_byte_limit")
                        break
                    matches.append(row)
                    coverage["result_bytes"] += size
                    hashes[relative] = snapshot.sha256
                if len(matches) >= maximum and index + 1 < len(files):
                    _partial(coverage, "result_limit")
            output = "\n".join(f"{_display_path(row['path'])}:{row['line']}:{row['text']}" for row in matches)
            output = output or "No matches found in inspected eligible files."
            if coverage["status"] == "partial":
                output += "\n[Partial coverage; narrow the directory, glob or expression before concluding absence.]"
            return ToolResult(success=True, output=output, data={"matches": matches, "files_read": sorted(hashes),
                "source_hashes": hashes, "coverage": coverage}, duration_ms=(time.monotonic() - start) * 1000)
        except asyncio.TimeoutError:
            _partial(coverage, "deadline")
            return ToolResult(success=False, output="Search deadline reached; coverage is partial.", data={"coverage": coverage})
        except Exception as exc:
            coverage["status"] = "failed"
            return ToolResult(success=False, output=f"Search failed: {exc}", data={"coverage": coverage})
        finally:
            stop.set()
