"""No processes or model calls: fake lifecycle and local-stream transport contracts."""
import copy
import json
from pathlib import Path
import struct
import threading
from types import SimpleNamespace

import httpx
import pytest

from local_coder.inference import DEFAULTS
from local_coder.inference_tuning import InferenceTuner, TuningCancelled, draft_compatibility, measure_stream
from local_coder.inference_tuning_api import InferenceTuningAPI
from local_coder.workbench import WorkbenchAPI


class Manager:
    def __init__(self, root, running=False):
        self.settings = dict(DEFAULTS)
        model, runtime = root / 'model.gguf', root / 'llama-server'
        model.write_bytes(b'GGUF' + b'x' * 40)
        runtime.write_bytes(b'fake-runtime')
        self.settings.update(model_path=str(model), runtime_path=str(runtime))
        self.running = running
        self.calls = []
        self.fail_context = False
        self.flags = ['--gpu-layers', '--cache-ram']

    def _runtime(self, value):
        return Path(value)

    def status(self):
        return {'state': 'running' if self.running else 'stopped', 'running': self.running,
                'ready': self.running, 'settings': copy.deepcopy(self.settings),
                'capabilities': {'flags': self.flags, 'speculative_decoding': False}}

    def configure(self, settings):
        assert not self.running
        self.settings = copy.deepcopy(settings)
        self.calls.append(('configure', copy.deepcopy(settings)))

    def start(self):
        self.calls.append(('start', None))
        if self.fail_context and self.settings['context_size'] < DEFAULTS['context_size']:
            raise RuntimeError('must not expose ephemeral-key')
        self.running = True
        return self.status()

    def stop(self):
        self.calls.append(('stop', None))
        self.running = False
        return self.status()

    def endpoint_config(self):
        return {'base_url': 'http://127.0.0.1:9999/v1', 'model': 'app-local', 'api_key': 'ephemeral-key'}


def measured(endpoint, settings, cancel):
    if cancel.is_set():
        raise TuningCancelled()
    return {'ttft_ms': 10, 'tokens_per_second': 20 if settings['context_size'] < DEFAULTS['context_size'] else 10,
            'completion_tokens': 16, 'token_count_source': 'runtime_usage'}


@pytest.mark.parametrize('running', [False, True])
def test_measure_restore_then_explicit_apply_and_reject_stale_fingerprint(tmp_path, running):
    manager = Manager(tmp_path, running)
    baseline = copy.deepcopy(manager.settings)
    tuner = InferenceTuner(tmp_path, manager, transport=measured)
    record = tuner.run({'variants': ['context'], 'repeats': 2}, threading.Event())
    assert record['status'] == 'completed' and record['restored']
    assert record['can_apply']
    assert manager.settings == baseline and manager.running == running
    assert len(record['results'][0]['samples']) == 2
    assert record['results'][1]['improved']
    assert record['results'][1]['tokens_per_second'] == 20
    assert tuner.history()[0]['results'][1]['tokens_per_second'] == 20
    assert 'ephemeral-key' not in json.dumps(record)
    assert 'ephemeral-key' not in (tmp_path / '.local-coder/inference/tuning.json').read_text()
    with pytest.raises(ValueError, match='Only a measured improved'):
        tuner.apply(record['id'], 'baseline')
    result = tuner.apply(record['id'], 'context')
    assert result['applied'] and manager.running == running
    assert manager.settings['context_size'] == DEFAULTS['context_size'] // 2
    with pytest.raises(ValueError, match='changed'):
        tuner.apply(record['id'], 'context')


def test_historical_metrics_are_display_only_and_supplied_metrics_rejected(tmp_path):
    manager = Manager(tmp_path)
    tuner = InferenceTuner(tmp_path, manager, transport=measured)
    record = tuner.run({'variants': ['context']}, threading.Event())
    restarted = InferenceTuner(tmp_path, manager, transport=measured)
    assert restarted.history()[0]['can_apply'] is False
    with pytest.raises(ValueError, match='engine session'):
        restarted.apply(record['id'], 'context')
    with pytest.raises(ValueError, match='metrics cannot be imported'):
        tuner.options({'metrics': {'tokens_per_second': 99999}})
    Path(manager.settings['model_path']).write_bytes(b'changed model content')
    with pytest.raises(ValueError, match='changed'):
        tuner.apply(record['id'], 'context')


