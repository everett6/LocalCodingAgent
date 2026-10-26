"""Integration tests use a real framed stdio subprocess, never a mock transport."""
import asyncio
import importlib.util
from functools import wraps
import json
import os
import sys
from pathlib import Path

import pytest

from local_coder.context.language_service import ADAPTERS, LanguageService, _utf16_character, _unicode_character
from local_coder.tools.language_context import LanguageContextTool


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


FAKE_SERVER = r'''
import json, os, pathlib, subprocess, sys, time
root, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
log = root / 'messages.log'

def send(value):
    data = json.dumps(value, ensure_ascii=False).encode()
    sys.stdout.buffer.write(('Content-Length: %d\r\n\r\n' % len(data)).encode() + data)
    sys.stdout.buffer.flush()

while True:
    headers = {}
    while True:
        row = sys.stdin.buffer.readline()
        if not row:
            sys.exit(0)
        if row == b'\r\n':
            break
        key, value = row.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    with log.open('a') as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + '\n')
    if 'method' not in message or 'id' not in message:
        continue
    method = message['method']
    if method == 'initialize':
        if mode == 'malformed':
            sys.stdout.buffer.write(b'Content-Length: 4\r\n\r\nnope')
            sys.stdout.buffer.flush()
            continue
        if mode == 'oversized':
            sys.stdout.buffer.write(b'Content-Length: 999999999\r\n\r\n')
            sys.stdout.buffer.flush()
            continue
        capabilities = {} if mode == 'unsupported' else {'textDocumentSync': 1, 'documentSymbolProvider': True, 'definitionProvider': True, 'referencesProvider': {}, 'hoverProvider': True}
        result = {'capabilities': capabilities}
    elif mode in ('hang', 'child'):
        if mode == 'child':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            (root / 'child.pid').write_text(str(child.pid))
        time.sleep(120)
        continue
    elif method == 'textDocument/documentSymbol':
        send({'jsonrpc': '2.0', 'id': 'server-edit', 'method': 'workspace/applyEdit', 'params': {'edit': {'changes': {}}}})
        send({'jsonrpc': '2.0', 'id': 'server-command', 'method': 'workspace/executeCommand', 'params': {'command': 'touch', 'arguments': ['evil']}})
        result = [{'name': 'target', 'kind': 12, 'selectionRange': {'start': {'line': 1, 'character': 4}}, 'range': {'start': {'line': 1, 'character': 0}}, 'children': []}]
    elif method in ('textDocument/definition', 'textDocument/references'):
        uris = [(root / 'sample.py').as_uri(), (root.parent / 'outside.py').as_uri(), (root / '.env').as_uri(), (root / '.local-coder' / 'state.py').as_uri(), (root / 'link.py').as_uri(), 'https://example.test/a.py']
        result = [{'uri': uri, 'range': {'start': {'line': 0, 'character': 4}, 'end': {'line': 0, 'character': 5}}} for uri in uris]
    elif method == 'textDocument/hover':
        result = {'contents': {'kind': 'markdown', 'value': 'target documentation'}}
    else:
        result = None
    send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
'''


def service_with_server(tmp_path, mode='normal', **options):
    script = tmp_path / 'fake_server.py'
    script.write_text(FAKE_SERVER)
    (tmp_path / 'sample.py').write_text('# 😀 target\ndef target():\n    return 1\n')
    (tmp_path / '.env').write_text('PRIVATE=hidden')
    (tmp_path / '.local-coder').mkdir(exist_ok=True)
    (tmp_path / '.local-coder' / 'state.py').write_text('SECRET = 1')
    (tmp_path / 'link.py').symlink_to(tmp_path / 'sample.py')
    return LanguageService(tmp_path, server_commands={'python': [sys.executable, str(script), str(tmp_path), mode]}, request_timeout=options.pop('request_timeout', 1), **options)


@async_test
async def test_real_framing_capabilities_utf16_versions_and_readonly(tmp_path):
    service = service_with_server(tmp_path)
    try:
        result = await service.context('sample.py', line=1, character=4)
        assert result['availability']['semantic'] is True
        assert result['version'] == 1
        assert result['hover'] == 'target documentation'
        assert result['symbols'][0]['name'] == 'target'
        assert result['definitions'] == [{'path': 'sample.py', 'line': 1, 'character': 3, 'provider': 'lsp'}]
        assert result['references'] == result['definitions']
        (tmp_path / 'sample.py').write_text('# 😀 target\ndef target():\n    return 2\n')
        again = await service.context('sample.py', line=1, character=4)
        assert again['version'] == 2
        messages = [json.loads(row) for row in (tmp_path / 'messages.log').read_text().splitlines()]
        definition = next(m for m in messages if m.get('method') == 'textDocument/definition')
        assert definition['params']['position'] == {'line': 0, 'character': 5}
        assert any(m.get('method') == 'textDocument/didChange' and m['params']['textDocument']['version'] == 2 for m in messages)
        replies = {m['id']: m for m in messages if 'method' not in m and 'id' in m}
        assert replies['server-edit']['result']['applied'] is False
        assert replies['server-command']['error']['code'] == -32601
        assert not (tmp_path / 'evil').exists()
    finally:
        await service.close()
    assert not service._clients


@async_test
async def test_unsupported_capabilities_do_not_send_unsupported_requests(tmp_path):
    service = service_with_server(tmp_path, 'unsupported')
    try:
        result = await service.context('sample.py')
        assert result['availability']['semantic'] is False
        assert result['symbols'][0]['provider'] == 'python-ast'
        messages = [json.loads(row) for row in (tmp_path / 'messages.log').read_text().splitlines()]
        assert not any(m.get('method', '').startswith('textDocument/') and 'id' in m for m in messages)
    finally:
        await service.close()


