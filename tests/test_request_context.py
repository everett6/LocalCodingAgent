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
