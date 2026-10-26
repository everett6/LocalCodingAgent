"""Read-only bounded local Python security audit."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from typing import Any

from local_coder.security.lessons import LESSONS_FILE, parse_lessons
from local_coder.security_analysis import AnalysisLimit, CHECKS, LIMITATIONS, analyze_python
from local_coder.tools.base import Tool
from local_coder.tools.scan_scope import IGNORED, scan_files
from local_coder.types import ToolName, ToolResult
from local_coder.workspace import Workspace


class AdvancedAuditTool(Tool):
    name = ToolName.ADVANCED_AUDIT
    description = 'Read-only local Python AST audit of weak crypto, dynamic SQL, unsafe deserialization, shell calls, outbound destinations, file paths and redacted high-entropy literals. Reports static candidates, never exploitation proof. No execution or network access.'
    parameters = {'type': 'object', 'properties': {
        'target_path': {'type': 'string', 'default': '.'},
        'checks': {'type': 'array', 'items': {'type': 'string', 'enum': list(CHECKS)}, 'maxItems': len(CHECKS)},
        'max_files': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 200},
        'max_findings': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100},
        'max_bytes': {'type': 'integer', 'minimum': 1024, 'maximum': 10000000, 'default': 2000000},
    }, 'additionalProperties': False}

    def __init__(self, project_root: str):
        self.workspace = Workspace(project_root)
        self.project_root = self.workspace.root

    async def execute(self, target_path: str = '.', checks: list[str] | None = None,
                      max_files: int = 200, max_findings: int = 100,
                      max_bytes: int = 2000000, **kwargs: Any) -> ToolResult:
        try:
            if kwargs:
                raise ValueError('Unknown audit arguments')
            for value, lower, upper, label in ((max_files, 1, 500, 'max_files'), (max_findings, 1, 500, 'max_findings'), (max_bytes, 1024, 10000000, 'max_bytes')):
                if type(value) is not int or not lower <= value <= upper:
                    raise ValueError(f'{label} must be an integer from {lower} through {upper}')
            if checks is not None and (not isinstance(checks, list) or len(checks) > len(CHECKS) or any(not isinstance(item, str) or item not in CHECKS for item in checks)):
                raise ValueError('Choose supported advanced audit checks')
            if not isinstance(target_path, str) or not 0 < len(target_path) <= 4096:
                raise ValueError('Choose a bounded local target path')
            target = self.workspace.resolve(target_path)
            relative = target.relative_to(self.workspace.root)
            if any(part in IGNORED for part in relative.parts) or target.name.startswith('.env') or target.suffix in {'.pem', '.key'}:
                raise ValueError('Private state and credential files are excluded from source audits')
            if not target.exists():
                raise ValueError('Audit target does not exist')
            data = await asyncio.to_thread(self._scan, target, checks, max_files, max_findings, max_bytes)
            return ToolResult(success=True, output=json.dumps(data, ensure_ascii=False), data=data)
        except (ValueError, OSError, TypeError) as exc:
            return ToolResult(success=False, error=str(exc) if isinstance(exc, ValueError) else 'Local source could not be inspected')

    def _scan(self, target: Path, checks, max_files: int, max_findings: int, max_bytes: int) -> dict:
        files, enumeration = scan_files(self.project_root, target)
        coverage = {**enumeration, 'files_scanned': 0, 'bytes_scanned': 0, 'parse_errors': 0,
                    'unsupported_files': 0, 'credential_files_excluded': 0, 'suppressed': 0,
                    'limits': {'max_files': max_files, 'max_findings': max_findings, 'max_bytes': max_bytes, 'max_file_bytes': 256000}}
        suppressions = []
        try:
            lessons = self.workspace.resolve(LESSONS_FILE)
            if lessons.is_file() and lessons.stat().st_size <= 64000:
                suppressions = [item for item in parse_lessons(lessons.read_text(encoding='utf-8')) if item.kind == 'suppress']
        except (OSError, ValueError, UnicodeError):
            coverage['skipped'].append('Security lessons could not be safely loaded')
        findings = []
        for file in files:
            relative = file.relative_to(self.project_root).as_posix()
            if file.name.startswith('.env') or file.suffix in {'.pem', '.key'}:
                coverage['credential_files_excluded'] += 1
                continue
            if file.suffix != '.py':
                coverage['unsupported_files'] += 1
                continue
            if coverage['files_scanned'] >= max_files or len(findings) >= max_findings:
                coverage['skipped'].append('Audit result or file budget reached; narrow the source scope')
                break
            try:
                checked = self.workspace.resolve(file)
                if checked != file or file.is_symlink():
                    raise ValueError('Source path changed during audit')
                expected = file.lstat()
                if not stat.S_ISREG(expected.st_mode):
                    raise ValueError('Source must be a regular file')
                size = expected.st_size
                if size > 256000 or coverage['bytes_scanned'] + size > max_bytes:
                    coverage['skipped'].append(f'{relative}: source byte budget reached')
                    continue
                flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
                with os.fdopen(os.open(file, flags), 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    current = file.lstat()
                    if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                            or self.workspace.resolve(file) != file):
                        raise ValueError('Source path changed during audit')
                    raw = stream.read(min(256001, max_bytes - coverage['bytes_scanned'] + 1))
                if len(raw) > 256000 or coverage['bytes_scanned'] + len(raw) > max_bytes:
                    coverage['skipped'].append(f'{relative}: source byte budget reached')
                    continue
                coverage['files_scanned'] += 1
                coverage['bytes_scanned'] += len(raw)
                candidates = analyze_python(raw.decode('utf-8'), relative, checks, max_findings=max_findings)
                for item in candidates:
                    if any(lesson.matches('advanced-audit', item['rule_id'], relative) or (item['check'] == 'entropy_secrets' and lesson.matches('secrets', 'hardcoded-credential', relative)) for lesson in suppressions):
                        coverage['suppressed'] += 1
                        continue
                    if len(findings) >= max_findings:
                        coverage['skipped'].append('Finding budget reached; narrow the source scope')
                        break
                    findings.append(item)
                if len(candidates) >= max_findings:
                    coverage['skipped'].append(f'{relative}: per-file candidate budget reached')
            except (SyntaxError, UnicodeError, RecursionError, AnalysisLimit):
                coverage['parse_errors'] += 1
                coverage['skipped'].append(f'{relative}: source could not be completely parsed or analyzed')
            except (OSError, ValueError):
                coverage['skipped'].append(f'{relative}: source unavailable or changed')
        coverage['skipped'] = coverage['skipped'][:100]
        coverage['status'] = 'partial' if coverage['skipped'] else 'complete'
        return {'engine': 'python-ast-local-flow', 'scanned_path': target.relative_to(self.project_root).as_posix(),
                'checks': list(CHECKS if checks is None else checks), 'supported_languages': ['Python'],
                'findings': findings, 'total_findings': len(findings), 'coverage': coverage,
                'verification': 'unverified', 'limitations': list(LIMITATIONS), 'secret_values_returned': False}