@pytest.mark.parametrize('mode', ['cancel', 'failure', 'unknown'])
def test_failure_cancel_unknown_metrics_restore_baseline(tmp_path, mode):
    manager = Manager(tmp_path, True)
    original = copy.deepcopy(manager.settings)
    cancel = threading.Event()
    calls = []
    def transport(endpoint, settings, event):
        calls.append(settings['context_size'])
        if mode == 'failure':
            raise RuntimeError('secret private server response')
        if mode == 'cancel':
            event.set()
            raise TuningCancelled()
        return {'ttft_ms': 10, 'tokens_per_second': None, 'completion_tokens': None}
    tuner = InferenceTuner(tmp_path, manager, transport=transport)
    record = tuner.run({'variants': ['context']}, cancel)
    assert record['restored'] and manager.running and manager.settings == original
    assert record['status'] == {'cancel': 'cancelled', 'failure': 'failed', 'unknown': 'completed'}[mode]
    assert not record['can_apply']
    assert 'secret private' not in json.dumps(record)


def test_failed_candidate_does_not_activate_or_prevent_restore(tmp_path):
    manager = Manager(tmp_path, True)
    manager.fail_context = True
    record = InferenceTuner(tmp_path, manager, transport=measured).run({'variants': ['context', 'draft']}, threading.Event())
    assert record['results'][1]['status'] == 'failed'
    assert record['results'][2]['status'] == 'unavailable'
    assert not record['can_apply'] and record['restored'] and manager.running


def _gguf(path, token='same'):
    def text(value):
        raw = value.encode()
        return struct.pack('<Q', len(raw)) + raw
    values = [('general.architecture', 8, text('llama')),
              ('tokenizer.ggml.model', 8, text('bpe')),
              ('tokenizer.ggml.tokens', 9, struct.pack('<IQ', 8, 1) + text(token))]
    path.write_bytes(struct.pack('<4sIQQ', b'GGUF', 3, 1, len(values)) + b''.join(text(key) + struct.pack('<I', kind) + value for key, kind, value in values))


def test_draft_requires_runtime_support_and_matching_complete_tokenizer(tmp_path):
    target, draft = tmp_path / 'target.gguf', tmp_path / 'draft.gguf'
    _gguf(target); _gguf(draft)
    settings = {'model_path': str(target), 'draft_model_path': str(draft)}
    caps = {'flags': ['--model-draft']}
    assert draft_compatibility(settings, caps)[0]
    assert not draft_compatibility(settings, {'flags': []})[0]
    _gguf(draft, 'other')
    assert not draft_compatibility(settings, caps)[0]
    draft.write_bytes(b'GGUF')
    assert not draft_compatibility(settings, caps)[0]


