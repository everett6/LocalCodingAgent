"""Passive, credential-stripping HTTP history ingestion and disclosure metadata."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import xml.etree.ElementTree as ET


def proxy_observations(content, format):
    if format=='har':
        entries=json.loads(content)['log']['entries']
        items=[{'url':entry['request']['url'],'method':entry['request']['method'],
                'status':entry['response']['status'],'mime':entry['response'].get('content',{}).get('mimeType','')} for entry in entries]
    elif format=='burp_xml':
        if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
            raise ValueError('DTD/entity declarations are not permitted in Burp imports')
        try:tree=ET.fromstring(content)
        except ET.ParseError as exc:raise ValueError('Invalid Burp XML') from exc
        items=[{'url':item.findtext('url'),'method':item.findtext('method'),'status':item.findtext('status'),'mime':item.findtext('mimetype') or ''} for item in tree.findall('item')]
    else:raise ValueError('Unknown proxy history format')
    if not isinstance(items,list) or len(items)>100:raise ValueError('Import at most 100 proxy history entries')
    observations=[]
    for item in items:
        parsed=urlsplit(item['url'])
        url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path or '/','',''))
        method=str(item['method']).upper()
        status=int(item['status'])
        if not re.fullmatch(r'[A-Z]{1,16}',method) or not 100<=status<=599:
            raise ValueError('Invalid HTTP method or response status in history')
        parameters=sorted({key for key,_ in parse_qsl(parsed.query,max_num_fields=100) if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.\[\]-]{0,79}',key)})
        observations.append({'url':url,'method':method,'status':status,'mime':str(item['mime'])[:120],
                             'parameter_names':parameters,'verification':'imported_observation_only'})
    return observations


def security_txt_observation(body: bytes, status: int, truncated=False):
    fields={};issues=[]
    try:text=body.decode('utf-8')
    except UnicodeDecodeError:return {'found':status==200,'valid_metadata':False,'issues':['Not valid UTF-8'],'authorization_granted':False}
    for line in text.splitlines():
        if not line or line.startswith('#') or ':' not in line:continue
        name,value=line.split(':',1);name=name.strip().lower()
        if name in {'contact','expires','canonical','policy','encryption','preferred-languages'}:
            fields.setdefault(name,[]).append(value.strip()[:1000])
    if status!=200:issues.append('security.txt was not returned with HTTP 200')
    if not fields.get('contact'):issues.append('Missing Contact field')
    if any(not urlsplit(value).scheme for value in fields.get('contact',[])):issues.append('Contact requires a URI scheme')
    expiration=fields.get('expires',[])
    if len(expiration)!=1:issues.append('Exactly one Expires field is required')
    else:
        try:
            parsed=datetime.fromisoformat(expiration[0].replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError('Timezone required')
            if parsed<=datetime.now(timezone.utc):issues.append('Expires date has passed')
        except ValueError:issues.append('Invalid Expires timestamp')
    if truncated:issues.append('Response truncated; metadata validation incomplete')
    return {'found':status==200,'fields':fields,'issues':issues,'valid_metadata':not issues,
            'signature_checked':False,'authorization_granted':False}
