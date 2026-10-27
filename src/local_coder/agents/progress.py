"""Bounded evidence fingerprints for repeated successful investigation calls."""
from __future__ import annotations

import hashlib
import json


class ProgressGuard:
    """Detect repeated observations without retaining tool output or credentials.

    A changed workspace revision or observation is new evidence. This does not
    infer correctness from a successful exit status or from a model's prose.
    """

    def __init__(self, snapshot: dict | None = None):
        snapshot = snapshot or {}
        self.observations = [value for value in snapshot.get('observations', [])[-32:]
                             if isinstance(value, str) and len(value) == 64]
        count = snapshot.get('repeats', 0)
        self.repeats = max(0, min(count, 100)) if type(count) is int else 0

    def observe(self, name, arguments, output, *, success, revision=None):
        raw = json.dumps([name, arguments, output, revision], sort_keys=True, default=str)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        repeated = success and fingerprint in self.observations
        self.repeats = self.repeats + 1 if repeated else 0
        self.observations = (self.observations + [fingerprint])[-32:]
        action = 'stop' if self.repeats >= 8 else 'recover' if self.repeats >= 3 else 'continue'
        return {'action': action, 'repeats': self.repeats, 'fingerprint': fingerprint,
                'reason': 'Repeated observations without new evidence' if repeated else 'New observation'}

    def snapshot(self):
        return {'observations': self.observations[-32:], 'repeats': self.repeats}
