"""Bounded generated helpers in OS isolation. Never falls back to host execution."""
from __future__ import annotations
import asyncio
import ctypes
import ctypes.util
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from local_coder.processes import terminate_tree, process_options


class SandboxUnavailable(RuntimeError):
    pass


class SandboxRunner:
    # No helper threads or child processes: per-process rlimits therefore also
    # bound aggregate helper CPU/address-space use. Deny namespace/kernel APIs
    # and sockets as defense in depth beyond the private PID/network namespaces.
    BLOCKED_SYSCALLS = (
        'clone', 'clone3', 'fork', 'vfork', 'unshare', 'setns', 'ptrace',
        'mount', 'umount2', 'pivot_root', 'chroot', 'move_mount', 'open_tree',
        'fsopen', 'fsconfig', 'fsmount', 'fspick', 'mount_setattr',
        'open_by_handle_at', 'name_to_handle_at', 'bpf', 'perf_event_open',
        'keyctl', 'add_key', 'request_key', 'userfaultfd',
        'io_uring_setup', 'io_uring_enter', 'io_uring_register',
        'kexec_load', 'kexec_file_load', 'init_module', 'finit_module',
        'delete_module', 'reboot', 'swapon', 'swapoff',
        'process_vm_readv', 'process_vm_writev', 'pidfd_getfd', 'process_madvise',
        'socket', 'socketpair', 'connect',
    )

    def __init__(self,backend='auto',image='python:3.12-slim'):
        if backend not in {'auto','bubblewrap','docker'}:raise ValueError('Unknown sandbox backend')
        self.backend=backend;self.image=image;self._status=None
    def status(self):
        return self._status or {'backend':self.backend,'ready':False,'reason':'Runtime isolation has not been probed','network':'disabled','host_filesystem':'not mounted'}
    @staticmethod
    def _python_runtime():
        if getattr(sys, 'frozen', False):
            # A packaged engine is an application executable, not a Python CLI.
            for candidate in ('/usr/bin/python3', '/usr/local/bin/python3', '/bin/python3'):
                path = Path(candidate)
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path.resolve()), Path('/usr')
            raise SandboxUnavailable('Bubblewrap helpers require an installed system Python; use an existing local Docker Python image otherwise')
        return str(Path(sys.executable).resolve()), Path(sys.prefix).resolve()
    @classmethod
    def _seccomp_filter(cls):
        """Export native libseccomp BPF to an anonymous, parent-owned FD."""
        if sys.platform != 'linux' or not hasattr(os, 'memfd_create'):
            raise SandboxUnavailable('Linux libseccomp and anonymous BPF descriptors are required')
        library = ctypes.util.find_library('seccomp')
        if not library:
            raise SandboxUnavailable('libseccomp is required for child-process confinement')
        descriptor = None
        context = None
        try:
            seccomp = ctypes.CDLL(library, use_errno=True)
            seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
            seccomp.seccomp_init.restype = ctypes.c_void_p
            seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
            seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
            seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
            seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
            seccomp.seccomp_rule_add.restype = ctypes.c_int
            seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
            seccomp.seccomp_export_bpf.restype = ctypes.c_int
            context = seccomp.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
            if not context:
                raise SandboxUnavailable('libseccomp could not create a filter')
            for name in cls.BLOCKED_SYSCALLS:
                number = seccomp.seccomp_syscall_resolve_name(name.encode('ascii'))
                if number < 0:
                    # clone/clone3 are mandatory on supported Linux platforms.
                    # Other absent calls (e.g. fork on arm64) have no native ABI.
                    if name in {'clone', 'clone3', 'unshare', 'setns', 'ptrace', 'mount'}:
                        raise SandboxUnavailable('libseccomp cannot resolve required confinement syscalls')
                    continue
                if seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                    raise SandboxUnavailable('libseccomp could not deny a confinement syscall')
            descriptor = os.memfd_create('local-coder-seccomp', flags=os.MFD_CLOEXEC)
            if seccomp.seccomp_export_bpf(context, descriptor) != 0:
                raise SandboxUnavailable('libseccomp could not export its filter')
            size = os.fstat(descriptor).st_size
            if not 0 < size <= 65536 or size % 8:
                raise SandboxUnavailable('libseccomp exported an invalid filter')
            os.lseek(descriptor, 0, os.SEEK_SET)
            result, descriptor = descriptor, None
            return result
        except (OSError, AttributeError) as exc:
            raise SandboxUnavailable('libseccomp filter creation is unavailable') from exc
        finally:
            if context:
                seccomp.seccomp_release(context)
            if descriptor is not None:
                os.close(descriptor)

    def _bwrap_base(self, descriptor):
        binary=shutil.which('bwrap')
        if not binary:return None
        args=[binary,'--unshare-all','--die-with-parent','--new-session','--cap-drop','ALL','--clearenv']
        for directory in ['/usr','/bin','/lib','/lib64']:
            if Path(directory).exists():args+=['--ro-bind',directory,directory]
        return args+['--proc','/proc','--dev','/dev','--tmpfs','/tmp','--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp',
                     '--seccomp',str(descriptor)]

    @staticmethod
    def _seal_bwrap(args):
        # Bind destinations must exist before the empty root is made read-only.
        return args+['--remount-ro','/','--remount-ro','/tmp','--remount-ro','/proc','--remount-ro','/dev']

    @staticmethod
    def _limits(binary, timeout):
        return [binary,'--as=536870912','--cpu='+str(max(1,int(timeout)+1)),
                '--fsize=1048576','--nofile=64','--nproc=1','--core=0','--']

    def probe(self):
        if self._status is not None:return self._status
        reasons=[]
        if self.backend in {'auto','bubblewrap'}:
            if not shutil.which('bwrap'):
                reasons.append('Bubblewrap is unavailable')
            elif not shutil.which('prlimit'):
                reasons.append('Bubblewrap requires prlimit for mandatory resource limits')
            else:
                descriptor=None
                try:
                    descriptor=self._seccomp_filter()
                    args=self._bwrap_base(descriptor)
                    result=subprocess.run(self._seal_bwrap(args)+['--']+self._limits(shutil.which('prlimit'),3)+['/usr/bin/true'],
                                          capture_output=True,timeout=3,pass_fds=(descriptor,),env={'PATH':'/usr/bin:/bin','HOME':'/tmp'})
                    if result.returncode==0:
                        self._status={'backend':'bubblewrap','ready':True,'network':'disabled','host_filesystem':'read-only runtime libraries only',
                                      'process_limit':1,'memory_limit_bytes':536870912,'writable_filesystem':False};return self._status
                    reasons.append('Bubblewrap confinement probe was rejected by the operating system')
                except (OSError,subprocess.TimeoutExpired,SandboxUnavailable) as exc:
                    reasons.append(str(exc) if isinstance(exc,SandboxUnavailable) else 'Bubblewrap confinement probe failed')
                finally:
                    if descriptor is not None:os.close(descriptor)
        if self.backend in {'auto','docker'} and shutil.which('docker'):
            try:
                result=subprocess.run(['docker','image','inspect','--format','{{.Id}}',self.image],capture_output=True,text=True,timeout=3)
                image=result.stdout.strip()
                if result.returncode==0 and image.startswith('sha256:') and len(image)==71:
                    self._status={'backend':'docker','ready':True,'image':image,'network':'disabled','host_filesystem':'not mounted',
                                  'process_limit':32,'memory_limit_bytes':536870912,'writable_filesystem':'16 MiB /tmp only'};return self._status
            except (OSError,subprocess.TimeoutExpired):pass
        self._status={'backend':self.backend,'ready':False,'reason':'; '.join(reasons+['No usable isolated runtime; enable confinement support or provide an existing local Docker Python image. No image was downloaded.']),'network':'disabled','host_filesystem':'not mounted'}
        return self._status
    async def run(self,source,input,timeout=10,name='generated'):
        if not isinstance(source,str) or not 1<=len(source.encode())<=65536:raise ValueError('Helper source must be 1-65536 bytes')
        payload=json.dumps(input,ensure_ascii=False).encode()
        if len(payload)>65536:raise ValueError('Helper input exceeds 64 KiB')
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not 0<timeout<=30:raise ValueError('Sandbox timeout must be 1-30 seconds')
        status=await asyncio.to_thread(self.probe)
        if not status['ready']:raise SandboxUnavailable(status['reason'])
        started=time.monotonic()
        with tempfile.TemporaryDirectory(prefix='local-coder-sandbox-') as temporary:
            folder=Path(temporary);(folder/'main.py').write_text(source+'\nimport json as _lc_json, sys as _lc_sys\n_lc_result = run(_lc_json.load(_lc_sys.stdin))\nprint(_lc_json.dumps(_lc_result))\n',encoding='utf-8');os.chmod(folder/'main.py',0o444)
            container=None
            descriptor=None
            if status['backend']=='bubblewrap':
                python,runtime=self._python_runtime()
                limit=shutil.which('prlimit')
                if not limit:raise SandboxUnavailable('Mandatory prlimit resource enforcement became unavailable')
                descriptor=self._seccomp_filter()
                args=self._bwrap_base(descriptor)
                if args is None:
                    os.close(descriptor)
                    raise SandboxUnavailable('Bubblewrap confinement became unavailable')
                if not runtime.is_relative_to(Path('/usr')):args+=['--ro-bind',str(runtime),str(runtime)]
                # Only the selected runtime, helper and JSON input cross the boundary.
                args+=['--ro-bind',temporary,'/work','--chdir','/work']
                args=self._seal_bwrap(args)+['--']
                args += self._limits(limit,timeout)
                args += [python,'-I','-B','/work/main.py']
            else:
                container='local-coder-helper-'+os.urandom(8).hex()
                args=['docker','run','--pull=never','--rm','--name',container,'--network','none','--read-only','--cap-drop','ALL',
                      '--security-opt','no-new-privileges','--pids-limit','32','--memory','512m','--cpus','1','--user','65534:65534',
                      '--tmpfs','/tmp:rw,noexec,nosuid,size=16m','--mount',f'type=bind,src={temporary},dst=/work,readonly',
                      '--entrypoint','/usr/bin/env','-i',status['image'],'-i','PATH=/usr/local/bin:/usr/bin:/bin','HOME=/tmp','python','-I','-B','/work/main.py']
                os.chmod(temporary,0o755)
            try:
                options={'pass_fds':(descriptor,)} if descriptor is not None else {}
                process=await asyncio.create_subprocess_exec(*args,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                                                            env={'PATH':'/usr/bin:/bin','HOME':str(Path.home())},**process_options(),**options)
            finally:
                if descriptor is not None:os.close(descriptor)
            # Bounded streaming drains prevent a helper from exhausting engine RAM.
            async def read(stream):
                chunks=[];size=0
                while chunk:=await stream.read(8192):
                    size+=len(chunk)
                    if size>131072:raise ValueError('Sandbox output exceeds 128 KiB')
                    chunks.append(chunk)
                return b''.join(chunks)
            async def exchange():
                process.stdin.write(payload);await process.stdin.drain();process.stdin.close()
                output,error=await asyncio.gather(read(process.stdout),read(process.stderr));await process.wait();return output,error
            try:
                output,error=await asyncio.wait_for(exchange(),timeout=timeout)
            except BaseException:
                await terminate_tree(process)
                raise
            finally:
                if container:
                    cleanup=await asyncio.create_subprocess_exec('docker','rm','-f',container,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    try:await asyncio.wait_for(cleanup.wait(),timeout=3)
                    except asyncio.TimeoutError:cleanup.kill();await cleanup.wait()
            return {'success':process.returncode==0,'exit_code':process.returncode,'output':output.decode(errors='replace'),'stdout':output.decode(errors='replace'),
                    'stderr':error.decode(errors='replace'),'isolation':status['backend'],'network':'disabled','duration_ms':int((time.monotonic()-started)*1000)}
