"""Bounded, local Python threat inventory; observations are not vulnerability claims."""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterable

from local_coder.workspace import Workspace

MAX_FILES = 120
MAX_BYTES = 200_000
MAX_OBSERVATIONS = 500
MAX_VISITED = 4000
_EXCLUDED = {'.git', '.local-coder', '.venv', 'venv', 'node_modules', 'vendor',
             '__pycache__', 'site-packages', '.aws', '.ssh', 'secrets', 'credentials'}
_SINKS = {
    'eval': ('CWE-95', 'OWASP A03:2021 Injection'),
    'exec': ('CWE-95', 'OWASP A03:2021 Injection'),
    'os.system': ('CWE-78', 'OWASP A03:2021 Injection'),
    'subprocess.run': ('CWE-78', 'OWASP A03:2021 Injection'),
    'subprocess.Popen': ('CWE-78', 'OWASP A03:2021 Injection'),
    'pickle.loads': ('CWE-502', 'OWASP A08:2021 Software and Data Integrity Failures'),
    'yaml.load': ('CWE-502', 'OWASP A08:2021 Software and Data Integrity Failures'),
    'open': ('CWE-22', 'OWASP A01:2021 Broken Access Control'),
}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f'{base}.{node.attr}' if base else node.attr
    return ''


def build_threat_model(root: str | Path, paths: Iterable[str | Path] | None = None) -> dict:
    """Inventory syntax only, never import project modules or invoke language servers.

    Paths outside the workspace raise ValueError. Symlinks, secret directories,
    dependencies, oversized files and non-Python files are excluded. No source
    snippets, argument literals, or credential values are included in the result.
    """
    workspace = Workspace(root)
    result = {
        'scope': {'root': str(workspace.root), 'language': 'python', 'files': [],
                  'limits': {'files': MAX_FILES, 'bytes_per_file': MAX_BYTES,
                             'observations': MAX_OBSERVATIONS, 'visited_entries': MAX_VISITED}},
        'entry_points': [], 'boundaries': [], 'sinks': [], 'links': [],
        'coverage_gaps': [
            'Syntax inventory only: no proven data flow, exploitability, or authorization correctness.',
            'Dynamic dispatch, runtime configuration, non-Python code and dependencies are not analyzed.',
            'ATLAS techniques require AI-specific evidence and are not inferred from generic Python calls.',
        ],
    }
    candidates: set[Path] = set()
    visited = 0
    exhausted = False

    def allowed(path: Path) -> bool:
        parts = path.relative_to(workspace.root).parts
        return not any(p in _EXCLUDED or p.startswith('.env') for p in parts)

    for supplied in ([Path('.')] if paths is None else paths):
        raw = workspace.root / supplied
        path = workspace.resolve(supplied)
        # Resolve validates escapes; raw ancestry prevents following internal symlinks.
        if any(p.is_symlink() for p in [raw, *raw.parents] if p != workspace.root.parent):
            continue
        if not allowed(path):
            continue
        if path.is_file():
            candidates.add(path)
            continue
        for directory, dirs, files in os.walk(path, followlinks=False):
            visited += 1
            if visited > MAX_VISITED:
                exhausted = True
                break
            dirs[:] = sorted(d for d in dirs if d not in _EXCLUDED
                             and not d.startswith('.env')
                             and not (Path(directory) / d).is_symlink())
            for filename in sorted(files):
                visited += 1
                if visited > MAX_VISITED:
                    exhausted = True
                    break
                candidate = Path(directory) / filename
                if candidate.suffix == '.py' and not candidate.is_symlink() and allowed(candidate):
                    candidates.add(candidate)
            if exhausted:
                break
        if exhausted:
            break
    if exhausted:
        result['coverage_gaps'].append('Directory entry limit reached; inventory is partial.')
    selected = sorted(candidates)
    if len(selected) > MAX_FILES:
        result['coverage_gaps'].append('File limit reached; inventory is partial.')
    count = 0

    def add(bucket: str, item: dict) -> None:
        nonlocal count
        if count < MAX_OBSERVATIONS:
            result[bucket].append(item)
            count += 1

    for path in selected[:MAX_FILES]:
        if path.suffix != '.py' or not path.is_file():
            continue
        relative = path.relative_to(workspace.root).as_posix()
        try:
            with path.open('rb') as source:
                data = source.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                result['coverage_gaps'].append(f'{relative}: exceeds file byte limit.')
                continue
            tree = ast.parse(data, filename=relative)
        except (OSError, SyntaxError, ValueError, RecursionError):
            result['coverage_gaps'].append(f'{relative}: unreadable or unparseable Python.')
            continue
        result['scope']['files'].append(relative)
        aliases = {}
        for node in ast.walk(tree):
            location = {'path': relative, 'line': getattr(node, 'lineno', 1)}
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    target = (f'{node.module}.{alias.name}' if isinstance(node, ast.ImportFrom)
                              and node.module else alias.name)
                    binding = alias.asname or alias.name.split('.')[0]
                    aliases[binding] = (alias.name.split('.')[0]
                                        if isinstance(node, ast.Import) and not alias.asname
                                        else target)
                    add('links', {**location, 'kind': 'import', 'target': target,
                                  'evidence': 'syntactic import; module may be external'})
        for node in ast.walk(tree):
            location = {'path': relative, 'line': getattr(node, 'lineno', 1)}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [_name(d.func if isinstance(d, ast.Call) else d)
                              for d in node.decorator_list]
                routes = [d for d in decorators if d.rsplit('.', 1)[-1] in
                          {'route', 'get', 'post', 'put', 'patch', 'delete', 'websocket'}]
                guards = [d for d in decorators if any(t in d.lower() for t in
                          ('login_required', 'permission', 'auth', 'require_role'))]
                add('entry_points', {**location, 'name': node.name,
                    'kind': 'route' if routes else 'function', 'route_decorators': routes,
                    'auth_guard_observations': guards,
                    'auth_status': 'decorator observed; effectiveness unverified' if guards
                                   else 'no recognized decorator; authorization unknown'})
            if isinstance(node, (ast.Attribute, ast.Name)):
                name = _name(node)
                if name in {'input', 'sys.argv', 'os.environ', 'request.args',
                            'request.form', 'request.json', 'request.data', 'request.query_params'}:
                    add('boundaries', {**location, 'kind': 'input_source', 'symbol': name,
                                       'evidence': 'syntactic reference; no flow established'})
            if isinstance(node, ast.Call):
                name = _name(node.func)
                prefix, _, tail = name.partition('.')
                canonical = aliases.get(prefix, prefix) + ('.' + tail if tail else '')
                add('links', {**location, 'kind': 'call', 'target': canonical,
                              'evidence': 'heuristic name/import link; runtime target unverified'})
                category = _SINKS.get(canonical)
                if category or name.endswith('.execute'):
                    cwe, owasp = category or ('CWE-89', 'OWASP A03:2021 Injection')
                    add('sinks', {**location, 'symbol': canonical, 'cwe': cwe,
                        'owasp': owasp, 'atlas': [],
                        'evidence': 'review surface only; safe arguments and guards not evaluated'})
    if count >= MAX_OBSERVATIONS:
        result['coverage_gaps'].append('Observation limit reached; inventory is partial.')
    return result


