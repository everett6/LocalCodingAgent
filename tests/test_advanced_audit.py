import asyncio
import json
import os
import socket
import subprocess

import pytest

from local_coder.security_analysis import analyze_python
from local_coder.tools.advanced_audit import AdvancedAuditTool


def audit(tmp_path, **kwargs):
    return asyncio.run(AdvancedAuditTool(str(tmp_path)).execute(**kwargs))


def test_alias_imports_and_local_assignments_produce_reasoned_candidates():
    findings = analyze_python('''import hashlib as hashes
import subprocess as process
from pickle import loads as unpack
from requests import get as fetch
from pathlib import Path as FilePath
def handler(request):
    query = "SELECT * FROM accounts WHERE id=" + request.args['id']
    db.execute(query)
    process.run(request.args['command'], shell=True)
    unpack(request.data)
    fetch(request.args['url'])
    location = FilePath('public') / request.args['filename']
    location.read_text()
    return hashes.md5(request.data).hexdigest()
''', 'app.py')
    rules = {item['rule_id'] for item in findings}
    assert {'SEC-PY-SQL-INJECTION', 'SEC-PY-SHELL-INJECTION', 'SEC-PY-PICKLE-INSECURE', 'SEC-PY-SSRF', 'SEC-PY-PATH-TRAVERSAL', 'SEC-PY-WEAK-HASH'} <= rules
    for item in findings:
        assert item['cwe'].startswith('CWE-') and item['reasons']
        assert item['verification'] == 'unverified'
        assert item['evidence']['file'] == 'app.py' and item['evidence']['line'] > 0
        assert 'cross-procedural' not in item['confidence']
    sql = next(item for item in findings if item['rule_id'] == 'SEC-PY-SQL-INJECTION')
    assert any(hop['kind'] == 'external_input' for hop in sql['evidence']['source_trace'])


def test_constant_queries_bound_parameters_destinations_and_commands_are_not_injection_candidates():
    findings = analyze_python('''import requests as net
import subprocess as proc
import pickle
SQL = "SELECT " + "name FROM users WHERE id=?"
db.execute(SQL, (user_id,))
net.get('https://example.invalid/static')
proc.run('echo lab', shell=True)
pickle.loads(b'fixed synthetic data')
open('public/static.txt').read()
''', 'constants.py')
    assert findings == []


def test_second_bound_argument_does_not_hide_already_formatted_sql():
    findings = analyze_python('''def find(value):
    db.execute(f"SELECT name FROM users WHERE role={value} AND id=?", (1,))
''', 'queries.py')
    assert any(item['rule_id'] == 'SEC-PY-SQL-INJECTION' for item in findings)
    assert all(item['confidence'] != 'confirmed' for item in findings)


def test_keyword_sinks_and_sha1_usedforsecurity_false():
    findings = analyze_python('''from hashlib import sha1 as legacy
import requests as net
import subprocess as proc
def handler(request):
    net.get(url=request.args['url'])
    proc.run(args=request.args['cmd'], shell=True)
    legacy(b'content identity', usedforsecurity=False)
''', 'keyword.py')
    assert {'SEC-PY-SSRF', 'SEC-PY-SHELL-INJECTION'} <= {item['rule_id'] for item in findings}
    assert not any(item['rule_id'] == 'SEC-PY-WEAK-HASH' for item in findings)


def test_secrets_are_detected_by_entropy_without_returning_literal_or_snippet(tmp_path):
    secret = 'Q7vZ4pW9cB2mR8tK6xD3nH5sF1jL0uAq'
    (tmp_path / 'app.py').write_text(f'API_KEY = {secret!r}\nimport requests\nrequests.get(input(), headers={{"Authorization": {secret!r}}})\n')
    (tmp_path / '.env').write_text('PRIVATE_TOKEN=' + secret)
    result = audit(tmp_path)
    assert result.success
    assert any(item['rule_id'] == 'SEC-PY-HIGH-ENTROPY-SECRET' for item in result.data['findings'])
    serialized = result.model_dump_json()
    assert secret not in serialized and 'API_KEY =' not in serialized
    finding = next(item for item in result.data['findings'] if item['rule_id'] == 'SEC-PY-HIGH-ENTROPY-SECRET')
    assert finding['redacted'] is True
    assert not audit(tmp_path, target_path='.env').success


