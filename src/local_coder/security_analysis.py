"""Bounded Python AST candidates with local value provenance, never execution.

This is a small intraprocedural approximation, not a complete taint engine. It
does not establish caller reachability, effective guards, or exploitability.
Source literals and source excerpts are deliberately absent from every result.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import hashlib
import math
import re
from typing import Any

from local_coder.tools.detection import dotted, python_candidates


CHECKS = ('weak_crypto', 'dynamic_sql', 'unsafe_deserialization', 'shell_injection',
          'ssrf', 'path_traversal', 'entropy_secrets')
LIMITATIONS = [
    'Intraprocedural assignments and import aliases only; no cross-function or cross-file taint propagation.',
    'Reachability, receiver types, authorization and guard effectiveness are not proven.',
    'Branches are conservatively merged; loops, exceptions, dynamic dispatch and runtime imports are incomplete.',
    'No project code, generated code, scanner process or network request was executed.',
]
_SECRET_NAME = re.compile(r'api[_-]?key|secret|token|password|credential|private[_-]?key', re.I)
_NONSECRET_NAME = re.compile(r'(?:hash|digest|checksum|fingerprint|public[_-]?key|token[_-]?id)$', re.I)


class AnalysisLimit(ValueError):
    """A source unit exceeded the analyzer's explicit complexity limit."""


@dataclass
class _Fact:
    constant: bool = False
    value: Any = None
    sources: tuple = ()
    pathlike: bool = False
    client: bool = False


