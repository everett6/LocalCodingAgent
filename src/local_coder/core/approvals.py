"""Human-only approval inbox; no agent tool can approve its own request."""
import asyncio
import threading
import time
import uuid


class ApprovalInbox:
    def __init__(self,timeout=90):self.timeout=timeout;self._items={};self._lock=threading.RLock()
    def list(self):
        with self._lock:return [dict(item) for item in self._items.values()]
    def respond(self,identity,approved):
        if type(approved) is not bool:raise ValueError('Approval must be a boolean')
        with self._lock:
            item=self._items.get(identity)
            if not item or item['status']!='pending':raise ValueError('Approval is no longer pending')
            if time.time() >= item['created_at'] + self.timeout:
                item['status']='expired'
                raise ValueError('Approval has expired')
            item['status']='approved' if approved else 'denied'
    def cancel_all(self):
        with self._lock:
            for item in self._items.values():
                if item['status']=='pending':item['status']='cancelled'
    async def request(self,description):
        from local_coder.memory.context_store import _clean
        description=_clean(str(description))
        if len(description)>200000:
            return False
        item={'id':uuid.uuid4().hex,'description':description, 'status':'pending','created_at':time.time()}
        with self._lock:
            self._items[item['id']]=item
            for identity in list(self._items)[:-100]:
                if self._items[identity]['status']!='pending':self._items.pop(identity)
        try:
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                with self._lock:
                    if item['status']!='pending':return item['status']=='approved'
                await asyncio.sleep(.05)
            with self._lock:item['status']='expired'
            return False
        except asyncio.CancelledError:
            with self._lock:item['status']='cancelled'
            raise