@async_test
@pytest.mark.parametrize('mode', ['malformed', 'oversized', 'hang'])
async def test_protocol_error_and_timeout_fall_back_and_reap(tmp_path, mode):
    service = service_with_server(tmp_path, mode, request_timeout=0.15)
    result = await asyncio.wait_for(service.context('sample.py'), 2)
    assert result['availability']['semantic'] is False
    assert 'LSP unavailable' in result['availability']['error']
    assert result['symbols'][0]['provider'] == 'python-ast'
    assert service._clients['python'].process is None
    await service.close()


@async_test
async def test_cancellation_reaps_server_and_child(tmp_path):
    service = service_with_server(tmp_path, 'child', request_timeout=5)
    task = asyncio.create_task(service.context('sample.py'))
    for _ in range(100):
        if (tmp_path / 'child.pid').exists():
            break
        await asyncio.sleep(0.02)
    assert (tmp_path / 'child.pid').exists()
    process = service._clients['python'].process
    child = int((tmp_path / 'child.pid').read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    if os.name != 'nt':
        status = Path(f'/proc/{child}/stat')
        try:
            state = status.read_text().split()[2]
        except FileNotFoundError:
            state = None  # The reaped process can disappear between observations.
        assert state in {None, 'Z'}
    await service.close()


def test_python_ast_real_symbols_and_syntax_errors(tmp_path):
    source = tmp_path / 'example.py'
    source.write_text('class Service:\n    async def run(self):\n        return 1\n\ndef top():\n    pass\n')
    service = LanguageService(tmp_path, auto_discover=False)
    symbols = service.syntax_symbols('example.py')
    assert {s['name'] for s in symbols} == {'Service', 'run', 'top'}
    assert next(s for s in symbols if s['name'] == 'run')['line'] == 2
    source.write_text('def broken(')
    assert service.syntax_symbols('example.py') == []
    assert 'Python AST' in service._parser_errors['python']


def test_utf16_preserves_unicode_line_separators_and_trailing_empty_line():
    text = '# \u2028😀x\r\n'
    assert _utf16_character(text, 1, 4) == 5
    assert _unicode_character(text, 1, 5) == 4
    assert _utf16_character(text, 2, 4) == 0


def test_malformed_nested_locations_and_symbols_are_ignored(tmp_path):
    (tmp_path / 'sample.py').write_text('pass')
    service = LanguageService(tmp_path, auto_discover=False)
    assert service._locations([{'uri': (tmp_path / 'sample.py').as_uri(), 'range': []}]) == []
    assert service._symbols([{'name': 'bad', 'range': []}], tmp_path / 'sample.py') == []


@async_test
async def test_adapter_availability_honest_and_bounded_fallback(tmp_path):
    (tmp_path / 'unknown.zork').write_text('some text')
    (tmp_path / 'example.py').write_text('def hello():\n    return 1\n')
    service = LanguageService(tmp_path, auto_discover=False)
    status = service.status()
    assert status['adapter_count'] >= 20
    assert len({a.language for a in ADAPTERS}) == len(ADAPTERS)
    assert not any(row['server_available'] for row in status['languages'])
    python = next(row for row in status['languages'] if row['language'] == 'python')
    assert python['parser'] == 'python-ast'
    context = await service.context('unknown.zork', char_budget=128)
    assert context['symbols'] == [] and context['providers'] == []
    assert len(context['text']) <= 128
    tool = LanguageContextTool(service)
    output = await tool.execute('example.py', char_budget=200)
    assert output.success and 'hello' in output.output and len(output.output) <= 200


@pytest.mark.parametrize('path', ['../escape.py', '.env', '.local-coder/state.py', 'link.py', 'secrets.json', 'nested/link/example.py'])
def test_excluded_paths_and_symlinks(tmp_path, path):
    (tmp_path / '.env').write_text('secret')
    (tmp_path / 'secrets.json').write_text('{}')
    (tmp_path / '.local-coder').mkdir()
    (tmp_path / '.local-coder' / 'state.py').write_text('secret')
    (tmp_path / 'example.py').write_text('pass')
    (tmp_path / 'link.py').symlink_to(tmp_path / 'example.py')
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested' / 'link').symlink_to(tmp_path, target_is_directory=True)
    service = LanguageService(tmp_path, auto_discover=False)
    with pytest.raises(ValueError):
        service.syntax_symbols(path)


@async_test
async def test_commands_require_explicit_absolute_executable(tmp_path):
    (tmp_path / 'example.py').write_text('pass')
    service = LanguageService(tmp_path, server_commands={'python': ['pylsp']})
    result = await service.context('example.py')
    assert 'ValueError' in result['availability']['error']
    assert service._clients == {}


def test_tree_sitter_uses_actual_installed_parser_if_available(tmp_path):
    if not importlib.util.find_spec('tree_sitter') or not importlib.util.find_spec('tree_sitter_javascript'):
        pytest.skip('Optional locally installed tree-sitter JavaScript grammar unavailable')
    (tmp_path / 'example.js').write_text('function actualParser() { return 1; }')
    service = LanguageService(tmp_path, auto_discover=False)
    assert any(s['name'] == 'actualParser' and s['provider'] == 'tree-sitter' for s in service.syntax_symbols('example.js'))
