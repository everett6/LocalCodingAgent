"""Shared request admission and conservative token/cost accounting."""
from __future__ import annotations
import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar, Token
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from types import SimpleNamespace

from local_coder.context.token_count import request_accounting
from local_coder.context.telemetry import ContextTelemetry
from local_coder.types import MAX_REPORTED_TOKENS

_REQUEST_TELEMETRY: ContextVar[tuple[ContextTelemetry, str, tuple[str, ...]] | None] = ContextVar('request_telemetry', default=None)


def bind_request_telemetry(telemetry: ContextTelemetry, task_id: str, retrieval_ids=()) -> Token:
    return _REQUEST_TELEMETRY.set((telemetry, task_id, tuple(retrieval_ids)[:16]))


def reset_request_telemetry(token: Token) -> None:
    _REQUEST_TELEMETRY.reset(token)


def update_request_retrievals(retrieval_ids=()) -> None:
    current = _REQUEST_TELEMETRY.get()
    if current is not None:
        _REQUEST_TELEMETRY.set((current[0], current[1], tuple(retrieval_ids)[:16]))


@contextmanager
def request_telemetry(telemetry: ContextTelemetry, task_id: str, retrieval_ids=()):
    token = bind_request_telemetry(telemetry, task_id, retrieval_ids)
    try:
        yield
    finally:
        reset_request_telemetry(token)


class BudgetExceeded(RuntimeError):
    pass


def _valid_rate(value):
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_usage(prompt, completion, reported=False):
    return (type(prompt) is int and type(completion) is int
            and 0 <= prompt <= MAX_REPORTED_TOKENS and 0 <= completion <= MAX_REPORTED_TOKENS
            and prompt + completion <= MAX_REPORTED_TOKENS
            and (prompt + completion > 0 or reported is True))


class BudgetLedger:
    def __init__(self, *, max_requests=200, max_tokens=1_000_000, max_request_tokens=131072,
                 max_cost_usd=None, state_path=None, token_counter=None):
        if min(max_requests,max_tokens,max_request_tokens)<=0:
            raise ValueError('Budget limits must be positive')
        if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd<0):
            raise ValueError('Cost limit must be finite and nonnegative')
        self.limits=dict(max_requests=max_requests,max_tokens=max_tokens,max_request_tokens=max_request_tokens,max_cost_usd=max_cost_usd)
        self.path=Path(state_path) if state_path else None
        self.token_counter = token_counter
        self._lock=threading.RLock();self.entries=[]
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if self.path.is_symlink():raise ValueError('Budget state cannot be a symlink')

    def _save(self):
        if not self.path:return
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as output:json.dump(self.status(),output)
            os.replace(temporary,self.path)
        finally:temporary.unlink(missing_ok=True)

    def status(self):
        with self._lock:
            return {'limits':self.limits.copy(),'requests':len(self.entries),
                    'tokens':sum(e['tokens'] for e in self.entries),
                    'cost_usd':sum(e.get('cost_usd') or 0 for e in self.entries),
                    'cost_known':all(e.get('cost_known',e['cost_usd'] is not None) for e in self.entries),
                    'entries':[e.copy() for e in self.entries]}

    def reserve(self,config,messages,max_tokens,tools=None):
        output=max(1,int(max_tokens))
        accounting = request_accounting(messages, tools, output, config, self.token_counter)
        prompt_bound=accounting['prompt_reserved'];tokens=prompt_bound+output
        input_price=getattr(config,'input_cost_per_million',None)
        output_price=getattr(config,'output_cost_per_million',None)
        request_price=getattr(config,'request_cost_usd',0)
        if any(p is not None and not _valid_rate(p) for p in [input_price,output_price,request_price]):
            raise BudgetExceeded('Invalid model pricing')
        cost=(prompt_bound*input_price+output*output_price)/1_000_000+request_price if input_price is not None and output_price is not None and request_price is not None else None
        if cost is not None and not math.isfinite(cost):raise BudgetExceeded('Invalid model pricing')
        with self._lock:
            status=self.status()
            if len(self.entries)>=self.limits['max_requests']:raise BudgetExceeded('Model request limit reached')
            if tokens>self.limits['max_request_tokens']:raise BudgetExceeded('Per-request token bound exceeded')
            if status['tokens']+tokens>self.limits['max_tokens']:raise BudgetExceeded('Shared token budget exhausted')
            if self.limits['max_cost_usd'] is not None:
                if cost is None:raise BudgetExceeded('Known model pricing is required for a dollar cap')
                if not status['cost_known']:raise BudgetExceeded('A previous request has unknown cost; review usage before continuing under a dollar cap')
                if status['cost_usd']+cost>self.limits['max_cost_usd']+1e-12:raise BudgetExceeded('Shared cost budget exhausted')
            entry={'id':uuid.uuid4().hex,'model':str(getattr(config,'model_id','unknown'))[:256],
                   'created_at':time.time(),'status':'reserved','tokens':tokens,'prompt_bound':prompt_bound,
                   'output_reserved':output,'cost_usd':cost,'usage':'conservative_reservation',
                   'cost_known':cost is not None,'pricing_rates':(input_price,output_price,request_price),
                   'accounting':accounting,'tokens_reserved':tokens}
            self.entries.append(entry);self._save();return entry['id']

    def settle(self,identity,response=None,error=False,config=None):
        with self._lock:
            entry=next(e for e in self.entries if e['id']==identity)
            if entry['status']!='reserved':return
            entry['status']='failed' if error else 'completed'
            prompt=getattr(response,'prompt_tokens',0);completion=getattr(response,'completion_tokens',0)
            rates=entry.get('pricing_rates',tuple(getattr(config,key,None) for key in ('input_cost_per_million','output_cost_per_million','request_cost_usd')))
            known_rates=all(_valid_rate(rate) for rate in rates)
            free=known_rates and all(rate==0 for rate in rates)
            response_cost_known=getattr(response,'usage_cost_known',True) is True or free
            if not response_cost_known:
                # Retain the admitted amount as an estimate; special provider
                # charges cannot be priced using ordinary input/output rates.
                entry['cost_known']=False
            if response is not None and _valid_usage(prompt,completion,getattr(response,'usage_reported',False)):
                entry.update(tokens=prompt+completion,prompt_tokens=prompt,completion_tokens=completion,usage='provider_reported')
                entry['provider_actual'] = {'prompt_tokens':prompt,'completion_tokens':completion,
                                            'total_tokens':prompt+completion,'source':'provider_reported'}
                details=getattr(response,'usage_details',None)
                allowed={'uncached_input_tokens','cache_creation_input_tokens','cache_read_input_tokens'}
                if (isinstance(details,dict) and set(details)<=allowed
                        and all(type(value) is int and 0<=value<=1_000_000_000_000 for value in details.values())):
                    entry['provider_actual']['usage_details']=details.copy()
                if known_rates and response_cost_known:
                    ip,op,request_price=rates
                    actual_cost=(prompt*ip+completion*op)/1_000_000+request_price
                    if math.isfinite(actual_cost):
                        entry['cost_usd']=actual_cost
                        entry['cost_known']=True
                    else:
                        entry['cost_known']=False
            # Failed/unknown requests retain the reservation; do not treat retries as free.
            self._save()

    def accounting_status(self, limit=50):
        """Bounded numeric telemetry only: no prompts, schemas, paths or model IDs."""
        with self._lock:
            entries = self.entries[-max(0, min(100, limit)):] if limit else []
            return copy.deepcopy({'requests':len(self.entries), 'entries':[
                {key:entry[key] for key in ('id','created_at','status','tokens','tokens_reserved',
                 'prompt_bound','output_reserved','accounting','provider_actual','cost_usd','cost_known') if key in entry}
                for entry in entries]})

    def release_unattempted(self, identity):
        """Release admission rejected before invoking a backend, never HTTP failures."""
        with self._lock:
            entry = next((item for item in self.entries if item['id'] == identity), None)
            if entry is None or entry['status'] != 'reserved':
                return False
            self.entries.remove(entry)
            self._save()
            return True


