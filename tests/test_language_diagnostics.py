import json

import pytest

from tests.test_language_service import async_test, service_with_server


def diagnostic_service(tmp_path, mode):
    service = service_with_server(tmp_path, mode)
    script = tmp_path / "fake_server.py"
    source = script.read_text().replace("log = root / 'messages.log'", "log = root / 'messages.log'\nversion = 0")
    source = source.replace("    if 'method' not in message or 'id' not in message:", """    if message.get('method') in ('textDocument/didOpen', 'textDocument/didChange'):
        version = message['params']['textDocument']['version']
    if 'method' not in message or 'id' not in message:""")
    source = source.replace("        result = {'capabilities': capabilities}", """        if mode.startswith('pull'):
            capabilities['diagnosticProvider'] = {}
        result = {'capabilities': capabilities}""")
    source = source.replace("    elif method == 'textDocument/documentSymbol':", """    elif method == 'textDocument/diagnostic':
        issue = {'range': {'start': {'line': 0, 'character': 4}}, 'severity': 1, 'message': 'Type mismatch', 'source': 'test'}
        result = {'kind': 'full', 'items': [issue]}
        if mode == 'pull-malformed':
            result = {'kind': 'unchanged', 'resultId': 'not-requested'}
        if mode == 'pull-edited':
            (root / 'sample.py').write_text('changed = True')
    elif method == 'textDocument/documentSymbol':
        if mode.startswith('push'):
            notification = {'uri': (root / 'sample.py').as_uri(), 'diagnostics': [{'range': {'start': {'line': 0, 'character': 4}}, 'message': 'Published type error'}]}
            if mode != 'push-unversioned':
                notification['version'] = version if mode == 'push' else version - 1
            send({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': notification})""")
    script.write_text(source)
    return service


@async_test
@pytest.mark.parametrize('mode,availability', [('pull', 'pull'), ('push', 'versioned-push')])
async def test_versioned_diagnostics_reach_context_and_refresh(tmp_path, mode, availability):
    service = diagnostic_service(tmp_path, mode)
    try:
        for version in (1, 2):
            result = await service.context('sample.py')
            assert result['version'] == version
            assert result['availability']['diagnostics'] == availability
            assert result['diagnostics'][0]['character'] == 3
            assert 'diagnostic: 1:3' in result['text']
            (tmp_path / 'sample.py').write_text('# 😀 target\nvalue = 2\n')
        messages = [json.loads(row) for row in (tmp_path / 'messages.log').read_text().splitlines()]
        assert any(m.get('method') == 'textDocument/didChange' for m in messages)
    finally:
        await service.close()


@async_test
@pytest.mark.parametrize('mode', ['push-stale', 'push-unversioned', 'pull-malformed', 'pull-edited'])
async def test_unverified_or_stale_diagnostics_are_not_current(tmp_path, mode):
    service = diagnostic_service(tmp_path, mode)
    try:
        result = await service.context('sample.py')
        assert result['diagnostics'] == []
        assert result['availability']['diagnostics'] in {'unavailable', 'stale-source'}
        assert 'Type mismatch' not in result['text']
    finally:
        await service.close()


def test_diagnostics_are_bounded_and_ignore_malformed_items(tmp_path):
    from local_coder.context.language_service import LanguageService
    item = {'range': {'start': {'line': 0, 'character': 0}}, 'message': '\x1b' + 'x' * 2000}
    result = LanguageService._diagnostics([item] * 100, 'sample')
    assert len(result) == 50
    assert len(result[0]['message']) == 1000
    assert '\x1b' not in result[0]['message']
    assert LanguageService._diagnostics([None, {}, {'range': [], 'message': 'bad'}], 'sample') == []
