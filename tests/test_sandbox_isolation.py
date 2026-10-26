"""Sandbox admission refuses partially isolated or unbounded helper runtimes."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from local_coder.core.sandbox import SandboxRunner, SandboxUnavailable


def test_bubblewrap_requires_prlimit_before_probing(monkeypatch):
    import local_coder.core.sandbox as module
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/bwrap' if name == 'bwrap' else None)
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0))
    status = SandboxRunner('bubblewrap').probe()
    assert status['ready'] is False
    assert 'prlimit' in status['reason']
    assert calls == []


def test_bubblewrap_requires_a_loadable_seccomp_filter(monkeypatch):
    runner = SandboxRunner('bubblewrap')
    def unavailable():
        raise SandboxUnavailable('libseccomp unavailable')
    monkeypatch.setattr(runner, '_seccomp_filter', unavailable, raising=False)
    monkeypatch.setattr('local_coder.core.sandbox.shutil.which', lambda name: '/usr/bin/' + name)
    status = runner.probe()
    assert status['ready'] is False
    assert 'seccomp' in status['reason']


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'memfd_create'), reason='Linux seccomp requires an anonymous file descriptor')
def test_seccomp_filter_is_bounded_and_denies_process_and_namespace_creation():
    runner = SandboxRunner('bubblewrap')
    try:
        descriptor = runner._seccomp_filter()
    except SandboxUnavailable as exc:
        pytest.skip(str(exc))
    try:
        raw = os.read(descriptor, 65537)
        assert 0 < len(raw) <= 65536 and len(raw) % 8 == 0
        assert {'clone', 'clone3', 'fork', 'vfork', 'unshare', 'setns', 'ptrace', 'mount'} <= set(runner.BLOCKED_SYSCALLS)
    finally:
        os.close(descriptor)


def test_bubblewrap_probe_passes_readonly_policy_and_seccomp_fd(monkeypatch):
    import local_coder.core.sandbox as module
    runner = SandboxRunner('bubblewrap')
    descriptor = os.open('/dev/null', os.O_RDONLY)
    calls = []
    monkeypatch.setattr(runner, '_seccomp_filter', lambda: descriptor, raising=False)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'', b'')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    status = runner.probe()
    assert status['ready'] is True
    args, kwargs = calls[0]
    assert args[args.index('--seccomp') + 1] == str(descriptor)
    assert kwargs['pass_fds'] == (descriptor,)
    assert '--as=536870912' in args and '--nproc=1' in args
    assert '--fsize=1048576' in args and '--nofile=64' in args
    assert args[args.index('--remount-ro') + 1] == '/'
    assert status['process_limit'] == 1
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_unavailable_backend_never_executes_helper(monkeypatch):
    runner = SandboxRunner('bubblewrap')
    runner._status = {'ready': False, 'reason': 'No safe runtime'}
    def forbidden(*_args, **_kwargs):
        pytest.fail('A helper cannot execute after failed sandbox admission')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(SandboxUnavailable, match='No safe runtime'):
        asyncio.run(runner.run('def run(value): return value', {'probe': True}))


def test_real_benign_helper_when_isolated_backend_is_available(monkeypatch):
    monkeypatch.setenv('LOCAL_CODER_SANDBOX_TEST_SECRET', 'must-not-cross-boundary')
    runner = SandboxRunner('auto')
    status = runner.probe()
    if not status['ready']:
        pytest.skip(status['reason'])
    result = asyncio.run(runner.run(
        'import os\ndef run(value):\n    return {"answer": value["x"] + 1, "secret": os.getenv("LOCAL_CODER_SANDBOX_TEST_SECRET")}',
        {'x': 6},
    ))
    assert result['success'], result['stderr']
    assert '"answer": 7' in result['output'] and '"secret": null' in result['output']


def test_real_bubblewrap_has_no_writable_filesystem_or_network_sockets():
    runner = SandboxRunner('bubblewrap')
    status = runner.probe()
    if not status['ready']:
        pytest.skip(status['reason'])
    source = '''import socket
def run(value):
    checks = {}
    try:
        with open('/tmp/lab-write', 'w') as output:
            output.write('temporary synthetic fixture')
        checks['filesystem_write_denied'] = False
    except OSError:
        checks['filesystem_write_denied'] = True
    try:
        connection = socket.socket()
        connection.close()
        checks['socket_creation_denied'] = False
    except PermissionError:
        checks['socket_creation_denied'] = True
    return checks
'''
    result = asyncio.run(runner.run(source, {}))
    assert result['success'], result['stderr']
    import json
    checks = json.loads(result['output'])
    assert checks == {'filesystem_write_denied': True, 'socket_creation_denied': True}


def test_packaged_engine_uses_system_python_for_helper(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', '/tmp/not-a-python-local-coder-engine')
    interpreter, runtime = SandboxRunner._python_runtime()
    assert interpreter != sys.executable
    assert Path(interpreter).is_file()
    assert str(runtime) == '/usr'