def test_stream_uses_only_loopback_auth_actual_usage_and_bounded_fixed_prompt(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"2 "}}]}\n\ndata: {"choices":[{"delta":{"content":"3"}}]}\n\ndata: {"choices":[],"usage":{"completion_tokens":2}}\n\ndata: [DONE]\n\n')
    client = httpx.Client
    monkeypatch.setattr('local_coder.inference_tuning.httpx.Client', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    endpoint = {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'test-key', 'model': 'local'}
    result = measure_stream(endpoint, {'prompt_cache': True}, threading.Event())
    assert result['completion_tokens'] == 2 and result['tokens_per_second'] > 0
    assert result['throughput_scope'] == 'end_to_end_request_including_prefill'
    assert requests[0].headers['Authorization'] == 'Bearer test-key'
    assert json.loads(requests[0].content)['max_tokens'] == 32
    for url in ['https://example.com/v1', 'http://127.0.0.1.example.com/v1', 'http://127.0.0.1/v1?key=bad']:
        with pytest.raises(ValueError, match='loopback'):
            measure_stream({**endpoint, 'base_url': url}, {'prompt_cache': True}, threading.Event())
    assert len(requests) == 1


def test_api_job_slot_cancel_and_history_storage_guards(tmp_path):
    class API(InferenceTuningAPI):
        def __init__(self):
            self.project_root, self.inference = tmp_path, Manager(tmp_path)
            self.lock = threading.RLock()
            self._job_lock, self._jobs, self._stopping = threading.Lock(), {}, False
            self._init_tuning_api()
    api = API()
    entered = threading.Event()
    def waiting(endpoint, settings, cancel):
        entered.set()
        cancel.wait(1)
        if cancel.is_set():
            raise TuningCancelled()
        return measured(endpoint, settings, cancel)
    api._tuner._measure = waiting
    handled, row = api._tuning_post('/inference/tuning/start', {'variants': ['context']})
    assert handled and entered.wait(1)
    with pytest.raises(ValueError, match='active workspace'):
        api._tuning_post('/inference/tuning/start', {})
    job = api._jobs[row['job_id']]
    api._tuning_post('/inference/tuning/jobs/' + row['job_id'] + '/cancel', {})
    job['thread'].join(2)
    assert not job['thread'].is_alive()
    assert api._tuning_get('/inference/tuning/jobs/' + row['job_id'], {})[1]['status'] == 'cancelled'
    assert not api._jobs and not api._job_lock.locked()
    target = tmp_path / '.local-coder/inference/tuning.json'
    target.unlink(); target.symlink_to(tmp_path / 'outside.json')
    with pytest.raises(ValueError, match='symlink'):
        api._tuning_get('/inference/tuning/history', {})


@pytest.mark.parametrize('options', [{'variants': [[]]}, {'variants': ['context'] * 5}, {'repeats': True}, {'repeats': 10}, {'variants': ['external']}])
def test_options_are_strict_and_bounded(options):
    with pytest.raises(ValueError):
        InferenceTuner.options(options)


def test_engine_shutdown_restores_settings_without_restarting_runtime(tmp_path):
    manager = Manager(tmp_path, True)
    baseline = copy.deepcopy(manager.settings)
    shutting_down = threading.Event()
    def transport(endpoint, settings, cancel):
        shutting_down.set()
        raise TuningCancelled()
    record = InferenceTuner(tmp_path, manager, transport=transport, shutdown=shutting_down.is_set).run({'variants': []}, threading.Event())
    assert record['status'] == 'cancelled' and record['restored']
    assert manager.settings == baseline and not manager.running
    assert record['stopped_for_engine_shutdown']


class ObservedLock:
    def __init__(self, watched_thread):
        self.inner = threading.RLock()
        self.watched_thread = watched_thread
        self.attempted = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == self.watched_thread:
            self.attempted.set()
        self.inner.acquire()
        return self

    def __exit__(self, *args):
        self.inner.release()


def drain_host(manager, gate):
    return SimpleNamespace(
        _stopping=False, _lifecycle_lock=gate, lock=threading.RLock(), _jobs={},
        inference=manager, approvals=SimpleNamespace(cancel_all=lambda: None),
        runtime_discovery=SimpleNamespace(cancel=lambda: None),
    )


def test_start_publication_is_atomic_with_shutdown_snapshot(tmp_path, monkeypatch):
    import local_coder.inference_tuning_api as module
    class API(InferenceTuningAPI):
        def __init__(self):
            self.project_root, self.inference = tmp_path, Manager(tmp_path)
            self.lock = ObservedLock('draining')
            self._job_lock, self._jobs, self._stopping = threading.Lock(), {}, False
            self._lifecycle_lock = threading.RLock()
            self.approvals = SimpleNamespace(cancel_all=lambda: None)
            self.runtime_discovery = SimpleNamespace(cancel=lambda: None)
            self._init_tuning_api()
    api = API()
    start_entered, release_start = threading.Event(), threading.Event()
    real_thread = threading.Thread
    class DelayedStart(real_thread):
        def start(self):
            start_entered.set()
            assert release_start.wait(3)
            super().start()
    # Patch only this module's thread factory, leaving the test's threads real.
    monkeypatch.setattr(module, 'threading', SimpleNamespace(Thread=DelayedStart, Event=threading.Event))
    def run(options, cancel, progress):
        assert cancel.wait(3)
        return {'status': 'cancelled'}
    api._tuner.run = run
    errors, drained = [], []
    def request():
        try:
            api._tuning_post('/inference/tuning/start', {'variants': []})
        except Exception as error:
            errors.append(error)
    def shutdown():
        try:
            drained.append(WorkbenchAPI.drain_jobs(api, timeout=2))
        except Exception as error:
            errors.append(error)
    sender = real_thread(target=request)
    drainer = real_thread(target=shutdown, name='draining')
    sender.start()
    assert start_entered.wait(3)
    drainer.start()
    try:
        assert api.lock.attempted.wait(3)
    finally:
        release_start.set()
    sender.join(3)
    drainer.join(3)
    assert not sender.is_alive() and not drainer.is_alive()
    assert errors == [] and drained == [True]
    assert api._jobs == {} and not api._job_lock.locked()


def test_runtime_start_and_final_shutdown_stop_share_lifecycle_gate(tmp_path):
    manager = Manager(tmp_path, True)
    gate = ObservedLock('draining')
    host = drain_host(manager, gate)
    tuner = InferenceTuner(tmp_path, manager, transport=measured,
                           shutdown=lambda: host._stopping, lifecycle_gate=gate)
    entered, release_start = threading.Event(), threading.Event()
    actual_start = manager.start
    def delayed_start():
        entered.set()
        assert release_start.wait(3)
        return actual_start()
    manager.start = delayed_start
    errors = []
    def restore():
        try:
            tuner._restore(manager.settings, True)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=restore)
    host._jobs['fixture'] = {'thread': worker, 'cancel': threading.Event()}
    worker.start()
    assert entered.wait(3)
    drainer = threading.Thread(target=lambda: WorkbenchAPI.drain_jobs(host, timeout=0), name='draining')
    drainer.start()
    try:
        assert gate.attempted.wait(3)
        # Start owns the gate, so shutdown cannot publish its final stop yet.
        assert not host._stopping
    finally:
        release_start.set()
    worker.join(3)
    drainer.join(3)
    assert not worker.is_alive() and not drainer.is_alive() and not errors
    assert host._stopping and not manager.running
    assert manager.calls[-1][0] == 'stop'