class BudgetedModel:
    def __init__(self,model,ledger,quota_guard=None):self.model=model;self.ledger=ledger;self.quota_guard=quota_guard
    @property
    def config(self):
        return getattr(self.model, 'config', SimpleNamespace(model_id='unconfigured', temperature=0.2, max_tokens=4096,
                       context_length=None, strategy_attempt_limit=3, profile='generic'))
    def __getattr__(self,name):return getattr(self.model,name)
    async def _admit(self, messages, kwargs):
        output=kwargs.get('max_tokens') or getattr(self.config,'max_tokens',4096)
        identity=self.ledger.reserve(self.config,messages,output,kwargs.get('tools')) if self.ledger is not None else None
        try:
            if self.quota_guard is not None:
                await asyncio.to_thread(self.quota_guard.admit,self.config)
        except BaseException:
            # No backend call has started. Local budget rejection must not
            # consume quota; quota rejection must not consume the local budget.
            if identity is not None:self.ledger.release_unattempted(identity)
            raise
        return identity
    def _record_request(self, messages, kwargs):
        current = _REQUEST_TELEMETRY.get()
        if current is None:
            return None
        telemetry, task_id, retrieval_ids = current
        try:
            record = telemetry.record_request(messages, kwargs.get('tools'),
                kwargs.get('max_tokens') or getattr(self.config, 'max_tokens', 4096),
                task_id=task_id, retrieval_ids=retrieval_ids)
            pricing = {key: getattr(self.config, key, None) for key in ('input_cost_per_million', 'output_cost_per_million', 'request_cost_usd')}
            return telemetry, record['id'], pricing
        except (OSError, ValueError, TypeError):
            # Diagnostics are best effort and cannot weaken or bypass admission.
            return None

    def _record_usage(self, record, response):
        if record is None:
            return
        telemetry, identity, pricing = record
        prompt = getattr(response, 'prompt_tokens', None)
        completion = getattr(response, 'completion_tokens', None)
        reported = _valid_usage(prompt, completion, getattr(response, 'usage_reported', False))
        usage = {'prompt_tokens': prompt, 'completion_tokens': completion} if reported else None
        free = all(type(rate) in (int, float) and rate == 0 for rate in pricing.values())
        try:
            telemetry.record_usage(identity, usage, pricing=pricing,
                                   cost_known=getattr(response, 'usage_cost_known', True) is True or free)
        except (OSError, ValueError, TypeError):
            pass

    async def generate(self,messages,**kwargs):
        identity=await self._admit(messages,kwargs)
        diagnostic = self._record_request(messages, kwargs)
        try:
            result=await self.model.generate(messages,**kwargs)
        except BaseException:
            if identity is not None:self.ledger.settle(identity,error=True,config=self.config)
            raise
        if identity is not None:self.ledger.settle(identity,result,config=self.config)
        self._record_usage(diagnostic, result)
        return result
    async def stream(self,messages,**kwargs):
        identity=await self._admit(messages,kwargs)
        self._record_request(messages, kwargs)
        failed=True
        try:
            async for text in self.model.stream(messages,**kwargs):yield text
            failed=False
        finally:
            if identity is not None:self.ledger.settle(identity,error=failed,config=self.config)