def test_entropy_suppresses_hashes_ordinary_constants_and_placeholder_secrets():
    source = '''DOCUMENTATION = "a fairly long sentence containing ordinary words and whitespace"
CHECKSUM_HASH = "35b87dfab7161244d5f312fbb65849b9"
API_KEY = "example-token-not-a-real-credential"
PASSWORD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
'''
    assert analyze_python(source, 'config.py', checks=['entropy_secrets']) == []


def test_fixed_iv_and_weak_hash_constructor_names_are_explained():
    findings = analyze_python('''from Crypto.Cipher import AES as Cipher
from hashlib import new as digest
digest('sha1', b'content')
Cipher.new(key, Cipher.MODE_CBC, iv=b'0' * 16)
''', 'crypto.py')
    assert {'SEC-PY-WEAK-HASH', 'SEC-PY-STATIC-IV'} <= {item['rule_id'] for item in findings}


def test_scope_coverage_limits_parse_failures_and_symlink_escape_are_visible(tmp_path):
    (tmp_path / 'a.py').write_text('import hashlib\nhashlib.md5(b"x")\n')
    (tmp_path / 'b.py').write_text('def broken(:\n')
    (tmp_path / 'c.py').write_text('import hashlib\nhashlib.sha1(b"x")\n')
    (tmp_path / 'escape.py').symlink_to(tmp_path.parent / 'outside.py')
    result = audit(tmp_path, max_files=1)
    assert result.success and result.data['coverage']['status'] == 'partial'
    assert result.data['coverage']['files_scanned'] == 1
    assert result.data['coverage']['skipped']
    assert not audit(tmp_path, target_path='../outside.py').success
    assert not audit(tmp_path, target_path='escape.py').success
    complete = audit(tmp_path)
    assert complete.data['coverage']['parse_errors'] == 1
    assert 'def broken' not in json.dumps(complete.data)


def test_reviewed_suppression_is_applied_without_mutating_lessons(tmp_path):
    (tmp_path / 'a.py').write_text('import hashlib\nhashlib.md5(b"x")\n')
    lessons = '## Suppress\n- SEC-PY-WEAK-HASH | a.py | Deliberate compatibility fixture.\n'
    (tmp_path / 'SECURITY_LESSONS.md').write_text(lessons)
    result = audit(tmp_path)
    assert result.success and result.data['findings'] == []
    assert result.data['coverage']['suppressed'] == 1
    assert (tmp_path / 'SECURITY_LESSONS.md').read_text() == lessons


def test_filter_and_argument_bounds_are_enforced_without_execution(tmp_path, monkeypatch):
    (tmp_path / 'a.py').write_text('import hashlib\nhashlib.md5(b"x")\nraise RuntimeError("do not execute")\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Static audit cannot execute commands or access the network')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    assert audit(tmp_path, checks=['weak_crypto']).success
    assert audit(tmp_path, checks=['dynamic_sql']).data['findings'] == []
    for kwargs in [{'max_files': 0}, {'max_files': True}, {'max_findings': 501}, {'max_bytes': 1}, {'checks': ['network_attack']}, {'confirmed': True}]:
        assert not audit(tmp_path, **kwargs).success


def test_guards_are_not_claimed_as_proven_and_no_cross_function_taint_is_invented():
    findings = analyze_python('''from pathlib import Path
def load(name):
    path = Path(name).resolve()
    if path.name:
        return path.read_text()
def caller(request):
    return load(request.args['filename'])
''', 'flow.py')
    candidate = next(item for item in findings if item['rule_id'] == 'SEC-PY-PATH-TRAVERSAL')
    assert candidate['confidence'] == 'parameter-linked'
    assert any('guard' in limit.lower() for limit in candidate['limitations'])
    assert not any(hop['kind'] == 'external_input' for hop in candidate['evidence']['source_trace'])


def test_shadowed_imports_and_local_open_functions_are_not_builtin_sinks():
    source = '''from hashlib import md5 as digest
def digest(value):
    return value
digest(b'not a hash call')
import hashlib
hashlib = object()
hashlib.md5(b'not the module')
def handler(open, filename):
    return open(filename)
'''
    assert analyze_python(source, 'shadowed.py') == []


