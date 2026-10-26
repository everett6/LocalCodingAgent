"""Human-facing local research controls; no target execution or remote fetches."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from local_coder.harness_api import _integer, _object, _query, _text
from local_coder.workspace import Workspace


class ResearchRequestError(ValueError):
    """Safe, human-facing messages containing no source or credential values."""


class ResearchAPI:
    def _research_store(self):
        from local_coder.security_reports import EvidenceStore
        return EvidenceStore(self.project_root)

    def _research_findings(self, path=None):
        findings, batches = {}, {}
        workspace = Workspace(self.project_root)
        selected = workspace.resolve(path).relative_to(workspace.root) if path else Path('.')
        for record in self._research_store().read(limit=10000):
            data = record.get("data", {})
            if record.get("kind") not in {"static_audit", "sarif_import"}:
                continue
            if record['kind'] == 'static_audit':
                key = ('audit', data.get('scanned_path', '.'), tuple(sorted(data.get('checks', []))))
                if data.get('coverage', {}).get('status') == 'complete':
                    batches[key] = [record]
                else:
                    batches.setdefault(key, []).append(record)
            else:
                batches[('import', record['hash'])] = [record]
        hashes, remaining = {}, 8_000_000
        for record in sorted((item for group in batches.values() for item in group), key=lambda row: row['index']):
            data = record['data']
            for finding in data.get("findings", []):
                finding = dict(finding)
                relative = finding.get('file') or finding.get('path') or finding.get('evidence', {}).get('file')
                located = isinstance(relative, str) and bool(relative)
                if not located and selected != Path('.'):
                    continue
                if located and not Path(relative).is_relative_to(selected):
                    continue
                original_hash = finding.get('source_sha256') or finding.get('evidence', {}).get('source_sha256')
                if located and relative not in hashes and original_hash:
                    try:
                        source = workspace.resolve(relative)
                        if any(part in {'.local-coder', '.git'} for part in source.relative_to(workspace.root).parts) or source.name.startswith('.env'):
                            hashes[relative] = ('unavailable', None)
                        elif not source.is_file():
                            hashes[relative] = ('missing', None)
                        elif source.stat().st_size > min(1_000_000, remaining):
                            hashes[relative] = ('unavailable', None)
                        else:
                            with source.open('rb') as stream:
                                raw = stream.read(min(1_000_001, remaining + 1))
                            remaining -= len(raw)
                            hashes[relative] = ('available', hashlib.sha256(raw).hexdigest()) if len(raw) <= 1_000_000 and remaining >= 0 else ('unavailable', None)
                    except (OSError, ValueError):
                        hashes[relative] = ('unavailable', None)
                state, current = hashes.get(relative, ('not_recorded', None))
                finding['source_status'] = 'not_recorded' if not original_hash else ('unchanged' if original_hash == current else 'changed') if state == 'available' else state
                finding.update(source_sha256=original_hash, audit_record_hash=record['hash'], audit_recorded_at=record['created_at'])
                identity = hashlib.sha256(json.dumps({key: value for key, value in finding.items() if key not in {'audit_record_hash', 'audit_recorded_at'}}, sort_keys=True).encode()).hexdigest()
                findings[identity] = finding
        if len(findings) > 1000:
            raise ResearchRequestError('The report exceeds 1,000 findings. Choose a narrower project path before exporting; no findings were silently omitted.')
        return list(findings.values())

    def _research_get(self, path, query):
        if path == "/research/dependencies":
            _object(query, {"path", "action", "max_manifests", "max_components"})
            from local_coder.tools.dependency_intelligence import DependencyIntelligenceTool
            action = _query(query, "action", "audit")
            if action not in {"inventory", "audit"}:
                raise ValueError("Choose inventory or audit")
            result = asyncio.run(DependencyIntelligenceTool(self.project_root).execute(
                action=action,
                target_path=_text(_query(query, "path", ".")),
                max_manifests=_integer(_query(query, "max_manifests", "50"), 1, 200),
                max_components=_integer(_query(query, "max_components", "2000"), 1, 10000),
            ))
            if not result.success:
                raise ResearchRequestError("Check the project path and supported dependency evidence limits")
            return True, result.data
        if path == "/research/advisories":
            filters = {"ecosystem", "package", "version", "advisory_id"}
            _object(query, filters)
            from local_coder.security_intelligence import LocalAdvisoryStore
            options = {key: _text(_query(query, key, ""), maximum=200) for key in filters if key in query}
            if 'advisory_id' in options and not re.fullmatch(r'(?:CVE-\d{4}-\d{4,}|GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|PYSEC-\d{4}-\d+)', options['advisory_id']):
                raise ValueError('Choose a supported CVE, GHSA or PYSEC identifier')
            return True, LocalAdvisoryStore(self.project_root).query(**options)
        if path == "/research/capabilities":
            _object(query, set())
            from local_coder.research_capabilities import research_capabilities
            return True, research_capabilities()
        if path == "/memory/structure":
            _object(query, {"path", "query", "budget", "limit"})
            target = _query(query, "path", "")
            return True, self.context_memory.structure(
                [_text(target)] if target else None,
                query=_text(_query(query, "query", ""), empty=True),
                char_budget=_integer(_query(query, "budget", "4000"), 0, 12000),
                limit=_integer(_query(query, "limit", "100"), 0, 500),
            )
        if path == "/research/evidence":
            _object(query, set())
            return True, {"integrity": self._research_store().verify(), "records": self._research_store().read(limit=100)}
        if path == "/research/report":
            _object(query, {"format", "path"})
            from local_coder.security_reports import export_sarif, findings_report
            target = _query(query, 'path', '') or '.'
            workspace = Workspace(self.project_root)
            selected = workspace.resolve(target).relative_to(workspace.root)
            findings = self._research_findings(str(selected))
            scoped = selected != Path('.')
            format_ = _query(query, "format", "sarif")
            if format_ == "sarif":
                report = export_sarif(findings)
                report['runs'][0]['properties'] = {'report_scope': {
                    'path': selected.as_posix(),
                    'location_filter_applied': scoped,
                    'unlocated_findings': 'excluded' if scoped else 'included',
                }}
                return True, report
            if format_ == "markdown":
                report = findings_report(findings)
                if scoped:
                    report += '\n\nScope: only findings with recorded file locations inside the selected project path are included. Findings without recorded file locations are excluded; this is not a complete project report.\n'
                return True, {"markdown": report}
            raise ValueError("Choose sarif or markdown")
        return False, None

    def _research_post(self, path, body):
        if path == "/research/advisories/import":
            _object(body, {"dataset", "source_label"})
            from local_coder.security_intelligence import LocalAdvisoryStore
            label = _text(body.get("source_label", "operator-import"), maximum=200)
            with self._harness_idle():
                imported = LocalAdvisoryStore(self.project_root).import_records(body.get("dataset"), source_label=label)
                return True, imported
        if path == "/research/audit":
            _object(body, {"target_path", "checks", "max_files", "max_findings", "max_bytes"})
            from local_coder.tools.advanced_audit import AdvancedAuditTool
            with self._harness_idle():
                result = asyncio.run(AdvancedAuditTool(self.project_root).execute(**body))
                if not result.success:
                    raise ValueError("Check the selected project path and audit limits")
                report = result.data
                evidence = self._research_store().append("static_audit", report)
                return True, {**report, "evidence_hash": evidence["hash"]}
        if path == "/research/sarif/import":
            _object(body, {"report"})
            from local_coder.security_reports import import_sarif
            findings = import_sarif(body.get("report"))
            with self._harness_idle():
                evidence = self._research_store().append("sarif_import", {"findings": findings})
                return True, {"findings": findings, "evidence_hash": evidence["hash"], "verification": "unverified"}
        return False, None
