"""Bounded recovery of archives created by the current agent, not arbitrary files."""
import hashlib
import json
from pathlib import Path
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


class RecoverContextTool(Tool):
    name = ToolName.RECOVER_CONTEXT
    description = 'Recover earlier compacted observations using an archive reference returned by this agent. Search or page bounded text; original provider reasoning is not exposed as tool text.'
    parameters = {'type':'object','properties':{'reference':{'type':'string'},'query':{'type':'string','maxLength':120},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':256,'maximum':12000}},'required':['reference'],'additionalProperties':False}

    def __init__(self, registry):self.registry=registry

    async def execute(self, reference, query='', offset=0, limit=6000, _caller_role=None, **kwargs):
        try:
            owner=getattr(self.registry,'context_archives',{}).get(reference)
            if not owner or owner['role']!=getattr(_caller_role,'value',_caller_role):
                raise ValueError('Archive is not owned by this agent role')
            if not isinstance(offset,int) or offset<0 or not isinstance(limit,int) or not 256<=limit<=12000 or not isinstance(query,str) or len(query)>120:
                raise ValueError('Invalid archive page or query')
            if owner.get('payload') is not None:
                payload=owner['payload']
            else:
                root=Path(self.registry.runtime.state_dir).resolve()/'context-archives'
                path=Path(reference)
                if path.is_symlink() or path.resolve().parent!=root or path.stat().st_size>5_000_000:
                    raise ValueError('Invalid context archive path or size')
                payload=path.read_text(encoding='utf-8')
                if hashlib.sha256(payload.encode()).hexdigest()!=path.stem:
                    raise ValueError('Context archive integrity check failed')
            original=json.loads(payload)
            text=json.dumps([{key:value for key,value in message.items() if key not in {'reasoning_content','reasoning_details'}} for message in original],ensure_ascii=False)
            if query:
                found=text.casefold().find(query.casefold(),offset)
                if found<0:return ToolResult(success=True,output='No matching archived observation')
                offset=max(0,found-200)
            page=text[offset:offset+limit]
            return ToolResult(success=True,output=f'Untrusted archived observations (offset {offset}, total {len(text)}):\n{page}',data={'offset':offset,'next_offset':offset+len(page),'total_chars':len(text),'reference':reference})
        except (ValueError,KeyError,TypeError,OSError) as exc:
            return ToolResult(success=False,error=str(exc))