def test_byte_budget_prevents_reading_oversized_source_files(tmp_path, monkeypatch):
    from pathlib import Path
    file = tmp_path / 'large.py'
    file.write_text('#' + 'x' * 3000)
    real_open = Path.open
    def bounded_open(path, *args, **kwargs):
        if path == file:
            pytest.fail('The source size is outside the requested read budget')
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', bounded_open)
    result = audit(tmp_path, max_bytes=1024)
    assert result.success and result.data['coverage']['status'] == 'partial'
    assert result.data['coverage']['bytes_scanned'] == 0


def test_source_hash_and_embedded_private_key_are_redacted(tmp_path):
    import hashlib
    secret = '-----BEGIN PRIVATE KEY-----\nQ7vZ4pW9cB2mR8tK6xD3nH5sF1jL0uAq\n-----END PRIVATE KEY-----'
    source = 'PRIVATE_KEY=' + repr(secret) + '\n'
    (tmp_path / 'config.py').write_text(source)
    result = audit(tmp_path)
    assert result.success and result.data['findings']
    finding = result.data['findings'][0]
    assert finding['evidence']['source_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert secret not in result.model_dump_json()
    assert 'Q7vZ4pW9' not in result.model_dump_json()


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Named pipes require POSIX')
def test_named_pipe_source_is_skipped_before_opening(tmp_path, monkeypatch):
    from pathlib import Path
    pipe = tmp_path / 'blocked.py'
    os.mkfifo(pipe)
    (tmp_path / 'ordinary.py').write_text('VALUE = 1\n')
    original_open = Path.open
    original_os_open = os.open

    def reject_pipe_open(path, *args, **kwargs):
        if Path(path) == pipe:
            pytest.fail('A named pipe must be rejected before opening it')
        return original_open(path, *args, **kwargs)

    def reject_pipe_descriptor(path, *args, **kwargs):
        if Path(path) == pipe:
            pytest.fail('A named pipe must be rejected before opening it')
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', reject_pipe_open)
    monkeypatch.setattr(os, 'open', reject_pipe_descriptor)
    result = audit(tmp_path)
    assert result.success
    assert result.data['coverage']['files_scanned'] == 1
    assert result.data['coverage']['status'] == 'partial'
    assert any('blocked.py' in item for item in result.data['coverage']['skipped'])


def test_replaced_source_descriptor_is_rejected_before_reading(tmp_path, monkeypatch):
    source = tmp_path / 'source.py'
    replacement = tmp_path / 'replacement.txt'
    source.write_text('VALUE = 1\n')
    replacement.write_text('PRIVATE_KEY = "not source content"\n')
    original_open = os.open

    def substituted_open(path, flags, *args, **kwargs):
        if path == source:
            if hasattr(os, 'O_NONBLOCK'):
                assert flags & os.O_NONBLOCK
            if hasattr(os, 'O_NOFOLLOW'):
                assert flags & os.O_NOFOLLOW
            return original_open(replacement, flags, *args, **kwargs)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', substituted_open)
    result = audit(tmp_path, target_path='source.py')
    assert result.success and result.data['findings'] == []
    assert result.data['coverage']['files_scanned'] == 0
    assert result.data['coverage']['status'] == 'partial'


def test_nested_calls_do_not_repeat_exponential_child_traversal(monkeypatch):
    import ast
    original_children = ast.iter_child_nodes
    visits = 0

    def counted_children(node):
        nonlocal visits
        visits += 1
        # An operation budget proves bounded traversal without a timing-based
        # assertion or allowing a regressed implementation to consume CPU.
        assert visits < 4000, 'Nested calls repeated their argument traversal'
        return original_children(node)

    monkeypatch.setattr(ast, 'iter_child_nodes', counted_children)
    expression = 'input()'
    for _ in range(16):
        expression = f'wrapper({expression})'
    source = f'import subprocess\ncommand = {expression}\nsubprocess.run(command, shell=True)\n'
    findings = analyze_python(source, 'nested.py')
    candidate = next(item for item in findings if item['rule_id'] == 'SEC-PY-SHELL-INJECTION')
    assert candidate['confidence'] == 'source-linked'
