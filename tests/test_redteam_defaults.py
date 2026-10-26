import json
import os
from pathlib import Path

import pytest

from local_coder.redteam import EngagementStore
from local_coder.redteam_defaults import RedTeamDefaults, normalize_origin


def test_first_load_writes_private_local_only_env_without_credentials(tmp_path):
    (tmp_path / 'src').mkdir()
    status = RedTeamDefaults(tmp_path).status()
    assert status['settings']['paths'] == ['src']
    assert status['settings']['origins'] == []
    assert status['settings']['active_http'] is False
    assert 'operator requested' in status['settings']['authorization'].lower()
    path = Path(status['env_path'])
    assert path == tmp_path / '.local-coder/redteam.defaults.env'
    if os.name == 'nt':
        assert not path.stat().st_mode & 0o222
    else:
        assert path.stat().st_mode & 0o777 == 0o400
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert 'API_KEY' not in path.read_text()
    assert RedTeamDefaults(tmp_path).status()['settings'] == status['settings']


def test_root_path_is_the_fallback_for_projects_without_src(tmp_path):
    assert RedTeamDefaults(tmp_path).status()['settings']['paths'] == ['.']


@pytest.mark.parametrize('value,expected', [
    ('example.com', 'https://example.com:443'),
    ('example.com:8443', 'https://example.com:8443'),
    ('localhost:3000', 'http://localhost:3000'),
    ('127.0.0.1:8090', 'http://127.0.0.1:8090'),
    ('::1', 'http://[::1]:80'),
    ('[::1]:3000', 'http://[::1]:3000'),
    ('https://localhost:3000/', 'https://localhost:3000'),
])
def test_origin_normalization_requires_no_dns_or_network(value, expected, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: pytest.fail('Defaults must not contact targets'))
    assert normalize_origin(value) == expected


@pytest.mark.parametrize('value', ['https://name:password@example.com', 'example.com/../private', 'example.com/%2e%2e/private', 'file:///etc/passwd', 'https://example.com/?token=secret', 'example.com\\evil', 'https://*.example.com', ''])
def test_ambiguous_or_credential_urls_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_origin(value)


def test_saved_scope_uses_authoritative_validation_and_atomic_roundtrip(tmp_path):
    defaults = RedTeamDefaults(tmp_path)
    original = defaults.status()
    result = defaults.update({'settings': original['settings'] | {'name': 'My review', 'origins': ['example.com'], 'active_http': False}})
    assert result['settings']['origins'] == ['https://example.com:443']
    assert defaults.status()['settings']['name'] == 'My review'
    assert not Path(result['env_path']).stat().st_mode & 0o222
    for values in [
        {'settings': {'paths': ['../outside']}},
        {'settings': {'max_requests': 51}},
        {'settings': {'active_http': True, 'origins': [], 'authorization': ''}},
        {'OPENAI_API_KEY': 'must-not-persist'},
        {'settings': {'authorization': '', 'active_http': True, 'origins': ['example.com']}},
    ]:
        with pytest.raises(ValueError):
            defaults.update(values)
    assert 'must-not-persist' not in Path(result['env_path']).read_text()
    assert defaults.status()['settings']['name'] == 'My review'


def test_env_import_never_sources_shell_or_expands_variables(tmp_path):
    defaults = RedTeamDefaults(tmp_path)
    defaults.status()
    literal = 'literal $(touch never-created) ${HOME}'
    result = defaults.update({'env_text': 'REDTEAM_NAME=' + json.dumps(literal) + '\nREDTEAM_PATHS=["."]\nREDTEAM_ACTIVE_HTTP=false\n'})
    assert result['settings']['name'] == literal
    assert not (tmp_path / 'never-created').exists()
    for text in ['export REDTEAM_ACTIVE_HTTP=true', 'REDTEAM_NAME=$(touch never-created)', 'OPENAI_API_KEY="secret"', 'REDTEAM_NAME="one"\nREDTEAM_NAME="two"']:
        with pytest.raises(ValueError):
            defaults.update({'env_text': text})
    assert not (tmp_path / 'never-created').exists()


