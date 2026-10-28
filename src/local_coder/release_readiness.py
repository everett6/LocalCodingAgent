"""Read-only release evidence, with no inferred legal or runner-success claims.

Attestations are trusted operator/release-pipeline inputs, not model-written
proof. Source presence and test-file presence are observations, never passes.
The report does not scan credentials, install packages or contact providers.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
import tomllib
from pathlib import Path

MAX_EVIDENCE_BYTES = 1_000_000
COMMERCIAL_GATES = ('licenses', 'brand_clearance', 'update_signing', 'key_storage', 'privacy', 'rollback', 'support')
NATIVE_PLATFORMS = ('linux', 'windows', 'macos')
ITEM_EVIDENCE = {
    15: ('.github/workflows/desktop.yml', '.github/workflows/ide-upstream.yml'),
    48: ('src/local_coder/harness/evaluation.py', 'tests/test_release_50.py'),
    49: ('src/local_coder/harness/improvement.py', 'tests/test_release_50.py'),
    50: ('src/local_coder/release_readiness.py', 'tests/test_release_50.py'),
}


def _local_file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if not relative or rel.is_absolute() or '..' in rel.parts:
        raise ValueError('Evidence must stay inside the project')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Evidence cannot follow symlinks')
    if not current.is_file() or current.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError('Missing or oversized evidence')
    return current


def _observe(root: Path, relative: str) -> dict:
    try:
        path = _local_file(root, relative)
        return {'path': relative, 'status': 'present', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'meaning': 'File presence and digest only; no execution or review inferred'}
    except (ValueError, OSError):
        return {'path': relative, 'status': 'missing'}


def _revision(root: Path) -> str | None:
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=2, check=True)
        value = result.stdout.strip()
        return value if re.fullmatch(r'[a-f0-9]{40,64}', value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _source_tree(root: Path) -> str:
    try:
        result = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal',
                                 '--', '.', ':!release-approval', ':!release-assets'],
                                capture_output=True, text=True, timeout=3, check=True)
        return 'dirty' if result.stdout.strip() else 'clean'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def _attestation(root: Path, value: object, revision: str | None) -> dict:
    result = {'status': 'unverified', 'evidence': [], 'missing_gates': ['Reviewed evidence for the current source revision is required']}
    if not isinstance(value, dict) or not revision or value.get('revision') != revision or value.get('status') != 'verified':
        return result
    reviewer = value.get('reviewed_by')
    if not isinstance(reviewer, str) or not 1 <= len(reviewer) <= 128 or any(ord(c) < 32 for c in reviewer):
        return result
    references = value.get('evidence')
    if not isinstance(references, list) or not 1 <= len(references) <= 20:
        return result
    observations = []
    for ref in references:
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not re.fullmatch(r'[a-f0-9]{64}', str(ref.get('sha256', ''))):
            return result
        observed = _observe(root, ref['path'])
        if observed.get('sha256') != ref['sha256']:
            return result
        observations.append(observed)
    return {'status': 'attested', 'revision': revision, 'reviewed_by': reviewer, 'evidence': observations,
            'missing_gates': [], 'limitation': 'Supplied operator attestation; this program does not independently verify its factual/legal conclusions'}


def _dependency_licenses(root: Path) -> list[dict]:
    """Inspect actual installed metadata/files, preserving missing/unknown licenses."""
    try:
        config = tomllib.loads(_local_file(root, 'pyproject.toml').read_text())
        requirements = config.get('project', {}).get('dependencies', [])[:50]
    except (ValueError, OSError):
        return []
    rows = []
    for requirement in requirements:
        match = re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*', str(requirement))
        if not match:
            continue
        name = match[0]
        row = {'name': name, 'status': 'unverified', 'license_files': []}
        try:
            dist = importlib.metadata.distribution(name)
            row.update(version=dist.version, license_expression=dist.metadata.get('License-Expression'),
                       license_metadata=dist.metadata.get('License'))
            for file in dist.files or []:
                if len(row['license_files']) >= 10:
                    break
                if not re.search(r'(^|/)(licen[cs]e|copying|notice)[^/]*$', str(file), re.I):
                    continue
                path = Path(dist.locate_file(file))
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_EVIDENCE_BYTES:
                    row['license_files'].append({'path': str(file), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            row['status'] = 'metadata_observed' if row['license_files'] else 'license_text_missing'
        except (importlib.metadata.PackageNotFoundError, OSError):
            row['status'] = 'not_installed'
        rows.append(row)
    return rows


def readiness_report(project_root: str | Path) -> dict:
    """Return all exact 50 acceptance items with bounded, non-promotional evidence.

    Optional .local-coder/release/evidence.json is an operator-controlled input:
    each section requires current revision, reviewer, verified status and hashed
    local evidence. Empty state, stale attestations and source files fail closed.
    A release approver must separately review trust in those attestations.
    """
    root = Path(project_root).resolve(strict=True)
    spec = json.loads(Path(__file__).with_name('release_spec.json').read_text())
    revision = _revision(root)
    source_tree = _source_tree(root)
    supplied = {}
    evidence_error = None
    try:
        value = json.loads(_local_file(root, '.local-coder/release/evidence.json').read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid release evidence object')
        supplied = value
    except (ValueError, OSError) as exc:
        evidence_error = type(exc).__name__
    commercial_input = supplied.get('commercial', {})
    commercial_input = commercial_input if isinstance(commercial_input, dict) else {}
    commercial = {name: _attestation(root, commercial_input.get(name), revision) for name in COMMERCIAL_GATES}
    root_license = next((_observe(root, name) for name in ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'COPYING')
                         if _observe(root, name)['status'] == 'present'), {'status': 'missing', 'path': 'LICENSE'})
    license_gate = commercial['licenses']
    license_gate.update(root_license=root_license,
                        bundled_notices=[_observe(root, name) for name in ('ide/UPSTREAM_LICENSE.txt', 'ide/extension/LICENSE.txt', 'docs/references/pentest-harness-LICENSE.txt')],
                        dependencies=_dependency_licenses(root),
                        missing_inventory=['Complete bundled Python/npm/Electron/Code OSS transitive license inventory and review'])
    if root_license['status'] != 'present' or license_gate['missing_inventory'] and license_gate['status'] != 'attested':
        license_gate['status'] = 'unverified'
        license_gate['missing_gates'] = ['Root project license and reviewed complete bundled dependency/license inventory are required']
    native_input = supplied.get('native_runs', {})
    native_input = native_input if isinstance(native_input, dict) else {}
    native_runs = {platform: _attestation(root, native_input.get(platform), revision) for platform in NATIVE_PLATFORMS}
    model_benchmark = _attestation(root, supplied.get('model_benchmark'), revision)
    model_benchmark['limitation'] = 'A supplied report must identify the real model and release-held-out suite; test doubles and offline detector scores cannot establish model quality'
    items_input = supplied.get('items', {})
    items_input = items_input if isinstance(items_input, dict) else {}
    items = []
    for item in spec['items']:
        row = {**item, **_attestation(root, items_input.get(str(item['id'])), revision)}
        row['available_sources'] = [_observe(root, path) for path in ITEM_EVIDENCE.get(item['id'], ())]
        if item['id'] == 15 and any(value['status'] != 'attested' for value in native_runs.values()):
            row.update(status='unverified', missing_gates=['Actual current-revision Linux, Windows and macOS test/build runner evidence'])
        if item['id'] in (48, 49) and model_benchmark['status'] != 'attested':
            row.update(status='unverified', missing_gates=['Actual model benchmark and measured profile comparison evidence'])
        if item['id'] == 50 and any(value['status'] != 'attested' for value in commercial.values()):
            row.update(status='unverified', missing_gates=['Missing commercial release evidence'])
        items.append(row)
    external = [name for name, value in commercial.items() if value['status'] != 'attested']
    external.extend('native_' + name for name, value in native_runs.items() if value['status'] != 'attested')
    if model_benchmark['status'] != 'attested':
        external.append('model_benchmark')
    return {'version': 1, 'ready': source_tree == 'clean' and not external and all(row['status'] == 'attested' for row in items),
            'revision': revision, 'source_tree': source_tree, 'spec_version': spec['version'], 'spec_hash': hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            'items': items, 'commercial': commercial, 'native_runs': native_runs, 'model_benchmark': model_benchmark,
            'missing_external_gates': external, 'evidence_input_error': evidence_error,
            'limitation': 'Attestations require an independent release approver. Presence of implementation/test files does not prove acceptance, security, legal clearance or runner success.'}
