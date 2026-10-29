"""Result limits leave scanner coverage explicitly partial."""
import asyncio

from local_coder.tools.security import RunSASTTool


def test_sast_observation_byte_limit_is_reported_without_claiming_complete_coverage(tmp_path, monkeypatch):
    (tmp_path / 'source.py').write_text('def endpoint(value):\n    return eval(value)\n')
    monkeypatch.setattr('local_coder.tools.security.MAX_SAST_RESULT_BYTES', 1)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool='builtin'))
    coverage = result.data['coverage']
    assert coverage['status'] == 'partial'
    assert coverage['result_limit_reached'] is True
    assert coverage.get('result_bytes', 0) <= 1
    assert 'narrow' in ' '.join(coverage['skipped']).lower()


def test_sast_finding_limit_applies_across_files_and_to_javascript(tmp_path, monkeypatch):
    (tmp_path / 'first.js').write_text('eval(value);\n' * 4)
    (tmp_path / 'second.js').write_text('eval(value);\n' * 4)
    monkeypatch.setattr('local_coder.tools.security.MAX_SAST_FINDINGS', 2)
    result = asyncio.run(RunSASTTool(str(tmp_path)).execute(tool='builtin'))
    assert len(result.data['findings']) <= 2
    assert result.data['coverage']['status'] == 'partial'
    assert result.data['coverage']['result_limit_reached'] is True
    assert all(item['verification'] == 'unverified' for item in result.data['findings'])
