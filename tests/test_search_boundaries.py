"""Local search privacy, provenance and bounded process lifecycle regressions."""
import asyncio
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil

import pytest

from local_coder.tools import search
from local_coder.tools.search import GrepTool, SearchFilesTool


@pytest.mark.parametrize('fallback', [False, True])
def test_search_excludes_private_and_alias_sources_on_both_engines(tmp_path, monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(search.shutil, 'which', lambda name: None)
    elif shutil.which('rg') is None:
        pytest.skip('ripgrep unavailable')
    public = tmp_path / 'public:source.txt'
    public.write_text('needle: public: value\n')
    (tmp_path / '.env').write_text('needle private-env-value\n')
    (tmp_path / 'secrets.json').write_text('needle private-json-value\n')
    (tmp_path / '.local-coder').mkdir()
    (tmp_path / '.local-coder/session.txt').write_text('needle private-engine-value\n')
    (tmp_path / 'alias.txt').symlink_to(tmp_path / '.env')
    os.link(tmp_path / '.env', tmp_path / 'hardlink.txt')
    if hasattr(os, 'mkfifo'):
        os.mkfifo(tmp_path / 'pipe.txt')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle', file_pattern='**/*'))
    assert result.success
    assert result.data['files_read'] == ['public:source.txt']
    assert result.data['matches'] == [{'path': 'public:source.txt', 'line': 1, 'text': 'needle: public: value'}]
    assert result.data['source_hashes']['public:source.txt'] == hashlib.sha256(public.read_bytes()).hexdigest()
    assert 'private-env-value' not in result.output and 'private-engine-value' not in str(result.data)
    assert 'hardlink.txt' not in result.output and 'private-json-value' not in result.output


@pytest.mark.parametrize('tool,arguments', [(GrepTool, {'pattern': 'needle'}), (SearchFilesTool, {'pattern': '*'})])
@pytest.mark.parametrize('path', ['.env', '.local-coder', 'alias'])
def test_explicit_private_or_symbolic_target_is_rejected(tmp_path, tool, arguments, path):
    (tmp_path / '.env').write_text('needle private\n')
    (tmp_path / '.local-coder').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / '.env')
    result = asyncio.run(tool(str(tmp_path)).execute(path=path, **arguments))
    assert not result.success and 'private' not in result.data.get('files_read', [])


def test_newline_filename_and_colons_in_text_keep_host_owned_provenance(tmp_path):
    name = 'odd:name\nsource.txt'
    (tmp_path / name).write_text('prefix\nneedle: first: second\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and result.data['files_read'] == [name]
    assert result.data['matches'][0]['line'] == 2
    assert result.data['matches'][0]['text'] == 'needle: first: second'
    assert '\\n' in result.output


@pytest.mark.parametrize('maximum,expected', [(0, 1), (-20, 1), (100000, 200)])
def test_grep_result_count_is_clamped_and_partial(tmp_path, maximum, expected):
    (tmp_path / 'source.txt').write_text('needle\n' * 220)
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle', max_results=maximum))
    assert result.success and len(result.data['matches']) == expected
    assert result.data['coverage']['status'] == 'partial' and 'Partial coverage' in result.output


def test_rg_preserves_gitignore_selection(tmp_path):
    if shutil.which('rg') is None:
        pytest.skip('ripgrep unavailable')
    (tmp_path / '.git').mkdir()
    (tmp_path / '.gitignore').write_text('ignored.txt\n')
    (tmp_path / 'ignored.txt').write_text('needle hidden-by-ignore\n')
    (tmp_path / 'public.txt').write_text('needle public\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and result.data['files_read'] == ['public.txt']
    assert result.data['coverage']['gitignore_respected']


def test_fallback_declares_gitignore_limit_and_keeps_private_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr(search.shutil, 'which', lambda name: None)
    (tmp_path / '.gitignore').write_text('ignored.txt\n')
    (tmp_path / 'ignored.txt').write_text('needle public-source\n')
    (tmp_path / '.env').write_text('needle private-env-value\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and not result.data['coverage']['gitignore_respected']
    assert 'private-env-value' not in result.output


def test_filename_walk_prunes_dependencies_before_descent(tmp_path, monkeypatch):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/a.py').write_text('pass\n')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules/never.py').write_text('pass\n')
    opened = []
    original = search._directory_entries

    @contextmanager
    def observe(root, directory):
        opened.append(directory)
        with original(root, directory) as values:
            yield values

    monkeypatch.setattr(search, '_directory_entries', observe)
    result = asyncio.run(SearchFilesTool(str(tmp_path)).execute('**/*.py'))
    assert result.success and result.data['paths'] == ['src/a.py']
    assert tmp_path / 'node_modules' not in opened


def test_filename_enumeration_and_listing_caps_are_explicit(tmp_path, monkeypatch):
    for index in range(10):
        (tmp_path / f'file{index}.txt').write_text('source')
    monkeypatch.setattr(search, 'MAX_ENTRIES', 5)
    result = asyncio.run(SearchFilesTool(str(tmp_path)).execute('*.txt', max_results=2))
    assert result.success and len(result.data['paths']) <= 2
    assert result.data['coverage']['entries_considered'] <= 5
    assert result.data['coverage']['status'] == 'partial'


def test_snapshot_search_never_opens_replaced_path_in_regex_subprocess(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_text('needle public-old-bytes\n')
    original = search._capture

    async def swap_after_snapshot(argv, root, timeout, data=None, **kwargs):
        if data is not None:
            source.write_text('needle changed-after-snapshot\n')
        return await original(argv, root, timeout, data, **kwargs)

    monkeypatch.setattr(search, '_capture', swap_after_snapshot)
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and 'public-old-bytes' in result.output
    assert 'changed-after-snapshot' not in result.output
    assert result.data['source_hashes']['source.txt'] == hashlib.sha256(b'needle public-old-bytes\n').hexdigest()


def test_aggregate_read_budget_is_not_exceeded(tmp_path, monkeypatch):
    for index in range(3):
        (tmp_path / f'source{index}.txt').write_text('needle\n')
    monkeypatch.setattr(search, 'MAX_TOTAL_BYTES', 10)
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and result.data['coverage']['bytes_read'] <= 10
    assert result.data['coverage']['read_budget_used'] <= 10
    assert result.data['coverage']['status'] == 'partial'


def test_failed_reads_reserve_budget_without_claiming_successful_bytes(tmp_path, monkeypatch):
    (tmp_path / 'source.txt').write_text('needle\n')
    monkeypatch.setattr(search, 'MAX_FILE_BYTES', 5)
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and result.data['coverage']['bytes_read'] == 0
    assert result.data['coverage']['read_bytes_reserved'] == 5
    assert result.data['coverage']['status'] == 'partial'


def test_explicit_public_file_target_has_complete_provenance(tmp_path):
    (tmp_path / 'source.txt').write_text('needle\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle', path='source.txt'))
    assert result.success and result.data['files_read'] == ['source.txt']


@pytest.mark.parametrize('tool,pattern', [(GrepTool, 'needle'), (SearchFilesTool, '*.txt')])
def test_rendered_result_budget_is_partial_and_bounded(tmp_path, monkeypatch, tool, pattern):
    for index in range(4):
        (tmp_path / ('long-file-name-' * 4 + str(index) + '.txt')).write_text('needle\n')
    monkeypatch.setattr(search, 'MAX_RESULT_BYTES', 300)
    result = asyncio.run(tool(str(tmp_path)).execute(pattern, max_results=200))
    assert result.success and result.data['coverage']['result_bytes'] <= 300
    assert result.data['coverage']['status'] == 'partial'


def test_control_characters_inside_a_match_cannot_forge_another_line_number(tmp_path):
    (tmp_path / 'source.txt').write_bytes(b'needle\r2:fake-prefix\nunmatched second line\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success
    assert [row['line'] for row in result.data['matches']] == [1]


def test_non_utf8_source_is_skipped_with_partial_coverage(tmp_path):
    (tmp_path / 'source.txt').write_bytes(b'needle\xff\n')
    result = asyncio.run(GrepTool(str(tmp_path)).execute('needle'))
    assert result.success and result.data['coverage']['status'] == 'partial'
    assert not result.data['files_read'] and result.data['coverage']['skipped']['unsupported_source_encoding'] == 1


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.data = b''
    def write(self, data):
        self.data = data
    async def drain(self):
        await asyncio.sleep(0)
    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, output=None):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdin = FakeWriter()
        self.returncode = None
        self.ended, self.started = asyncio.Event(), asyncio.Event()
        if output:
            self.stdout.feed_data(output)
    async def wait(self):
        self.started.set()
        await self.ended.wait()
        return self.returncode
    def finish(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.ended.set()


@pytest.mark.parametrize('condition', ['overflow', 'timeout', 'cancel'])
def test_bounded_capture_reaps_owned_process_and_closes_stdin(tmp_path, monkeypatch, condition):
    async def go():
        process = FakeProcess(b'1:needle\n' * 100 if condition == 'overflow' else None)
        calls = []
        async def create(*args, **kwargs):
            assert kwargs.get('start_new_session') or kwargs.get('creationflags')
            assert 'RIPGREP_CONFIG_PATH' not in kwargs['env']
            return process
        async def terminate(proc):
            calls.append(proc)
            proc.finish()
        monkeypatch.setattr(search.asyncio, 'create_subprocess_exec', create)
        monkeypatch.setattr(search, 'terminate_tree', terminate)
        monkeypatch.setenv('RIPGREP_CONFIG_PATH', '/untrusted-config')
        task = asyncio.create_task(search._capture(['rg', '--no-config'], tmp_path,
            .01 if condition == 'timeout' else 2, data=b'needle\n', limit=32))
        if condition == 'cancel':
            await process.started.wait()
            task.cancel()
        if condition in {'timeout', 'cancel'}:
            with pytest.raises(asyncio.TimeoutError if condition == 'timeout' else asyncio.CancelledError):
                await task
        else:
            capture = await task
            assert capture.limited and len(capture.stdout) == 32
        assert calls == [process] and process.returncode is not None and process.stdin.closed
    asyncio.run(go())
