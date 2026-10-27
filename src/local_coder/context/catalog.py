"""Deterministic schema selection; discovery cannot extend role permissions."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any


def select_tool_catalog(schemas: Sequence[dict[str, Any]], *, role: str, task: str,
                        char_budget: int, requested_names: Sequence[str] = (),
                        preferred_names: Sequence[str] = ()) -> dict[str, Any]:
    """Select whole schemas under a serialized ceiling, from permitted inputs only.

    A caller implements discovery by resubmitting explicit requested_names to this
    function. Selection grants no execution authority; registry checks still apply.
    """
    cap = max(2, min(int(char_budget), 200000))
    permitted = {item['function']['name']: item for item in schemas
                 if isinstance(item, dict) and isinstance(item.get('function'), dict)
                 and isinstance(item['function'].get('name'), str)}
    requested = set(list(requested_names)[:128]) & permitted.keys()
    preferred = {name: index for index, name in enumerate(list(preferred_names)[:128]) if name in permitted}
    words = set(re.findall(r'[a-z]+', task[:8000].lower()))
    core = {name: index for index, name in enumerate(('read_file', 'code_search', 'language_context', 'list_files', 'search_files'))}
    essential = {name: index for index, name in enumerate(('redteam_workflow', 'redteam_workspace', 'redteam_read_source', 'redteam_record_finding'))} if role == 'red_team' else {}
    if role in {'security', 'reviewer', 'exploit_validator'}:
        essential = {name: index for index, name in enumerate(('record_finding', 'read_file', 'code_search', 'run_sast'))}
    role_words = set(re.findall(r'[a-z]+', role[:100].lower()))

    def priority(name: str) -> tuple[int, int, str]:
        parts = set(name.split('_'))
        relevance = len(parts & words) + len(parts & role_words)
        if 'test' in words or 'tests' in words:
            relevance += 3 * (name == 'run_tests')
        if name == 'tool_catalog':
            return (-3, 0, name)
        if name in requested:
            return (-2, -relevance, name)
        if name in essential:
            return (-1, essential[name], name)
        if name in preferred:
            return (0, preferred[name], name)
        if name in core:
            return (1, core[name], name)
        return (2 if relevance else 3, -relevance, name)

    selected, reasons, omitted = [], {}, []
    for name in sorted(permitted, key=priority):
        schema = permitted[name]
        if len(json.dumps(selected + [schema])) <= cap:
            selected.append(schema)
            reasons[name] = 'explicit permitted discovery' if name in requested else 'role essential' if name in essential else 'core context' if name in core else 'task/role match' if priority(name)[0] == 1 else 'remaining capacity'
        else:
            omitted.append({'name': name, 'reason': 'schema character budget'})
    return {'schemas': selected, 'permitted_names': sorted(permitted), 'omitted': omitted,
            'reasons': reasons, 'char_budget': cap, 'discovery': 'explicit permitted tool name'}
