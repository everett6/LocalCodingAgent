import pytest
from local_coder.request_context import prepare_request
from local_coder.types import AgentMode


def test_commands_and_file_mentions_preserve_line_evidence(tmp_path):
    (tmp_path / 'handler.py').write_text('def handle(value):\n    return value\n')
    request, mode, action = prepare_request('/hunt check @handler.py', AgentMode.CODING, 'plan', str(tmp_path))
    assert mode == AgentMode.BUG_HUNTING and action == 'run'
    assert '1: def handle(value):' in request
    assert prepare_request('/review', mode, 'run', str(tmp_path))[2] == 'review'


def test_prompt_mentions_reject_escapes_and_credentials(tmp_path):
    (tmp_path / '.env').write_text('secret')
    for mention in ['@../outside.py', '@.env']:
        with pytest.raises(ValueError):
            prepare_request(mention, AgentMode.CODING, 'run', str(tmp_path))


@pytest.mark.parametrize('relative', ['.aws/config', '.ssh/config', '.npmrc', '.kube/config', 'client.KEY', 'client.p12', 'client.PFX', 'client.keystore'])
def test_prompt_mentions_reject_private_local_sources(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('synthetic-private-fixture')
    with pytest.raises(ValueError, match='Credential'):
        prepare_request('@' + relative, AgentMode.CODING, 'run', str(tmp_path))


def test_prompt_mention_budget_includes_rendered_line_numbers(tmp_path):
    (tmp_path / 'many-lines.txt').write_text('\n' * 20_000)
    with pytest.raises(ValueError, match='context budget'):
        prepare_request('@many-lines.txt', AgentMode.CODING, 'run', str(tmp_path))


def test_prompt_mentions_use_snapshot_bytes_and_reject_symlink_aliases(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('value = 1\n')
    try:
        (tmp_path / 'alias.py').symlink_to(source)
    except OSError:
        pytest.skip('symlinks unavailable')
    with pytest.raises((ValueError, OSError)):
        prepare_request('@alias.py', AgentMode.CODING, 'run', str(tmp_path))
