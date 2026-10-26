"""Model-facing, read-only offline dependency evidence tool."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from local_coder.security_intelligence import DependencyInventory, LocalAdvisoryStore
from local_coder.tools.base import Tool
from local_coder.types import ToolName, ToolResult


_MAX_AUDIT_RECORDS = 10000
_MAX_AUDIT_COMPARISONS = 100000


class DependencyIntelligenceTool(Tool):
    name = ToolName.DEPENDENCY_INTELLIGENCE
    description = 'Read-only offline npm/Python dependency inventory, operator-imported advisory lookup, and exact-version advisory audit. Does not install, fetch, resolve versions, execute code, or establish that dependencies are safe. Ranges and markers remain unresolved.'
    parameters = {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['inventory', 'query', 'audit'], 'default': 'inventory'},
        'target_path': {'type': 'string', 'default': '.'},
        'ecosystem': {'type': 'string', 'enum': ['pypi', 'npm']},
        'package': {'type': 'string'}, 'version': {'type': 'string'}, 'advisory_id': {'type': 'string'},
        'max_manifests': {'type': 'integer', 'minimum': 1, 'maximum': 200, 'default': 50},
        'max_components': {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 2000},
    }, 'additionalProperties': False}

    def __init__(self, project_root: str):
        self.inventory = DependencyInventory(project_root)
        self.advisories = LocalAdvisoryStore(project_root)

    async def execute(self, action='inventory', target_path='.', ecosystem=None, package=None,
                      version=None, advisory_id=None, max_manifests=50, max_components=2000,
                      **kwargs: Any) -> ToolResult:
        try:
            if kwargs or action not in {'inventory', 'query', 'audit'}:
                raise ValueError('Choose inventory, query, or audit; advisory imports require the human-facing API')
            data = await asyncio.to_thread(self._execute, action, target_path, ecosystem, package, version, advisory_id, max_manifests, max_components)
            return ToolResult(success=True, output=json.dumps(data, ensure_ascii=False), data=data)
        except (ValueError, OSError, TypeError, RecursionError):
            return ToolResult(success=False, error='Invalid or unavailable local dependency evidence; choose supported arguments and workspace-contained regular files')

    def _execute(self, action, target, ecosystem, package, version, advisory_id, max_manifests, max_components):
        if action == 'query':
            return self.advisories.query(ecosystem, package, version, advisory_id)
        if any(value is not None for value in (ecosystem, package, version, advisory_id)):
            raise ValueError('Lookup filters apply only to query actions')
        data = self.inventory.scan(target, max_manifests, max_components)
        if action == 'inventory':
            return data
        records, cache_status, provenance = self.advisories._load()
        indexed = {}
        for record in records:
            indexed.setdefault((record['ecosystem'], record['package']), []).append(record)
        matches = []
        unresolved = list(data['unresolved'][:_MAX_AUDIT_RECORDS])
        truncated = len(data['unresolved']) > _MAX_AUDIT_RECORDS
        comparisons = 0
        for component in data['components']:
            if truncated:
                break
            if component['version'] is None:
                continue
            relevant = indexed.get((component['ecosystem'], component['name']), [])
            if not relevant:
                if len(matches) + len(unresolved) >= _MAX_AUDIT_RECORDS:
                    truncated = True
                    break
                unresolved.append({'component_id': component['id'], 'reason': 'Package absent from imported advisory cache'})
                continue
            for record in relevant:
                comparisons += 1
                if comparisons > _MAX_AUDIT_COMPARISONS:
                    truncated = True
                    break
                if record['affected_ranges']:
                    if len(matches) + len(unresolved) >= _MAX_AUDIT_RECORDS:
                        truncated = True
                        break
                    unresolved.append({'id': record['id'], 'component_id': component['id'], 'reason': 'Imported affected ranges are not evaluated'})
                if component['version'] in record['versions']:
                    if len(matches) + len(unresolved) >= _MAX_AUDIT_RECORDS:
                        truncated = True
                        break
                    matches.append({**record, 'component_id': component['id'], 'conditional': component['conditional'], 'match_type': 'exact_imported_version', 'verification': 'unverified'})
        data['coverage']['limits'].update(max_audit_records=_MAX_AUDIT_RECORDS, max_advisory_comparisons=_MAX_AUDIT_COMPARISONS)
        if truncated:
            data['coverage']['status'] = 'partial'
            data['coverage']['skipped'].append('Advisory audit record or comparison budget reached; narrow the dependency scope')
        data.update(matches=matches, unresolved=unresolved, cache_status=cache_status, advisory_provenance=provenance, no_match_is_not_safety_evidence=True)
        if unresolved or cache_status == 'cache_absent':
            data['coverage']['status'] = 'partial'
        return data
