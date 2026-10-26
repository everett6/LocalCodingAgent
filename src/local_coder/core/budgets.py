"""Shared request admission and conservative token/cost accounting."""
from __future__ import annotations
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
from types import SimpleNamespace

from local_coder.context.token_count import request_accounting


class BudgetExceeded(RuntimeError):
    pass


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
                    'cost_known':all(e['cost_usd'] is not None for e in self.entries),
                    'entries':[e.copy() for e in self.entries]}

    def reserve(self,config,messages,max_tokens,tools=None):
        output=max(1,int(max_tokens))
        accounting = request_accounting(messages, tools, output, config, self.token_counter)
        prompt_bound=accounting['prompt_reserved'];tokens=prompt_bound+output
        input_price=getattr(config,'input_cost_per_million',None)
        output_price=getattr(config,'output_cost_per_million',None)
        request_price=getattr(config,'request_cost_usd',0)
        if any(p is not None and (isinstance(p,bool) or not isinstance(p,(int,float)) or not math.isfinite(p) or p<0) for p in [input_price,output_price,request_price]):
            raise BudgetExceeded('Invalid model pricing')
        cost=(prompt_bound*input_price+output*output_price)/1_000_000+request_price if input_price is not None and output_price is not None and request_price is not None else None
        with self._lock:
            status=self.status()
            if len(self.entries)>=self.limits['max_requests']:raise BudgetExceeded('Model request limit reached')
            if tokens>self.limits['max_request_tokens']:raise BudgetExceeded('Per-request token bound exceeded')
            if status['tokens']+tokens>self.limits['max_tokens']:raise BudgetExceeded('Shared token budget exhausted')
            if self.limits['max_cost_usd'] is not None:
                if cost is None:raise BudgetExceeded('Known model pricing is required for a dollar cap')
                if status['cost_usd']+cost>self.limits['max_cost_usd']+1e-12:raise BudgetExceeded('Shared cost budget exhausted')
            entry={'id':uuid.uuid4().hex,'model':str(getattr(config,'model_id','unknown'))[:256],
                   'created_at':time.time(),'status':'reserved','tokens':tokens,'prompt_bound':prompt_bound,
                   'output_reserved':output,'cost_usd':cost,'usage':'conservative_reservation',
                   'accounting':accounting,'tokens_reserved':tokens}
            self.entries.append(entry);self._save();return entry['id']

    def settle(self,identity,response=None,error=False,config=None):
        with self._lock:
            entry=next(e for e in self.entries if e['id']==identity)
            if entry['status']!='reserved':return
            entry['status']='failed' if error else 'completed'
            prompt=getattr(response,'prompt_tokens',0);completion=getattr(response,'completion_tokens',0)
            if response is not None and type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0 and prompt+completion>0:
                entry.update(tokens=prompt+completion,prompt_tokens=prompt,completion_tokens=completion,usage='provider_reported')
                entry['provider_actual'] = {'prompt_tokens':prompt,'completion_tokens':completion,
                                            'total_tokens':prompt+completion,'source':'provider_reported'}
                ip=getattr(config,'input_cost_per_million',None);op=getattr(config,'output_cost_per_million',None)
                request_price=getattr(config,'request_cost_usd',0)
                if ip is not None and op is not None and request_price is not None:entry['cost_usd']=(prompt*ip+completion*op)/1_000_000+request_price
            # Failed/unknown requests retain the reservation; do not treat retries as free.
            self._save()

    def accounting_status(self, limit=50):
        """Bounded numeric telemetry only: no prompts, schemas, paths or model IDs."""
        with self._lock:
            entries = self.entries[-max(0, min(100, limit)):] if limit else []
            return copy.deepcopy({'requests':len(self.entries), 'entries':[
                {key:entry[key] for key in ('id','created_at','status','tokens','tokens_reserved',
                 'prompt_bound','output_reserved','accounting','provider_actual') if key in entry}
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
    async def generate(self,messages,**kwargs):
        identity=await self._admit(messages,kwargs)
        try:
            result=await self.model.generate(messages,**kwargs)
        except BaseException:
            if identity is not None:self.ledger.settle(identity,error=True,config=self.config)
            raise
        if identity is not None:self.ledger.settle(identity,result,config=self.config)
        return result
    async def stream(self,messages,**kwargs):
        identity=await self._admit(messages,kwargs)
        failed=True
        try:
            async for text in self.model.stream(messages,**kwargs):yield text
            failed=False
        finally:
            if identity is not None:self.ledger.settle(identity,error=failed,config=self.config)