def _sources(*facts: _Fact) -> tuple:
    result = []
    for fact in facts:
        for item in fact.sources:
            if item not in result:
                result.append(item)
            if len(result) >= 16:
                return tuple(result)
    return tuple(result)


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def analyze_python(source: str, filename: str, checks: list[str] | None = None,
                   max_findings: int = 100) -> list[dict]:
    """Return redacted candidates; callers handle parse/coverage failures."""
    selected = set(CHECKS if checks is None else checks)
    if selected - set(CHECKS):
        raise ValueError('Unknown advanced audit check')
    if not isinstance(source, str) or len(source.encode('utf-8')) > 256000:
        raise AnalysisLimit('Python source exceeds the 256 KB analysis limit')
    if type(max_findings) is not int or not 1 <= max_findings <= 500:
        raise ValueError('max_findings must be from 1 through 500')
    tree = ast.parse(source, filename=filename)
    for index, _ in enumerate(ast.walk(tree)):
        if index >= 40000:
            raise AnalysisLimit('Python source exceeds the 40000 AST node limit')
    # Reuse the established detector's sink rules and caller-site observations.
    # Its raw snippets/guard expressions never cross this module's output boundary.
    existing = {(item['line'], item['rule_id']): item for item in python_candidates(source, filename)}
    findings = []
    identities = set()
    source_hash = hashlib.sha256(source.encode('utf-8')).hexdigest()

    class Analyzer(ast.NodeVisitor):
        def __init__(self):
            self.bindings = {}
            self.aliases = {}
            self.function = '<module>'

        def canonical(self, node):
            name = dotted(node)
            first, _, rest = name.partition('.')
            if first in self.bindings and first not in self.aliases:
                # Framework request names remain lexical input hints, not a
                # proof of their runtime type. Other rebound names must not be
                # mistaken for the original builtin or imported module.
                return name if first == 'request' else '<local>' + ('.' + rest if rest else '')
            return self.aliases.get(first, first) + ('.' + rest if rest else '')

        def fact(self, node, depth=0):
            if node is None or depth > 60:
                return _Fact()
            if isinstance(node, ast.Constant):
                return _Fact(True, node.value)
            if isinstance(node, ast.Name):
                return self.bindings.get(node.id, _Fact())
            name = self.canonical(node.func) if isinstance(node, ast.Call) else self.canonical(node)
            if (name == 'input' and 'input' not in self.bindings) or name.startswith(('request.', 'flask.request.', 'sys.argv', 'os.environ')):
                return _Fact(sources=({'kind': 'external_input', 'line': node.lineno},))
            if isinstance(node, ast.Call):
                # Calls must be handled before generic child traversal: visiting
                # their children and then arguments again doubles work at every
                # nested call. Reuse this receiver/argument snapshot, whose facts
                # are valid for the current bindings and aliases only.
                arguments = [self.fact(arg, depth + 1) for arg in node.args]
                arguments += [self.fact(kw.value, depth + 1) for kw in node.keywords]
                receiver = self.fact(node.func.value, depth + 1) if isinstance(node.func, ast.Attribute) else None
                callee = receiver if receiver is not None else self.fact(node.func, depth + 1)
                traces = _sources(callee, *arguments)
                if name in {'pathlib.Path', 'pathlib.PurePath', 'os.path.join'}:
                    return _Fact(all(arg.constant for arg in arguments), sources=_sources(*arguments), pathlike=True)
                if name in {'requests.Session', 'requests.session', 'httpx.Client', 'httpx.AsyncClient'}:
                    return _Fact(client=True)
                if receiver is not None:
                    if receiver.pathlike:
                        return _Fact(receiver.constant and all(arg.constant for arg in arguments),
                                     sources=_sources(receiver, *arguments), pathlike=True)
                    if node.func.attr in {'format', 'join'} and receiver.constant and all(arg.constant for arg in arguments):
                        return _Fact(True)
                return _Fact(sources=traces)
            children = [self.fact(child, depth + 1) for child in ast.iter_child_nodes(node)
                        if not isinstance(child, (ast.Load, ast.Store, ast.operator, ast.unaryop))]
            traces = _sources(*children)
            if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.UnaryOp)):
                return _Fact(bool(children) and all(child.constant for child in children), sources=traces,
                             pathlike=any(child.pathlike for child in children))
            return _Fact(sources=traces)

        def add(self, node, check, rule, cwe, message, value=None, severity='HIGH', reasons=None, **extra):
            if check not in selected or len(findings) >= max_findings or (node.lineno, rule) in identities:
                return
            identities.add((node.lineno, rule))
            fact = self.fact(value)
            trace = list(fact.sources)
            confidence = 'source-linked' if any(item['kind'] == 'external_input' for item in trace) else 'parameter-linked' if any(item['kind'] == 'parameter' for item in trace) else 'heuristic'
            base = existing.get((node.lineno, rule), {})
            evidence = {'file': filename, 'line': node.lineno,
                        'source_sha256': source_hash,
                        'sink': self.canonical(node.func)[:160] if isinstance(node, ast.Call) else 'string_assignment',
                        'source_trace': trace, 'function': self.function,
                        'possible_caller_lines': base.get('callers', [])[:20]}
            findings.append({
                'file': filename, 'line': node.lineno, 'rule_id': rule, 'check': check,
                'cwe': cwe, 'severity': severity, 'confidence': confidence,
                'message': message, 'verification': 'unverified',
                'reasons': reasons or ['A matching AST sink receives a nonconstant value.',
                                       'Input provenance is local evidence; attacker control and effective defenses require review.'],
                'evidence': evidence, 'limitations': list(LIMITATIONS), **extra,
            })

        def visit_Import(self, node):
            for alias in node.names:
                key = alias.asname or alias.name.split('.')[0]
                self.bindings.pop(key, None)
                self.aliases[key] = alias.name if alias.asname else alias.name.split('.')[0]

        def visit_ImportFrom(self, node):
            if node.module:
                for alias in node.names:
                    key = alias.asname or alias.name
                    self.bindings.pop(key, None)
                    self.aliases[key] = node.module + '.' + alias.name

        def visit_FunctionDef(self, node):
            previous = self.bindings, self.aliases, self.function
            self.bindings, self.aliases, self.function = dict(self.bindings), dict(self.aliases), node.name
            self.aliases.pop(node.name, None)
            self.bindings[node.name] = _Fact()
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [arg for arg in (node.args.vararg, node.args.kwarg) if arg]:
                self.aliases.pop(arg.arg, None)
                self.bindings[arg.arg] = _Fact(sources=({'kind': 'parameter', 'name': arg.arg[:120], 'line': arg.lineno},))
            for statement in node.body:
                self.visit(statement)
            self.bindings, self.aliases, self.function = previous
            self.aliases.pop(node.name, None)
            self.bindings[node.name] = _Fact()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            previous = self.bindings, self.aliases
            self.bindings, self.aliases = dict(self.bindings), dict(self.aliases)
            for statement in node.body:
                self.visit(statement)
            self.bindings, self.aliases = previous
            self.aliases.pop(node.name, None)
            self.bindings[node.name] = _Fact()

        def secret(self, value, label):
            if 'entropy_secrets' not in selected or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return
            literal = value.value
            if not _SECRET_NAME.search(label) or _NONSECRET_NAME.search(label):
                return
            if re.search(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', literal):
                self.add(value, 'entropy_secrets', 'SEC-PY-PRIVATE-KEY', 'CWE-321',
                         'Private-key marker in a sensitive source assignment; inspect whether the key is real and exposed.',
                         severity='HIGH', reasons=['A known private-key envelope appears in a sensitive source field.',
                                                   'The key is not parsed, validated, used or returned.'],
                         redacted=True, literal_length=len(literal))
                return
            if (not 20 <= len(literal) <= 4096 or any(character.isspace() for character in literal)
                    or re.search(r'example|placeholder|changeme|dummy|not[_-]?a[_-]?real', literal, re.I)):
                return
            entropy = _entropy(literal)
            if entropy < 3.5 or len(set(literal)) < 12:
                return
            self.add(value, 'entropy_secrets', 'SEC-PY-HIGH-ENTROPY-SECRET', 'CWE-798',
                     'High-entropy literal assigned to a sensitive field; verify whether it is a real credential.',
                     severity='MEDIUM', reasons=['Sensitive assignment name and at least 20 non-whitespace characters.',
                                                 'Shannon entropy exceeds 3.5 bits per character; entropy alone does not establish a secret.'],
                     redacted=True, entropy_bits_per_character=round(entropy, 2), literal_length=len(literal))

        def bind(self, target, value):
            if isinstance(target, ast.Name):
                self.secret(value, target.id)
                fact = self.fact(value)
                if not fact.constant and fact.sources:
                    fact = _Fact(sources=_sources(fact, _Fact(sources=({'kind': 'assignment', 'name': target.id[:120], 'line': target.lineno},))), pathlike=fact.pathlike, client=fact.client)
                self.bindings[target.id] = fact
                self.aliases.pop(target.id, None)
            elif isinstance(target, ast.Attribute):
                self.secret(value, target.attr)
            elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                self.secret(value, target.slice.value)

        def visit_Assign(self, node):
            self.visit(node.value)
            for target in node.targets:
                self.bind(target, node.value)

        def visit_AnnAssign(self, node):
            if node.value is not None:
                self.visit(node.value)
                self.bind(node.target, node.value)

        def visit_Dict(self, node):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    self.secret(value, key.value)
            self.generic_visit(node)

        def visit_If(self, node):
            self.visit(node.test)
            before = dict(self.bindings)
            for statement in node.body:
                self.visit(statement)
            left = dict(self.bindings)
            self.bindings = dict(before)
            for statement in node.orelse:
                self.visit(statement)
            right = self.bindings
            self.bindings = {}
            for key in left.keys() | right.keys():
                first, second = left.get(key, _Fact()), right.get(key, _Fact())
                self.bindings[key] = first if first == second else _Fact(constant=first.constant and second.constant,
                    sources=_sources(first, second), pathlike=first.pathlike or second.pathlike, client=first.client or second.client)

        def visit_Call(self, node):
            name = self.canonical(node.func)
            keyword = {item.arg: item.value for item in node.keywords if item.arg}
            argument = node.args[0] if node.args else next((keyword[key] for key in ('query', 'sql', 'operation', 'args', 'data', 'file', 'url') if key in keyword), None)
            dynamic = argument is not None and not self.fact(argument).constant
            hash_name = name in {'hashlib.md5', 'hashlib.sha1'}
            if name == 'hashlib.new' and argument is not None:
                hash_name = str(self.fact(argument).value).lower() in {'md4', 'md5', 'sha1'}
            if hash_name and self.fact(keyword.get('usedforsecurity')).value is not False:
                self.add(node, 'weak_crypto', 'SEC-PY-WEAK-HASH', 'CWE-328',
                         'Legacy hash primitive; inspect whether collision resistance protects a security decision.',
                         severity='MEDIUM', reasons=['Known MD4, MD5 or SHA-1 constructor selected.', 'Compatibility checksums may be intentional; security use is not established.'])
            iv = keyword.get('iv', keyword.get('nonce'))
            if name in {'Crypto.Cipher.AES.new', 'Cryptodome.Cipher.AES.new'} and iv is None and len(node.args) > 2:
                iv = node.args[2]
            if name.endswith(('.modes.CBC', '.modes.CFB', '.modes.OFB', '.modes.CTR')):
                iv = argument
            if iv is not None and self.fact(iv).constant and (name in {'Crypto.Cipher.AES.new', 'Cryptodome.Cipher.AES.new'} or '.modes.' in name):
                self.add(node, 'weak_crypto', 'SEC-PY-STATIC-IV', 'CWE-329',
                         'Constant IV or nonce at a known cipher constructor; verify mode-specific freshness requirements.',
                         severity='MEDIUM', reasons=['The IV or nonce is locally constant.', 'Key reuse, mode and repeated encryption are not proven.'])
            if dynamic:
                if name.endswith(('.execute', '.executemany', '.executescript')):
                    self.add(node, 'dynamic_sql', 'SEC-PY-SQL-INJECTION', 'CWE-89',
                             'Nonconstant SQL text; inspect interpolation, parameter binding and receiver type.', argument,
                             reasons=['Query text is nonconstant at an execute-like call.', 'Additional bound parameters do not sanitize text already interpolated into a query.', 'The receiver must be verified as a SQL API; arbitrary execute methods are not proven SQL.'])
                shell = name in {'os.system', 'os.popen'} or (name in {'subprocess.run', 'subprocess.call', 'subprocess.Popen', 'subprocess.check_call', 'subprocess.check_output'} and self.fact(keyword.get('shell')).value is True)
                if shell:
                    self.add(node, 'shell_injection', 'SEC-PY-SHELL-INJECTION', 'CWE-78', 'Nonconstant command reaches an explicitly shell-enabled call.', argument)
                if name in {'pickle.load', 'pickle.loads', 'dill.load', 'dill.loads', 'marshal.load', 'marshal.loads'}:
                    self.add(node, 'unsafe_deserialization', 'SEC-PY-PICKLE-INSECURE', 'CWE-502', 'Nonconstant bytes or stream reaches an unsafe object deserializer.', argument)
                if name in {'open', 'io.open', 'os.open'}:
                    self.add(node, 'path_traversal', 'SEC-PY-PATH-TRAVERSAL', 'CWE-22', 'Nonconstant file path; inspect canonical containment and caller authority.', argument)
            verbs = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'request'}
            network = name == 'urllib.request.urlopen' or name.split('.')[0] in {'requests', 'httpx'} and name.split('.')[-1] in verbs
            if isinstance(node.func, ast.Attribute):
                receiver = self.fact(node.func.value)
                network = network or receiver.client and node.func.attr in verbs
                if receiver.pathlike and node.func.attr in {'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes'} and not receiver.constant:
                    self.add(node, 'path_traversal', 'SEC-PY-PATH-TRAVERSAL', 'CWE-22', 'Caller-derived Path reaches file access; canonicalization alone does not prove containment.', node.func.value)
            if network:
                url = keyword.get('url')
                if url is None:
                    index = 1 if name.endswith('.request') else 0
                    url = node.args[index] if len(node.args) > index else None
                if url is not None and not self.fact(url).constant:
                    self.add(node, 'ssrf', 'SEC-PY-SSRF', 'CWE-918', 'Nonconstant outbound destination; inspect exact destination policy, DNS and redirects.', url)
            self.generic_visit(node)

    Analyzer().visit(tree)
    return sorted(findings, key=lambda item: (item['line'], item['rule_id']))
