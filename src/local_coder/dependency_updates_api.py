"""Operator-only bounded refresh of individual official OSV records.

No network operation occurs on GET or from model tools. Callers must provide
normal authentication and dispatch this mixin with other human-facing APIs.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import time

import httpx

from local_coder.harness_api import _object
from local_coder.security_intelligence import LocalAdvisoryStore, _json

_MAX_DOWNLOAD = 1_000_000
_SOURCES = [{'id': 'osv-record', 'label': 'Official OSV individual record',
             'origin': 'https://api.osv.dev', 'format': 'OSV'}]


def _fetch_osv(identifier):
    # A fixed origin and ID grammar prevent arbitrary URLs, credential forwarding,
    # redirects and traversal. trust_env=False excludes proxy/netrc credentials.
    url = f'https://api.osv.dev/v1/vulns/{identifier}'
    deadline = time.monotonic() + 20
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False,
                      headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as client:
        with client.stream('GET', url) as response:
            if response.status_code != 200:
                raise ValueError('Official advisory source did not return a record')
            # Reject compressed responses entirely, avoiding decompression bombs.
            if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise ValueError('Compressed advisory responses are unsupported')
            length = response.headers.get('content-length')
            if length and (not length.isdigit() or int(length) > _MAX_DOWNLOAD):
                raise ValueError('Advisory download exceeds the byte limit')
            raw = bytearray()
            for chunk in response.iter_raw(chunk_size=16384):
                if time.monotonic() > deadline or len(raw) + len(chunk) > _MAX_DOWNLOAD:
                    raise ValueError('Advisory download exceeded its time or byte limit')
                raw.extend(chunk)
    record = _json(bytes(raw))
    if not isinstance(record, dict) or record.get('id') != identifier:
        raise ValueError('Official source returned an unexpected record')
    return record, hashlib.sha256(raw).hexdigest()


class DependencyUpdatesAPI:
    def _dependency_updates_get(self, path, query):
        if path != '/research/advisories/updates':
            return False, None
        _object(query, set())
        records, cache_status, provenance = LocalAdvisoryStore(self.project_root)._load()
        return True, {'sources': _SOURCES, 'cache_status': cache_status,
                      'total': len(records), 'legacy_range_records': sum(bool(r['affected_ranges']) and r.get('record_version') != 2 for r in records),
                      'withdrawn': sum(bool(r.get('withdrawn')) for r in records),
                      'provenance': provenance, 'coverage': 'incomplete',
                      'no_match_is_not_safety_evidence': True,
                      'range_support': {'npm': 'bounded comparator sets, caret, tilde and OSV SEMVER intervals',
                                        'pypi': 'packaging PEP 440 adapter' if importlib.util.find_spec('packaging') else 'unavailable; Python ranges remain unresolved'},
                      'automatic_updates': False}

    def _dependency_updates_post(self, path, body):
        if path != '/research/advisories/update':
            return False, None
        _object(body, {'source', 'advisory_id'})
        identifier = body.get('advisory_id')
        if body.get('source') != 'osv-record' or not isinstance(identifier, str) or not re.fullmatch(
                r'(?:CVE-\d{4}-\d{4,}|GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|PYSEC-\d{4}-\d+)', identifier):
            raise ValueError('Choose the official OSV source and a CVE, GHSA or PYSEC identifier')
        with self._harness_idle():
            try:
                record, digest = _fetch_osv(identifier)
            except httpx.HTTPError:
                raise ValueError('Official advisory source is unavailable; cached records were retained') from None
            # The human import path accepts the same v2 OSV envelope.
            result = LocalAdvisoryStore(self.project_root).import_records(
                {'version': 2, 'advisories': [record]},
                source_label=f'Official OSV {identifier} sha256:{digest}')
        return True, {**result, 'source': 'osv-record', 'record_sha256': digest,
                      'coverage': 'incomplete', 'no_match_is_not_safety_evidence': True}
