import asyncio
import json

import httpx
import pytest

from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.types import Message, ModelConfig


@pytest.mark.parametrize('profile,model_id',[
    ('openrouter_deepseek','deepseek/deepseek-v4.1-flash-1017'),
    ('openrouter_glm','z-ai/glm-5.3'),
    ('openrouter_kimi','moonshotai/kimi-k3.1-pro'),
])
def test_openrouter_reasoning_contract_and_opaque_tool_roundtrip(profile,model_id):
    seen=[]
    blocks=[{'type':'reasoning.encrypted','data':'opaque+signature==','id':'r1','index':0}]
    def respond(request):
        payload=json.loads(request.content);seen.append(payload)
        assert request.headers['authorization']=='Bearer synthetic-key'
        return httpx.Response(200,json={'choices':[{'message':{'content':'Inspect','reasoning':'protocol-text','reasoning_details':blocks},'finish_reason':'stop'}]})
    async def scenario():
        backend=OpenAICompatibleBackend(ModelConfig(name='router',model_id=model_id,backend='openai_compatible',base_url='https://openrouter.ai/api/v1',profile=profile,api_key='synthetic-key'))
        await backend.close()
        backend.client=httpx.AsyncClient(base_url=backend.config.base_url,headers={'Authorization':'Bearer synthetic-key'},transport=httpx.MockTransport(respond))
        response=await backend.generate([Message(role='user',content='Review')])
        assert response.reasoning_details==blocks and response.reasoning_content=='protocol-text'
        await backend.generate([Message(role='assistant',content=response.content,reasoning_content=response.reasoning_content,reasoning_details=response.reasoning_details)])
        assert seen[0]['reasoning']=={'effort':'high','exclude':False}
        assert 'thinking' not in seen[0] and 'enable_thinking' not in seen[0]
        assert seen[0]['provider']['require_parameters'] is True
        assert seen[1]['messages'][0]['reasoning_details']==blocks
        assert ('temperature' in seen[0]) is (profile!='openrouter_kimi')
        await backend.close()
    asyncio.run(scenario())


def test_provider_error_body_never_exposes_credentials():
    async def scenario():
        backend=OpenAICompatibleBackend(ModelConfig(name='x',model_id='x',profile='openrouter_glm'))
        await backend.close()
        backend.client=httpx.AsyncClient(base_url='https://openrouter.ai/api/v1',transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'error':{'message':'private-provider-key'}})))
        with pytest.raises(RuntimeError) as exc:await backend.generate([Message(role='user',content='test')])
        assert 'private-provider-key' not in str(exc.value)
        await backend.close()
    asyncio.run(scenario())