@pytest.mark.parametrize('phase', ['baseline', 'candidate', 'apply'])
def test_shutdown_before_next_start_prevents_all_start_paths(tmp_path, phase):
    manager = Manager(tmp_path, True)
    gate = threading.RLock()
    host = drain_host(manager, gate)
    tuner = InferenceTuner(tmp_path, manager, transport=measured,
                           shutdown=lambda: host._stopping, lifecycle_gate=gate)
    record = tuner.run({'variants': ['context']}, threading.Event()) if phase == 'apply' else None
    manager.calls.clear()
    configured, release_configure = threading.Event(), threading.Event()
    actual_configure = manager.configure
    configure_calls = []
    def configure(settings):
        actual_configure(settings)
        configure_calls.append(settings)
        blocked_call = 2 if phase == 'candidate' else 1
        if len(configure_calls) == blocked_call:
            configured.set()
            assert release_configure.wait(3)
    manager.configure = configure
    cancel = threading.Event()
    results, errors = [], []
    def work():
        try:
            if record:
                results.append(tuner.apply(record['id'], 'context'))
            else:
                results.append(tuner.run({'variants': ['context']}, cancel))
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=work)
    host._jobs['fixture'] = {'thread': worker, 'cancel': cancel}
    worker.start()
    assert configured.wait(3)
    try:
        WorkbenchAPI.drain_jobs(host, timeout=0)
        starts_at_stop = sum(call[0] == 'start' for call in manager.calls)
    finally:
        release_configure.set()
    worker.join(3)
    assert not worker.is_alive() and not errors
    assert not manager.running and host._stopping
    assert sum(call[0] == 'start' for call in manager.calls) == starts_at_stop
