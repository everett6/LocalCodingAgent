"""Explicit approved release gate. No signing credentials or publishing here.

The environment supplies only a trusted public verification key/fingerprint.
An independent release approver supplies current-revision hashed attestations
and a detached signature over the artifact/evidence manifest. This gate never
creates keys, changes user GPG state, signs artifacts or infers legal clearance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from local_coder.release_readiness import readiness_report
from local_coder.statedir import write_state_file


def _read(path: Path, maximum: int = 1_000_000) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('Missing, linked or oversized release evidence')
    return path.read_bytes()


def verify_release(root: Path, approval: Path, assets: Path) -> dict:
    public_key = os.environ.get('RELEASE_SIGNING_PUBLIC_KEY', '')
    fingerprint = os.environ.get('RELEASE_SIGNING_FINGERPRINT', '').upper()
    if not public_key or not re.fullmatch(r'[A-F0-9]{40,64}', fingerprint):
        raise ValueError('Trusted release signing public key and fingerprint are required')
    if len(public_key) > 100_000 or not public_key.startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----') or 'PRIVATE KEY' in public_key:
        raise ValueError('Only a public signing verification key is accepted')
    manifest_path = approval / 'ide-linux-x64.json'
    manifest_bytes = _read(manifest_path)
    evidence_bytes = _read(approval / 'evidence.json')
    _read(approval / 'ide-linux-x64.json.asc', 100_000)
    manifest = json.loads(manifest_bytes)
    built_manifest = json.loads(_read(assets / 'ide-linux-x64.json'))
    if not isinstance(manifest, dict) or not isinstance(built_manifest, dict) or any(manifest.get(key) != value for key, value in built_manifest.items()):
        raise ValueError('Approved manifest must match the built artifact manifest')
    if not isinstance(manifest, dict) or not re.fullmatch(r'ide-vscode-[A-Za-z0-9.+-]{1,128}', str(manifest.get('tag', ''))):
        raise ValueError('Invalid release manifest tag')
    if manifest.get('platform') != 'linux' or manifest.get('arch') != 'x64' or manifest.get('asset') != 'local-coder-ide-linux-x64.tar.gz':
        raise ValueError('Unexpected native release target')
    revision = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    if manifest.get('revision') != revision or os.environ.get('GITHUB_SHA', revision) != revision:
        raise ValueError('Release manifest must match the checked-out revision')
    if manifest.get('readiness_evidence_sha256') != hashlib.sha256(evidence_bytes).hexdigest():
        raise ValueError('Release signature must bind the readiness evidence digest')
    artifact = assets / manifest['asset']
    if artifact.is_symlink() or not artifact.is_file() or artifact.stat().st_size > 2_000_000_000:
        raise ValueError('Invalid release artifact')
    digest = hashlib.sha256()
    with artifact.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if manifest.get('sha256') != digest.hexdigest():
        raise ValueError('Release artifact digest mismatch')
    with tempfile.TemporaryDirectory(prefix='clin-release-verification-') as directory:
        os.chmod(directory, 0o700)
        command = ['gpg', '--no-options', '--homedir', directory, '--batch', '--no-auto-key-retrieve']
        try:
            inspected = subprocess.run(command + ['--with-colons', '--import-options', 'show-only', '--import'], input=public_key, text=True, capture_output=True, check=True, timeout=10)
            primary = re.search(r'^fpr:::::::::([A-F0-9]+):', inspected.stdout, re.M)
            if primary is None or primary[1] != fingerprint:
                raise ValueError('Signing public key fingerprint mismatch')
            subprocess.run(command + ['--import'], input=public_key, text=True, capture_output=True, check=True, timeout=10)
            verified = subprocess.run(command + ['--status-fd=1', '--verify', str(approval / 'ide-linux-x64.json.asc'), str(manifest_path)], text=True, capture_output=True, check=True, timeout=10)
            signatures = [line.split() for line in verified.stdout.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
            if not any(fields[2] == fingerprint or fields[-1] == fingerprint for fields in signatures):
                raise ValueError('A valid signature by the trusted release key is required')
        except (OSError, subprocess.SubprocessError):
            raise ValueError('Release signature verification failed') from None
    # Only verified, manifest-bound evidence enters project-local readiness state.
    write_state_file(root, '.local-coder/release/evidence.json', evidence_bytes.decode())
    report = readiness_report(root)
    if not report['ready']:
        raise ValueError('Commercial, native, benchmark and all 50 acceptance attestations must pass the readiness gate')
    return {'tag': manifest['tag'], 'revision': revision, 'artifact_sha256': digest.hexdigest(), 'signing_fingerprint': fingerprint}


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: verify-release.py APPROVAL_DIRECTORY ASSET_DIRECTORY')
    try:
        print(json.dumps(verify_release(Path.cwd(), Path(sys.argv[1]), Path(sys.argv[2]))))
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit('Release blocked: review evidence, source revision and trusted artifact signature') from None