def render_threat_briefing(model: dict, max_chars: int = 6000) -> str:
    """Render a bounded briefing without adding exploit claims."""
    lines = ['Local threat inventory (heuristic observations, not confirmed vulnerabilities).']
    for bucket in ('entry_points', 'boundaries', 'sinks', 'links'):
        lines.append(f'{bucket}: {len(model.get(bucket, []))}')
        for item in model.get(bucket, [])[:20]:
            lines.append(f"  {item['path']}:{item['line']} {item.get('name', item.get('symbol', item.get('target', '')))}")
    lines.extend(model.get('coverage_gaps', []))
    return '\n'.join(lines)[:max(0, min(max_chars, 20_000))]


def evaluate_findings(predicted_ids: Iterable[str], expected_ids: Iterable[str]) -> dict:
    """Set-based labeled evaluation, not a claim about general model accuracy.

    Undefined precision or recall is None; duplicate finding IDs count once.
    """
    predicted, expected = set(predicted_ids), set(expected_ids)
    tp, fp, fn = predicted & expected, predicted - expected, expected - predicted
    return {'true_positives': sorted(tp), 'false_positives': sorted(fp),
            'false_negatives': sorted(fn),
            'precision': len(tp) / len(predicted) if predicted else None,
            'recall': len(tp) / len(expected) if expected else None}
