import asyncio,json
from local_coder.proxy_history import proxy_observations,security_txt_observation
from local_coder.redteam import EngagementStore,EngagementScope
from local_coder.tools.redteam import RedTeamImportTool
from local_coder.security_playbooks import SecurityPlaybookTool,PLAYBOOKS


def test_playbooks_are_local_bounded_review_methods_with_negative_checks():
    assert len(PLAYBOOKS)>=12
    result=asyncio.run(SecurityPlaybookTool().execute(id='ssrf'))
    assert result.success and result.data['evidence_required']
    assert not asyncio.run(SecurityPlaybookTool().execute(id='remote-arbitrary-skill')).success


def test_har_import_never_retains_credentials_query_values_or_bodies(tmp_path):
    request={'url':'https://app.example.test/users?token=very-secret-query&id=99','method':'GET','headers':[{'name':'Authorization','value':'private-bearer'}],'postData':{'text':'secret-body'}}
    response={'status':200,'content':{'mimeType':'application/json','text':'private-response'},'headers':[{'name':'Set-Cookie','value':'private-cookie'}]}
    content=json.dumps({'log':{'entries':[{'request':request,'response':response}]}})
    record=EngagementStore(str(tmp_path)).create(EngagementScope(name='test',objective='Review imported history',origins=['https://app.example.test']))
    (tmp_path/'history.har').write_text(content)
    result=asyncio.run(RedTeamImportTool(EngagementStore(str(tmp_path)),record['id']).execute(path='history.har',format='har'))
    assert result.success and result.data['observations'][0]['parameter_names']==['id','token']
    saved=EngagementStore(str(tmp_path)).load(record['id'])
    for secret in ['very-secret-query','private-bearer','secret-body','private-response','private-cookie']:assert secret not in str(saved)
    assert not saved['findings']


def test_burp_xml_import_rejects_entities_and_scope_escapes(tmp_path):
    import pytest
    with pytest.raises(ValueError):proxy_observations('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><items/>','burp_xml')
    data='<items><item><url>https://outside.example.test/</url><method>GET</method><status>200</status><response base64="true">c2VjcmV0</response></item></items>'
    record=EngagementStore(str(tmp_path)).create(EngagementScope(name='test',objective='Review history',origins=['https://app.example.test']))
    (tmp_path/'burp.xml').write_text(data)
    result=asyncio.run(RedTeamImportTool(EngagementStore(str(tmp_path)),record['id']).execute(path='burp.xml',format='burp_xml'))
    assert not result.success and not EngagementStore(str(tmp_path)).load(record['id'])['events']


def test_security_txt_is_disclosure_metadata_not_authorization_or_exploit():
    result=security_txt_observation(b'Contact: mailto:security@example.test\nExpires: 2099-01-01T00:00:00Z\n',200)
    assert result['valid_metadata'] and not result['authorization_granted'] and not result['signature_checked']
    assert not security_txt_observation(b'Contact: no-scheme\nExpires: old\n',200)['valid_metadata']
    assert not security_txt_observation(b'not found',404)['found']
