"""Local static security evidence. Never imports or executes scanned source.

This deliberately bounded analysis is intraprocedural. Caller locations and
possible guards are evidence for human review, never a proof of exploitability.
"""
from __future__ import annotations
import ast
import json
from typing import Any

MAX_CANDIDATE_SOURCE_BYTES = 256000
MAX_CANDIDATE_AST_NODES = 40000
MAX_CANDIDATE_FINDINGS = 1000
MAX_CANDIDATE_BYTES = 1_000_000


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


def python_candidates(source: str, filename: str) -> list[dict[str, Any]]:
    # Local import avoids a module cycle: security_analysis also reuses this
    # detector after defining the common limit exception.
    from local_coder.security_analysis import AnalysisLimit
    if not isinstance(source, str) or len(source.encode('utf-8')) > MAX_CANDIDATE_SOURCE_BYTES:
        raise AnalysisLimit('Python candidate source exceeds the byte limit')
    tree = ast.parse(source, filename=filename)
    for index, _ in enumerate(ast.walk(tree)):
        if index >= MAX_CANDIDATE_AST_NODES:
            raise AnalysisLimit('Python candidate AST exceeds the node limit')
    lines = source.splitlines()
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split('.')[0]] = name.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    def canonical(node):
        name = dotted(node)
        first, _, rest = name.partition('.')
        return aliases.get(first, first) + ('.' + rest if rest else '')
    callers = {}
    for call in ast.walk(tree):
        if isinstance(call, ast.Call):
            callers.setdefault(dotted(call.func).split('.')[-1], []).append(call.lineno)
    findings = []
    finding_bytes = 0

    class Flow(ast.NodeVisitor):
        def __init__(self):
            self.bindings = {}
            self.function = '<module>'
            self.guards = []
            self.decorators = []

        def trace(self, node, seen=None):
            seen = set() if seen is None else set(seen)
            if isinstance(node, ast.Name) and node.id in self.bindings and node.id not in seen:
                seen.add(node.id)
                binding = self.bindings[node.id]
                if isinstance(binding, dict):
                    return [binding]
                return self.trace(binding, seen) + [{"kind": "assignment", "name": node.id, "line": getattr(binding, 'lineno', None)}]
            name = canonical(node.func) if isinstance(node, ast.Call) else canonical(node)
            if name == 'input' or name.startswith(('request.', 'flask.request.', 'sys.argv', 'os.environ')):
                return [{"kind": "external_input", "expression": ast.unparse(node)[:200], "line": node.lineno}]
            result = []
            for child in ast.iter_child_nodes(node):
                result.extend(self.trace(child, seen))
                if len(result) >= 24:
                    break
            # Bound evidence independently of source size.
            return result[:24]

        def dynamic(self, node, seen=None):
            seen = set() if seen is None else set(seen)
            if isinstance(node, ast.Constant):
                return False
            if isinstance(node, ast.Name) and node.id in self.bindings and node.id not in seen:
                seen.add(node.id)
                value = self.bindings[node.id]
                return True if isinstance(value, dict) else self.dynamic(value, seen)
            return True

        def visit_FunctionDef(self, node):
            previous = self.bindings, self.function, self.guards, self.decorators
            self.bindings = dict(self.bindings)
            self.function = node.name
            self.guards = []
            self.decorators = [ast.unparse(d)[:200] for d in node.decorator_list]
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for arg in args + [a for a in (node.args.vararg, node.args.kwarg) if a]:
                self.bindings[arg.arg] = {"kind": "parameter", "name": arg.arg, "line": arg.lineno}
            for stmt in node.body:
                self.visit(stmt)
            self.bindings, self.function, self.guards, self.decorators = previous
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            self.visit(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = node.value
        def visit_AnnAssign(self, node):
            if node.value is not None:
                self.visit(node.value)
                if isinstance(node.target, ast.Name):
                    self.bindings[node.target.id] = node.value

        def visit_If(self, node):
            self.visit(node.test)
            previous = dict(self.bindings)
            guard = {"line": node.lineno, "condition": ast.unparse(node.test)[:200], "effect": "not_proven"}
            self.guards.append(guard)
            for stmt in node.body:
                self.visit(stmt)
            body_bindings = dict(self.bindings)
            self.bindings = dict(previous)
            self.guards[-1] = {**guard, "condition": "not (" + guard['condition'] + ")"}
            for stmt in node.orelse:
                self.visit(stmt)
            # Never let one branch's apparent sanitization erase the other.
            for name, value in body_bindings.items():
                if name not in self.bindings or self.bindings[name] is not value:
                    if not isinstance(value, dict) and self.trace(value):
                        self.bindings[name] = value
            self.guards.pop()

        def add(self, node, rule, message, value, preconditions):
            nonlocal finding_bytes
            if len(findings) >= MAX_CANDIDATE_FINDINGS:
                raise AnalysisLimit('Python candidate finding count exceeds the limit')
            trace = self.trace(value)
            finding = {"file": filename, "line": node.lineno, "severity": "HIGH",
                "rule_id": rule, "message": message, "code": lines[node.lineno - 1].strip(),
                "confidence": "source-linked" if any(t['kind'] == 'external_input' for t in trace) else "heuristic",
                "verification": "unverified", "source_trace": trace,
                "sink": {"expression": canonical(node.func), "line": node.lineno},
                "function": self.function, "callers": callers.get(self.function, [])[:20],
                "guards": list(self.guards), "decorators": list(self.decorators),
                "preconditions": preconditions,
                "coverage_limits": ["intraprocedural only", "guards and authorization not proven", "no code executed"]}
            finding_bytes += len(json.dumps(finding).encode('utf-8'))
            if finding_bytes > MAX_CANDIDATE_BYTES:
                raise AnalysisLimit('Python candidate finding bytes exceed the limit')
            findings.append(finding)

        def visit_Call(self, node):
            name = canonical(node.func)
            arg = node.args[0] if node.args else None
            if arg is not None:
                rule = message = None
                preconditions = ["Sink is reachable by an attacker", "Attacker controls the relevant input", "Existing guards do not prevent the unsafe value"]
                if name in {'eval', 'exec'}:
                    rule, message = 'SEC-PY-CODE-EXEC', 'Dynamic code execution; inspect caller-controlled input.'
                elif name in {'os.system', 'os.popen'}:
                    rule, message = 'SEC-PY-OS-SYSTEM', 'Shell command construction requires caller and quoting review.'
                elif name in {'subprocess.run', 'subprocess.call', 'subprocess.Popen', 'subprocess.check_call', 'subprocess.check_output'} and any(kw.arg == 'shell' and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
                    rule, message = 'SEC-PY-SHELL-INJECTION', 'Subprocess explicitly enables a shell.'
                elif name.endswith(('.execute', '.executemany', '.executescript')) and self.dynamic(arg):
                    # A second argument is not itself proof of safety if the SQL
                    # text was already formatted before parameter binding.
                    if isinstance(arg, (ast.JoinedStr, ast.BinOp, ast.Call, ast.Name)):
                        rule, message = 'SEC-PY-SQL-INJECTION', 'Dynamic SQL text; verify parameter binding and attacker reachability.'
                        preconditions.append('Receiver must be a SQL execution API; arbitrary .execute methods are not proven SQL')
                elif name in {'pickle.load', 'pickle.loads', 'dill.load', 'dill.loads', 'marshal.loads'}:
                    rule, message = 'SEC-PY-PICKLE-INSECURE', 'Unsafe deserialization requires trusted input provenance.'
                elif name == 'yaml.load':
                    loader = next((k.value for k in node.keywords if k.arg == 'Loader'), node.args[1] if len(node.args) > 1 else None)
                    if loader is None or canonical(loader) not in {'yaml.SafeLoader', 'yaml.CSafeLoader'}:
                        rule, message = 'SEC-PY-YAML-UNSAFE', 'yaml.load without a known SafeLoader; inspect input provenance.'
                elif name in {'requests.get', 'requests.post', 'requests.request', 'httpx.get', 'httpx.post', 'urllib.request.urlopen'}:
                    value = node.args[1] if name == 'requests.request' and len(node.args) > 1 else arg
                    if self.dynamic(value) and self.trace(value):
                        arg = value
                        rule, message = 'SEC-PY-SSRF', 'Caller-derived request destination; inspect scheme, address and redirect restrictions.'
                        preconditions.append('Attacker can select an unexpected internal destination; network access and redirect policy permit it')
                if rule:
                    self.add(node, rule, message, arg, preconditions)
            if name == 'jwt.decode' and any(kw.arg == 'options' and isinstance(kw.value, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == 'verify_signature' and isinstance(v, ast.Constant) and v.value is False for k, v in zip(kw.value.keys, kw.value.values)) for kw in node.keywords):
                self.add(node, 'SEC-PY-JWT-VERIFY-DISABLED', 'JWT signature verification explicitly disabled; inspect whether claims authorize access.', node.args[0] if node.args else node,
                         ['Decoded claims must be used in a security decision', 'No independent signature verification protects the decision'])
            self.generic_visit(node)
    Flow().visit(tree)
    return findings
