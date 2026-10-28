"""Bounded source structure without imports, execution, or parser downloads.

Python call targets are static name/import candidates, never runtime call proof.
Dynamic attributes, shadowed names and ambiguous bindings remain unresolved.
Other languages and invalid Python explicitly use bounded text windows.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import PurePosixPath

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def module_names(path: str) -> list[str]:
    """Physical module names plus the conventional, explicitly labeled src root."""
    parts = list(PurePosixPath(path).with_suffix('').parts)
    if parts and parts[-1] == '__init__':
        parts.pop()
    names = ['.'.join(parts)]
    if parts and parts[0] == 'src':
        names.append('.'.join(parts[1:]))
    return [name for name in names if name]


def _import_module(path: str, module: str | None, level: int) -> str | None:
    if not level:
        return module or ''
    package = list(PurePosixPath(path).parent.parts)
    if level > len(package):
        return None
    if level > 1:
        package = package[:-(level - 1)]
    return '.'.join(package + (module.split('.') if module else []))


def _expression(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expression(node.value)
        return f'{base}.{node.attr}' if base else None
    return None


class _Bindings(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.names: dict[str, list[dict]] = {}
        self.calls: list[ast.Call] = []

    def bind(self, name: str, kind: str, **details):
        self.names.setdefault(name, []).append({'kind': kind, **details})

    def visit_FunctionDef(self, node):
        self.bind(node.name, 'definition')

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.bind(node.name, 'definition')

    def visit_Lambda(self, node):
        # Lambda parameters introduce another scope; do not guess its binding.
        return

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bind(node.id, 'dynamic')

    def visit_Global(self, node):
        for name in node.names:
            self.bind(name, 'dynamic')

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bind(node.name, 'dynamic')
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.bind(node.name, 'dynamic')
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.bind(node.rest, 'dynamic')
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name.split('.')[0], 'module', module=alias.name if alias.asname else alias.name.split('.')[0])

    def visit_ImportFrom(self, node):
        module = _import_module(self.path, node.module, node.level)
        for alias in node.names:
            self.bind(alias.asname or alias.name, 'import', module=module, member=alias.name)

    def visit_Call(self, node):
        if len(self.calls) < 512:
            self.calls.append(node)
        self.generic_visit(node)


def _regions(lines: list[str], start: int, end: int, excluded: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result, cursor = [], start
    for first, last in sorted(excluded):
        if cursor < first:
            result.append((cursor, first - 1))
        cursor = max(cursor, last + 1)
    if cursor <= end:
        result.append((cursor, end))
    return [(first, last) for first, last in result if ''.join(lines[first - 1:last]).strip()]


def _windows(lines: list[str], start: int, end: int, boundaries: set[int], max_chars: int):
    """Prefer whole statement endpoints, with explicit bounded oversize fallback."""
    cursor = start
    while cursor <= end:
        used, last = 0, cursor - 1
        while last < end and used + len(lines[last]) <= max_chars:
            used += len(lines[last])
            last += 1
        if last < cursor:
            line = lines[cursor - 1]
            for offset in range(0, len(line), max_chars):
                yield cursor, cursor, line[offset:offset + max_chars], True
            cursor += 1
            continue
        if last < end:
            candidates = [line for line in boundaries if cursor <= line <= last]
            if candidates:
                last = max(candidates)
        yield cursor, last, ''.join(lines[cursor - 1:last]), last not in boundaries and last != end
        cursor = last + 1


def structure_chunks(path: str, source: str, *, max_chars: int = 1800, max_chunks: int = 128) -> list[dict]:
    """Return content plus stable anchors, AST-owned spans and static call facts.

    Anchors are metadata, never source-file edits. Parent chunks exclude nested
    definition bodies; source references describe contiguous original lines.
    """
    if not isinstance(path, str) or not isinstance(source, str):
        raise ValueError('Source path and source text are required')
    pure = PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts:
        raise ValueError('Structure paths must be project relative')
    max_chars = max(1, min(int(max_chars), 10000))
    max_chunks = max(0, min(int(max_chunks), 128))
    lines = source.splitlines(keepends=True)
    scopes: dict[str, dict] = {}
    definitions: list[dict] = []
    status = 'unsupported_language'
    tree = None
    if pure.suffix == '.py':
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            status = 'parse_failed'
    if tree is None:
        result = []
        for first, last, content, oversized in _windows(lines, 1, len(lines), set(), max_chars):
            if content.strip():
                number = len(result) + 1
                anchor = f'code:{path}::<module>~{number}'
                result.append({'content': content, 'metadata': {'symbol': '<module>', 'qualified_name': '<module>', 'symbol_kind': 'module',
                               'start_line': first, 'end_line': last, 'anchor': anchor, 'symbol_anchor': f'code:{path}::<module>',
                               'syntax_provider': 'bounded_text', 'structure_status': status, 'calls': [], 'chunking': 'bounded_text'}})
            if len(result) >= max_chunks:
                break
        return result[:max_chunks]

    def collect(node: ast.AST, owner: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEFINITIONS):
                qualified = f'{owner}.{child.name}' if owner else child.name
                first = min([child.lineno] + [decorator.lineno for decorator in child.decorator_list])
                kind = 'class' if isinstance(child, ast.ClassDef) else 'function'
                item = {'node': child, 'qualified': qualified, 'parent': owner, 'start': first,
                        'end': child.end_lineno or child.lineno, 'kind': kind, 'name': child.name}
                definitions.append(item)
                # Duplicate declarations stay chunks but cannot prove a target.
                scopes.setdefault(qualified, item)
                collect(child, qualified)
            else:
                collect(child, owner)

    module = {'node': tree, 'qualified': '', 'parent': None, 'start': 1, 'end': len(lines), 'kind': 'module', 'name': '<module>'}
    scopes[''] = module
    collect(tree, '')
    duplicates = Counter(item['qualified'] for item in definitions)
    for scope in [module, *definitions]:
        bindings = _Bindings(path)
        node = scope['node']
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs + ([args.vararg] if args.vararg else []) + ([args.kwarg] if args.kwarg else []):
                bindings.bind(arg.arg, 'parameter')
        for statement in node.body:
            bindings.visit(statement)
        scope['bindings'] = bindings

    def resolve(expression: str | None, owner: str) -> tuple[list[dict], str]:
        if not expression:
            return [], 'dynamic call expression'
        if any(duplicates['.'.join(owner.split('.')[:index])] > 1 for index in range(1, len(owner.split('.')) + 1)):
            return [], 'ambiguous enclosing definition'
        base, *attributes = expression.split('.')
        current, entered_function = owner, False
        while current is not None:
            scope = scopes[current]
            if scope['kind'] == 'class' and entered_function:
                current = scope['parent']
                continue
            matches = scope['bindings'].names.get(base, [])
            if matches:
                if len(matches) != 1 or '*' in scope['bindings'].names:
                    return [], 'ambiguous or wildcard binding'
                binding = matches[0]
                kind = binding['kind']
                if kind in {'parameter', 'dynamic'}:
                    return [], 'parameter or reassigned binding'
                if kind == 'definition':
                    qualified = f'{current}.{base}' if current else base
                    if duplicates[qualified] > 1:
                        return [], 'ambiguous definition'
                    if attributes:
                        definition = scopes.get(qualified)
                        if not definition or definition['kind'] != 'class':
                            return [], 'dynamic attribute call'
                        qualified += '.' + '.'.join(attributes)
                    return [{'source_path': path, 'qualified_name': qualified}], 'lexical definition binding'
                if not binding.get('module'):
                    return [], 'relative import escapes indexed package'
                if kind == 'module':
                    if not attributes:
                        return [], 'module is not a proven callable'
                    return [{'module': '.'.join([binding['module'], *attributes[:-1]]), 'qualified_name': attributes[-1]}], 'explicit module import binding'
                member = binding['member']
                qualified = '.'.join([member, *attributes])
                targets = [{'module': binding['module'], 'qualified_name': qualified}]
                if attributes:
                    targets.append({'module': binding['module'] + '.' + member, 'qualified_name': '.'.join(attributes)})
                return targets, 'explicit from-import binding'
            entered_function = entered_function or scope['kind'] == 'function'
            current = scope['parent']
        return [], 'unbound, builtin or external name'

    result, declaration_count = [], Counter()
    for scope in [module, *definitions]:
        owner = scope['qualified']
        qualified = owner or '<module>'
        declaration_count[owner] += 1
        children = [(item['start'], item['end']) for item in definitions if item['parent'] == owner and item is not scope]
        regions = _regions(lines, scope['start'], scope['end'], children)
        boundaries = {node.end_lineno for node in ast.walk(scope['node']) if isinstance(node, ast.stmt) and getattr(node, 'end_lineno', None)}
        part = 0
        for start, end in regions:
            for first, last, content, oversized in _windows(lines, start, end, boundaries, max_chars):
                if not content.strip():
                    continue
                part += 1
                root_anchor = f'code:{path}::{qualified}'
                suffix = f'~declaration{declaration_count[owner]}' if declaration_count[owner] > 1 else ''
                anchor = root_anchor + suffix + (f'~{part}' if part > 1 else '')
                calls = []
                for call in scope['bindings'].calls:
                    if not first <= call.lineno <= last:
                        continue
                    expression = _expression(call.func)
                    targets, reason = resolve(expression, owner)
                    calls.append({'expression': (expression or '<dynamic>')[:300], 'line': call.lineno,
                                  'resolution': 'static_candidate' if targets else 'unresolved', 'reason': reason,
                                  'targets': targets})
                    if len(calls) >= 32:
                        break
                result.append({'content': content, 'metadata': {
                    'symbol': scope['name'], 'qualified_name': qualified, 'symbol_kind': scope['kind'],
                    'start_line': first, 'end_line': last, 'anchor': anchor, 'symbol_anchor': root_anchor,
                    'syntax_provider': 'python_ast', 'structure_status': 'ast', 'ambiguous_symbol': duplicates[owner] > 1,
                    'chunking': 'bounded_oversize' if oversized else 'ast_boundary', 'calls': calls,
                }})
                if len(result) >= max_chunks:
                    return result
    return result
