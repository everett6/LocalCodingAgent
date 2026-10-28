"""Capability status is evidence, never an implicit executable permission."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess

from local_coder.research_capabilities import STATUSES, research_capabilities


def test_inventory_retains_all_two_hundred_original_titles_and_eight_groups():
    result = research_capabilities()
    items = result['capabilities']
    assert result['total'] == len(items) == 200
    assert [item['id'] for item in items] == list(range(1, 201))
    assert len({item['title'] for item in items}) == 200
    assert [group['id'] for group in result['groups']] == list(range(1, 9))
    for group in result['groups']:
        members = [item for item in items if item['group_id'] == group['id']]
        assert len(members) == group['count'] == 25
        assert all(item['group'] == group['title'] for item in members)
    # Independent digest of IDs/titles extracted from the user attachment.
    titles = [(item['id'], item['title']) for item in items]
    digest = hashlib.sha256(json.dumps(titles, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    assert digest == '05f5cc7cc2e82c28ec69b594bf642d20ea938b613bf009dab1f10a14d1dea9a8'


def test_status_totals_are_honest_and_implemented_claims_have_source_and_test_evidence():
    result = research_capabilities()
    root = Path(__file__).resolve().parents[1]
    assert set(result['counts']) == set(STATUSES)
    assert sum(result['counts'].values()) == 200
    assert 0 < result['counts']['implemented'] < 200
    assert result['counts']['planned'] > 0
    for item in result['capabilities']:
        assert item['status'] in STATUSES
        assert item['summary'] and item['limitations']
        assert item['requested_behavior']
        if item['status'] == 'implemented':
            assert {evidence['kind'] for evidence in item['evidence']} == {'source', 'test'}
        if item['status'] == 'planned':
            assert item['evidence'] == []
            assert 'not an available tool' in item['limitations'][0]
        for evidence in item['evidence']:
            path = Path(evidence['path'])
            assert not path.is_absolute() and '..' not in path.parts
            assert (root / path).is_file(), evidence
            assert evidence['kind'] in {'source', 'test'} and evidence['note']


def test_dangerous_requests_are_labeled_synthetic_without_attack_or_bypass_features():
    items = {item['id']: item for item in research_capabilities()['capabilities']}
    assert items[71]['status'] == items[96]['status'] == 'synthetic_only'
    assert 'denial-of-service' in ' '.join(items[71]['limitations'])
    assert 'No process-memory secret harvester' in ' '.join(items[96]['limitations'])
    assert items[130]['status'] == 'partial'
    assert 'No jailbreak' in ' '.join(items[130]['limitations'])
    assert items[101]['status'] == 'partial'
    assert 'Not every runtime tool is sandboxed' in ' '.join(items[101]['limitations'])
    assert items[180]['status'] == 'partial'
    assert 'Hashes do not provide independent identity signatures' in ' '.join(items[180]['limitations'])


def test_inventory_is_deterministic_independent_and_never_contacts_or_executes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Reading the capability inventory must not execute or contact anything')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    first = research_capabilities()
    unchanged = json.dumps(first, sort_keys=True)
    first['capabilities'][0]['title'] = 'tampered'
    first['capabilities'][0]['evidence'].clear()
    first['counts']['implemented'] = 200
    second = research_capabilities()
    assert json.dumps(second, sort_keys=True) == unchanged