def test_ensure_engagement_reuses_only_exact_local_authorized_scope(tmp_path):
    (tmp_path / 'src').mkdir()
    store = EngagementStore(str(tmp_path))
    defaults = RedTeamDefaults(tmp_path)
    first = defaults.ensure_engagement(store)
    assert first == defaults.ensure_engagement(store)
    record = store.load(first)
    assert record['scope']['active_http'] is False and record['scope']['origins'] == []
    assert record['scope']['authorization']
    defaults.update({'settings': {'objective': 'Review a different local boundary'}})
    second = defaults.ensure_engagement(store)
    assert first != second
    assert defaults.status()['last_engagement_id'] == second
    defaults.update({'settings': {'origins': ['localhost:8090'], 'authorization': 'Explicit synthetic app scope', 'active_http': True}, 'preset': 'api-review'})
    third = defaults.ensure_engagement(store)
    assert store.load(third)['scope']['origins'] == []
    assert store.load(third)['scope']['active_http'] is False


def test_api_preset_requires_explicit_origins_and_does_not_enable_http(tmp_path):
    defaults = RedTeamDefaults(tmp_path)
    with pytest.raises(ValueError):
        defaults.update({'preset': 'api-review'})
    result = defaults.update({'preset': 'api-review', 'settings': {'origins': ['app.example.com']}})
    assert result['settings']['active_http'] is False
    assert result['settings']['origins'] == ['https://app.example.com:443']
    assert result['preview']['valid'] is True


def test_defaults_reject_symlinks_and_foreign_or_public_files(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '.local-coder').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        RedTeamDefaults(tmp_path).status()
    (tmp_path / '.local-coder').unlink()
    (tmp_path / '.local-coder').mkdir()
    path = tmp_path / '.local-coder/redteam.defaults.env'
    path.symlink_to(outside / 'file')
    with pytest.raises(ValueError):
        RedTeamDefaults(tmp_path).status()
    path.unlink()
    path.write_text('REDTEAM_PATHS=["."]\n')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        RedTeamDefaults(tmp_path).status()


def test_missing_previous_engagement_does_not_block_local_review(tmp_path):
    defaults = RedTeamDefaults(tmp_path)
    defaults.update({'last_engagement_id': 'eng-' + 'a' * 12})
    identity = defaults.ensure_engagement(EngagementStore(str(tmp_path)))
    assert identity != 'eng-' + 'a' * 12


def test_local_review_statement_cannot_authorize_http_by_accident(tmp_path):
    defaults = RedTeamDefaults(tmp_path)
    with pytest.raises(ValueError, match='authorization'):
        defaults.update({'preset': 'api-review', 'settings': {'origins': ['example.com'], 'active_http': True}})


def test_windows_defaults_without_posix_apis_replaces_readonly_file(tmp_path, monkeypatch):
    import local_coder.redteam_defaults as module

    monkeypatch.setattr(module, '_WINDOWS', True)
    for attribute in ('getuid', 'fchmod', 'O_NOFOLLOW'):
        monkeypatch.delattr(module.os, attribute, raising=False)
    replace = os.replace
    attempts = []

    def windows_replace(source, destination):
        if Path(destination).exists():
            assert Path(destination).stat().st_mode & 0o200
        attempts.append(str(destination))
        return replace(source, destination)

    monkeypatch.setattr(module.os, 'replace', windows_replace)
    defaults = RedTeamDefaults(tmp_path)
    original = defaults.status()
    updated = defaults.update({'settings': {'name': 'Windows saved review'}})
    assert updated['settings']['name'] == 'Windows saved review'
    assert len(attempts) == 2
    assert not Path(original['env_path']).stat().st_mode & 0o222
    assert RedTeamDefaults(tmp_path).status()['settings']['name'] == 'Windows saved review'
