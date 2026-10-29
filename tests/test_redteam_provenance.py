"""Repository-only fixtures for evidence binding, freshness and bounded recovery."""
import asyncio
import hashlib
import json
import os
import stat

import pytest

from local_coder.redteam import EngagementScope, EngagementStore
from local_coder.tools.redteam import RedTeamFindingTool, RedTeamImportTool, RedTeamWorkflowTool, RedTeamWorkspaceTool


def setup(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app/a.py').write_text('eval(user_input)\n')
    (tmp_path / 'app/b.py').write_text('print("safe")\n')
    store = EngagementStore(str(tmp_path))
    record = store.create(EngagementScope(name='Scoped fixture', objective='Review source', paths=['app']))
    return store, record['id']


def execute(tool, **arguments):
    return asyncio.run(tool.execute(**arguments))


def test_unrelated_source_and_stale_source_cannot_support_findings(tmp_path):
    store, identity = setup(tmp_path)
    source = execute(RedTeamWorkspaceTool(store, identity), action='read', path='app/a.py')
    finding = RedTeamFindingTool(store, identity)
    result = execute(finding, title='Borrowed source', asset='app/b.py', evidence_ids=[source.data['evidence_id']])
    assert not result.success
    (tmp_path / 'app/a.py').write_text('print("fixed")\n')
    stale = execute(finding, title='Stale source', asset='app/a.py', evidence_ids=[source.data['evidence_id']])
    assert not stale.success
    assert not store.load(identity)['findings']


def test_scan_evidence_binds_only_scanned_current_assets(tmp_path):
    store, identity = setup(tmp_path)
    scan = execute(RedTeamWorkspaceTool(store, identity), action='scan', path='app/a.py')
    assert scan.success
    assert scan.data['source_hashes']['app/a.py'] == hashlib.sha256((tmp_path / 'app/a.py').read_bytes()).hexdigest()
    tool = RedTeamFindingTool(store, identity)
    assert not execute(tool, title='Wrong scan asset', asset='app/b.py', evidence_ids=[scan.data['evidence_id']]).success
    assert execute(tool, title='Current candidate', asset='app/a.py', evidence_ids=[scan.data['evidence_id']]).success
    (tmp_path / 'app/a.py').unlink()
    assert not execute(tool, title='Deleted source', asset='app/a.py', evidence_ids=[scan.data['evidence_id']]).success
    snapshot = store.investigation_snapshot(identity)
    assert snapshot['findings'][0]['source_status'] == 'stale'


def test_snapshot_is_bounded_redacted_and_stable_without_raw_journal(tmp_path):
    store, identity = setup(tmp_path)
    secret = 'sk-' + 'a' * 40
    for _ in range(25):
        execute(RedTeamWorkspaceTool(store, identity), action='read', path='app/a.py')
    store.evidence(identity, 'http_failed', {'reason': secret, 'raw_source': 'do not recover this'})
    snapshot = store.investigation_snapshot(identity)
    assert len(snapshot['evidence']) == 20
    assert snapshot['counts']['evidence'] == 25
    assert snapshot['scope']['paths'] == ['app']
    assert snapshot['evidence'][0]['freshness'] == 'current'
    assert snapshot['evidence'][0]['path'] == 'app/a.py'
    assert snapshot['evidence'][0]['sha256']
    assert 'eval(user_input)' not in json.dumps(snapshot)
    assert secret not in json.dumps(snapshot) and 'do not recover this' not in json.dumps(snapshot)
    workflow = execute(RedTeamWorkflowTool(store, identity))
    assert workflow.success and workflow.data == snapshot
    assert 'events' not in json.loads(workflow.output)
    (tmp_path / 'app/a.py').write_text('changed\n')
    stale = store.investigation_snapshot(identity)
    assert stale['evidence'][0]['freshness'] == 'stale'
    assert stale['gaps']
    assert 'Evidence journal' in store.report(identity)['markdown']


def test_scan_changes_are_marked_partial_and_not_current(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    def changing_scan(scanner, root, files, coverage):
        (tmp_path / 'app/a.py').write_text('changed during scan\n')
        return []
    monkeypatch.setattr('local_coder.tools.redteam.RunSASTTool._run_builtin_sast', changing_scan)
    scan = execute(RedTeamWorkspaceTool(store, identity), action='scan')
    assert scan.success and scan.data['coverage']['status'] == 'partial'
    assert 'app/a.py' in scan.data['coverage']['changed_sources']
    evidence = store.investigation_snapshot(identity)['evidence'][0]
    assert evidence['freshness'] == 'stale'
    assert evidence['content_digest']


def test_legacy_unhashed_sources_remain_unknown(tmp_path):
    store, identity = setup(tmp_path)
    evidence = store.evidence(identity, 'source_read', {'path': 'app/a.py'})
    result = execute(RedTeamFindingTool(store, identity), title='Legacy unverified', asset='app/a.py', evidence_ids=[evidence])
    assert result.success
    assert store.investigation_snapshot(identity)['evidence'][0]['freshness'] == 'unknown'
    assert store.investigation_snapshot(identity)['findings'][0]['source_status'] == 'unknown'


def test_sarif_asset_hashes_support_only_imported_asset(tmp_path):
    store, identity = setup(tmp_path)
    payload = {'version': '2.1.0', 'runs': [{'results': [{'ruleId': 'fixture', 'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'app/a.py'}}}]}]}]}
    (tmp_path / 'app/report.sarif').write_text(json.dumps(payload))
    imported = execute(RedTeamImportTool(store, identity), path='app/report.sarif', format='sarif')
    assert imported.success
    event = store.load(identity)['events'][-1]
    assert event['data']['source_hashes']['app/a.py']
    assert not execute(RedTeamFindingTool(store, identity), title='Borrowed import', asset='app/b.py', evidence_ids=[imported.data['evidence_id']]).success
    assert store.investigation_snapshot(identity)['findings'][0]['source_status'] == 'current'


def test_reads_refuse_symlinks_large_files_and_fifo_without_opening(tmp_path):
    store, identity = setup(tmp_path)
    (tmp_path / 'app/link.py').symlink_to(tmp_path / 'app/a.py')
    (tmp_path / 'app/large.py').write_bytes(b'x' * 64001)
    tool = RedTeamWorkspaceTool(store, identity)
    for path in ['app/link.py', 'app/large.py']:
        assert not execute(tool, action='read', path=path).success
    if os.name != 'nt':
        os.mkfifo(tmp_path / 'app/pipe.py')
        assert not execute(tool, action='read', path='app/pipe.py').success


def test_empty_static_scan_is_honestly_current(tmp_path):
    store, identity = setup(tmp_path)
    (tmp_path / 'app/a.py').unlink(); (tmp_path / 'app/b.py').unlink()
    scan = execute(RedTeamWorkspaceTool(store, identity), action='scan')
    assert scan.success
    snapshot = store.investigation_snapshot(identity)
    assert snapshot['evidence'][0]['freshness'] == 'current'
    assert snapshot['coverage'][0]['status'] == 'complete'


def test_read_rejects_file_replaced_during_snapshot(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    source = tmp_path / 'app/a.py'
    original = os.fstat
    calls = []
    def changed(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            calls.append(fd)
            if len(calls) == 2:
                replacement = tmp_path / 'app/replacement.py'
                replacement.write_text('different source\n')
                replacement.replace(source)
        return info
    monkeypatch.setattr(os, 'fstat', changed)
    result = execute(RedTeamWorkspaceTool(store, identity), action='read', path='app/a.py')
    assert not result.success
    assert not store.load(identity)['events']


def test_scan_digest_stable_across_duplicate_runs_and_source_token_names(tmp_path):
    store, identity = setup(tmp_path)
    (tmp_path / 'app/tokens.py').write_text('def count_tokens(text):\n    return len(text)\n')
    tool = RedTeamWorkspaceTool(store, identity)
    first = execute(tool, action='scan')
    second = execute(tool, action='scan')
    assert first.success and second.success
    assert first.data['source_hashes']['app/tokens.py'] == hashlib.sha256((tmp_path / 'app/tokens.py').read_bytes()).hexdigest()
    snapshot = store.investigation_snapshot(identity)
    assert snapshot['evidence'][0]['id'] != snapshot['evidence'][1]['id']
    assert snapshot['evidence'][0]['content_digest'] == snapshot['evidence'][1]['content_digest']
    assert all(item['freshness'] == 'current' for item in snapshot['evidence'])


def test_snapshot_rechecks_each_source_once_and_legacy_scan_is_unknown(tmp_path, monkeypatch):
    store, identity = setup(tmp_path)
    digest = hashlib.sha256((tmp_path / 'app/a.py').read_bytes()).hexdigest()
    for _ in range(25):
        store.evidence(identity, 'source_read', {'path': 'app/a.py', 'sha256': digest})
    original = store.source_bytes
    calls = []
    def counted(record, path, **kwargs):
        calls.append(path)
        return original(record, path, **kwargs)
    monkeypatch.setattr(store, 'source_bytes', counted)
    assert len(store.investigation_snapshot(identity)['evidence']) == 20
    assert calls == ['app/a.py']
    store.evidence(identity, 'static_scan', {'files': ['app/a.py'], 'findings': [], 'coverage': {'status': 'complete'}})
    assert store.investigation_snapshot(identity)['evidence'][-1]['freshness'] == 'unknown'


def test_snapshot_scope_titles_and_steps_redact_credentials(tmp_path):
    store, identity = setup(tmp_path)
    secret = 'sk-' + 'b' * 40
    execute(RedTeamWorkflowTool(store, identity), steps=[{'title': 'Inspect api_key=' + secret, 'status': 'pending'}])
    snapshot = store.investigation_snapshot(identity)
    assert secret not in json.dumps(snapshot)
    assert len(snapshot['steps']) == 1


def test_credential_named_sources_cannot_be_recovered_as_code(tmp_path):
    store, identity = setup(tmp_path)
    tool = RedTeamWorkspaceTool(store, identity)
    for name in ['.netrc', '.npmrc', 'id_rsa', 'private.PEM', 'credentials.json']:
        (tmp_path / 'app' / name).write_text('private fixture contents')
        assert not execute(tool, action='read', path='app/' + name).success
    assert not store.load(identity)['events']


def test_rerecording_with_fresh_evidence_replaces_stale_finding_links(tmp_path):
    store, identity = setup(tmp_path)
    workspace = RedTeamWorkspaceTool(store, identity)
    finding = RedTeamFindingTool(store, identity)
    first = execute(workspace, action='read', path='app/a.py')
    assert execute(finding, title='Candidate', asset='app/a.py', evidence_ids=[first.data['evidence_id']]).success
    (tmp_path / 'app/a.py').write_text('eval(other_input)\n')
    second = execute(workspace, action='read', path='app/a.py')
    saved = execute(finding, title='Candidate', asset='app/a.py', evidence_ids=[second.data['evidence_id']])
    assert saved.success
    assert first.data['evidence_id'] not in saved.data['evidence_ids']
    assert store.investigation_snapshot(identity)['findings'][0]['source_status'] == 'current'
    assert any(event['kind'] == 'finding_recorded' and first.data['evidence_id'] in event['data']['finding']['evidence_ids'] for event in store.load(identity)['events'])


@pytest.mark.parametrize('fallback', [False, True])
def test_parent_directory_swap_never_reads_outside_fixture(tmp_path, monkeypatch, fallback):
    store, identity = setup(tmp_path)
    if fallback:
        monkeypatch.setattr('local_coder.redteam._SOURCE_DIRFD_SUPPORTED', False)
    outside = tmp_path / 'outside-fixture'
    outside.mkdir()
    sentinel = b'outside fixture sentinel must never be read\n'
    (outside / 'a.py').write_bytes(sentinel)
    original_open, original_fdopen = os.open, os.fdopen
    swapped, observed = [], []
    def swap_parent(path, flags, *args, **kwargs):
        if not swapped and str(path).endswith('a.py'):
            (tmp_path / 'app').rename(tmp_path / 'app-original')
            (tmp_path / 'app').symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original_open(path, flags, *args, **kwargs)
    class Watched:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): self.stream.__enter__(); return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, *args):
            data = self.stream.read(*args)
            observed.append(data)
            return data
    def watch(fd, mode='r', *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        return Watched(stream) if mode == 'rb' else stream
    monkeypatch.setattr(os, 'open', swap_parent)
    monkeypatch.setattr(os, 'fdopen', watch)
    result = execute(RedTeamWorkspaceTool(store, identity), action='read', path='app/a.py')
    assert swapped and not result.success
    assert sentinel not in observed
    assert not store.load(identity)['events']


def test_new_source_invalidates_a_previous_empty_scan(tmp_path):
    store, identity = setup(tmp_path)
    (tmp_path / 'app/a.py').unlink(); (tmp_path / 'app/b.py').unlink()
    execute(RedTeamWorkspaceTool(store, identity), action='scan')
    assert store.investigation_snapshot(identity)['evidence'][0]['freshness'] == 'current'
    (tmp_path / 'app/nested').mkdir()
    (tmp_path / 'app/nested/new.py').write_text('value = 1\n')
    from local_coder.agents.red_team_loop import completion_feedback
    snapshot = store.investigation_snapshot(identity)
    assert snapshot['evidence'][0]['freshness'] == 'stale'
    assert completion_feedback(snapshot) is not None
