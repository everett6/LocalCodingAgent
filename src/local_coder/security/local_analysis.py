"""Bounded Python lexical semantics and same-file route/service observations.

Guards and call edges are observations; dynamic dispatch, middleware and actual
runtime authorization remain explicitly unknown. No source unit is executed.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from local_coder.security_analysis import AnalysisLimit
from local_coder.tools.detection import dotted

CLASSES = ['code_execution', 'command_injection', 'sql_injection', 'deserialization',
           'ssrf', 'authentication', 'authorization']
GAPS = ['Dynamic dispatch and cross-file calls are unresolved.',
        'Middleware, guard dominance and authorization effectiveness are unproven.',
        'Observed service arguments are data boundaries, not proof of attacker control.']
MAX_AST_NODES = 40000
MAX_OBSERVATION_BYTES = 1_000_000


def analyze_unit(source: str, filename: str) -> dict[str, Any]:
    if Path(filename).suffix != '.py':
        return {'language': Path(filename).suffix, 'status': 'unsupported',
                'gaps': ['Semantic analysis supports Python only.'], 'entry_points': [],
                'authorization_traces': [], 'functions': {}, 'vulnerability_classes': []}
    if len(source.encode('utf-8')) > 256000:
        raise AnalysisLimit('Python semantic source exceeds 256 KB')
    tree = ast.parse(source, filename)
    for index, _ in enumerate(ast.walk(tree)):
        if index >= MAX_AST_NODES:
            raise AnalysisLimit('Python semantic AST exceeds the node limit')
    remaining = MAX_OBSERVATION_BYTES
    def charge(value):
        nonlocal remaining
        remaining -= len(json.dumps(value).encode('utf-8'))
        if remaining < 0:
            raise AnalysisLimit('Python semantic observations exceed the byte limit')
        return value
    # Only top-level functions are resolvable by this same-file analysis. Nested
    # functions and methods require receiver/scope resolution that we do not claim.
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    summaries, entries, traces = {}, [], []
    for name, node in functions.items():
        # Stop at nested scopes; never attribute a nested function's guards/calls
        # to the outer route.
        owned = []
        pending = list(reversed(node.body))
        while pending:
            child = pending.pop()
            owned.append(child)
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                pending.extend(reversed(list(ast.iter_child_nodes(child))))
        guards = [charge({'kind': 'condition', 'line': item.lineno,
                   'observed_names': sorted({dotted(n) for n in ast.walk(item.test)
                                             if isinstance(n, (ast.Name, ast.Attribute))})[:16],
                   'effect': 'unproven'}) for item in owned if isinstance(item, ast.If)]
        decorators = [charge({'kind': 'decorator', 'line': item.lineno,
                       'name': dotted(item.func if isinstance(item, ast.Call) else item),
                       'effect': 'unproven'}) for item in node.decorator_list]
        transforms = []
        for item in owned:
            if len(transforms) >= 100:
                break
            if isinstance(item, (ast.Call, ast.BinOp, ast.JoinedStr)):
                transforms.append(charge({'kind': 'transform', 'line': item.lineno,
                    'operation': dotted(item.func) if isinstance(item, ast.Call) else type(item).__name__}))
        summaries[name] = {'guards': guards + decorators, 'transforms': transforms}
        route = any(d['name'].split('.')[-1] in {'route', 'get', 'post', 'put', 'patch', 'delete', 'websocket'}
                    for d in decorators)
        if route:
            entries.append(charge({'file': filename, 'function': name, 'line': node.lineno,
                            'decorators': decorators, 'coverage': 'observed_static'}))
            for call in owned:
                if not isinstance(call, ast.Call):
                    continue
                service = dotted(call.func)
                if service not in functions or service == name:
                    continue
                traces.append(charge({'file': filename, 'route': name, 'service': service,
                               'call_line': call.lineno,
                               'guards': [guard for guard in guards + decorators if guard['line'] < call.lineno],
                               'boundaries': [{'argument': i, 'line': arg.lineno,
                                               'kind': type(arg).__name__} for i, arg in enumerate(call.args)],
                               'gaps': list(GAPS), 'verification': 'unverified'}))
    for trace in traces:
        trace['service_guards'] = charge(summaries[trace['service']]['guards'])
        trace['callee_resolution'] = 'same_file_lexical_name_only'
    result = {'language': 'python', 'status': 'partial', 'entry_points': entries,
            'authorization_traces': traces, 'functions': summaries,
            'vulnerability_classes': [{'name': name, 'disposition': 'static_candidates_only' if name not in {'authentication', 'authorization'}
                                       else 'observations_only'} for name in CLASSES], 'gaps': list(GAPS),
            'limits': {'source_bytes': 256000, 'ast_nodes': MAX_AST_NODES,
                       'observation_bytes': MAX_OBSERVATION_BYTES, 'transforms_per_function': 100}}
    if len(json.dumps(result).encode('utf-8')) > MAX_OBSERVATION_BYTES:
        raise AnalysisLimit('Python semantic observations exceed the byte limit')
    return result
