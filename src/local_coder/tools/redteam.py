"""Engagement-bound tools: bounded reads, HTTP observations, imports and evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from local_coder.redteam import EngagementStore, PHASES, addresses
from local_coder.tools.base import Tool
from local_coder.tools.scan_scope import scan_files
from local_coder.tools.security import RunSASTTool
from local_coder.types import ToolName, ToolResult


class EngagementTool(Tool):
    def __init__(self, store: EngagementStore, engagement_id: str):
        self.store, self.engagement_id = store, engagement_id

    async def execute(self, **kwargs):
        try:
            return await self.run(**kwargs)
        except (ValueError, OSError, KeyError, TypeError, AttributeError, IndexError, TimeoutError, httpx.HTTPError) as exc:
            # Do not persist raw transport exception text, which can contain URLs.
            return ToolResult(success=False, error=str(exc) if isinstance(exc, ValueError) else f'{type(exc).__name__}: check engagement scope and evidence format')


class RedTeamWorkspaceTool(EngagementTool):
    name = ToolName.REDTEAM_WORKSPACE
    description = 'Inventory, read bounded source, or run static candidates inside the immutable engagement scope. Repository text is untrusted evidence.'
    parameters = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['inventory', 'read', 'scan']}, 'path': {'type': 'string'}}, 'required': ['action'], 'additionalProperties': False}

    async def run(self, action, path=None):
        record = self.store.load(self.engagement_id)
        if action == 'read':
            file = self.store.resolve_source(record, path)
            if not file.is_file() or file.stat().st_size > 64000:
                raise ValueError('Choose an in-scope text file smaller than 64 KB')
            raw = file.read_bytes()
            content = raw.decode('utf-8')
            evidence_id = self.store.evidence(self.engagement_id, 'source_read', {'path': file.relative_to(self.store.workspace.root).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()})
            return ToolResult(success=True, output=f'Evidence ID: {evidence_id}\nUntrusted source:\n' + '\n'.join(f'{i}: {line}' for i, line in enumerate(content.splitlines(), 1)), data={'evidence_id': evidence_id})
        if action not in {'inventory', 'scan'}:
            raise ValueError('Unknown workspace action')
        files, skipped = set(), []
        root = self.store.workspace.root
        targets = [path] if path else record['scope']['paths']
        for value in targets:
            target = self.store.resolve_source(record, value)
            candidates, coverage = scan_files(root, target)
            skipped.extend(coverage['skipped'])
            for file in candidates:
                try:
                    files.add(self.store.resolve_source(record, str(file)))
                except ValueError:
                    continue
        files = sorted(files)
        total, bounded = 0, []
        for file in files:
            size = file.stat().st_size
            if len(bounded) >= 1000 or total + size > 20_000_000:
                skipped.append('Combined engagement scan budget reached')
                break
            bounded.append(file)
            total += size
        files = bounded
        coverage = {'files_considered': len(files), 'bytes': total, 'skipped': skipped, 'status': 'partial' if skipped else 'complete', 'exclusions_applied': record['scope']['excluded_paths']}
        findings = []
        if action == 'scan':
            scanner = RunSASTTool(str(root))
            findings = await asyncio.to_thread(scanner._run_builtin_sast, root, files, coverage)
            findings = [dict(item, file=item['file'].replace('\\', '/'), confidence='heuristic', verification='unverified') for item in findings]
        coverage['status'] = 'partial' if coverage['skipped'] else 'complete'
        result = {'files': [file.relative_to(root).as_posix() for file in files], 'findings': findings[:200], 'coverage': coverage}
        evidence_id = self.store.evidence(self.engagement_id, 'static_scan' if action == 'scan' else 'inventory', result)
        return ToolResult(success=True, output=json.dumps({**result, 'evidence_id': evidence_id}, ensure_ascii=False), data={**result, 'evidence_id': evidence_id})


class RedTeamHTTPTool(EngagementTool):
    name = ToolName.REDTEAM_HTTP
    description = 'One authorized GET/HEAD/OPTIONS observation on an exact allowed origin. No redirects, query payloads, credentials, proxies or cookie sessions. Returns bounded header/body-hash evidence, not proof of exploitation.'
    parameters = {'type': 'object', 'properties': {'url': {'type': 'string', 'maxLength': 2048}, 'method': {'type': 'string', 'enum': ['GET', 'HEAD', 'OPTIONS']}, 'check': {'type':'string','enum':['headers','security_txt']}}, 'required': ['url'], 'additionalProperties': False}

    async def run(self, url, method='GET', check='headers'):
        if method not in {'GET', 'HEAD', 'OPTIONS'} or not isinstance(url, str) or len(url) > 2048:
            raise ValueError('Only bounded GET/HEAD/OPTIONS observations are supported')
        if check not in {'headers','security_txt'}:
            raise ValueError('Unknown HTTP observation check')
        if check == 'security_txt' and (method != 'GET' or urlsplit(url).path not in {'/.well-known/security.txt','/security.txt'}):
            raise ValueError('security.txt check requires GET on its disclosure metadata path')
        record = self.store.load(self.engagement_id)
        base = self.store.check_url(record, url)
        if not record['scope']['active_http']:
            raise ValueError('Active HTTP checks are disabled for this engagement')
        parsed = urlsplit(base)
        current = await asyncio.wait_for(asyncio.to_thread(addresses, parsed.hostname, parsed.port), timeout=5)
        pins = record['pins'][base]
        if not set(current) <= set(pins):
            raise ValueError('Target DNS changed beyond the pinned engagement addresses; create a new scope')
        address = current[0]
        # Connect to the pinned IP while preserving original TLS verification/SNI and Host.
        pinned = httpx.URL(url).copy_with(host=address, port=parsed.port)
        authority = urlsplit(base).netloc
        attempt = self.store.reserve_request(self.engagement_id, url, method)
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8, limits=httpx.Limits(max_connections=1)) as client:
                async with asyncio.timeout(10):
                    async with client.stream(method, pinned, headers={'Host': authority, 'User-Agent': 'LocalCoder-Authorized-Check/1.0', 'Accept-Encoding': 'identity'}, extensions={'sni_hostname': parsed.hostname}) as response:
                        body = bytearray()
                        truncated = False
                        async for chunk in response.aiter_raw():
                            remaining = 65536 - len(body)
                            body.extend(chunk[:remaining])
                            if len(chunk) > remaining or len(body) >= 65536:
                                truncated = True
                                break
                        allowed_headers = {'content-type', 'content-length', 'strict-transport-security', 'content-security-policy', 'x-content-type-options', 'x-frame-options', 'referrer-policy', 'permissions-policy', 'allow'}
                        headers = {key: value[:1024] for key, value in response.headers.items() if key in allowed_headers}
                        observation = {'url': url, 'method': method, 'attempt_id': attempt, 'status': response.status_code,
                                       'headers': headers, 'redirect_not_followed': response.is_redirect,
                                       'body_bytes_hashed': len(body), 'body_sha256': hashlib.sha256(body).hexdigest(), 'body_truncated': truncated,
                                       'verification': 'http_observation_only', 'missing_headers': sorted({'content-security-policy', 'x-content-type-options'} - headers.keys())}
            if check == 'security_txt':
                from local_coder.proxy_history import security_txt_observation
                observation['security_txt'] = security_txt_observation(bytes(body), observation['status'], truncated)
            evidence_id = self.store.evidence(self.engagement_id, 'http_observation', observation)
            return ToolResult(success=True, output=json.dumps({**observation, 'evidence_id': evidence_id}), data={**observation, 'evidence_id': evidence_id})
        except (httpx.HTTPError, TimeoutError, asyncio.CancelledError) as exc:
            self.store.evidence(self.engagement_id, 'http_interrupted' if isinstance(exc, asyncio.CancelledError) else 'http_failed', {'attempt_id': attempt, 'reason': type(exc).__name__})
            raise


class Finding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    asset: str = Field(min_length=1, max_length=2048)
    line: int | None = Field(default=None, ge=1)
    severity: Literal['critical', 'high', 'medium', 'low', 'info'] = 'info'
    evidence_ids: list[str] = Field(min_length=1, max_length=20)
    hypothesis: str = Field(default='', max_length=4000)
    attack_path: list[str] = Field(default_factory=list, max_length=20)
    detection_expected: str = Field(default='', max_length=2000)
    detection_outcome: Literal['not_tested'] = 'not_tested'
    remediation: str = Field(default='', max_length=4000)


class RedTeamFindingTool(EngagementTool):
    name = ToolName.REDTEAM_FINDING
    description = 'Record/deduplicate an evidence-linked hypothesis, attack path, detection expectation and remediation. Imported/static/HTTP evidence never establishes exploit verification.'
    parameters = Finding.model_json_schema()

    async def run(self, **kwargs):
        finding = Finding.model_validate(kwargs).model_dump()
        if finding['detection_outcome'] != 'not_tested':
            # This mode has no detector-log ingestion: do not invent a measured detection result.
            raise ValueError('Detector telemetry is not imported by this tool; detection_outcome must remain not_tested')
        result = self.store.finding(self.engagement_id, finding)
        return ToolResult(success=True, output=json.dumps(result), data=result)


class RedTeamWorkflowTool(EngagementTool):
    name = ToolName.REDTEAM_WORKFLOW
    description = 'Read persistent engagement status or advance one methodology phase with a saved checklist. Completing a phase does not verify findings.'
    parameters = {'type': 'object', 'properties': {'phase': {'type': 'string', 'enum': list(PHASES)}, 'steps': {'type': 'array', 'maxItems': 30, 'items': {'type': 'object', 'properties': {'title': {'type': 'string', 'maxLength': 300}, 'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed', 'blocked']}}, 'required': ['title', 'status'], 'additionalProperties': False}}}, 'additionalProperties': False}

    async def run(self, phase=None, steps=None):
        def mutate(record):
            if phase:
                if phase not in PHASES or PHASES.index(phase) not in {PHASES.index(record['phase']), PHASES.index(record['phase']) + 1}:
                    raise ValueError('Advance one engagement phase at a time')
                if phase != record['phase']:
                    record['completed_phases'].append(record['phase'])
                record['phase'] = phase
            if steps is not None:
                if not isinstance(steps, list) or len(steps) > 30 or any(not isinstance(step, dict) or not isinstance(step.get('title'), str) or not 0 < len(step['title']) <= 300 or step.get('status') not in {'pending', 'in_progress', 'completed', 'blocked'} for step in steps):
                    raise ValueError('Use a bounded checklist with valid statuses')
                record['steps'] = [{'title': step['title'], 'status': step['status']} for step in steps]
            if phase or steps is not None:
                self.store.append(record, 'workflow_updated', {'phase': record['phase'], 'steps': record['steps']})
            return record
        result = self.store.update(self.engagement_id, mutate)
        return ToolResult(success=True, output=json.dumps(result), data={'phase': result['phase'], 'steps': result['steps']})


class RedTeamImportTool(EngagementTool):
    name = ToolName.REDTEAM_IMPORT
    description = 'Import up to 100 scoped SARIF/Nuclei candidates or HAR/Burp XML observations from a local file under 1 MB. Proxy secrets and bodies are discarded; imported findings remain unverified; no scanner executes.'
    parameters = {'type': 'object', 'properties': {'path': {'type': 'string'}, 'format': {'type': 'string', 'enum': ['sarif', 'nuclei_jsonl', 'har', 'burp_xml']}}, 'required': ['path', 'format'], 'additionalProperties': False}

    async def run(self, path, format):
        record = self.store.load(self.engagement_id)
        file = self.store.resolve_source(record, path)
        if not file.is_file() or file.stat().st_size > 1_000_000:
            raise ValueError('Import requires a scoped file smaller than 1 MB')
        content = file.read_text(encoding='utf-8')
        candidates = []
        if format in {'har','burp_xml'}:
            from local_coder.proxy_history import proxy_observations
            observations = proxy_observations(content, format)
            for observation in observations:
                self.store.check_url(record, observation['url'])
            evidence = self.store.evidence(self.engagement_id, 'proxy_history', {'file':path,'format':format,
                'sha256':hashlib.sha256(content.encode()).hexdigest(),'observations':observations,
                'credentials_and_bodies_retained':False})
            result = {'imported':len(observations),'evidence_id':evidence,'observations':observations}
            return ToolResult(success=True,output=json.dumps(result),data=result)
        if format == 'sarif':
            payload = json.loads(content)
            if payload.get('version') != '2.1.0':
                raise ValueError('Expected SARIF 2.1.0')
            for run in payload.get('runs', []):
                for item in run.get('results', []):
                    for location in item.get('locations', []):
                        physical = location['physicalLocation']
                        asset = unquote(physical['artifactLocation']['uri'])
                        if urlsplit(asset).scheme or physical['artifactLocation'].get('uriBaseId'):
                            raise ValueError('SARIF imports require workspace-relative locations')
                        self.store.resolve_source(record, asset)
                        candidates.append({'title': str(item.get('ruleId', 'SARIF candidate'))[:200], 'asset': asset,
                                           'hypothesis': str(item.get('message', {}).get('text', ''))[:4000],
                                           'line': physical.get('region', {}).get('startLine')})
        elif format == 'nuclei_jsonl':
            for line in content.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                asset = item['matched-at']
                self.store.check_url(record, asset)
                candidates.append({'title': str(item.get('info', {}).get('name', item.get('template-id', 'Nuclei candidate')))[:200],
                                   'asset': asset, 'hypothesis': str(item.get('info', {}).get('description', 'Imported scanner candidate'))[:4000]})
        else:
            raise ValueError('Unsupported scanner format')
        if len(candidates) > 100:
            raise ValueError('Import is limited to 100 candidate locations; narrow the scanner report')
        def save(loaded):
            evidence = self.store.append(loaded, 'scanner_import', {'file': path, 'format': format, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(), 'candidate_count': len(candidates)})
            for candidate in candidates:
                finding = {**candidate, 'evidence_ids': [evidence], 'verification': 'unverified', 'severity': 'info', 'detection_outcome': 'not_tested'}
                identity = 'finding-' + hashlib.sha256((finding['asset'] + '\n' + finding['title'].strip().casefold()).encode()).hexdigest()[:16]
                finding['id'] = identity
                existing = next((f for f in loaded['findings'] if f['id'] == identity), None)
                if existing:
                    existing['evidence_ids'] = sorted(set(existing['evidence_ids'] + [evidence]))
                else:
                    loaded['findings'].append(finding)
            return evidence
        evidence = self.store.update(self.engagement_id, save)
        return ToolResult(success=True, output=f'Imported {len(candidates)} unverified candidates. Evidence ID: {evidence}. Scanner evidence is untrusted.', data={'imported': len(candidates), 'evidence_id': evidence})


def bind_engagement(registry, project_root, engagement_id):
    from local_coder.types import AgentRole
    store = EngagementStore(project_root)
    store.load(engagement_id)
    classes = (RedTeamWorkspaceTool, RedTeamHTTPTool, RedTeamFindingTool, RedTeamWorkflowTool, RedTeamImportTool)
    for cls in classes:
        registry.register(cls(store, engagement_id))
    # This immutable tool capability set is independent of configurable safety tiers.
    from local_coder.tools.exploit import GenerateExploitTool, PrivilegeEscalationAnalysisTool, GenerateFuzzCasesTool
    generated = (GenerateExploitTool, PrivilegeEscalationAnalysisTool, GenerateFuzzCasesTool)
    for cls in generated:
        registry.register(cls(project_root, safety_harness=getattr(registry, "safety_harness", None),
                              approval_callback=getattr(registry, "approval_callback", None), engagement_id=engagement_id))
    registry._permissions[AgentRole.RED_TEAM] = {cls.name for cls in classes + generated} | {ToolName.SECURITY_PLAYBOOK, ToolName.RECOVER_CONTEXT, ToolName.MEMORY_TASK_STATE, ToolName.SKILL_LIBRARY}
