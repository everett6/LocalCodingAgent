from local_coder.remote import RemoteControlServer


class Runtime:
    def __init__(self):self.ready=False;self.closed=False
    def status(self):return {'state':'running' if self.ready else 'stopped','ready':self.ready,'settings':{'context_size':8192}}
    def configure(self,body):return self.status()
    def start(self,body=None):self.ready=True;return self.status()
    def stop(self):self.closed=True;self.ready=False;return self.status()
    def install_runtime(self,asset=None):return self.status()
    def endpoint_config(self):return {'model':'local-test','base_url':'http://127.0.0.1:4567/v1','api_key':'ephemeral-local-secret'} if self.ready else None


def test_api_selects_local_provider_without_exposing_auth_key(tmp_path):
    server=RemoteControlServer(str(tmp_path));server.inference=Runtime()
    assert server.handle('POST','/inference/start',{})[0]==202
    settings=server.handle('GET','/settings')[1]
    assert settings['provider']['model_id']=='local-test'
    assert 'ephemeral-local-secret' not in str(settings)
    coordinator=server._coordinator()
    assert coordinator.config.models['workbench'].resolve_api_key()=='ephemeral-local-secret'
    assert server.handle('GET','/inference')[1]['selected_provider'] is True
    assert server.handle('POST','/inference/stop',{})[0]==200
    assert server.inference.closed


def test_inference_mutations_reject_active_jobs_and_shutdown_stops_runtime(tmp_path):
    server=RemoteControlServer(str(tmp_path));server.inference=Runtime()
    assert server._job_lock.acquire(False)
    try:
        for route in ['start','stop','configure','install-runtime']:
            assert server.handle('POST','/inference/'+route,{})[0]==409
    finally:server._job_lock.release()
    server.drain_jobs()
    assert server.inference.closed
