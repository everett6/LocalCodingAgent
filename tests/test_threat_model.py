"""Deterministic inventory checks with local safe and vulnerable examples."""
import json

import pytest

from local_coder.security.threat_model import (
    build_threat_model, evaluate_findings, render_threat_briefing,
)


def test_inventory_links_guards_and_review_surfaces(tmp_path):
    (tmp_path / 'handler.py').write_text(
        'from helpers import run\n'
        '@app.route("/example")\n@login_required\n'
        'def handler():\n    return run(request.args["value"])\n'
    )
    (tmp_path / 'helpers.py').write_text(
        'import subprocess as sp\n'
        'def run(value):\n    return sp.run(value, shell=True)\n'
        'def safe():\n    return sp.run(["echo", "constant"], shell=False)\n'
    )
    model = build_threat_model(tmp_path)
    assert model == build_threat_model(tmp_path)
    assert model['entry_points'][0]['kind'] == 'route'
    assert model['entry_points'][0]['auth_guard_observations'] == ['login_required']
    assert any(x['symbol'] == 'request.args' for x in model['boundaries'])
    assert any(x['target'] == 'helpers.run' for x in model['links'])
    # Both syntactically safe and unsafe calls are review surfaces, not findings.
    assert len(model['sinks']) == 2
    assert all(x['symbol'] == 'subprocess.run' for x in model['sinks'])
    assert 'shell=True' not in json.dumps(model)
    assert 'confirmed vulnerabilities' in render_threat_briefing(model)
    assert len(render_threat_briefing(model, 40)) == 40


def test_excludes_symlinks_dependencies_and_secret_directories(tmp_path):
    (tmp_path / 'good.py').write_text('def okay():\n    return 1\n')
    for name in ('secrets', '.aws', 'node_modules', '.venv'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'hidden.py').write_text('eval("secret literal")\n')
    (tmp_path / 'alias.py').symlink_to(tmp_path / 'good.py')
    model = build_threat_model(tmp_path)
    assert model['scope']['files'] == ['good.py']
    assert not model['sinks']
    assert build_threat_model(tmp_path, ['alias.py'])['scope']['files'] == []
    with pytest.raises(ValueError, match='escapes workspace'):
        build_threat_model(tmp_path, ['../outside.py'])


def test_limits_and_parse_gaps(tmp_path, monkeypatch):
    import local_coder.security.threat_model as module
    (tmp_path / 'a.py').write_text('this is invalid python!')
    (tmp_path / 'b.py').write_text('eval("do not echo this")\n' * 10)
    monkeypatch.setattr(module, 'MAX_OBSERVATIONS', 2)
    model = build_threat_model(tmp_path)
    assert any('unparseable' in x for x in model['coverage_gaps'])
    assert any('Observation limit' in x for x in model['coverage_gaps'])
    assert sum(len(model[x]) for x in ('entry_points', 'boundaries', 'sinks', 'links')) == 2
    assert 'do not echo this' not in json.dumps(model)


def test_labeled_metrics_do_not_claim_accuracy():
    metrics = evaluate_findings(['vulnerable-eval', 'safe-constant', 'vulnerable-eval'],
                                ['vulnerable-eval', 'missed-case'])
    assert metrics == {'true_positives': ['vulnerable-eval'],
                       'false_positives': ['safe-constant'],
                       'false_negatives': ['missed-case'], 'precision': 0.5, 'recall': 0.5}
    assert evaluate_findings([], [])['precision'] is None
    assert evaluate_findings([], [])['recall'] is None
