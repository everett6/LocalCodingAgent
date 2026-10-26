"""Authenticated operator evaluation controls sharing the workspace job slot."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
from pathlib import Path
from uuid import uuid4

from local_coder.core.budgets import BudgetedModel, BudgetExceeded
from local_coder.harness.evaluation import EvaluationStore, _limits, catalog, make_model_evaluator, suite_hash, spending_admission as _spending_admission
from local_coder.models.router import resolve_model_name
from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.types import AgentEvent, AgentMode, AgentRole




class EvaluationAPI:
    def _init_evaluation_api(self):
        self._evaluation_jobs = {}

    def _evaluation_store(self):
        root = getattr(self, '_harness_root', None) or getattr(self, 'workspace', None) or self.project_root
        return EvaluationStore(root if isinstance(root, (str, Path)) else root.root)

    def _remediation_measurements(self):
        """Count canonical observed regressions separately from fixture scores."""
        from local_coder.security.evidence import EvidenceStore
        result = {'available': True, 'successful_fixes': 0, 'reproduced_findings': 0, 'stale_records': 0}
        try:
            for row in EvidenceStore(self.project_root).list():
                if row.get('evidence_stale'):
                    result['stale_records'] += 1
                    continue
                before, after = row.get('before') or {}, row.get('after') or {}
                reproduced = before.get('status') == 'failed' and before.get('failure_kind') == 'assertion' and before.get('failed', 0) > 0
                if reproduced:
                    result['reproduced_findings'] += 1
                if (reproduced and row.get('state') in {'fixed', 'reverified'}
                        and after.get('status') == 'passed' and after.get('passed', 0) > 0
                        and all(before.get(key) and before[key] == after.get(key)
                                for key in ('test_path', 'test_digest', 'test_ids', 'image_id', 'backend'))):
                    result['successful_fixes'] += 1
        except (ValueError, OSError):
            return {'available': False, 'successful_fixes': None, 'reproduced_findings': None, 'stale_records': None}
        return result

    def _evaluation_get(self, path, query):
        if path != '/evaluations' and not path.startswith('/evaluations/'):
            return False, None
        if query:
            raise ValueError('Evaluation routes accept no query parameters')
        if path == '/evaluations':
            settings = self.harness_settings() if hasattr(self, 'harness_settings') else None
            return True, {'catalog': catalog(), 'history': list(reversed(self._evaluation_store().history())),
                          'project_remediation': self._remediation_measurements(),
                          'model_evaluation': {'max_cost_usd': getattr(settings, 'max_cost_usd', None),
                           'spending_rule': 'Cloud, metered and unknown-cost models require a positive harness dollar cap; no automatic provider fallback.'}}
        match = re.fullmatch(r'/evaluations/jobs/([0-9a-f]{32})', path)
        if match:
            with self.lock:
                row = getattr(self, '_evaluation_jobs', {}).get(match[1])
                if row is None:
                    raise ValueError('Unknown or expired evaluation job')
                return True, copy.deepcopy(row)
        identity = path.removeprefix('/evaluations/')
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid evaluation report ID')
        report = next((item for item in self._evaluation_store().history() if item['id'] == identity), None)
        if report is None:
            raise ValueError('Evaluation report does not exist')
        return True, report

    def _evaluation_post(self, path, body):
        cancel = re.fullmatch(r'/evaluations/jobs/([0-9a-f]{32})/cancel', path)
        if cancel:
            if body != {}:
                raise ValueError('Cancellation accepts an empty object')
            with self.lock:
                row = getattr(self, '_evaluation_jobs', {}).get(cancel[1])
                if row is None:
                    raise ValueError('Unknown or expired evaluation job')
                job = self._jobs.get(cancel[1])
                if job and not job['cancel'].is_set():
                    job['cancel'].set()
                    row['status'] = 'cancelling'
                    loop, task = job.get('loop'), job.get('task')
                    if loop and task and not loop.is_closed():
                        loop.call_soon_threadsafe(task.cancel)
                return True, copy.deepcopy(row)
        if path not in {'/evaluations/run', '/evaluations/model/run'}:
            return False, None
        allowed = {'suite_hash', 'max_cases', 'timeout_seconds'} | ({'confirm_model_calls'} if path == '/evaluations/model/run' else set())
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError('Only fixed-suite limits and explicit model confirmation are accepted')
        if path == '/evaluations/run':
            return True, self._evaluation_store().run(expected_suite_hash=body.get('suite_hash'), max_cases=body.get('max_cases', 20), timeout_seconds=body.get('timeout_seconds', 10))
        if body.get('suite_hash') != suite_hash() or body.get('confirm_model_calls') is not True:
            raise ValueError('Confirm model calls and supply the displayed suite hash')
        _limits(body.get('max_cases', 20), body.get('timeout_seconds', 60))
        return True, self._start_evaluation_job(dict(body))

    def _start_evaluation_job(self, options):
        if not self._job_lock.acquire(blocking=False):
            raise ValueError('A task is already running in this workspace')
        job_id = uuid4().hex
        job = {'cancel': threading.Event(), 'loop': None, 'task': None, 'events': []}
        row = {'job_id': job_id, 'session_id': job_id, 'status': 'queued'}
        try:
            with self.lock:
                if self._stopping:
                    raise ValueError('Engine is shutting down')
                rows = self._evaluation_jobs
                while len(rows) >= 30:
                    expired = next(key for key, value in rows.items() if value['status'] not in {'queued', 'running', 'cancelling'})
                    del rows[expired]
                rows[job_id] = row
                self._jobs[job_id] = job
            self.sessions.save(job_id, request='Evaluate configured security model against fixed suite', mode=AgentMode.CYBERSECURITY.value, phase='model_evaluation', status='queued')
            thread = threading.Thread(target=self._evaluation_job_thread, args=(job_id, job, options), daemon=True)
            with self.lock:
                if self._stopping:
                    raise ValueError('Engine is shutting down')
                job['thread'] = thread
                thread.start()
            return copy.deepcopy(row)
        except BaseException:
            with self.lock:
                self._jobs.pop(job_id, None)
                self._evaluation_jobs.pop(job_id, None)
            self._job_lock.release()
            raise

    def _evaluation_job_thread(self, job_id, job, options):
        self._context.session_id, self._context.mode, self._context.goal_id = job_id, AgentMode.CYBERSECURITY, None

        async def execute():
            coordinator, result, status, error, notice = None, None, 'failed', '', None
            try:
                with self.lock:
                    job['loop'], job['task'] = asyncio.get_running_loop(), asyncio.current_task()
                    if job['cancel'].is_set():
                        raise asyncio.CancelledError()
                    self._evaluation_jobs[job_id]['status'] = 'running'
                self.sessions.save(job_id, status='running')
                self._event(AgentEvent(source='SERVER', event_type='evaluation_started', message='Fixed-suite model evaluation started'))
                coordinator = self._coordinator()
                manager = coordinator.model_manager
                selected = resolve_model_name(manager.config, AgentRole.SECURITY)
                config = manager.config.models[selected]
                _spending_admission(config, coordinator.budget)
                # Restrict this dedicated coordinator before initialize/get_model:
                # construct only the explicitly configured role backend, no probes.
                manager.config = copy.deepcopy(manager.config)
                manager.config.models = {selected: config}
                manager.config.routing.fallbacks = {}
                model = await manager.get_model(AgentRole.SECURITY, selected)
                while isinstance(model, BudgetedModel):
                    model = model.model
                evaluator = make_model_evaluator(lambda overlay: model, ledger=coordinator.budget,
                    quota_guard=getattr(manager, 'quota_guard', None), expected_suite_hash=options['suite_hash'],
                    max_cases=options.get('max_cases', 20), timeout_seconds=options.get('timeout_seconds', 60), require_usage=False)
                measured = await evaluator({})
                if job['cancel'].is_set():
                    raise asyncio.CancelledError()
                measured['report']['model'] = {'name': selected, 'model_id': str(config.model_id),
                                                'backend': str(getattr(config.backend, 'value', config.backend))}
                result = self._evaluation_store().record_model(measured['report'])
                status = 'completed'
            except asyncio.CancelledError:
                status, error = 'cancelled', 'Model evaluation cancelled; no completed report was recorded'
            except ProviderQuotaExhausted as exc:
                status, error, notice = 'quota_exhausted', str(exc), exc.notice
            except BudgetExceeded as exc:
                status, error = 'budget_exhausted', str(exc)
            except asyncio.TimeoutError:
                error = 'Model evaluation exceeded its time limit'
            except Exception:
                error = 'Model evaluation failed; check configured model availability, pricing, spending cap and response format'
            finally:
                if coordinator is not None:
                    try:
                        cleanup = asyncio.create_task(asyncio.wait_for(coordinator.model_manager.close_all(), timeout=15))
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            status, error = 'cancelled', 'Model evaluation cancelled'
                            await asyncio.shield(cleanup)
                    except Exception:
                        status, error = 'failed', 'Model evaluation cleanup failed'
                final = {'status': status, 'result': result, 'error': error, 'quota_notice': notice}
                self._event(AgentEvent(source='SERVER', event_type='evaluation_finished', message=f'Model evaluation: {status}'))
                with self.lock:
                    final['events'] = copy.deepcopy(job.get('events', [])[-600:])
                    self._evaluation_jobs[job_id].update(self._redact(final))
                self.sessions.save(job_id, status=status, result='' if result is None else json.dumps(result), error=error)
        try:
            asyncio.run(execute())
        finally:
            with self.lock:
                self._jobs.pop(job_id, None)
            self._job_lock.release()
