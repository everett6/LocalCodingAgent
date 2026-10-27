"""Reproducible bounded fuzzing of a fixed, inert local fixture.

No user callback, project import, process, endpoint, network or model is invoked.
The discrepancy is intentionally synthetic; it never verifies a project finding.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

from local_coder.statedir import write_state_file


def run_fixture_fuzz(root: str | Path, *, seed: int = 0, max_cases: int = 12,
                     max_steps: int = 32) -> dict:
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError('seed must be an unsigned 32-bit integer')
    if type(max_cases) is not int or not 1 <= max_cases <= 64:
        raise ValueError('max_cases must be from 1 through 64')
    if type(max_steps) is not int or not 1 <= max_steps <= 128:
        raise ValueError('max_steps must be from 1 through 128')
    # A fixed legacy truthiness fixture disagrees with a strict decimal policy
    # for zero strings. Values are inert and never used as code or destinations.
    rng = random.Random(seed)
    cases = ['00'] + [''.join(rng.choice('0123456789') for _ in range(rng.randint(1, 16)))
                       for _ in range(max_cases - 1)]

    def discrepancy(value: str) -> bool:
        return value.isdecimal() and bool(value) != (int(value) > 0)

    counterexamples = []
    remaining = max_steps
    for index, value in enumerate(cases):
        if not discrepancy(value):
            continue
        minimized = value
        position = 0
        while position < len(minimized) and remaining:
            candidate = minimized[:position] + minimized[position + 1:]
            remaining -= 1
            if discrepancy(candidate):
                minimized, position = candidate, 0
            else:
                position += 1
        counterexamples.append({'case': index, 'input': value, 'minimized': minimized,
                                'one_character_minimal': len(minimized) == 1,
                                'property': 'legacy truthiness equals strictly positive integer policy'})
    report = {'version': 1, 'seed': seed, 'generator': 'python_random_decimal_v1',
              'target_scope': 'fixed_synthetic_fixture', 'target': 'legacy_decimal_truthiness',
              'executed_cases': len(cases), 'cases': cases, 'max_steps': max_steps,
              'minimization_steps': max_steps - remaining, 'counterexamples': counterexamples,
              'negative_controls': {'status': 'passed' if not discrepancy('1') and not discrepancy('12') else 'failed',
                                    'inputs': ['1', '12']},
              'verification': 'synthetic_observation_only',
              'gaps': ['Project parsers are not executed; separately isolated project regression is required.']}
    digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    report['archive'] = f'.local-coder/security/fuzz/{digest}.json'
    write_state_file(root, report['archive'], json.dumps(report, sort_keys=True))
    return report
