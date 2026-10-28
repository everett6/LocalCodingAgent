"""Transactional project goals, evidence, decisions and resumable briefings."""
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from local_coder.memory.context_store import ContextMemory, _clean


_UNSET = object()


class GoalLedger:
    def __init__(self, root):
        self.memory = ContextMemory(root)
        with self.memory._connection() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS goals
                (id TEXT PRIMARY KEY, parent TEXT REFERENCES goals(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS goal_events
                (seq INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT REFERENCES goals(id), payload TEXT NOT NULL);''')

    def revision(self):
        def git(*args):
            try:
                result = subprocess.run(['git', '-C', str(self.memory.project_root), *args], capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError('Unable to fingerprint the Git workspace') from exc
            if result.returncode:
                raise ValueError('Goal verification requires a Git workspace')
            return result.stdout
        digest = hashlib.sha256(git('rev-parse', 'HEAD') + git('diff', 'HEAD', '--binary'))
        try:
            for name in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
                if name:
                    path = self.memory.project_root / os.fsdecode(name)
                    if path.is_symlink():
                        digest.update(name + str(path.readlink()).encode())
                    elif path.is_file():
                        digest.update(name + path.read_bytes())
        except OSError as exc:
            raise ValueError('Unable to fingerprint workspace files') from exc
        return digest.hexdigest()

    @staticmethod
    def _proof_id(evidence):
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest() if evidence else None

    @classmethod
    def _current(cls, row, rows, revision, seen=None):
        seen = set() if seen is None else set(seen)
        if row['id'] in seen:
            return False
        seen.add(row['id'])
        proof = row.get('evidence')
        if not isinstance(proof, dict) or proof.get('passed') is not True or proof.get('revision') != revision:
            return False
        if any(item['status'] != 'passed' for item in cls._acceptance(row)):
            return False
        refs = list(row.get('dependencies', [])) + [item['id'] for item in rows.values() if item.get('parent') == row['id']]
        saved = proof.get('upstream', {})
        return all(ref in rows and rows[ref]['status'] == 'completed'
                   and saved.get(ref) == cls._proof_id(rows[ref].get('evidence'))
                   and cls._current(rows[ref], rows, revision, seen) for ref in refs)

    @staticmethod
    def _acceptance(row):
        proof = row.get('evidence') or {}
        qualified = proof.get('acceptance', {})
        checks = proof.get('checks', [])
        return [{'criterion': criterion, 'status': 'passed' if proof.get('passed') is True
                 and qualified.get(criterion) and all(check in checks for check in qualified[criterion]) else 'unresolved',
                 'checks': qualified.get(criterion, [])}
                for criterion in row.get('criteria', '').splitlines() if criterion.strip()]

    def list(self):
        with self.memory._connection() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM goals ORDER BY rowid')]
        try:
            revision = self.revision()
        except ValueError:
            revision = None
        indexed = {row['id']: row for row in rows}
        for row in rows:
            row['stalled'] = row['attempts'] >= 3 and row['status'] != 'completed'
            row['evidence_stale'] = bool(row.get('evidence')) and not self._current(row, indexed, revision)
            row['acceptance'] = self._acceptance(row)
            if row['evidence_stale']:
                for criterion in row['acceptance']:
                    if criterion['status'] == 'passed':
                        criterion['status'] = 'stale'
        return rows

    def get(self, identity):
        return next((r for r in self.list() if r['id'] == identity), None)

    def _write(self, db, row, event):
        row = _clean(row)
        db.execute('INSERT INTO goals VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (row['id'], row.get('parent'), json.dumps(row)))
        db.execute('INSERT INTO goal_events(goal_id,payload) VALUES(?,?)', (row['id'], json.dumps(_clean({
            'type': event, 'time': datetime.now(timezone.utc).isoformat(), 'state': row}))))
        return row

    def create(self, objective, parent=None, criteria='', identity=None, dependencies=None):
        if not isinstance(objective, str) or not 0 < len(objective.strip()) <= 8000:
            raise ValueError('A bounded objective is required')
        if not isinstance(criteria, str) or len(criteria) > 8000:
            raise ValueError('Acceptance criteria are too long')
        dependencies = dependencies or []
        if not isinstance(dependencies, list) or len(dependencies) > 50:
            raise ValueError('Invalid dependencies')
        with self.memory._connection() as db:
            for ref in ([parent] if parent else []) + dependencies:
                if not isinstance(ref, str) or not db.execute('SELECT 1 FROM goals WHERE id=?', (ref,)).fetchone():
                    raise ValueError('Unknown parent or dependency')
            identity = identity or uuid.uuid4().hex
            existing = db.execute('SELECT payload FROM goals WHERE id=?', (identity,)).fetchone()
            if existing:
                return json.loads(existing[0])
            return self._write(db, {'id': identity, 'parent': parent, 'objective': objective, 'criteria': criteria,
                'dependencies': dependencies, 'status': 'pending', 'next_action': objective, 'blocker': '',
                'attempts': 0, 'decisions': [], 'evidence': None}, 'created')

    def update(self, identity, changes, *, evidence=_UNSET):
        allowed = {'status', 'next_action', 'blocker', 'decision'}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError('Unknown goal fields')
        if any(not isinstance(v, str) or len(v) > 8000 for v in changes.values()):
            raise ValueError('Goal fields must be bounded text')
        with self.memory._connection() as db:
            found = db.execute('SELECT payload FROM goals WHERE id=?', (identity,)).fetchone()
            if not found:
                raise ValueError('Unknown goal')
            row = json.loads(found[0])
            rows = {item['id']: item for item in
                    (json.loads(result[0]) for result in db.execute('SELECT payload FROM goals'))}
            status = changes.get('status', row['status'])
            if status not in {'pending', 'ready', 'running', 'blocked', 'verifying', 'completed'}:
                raise ValueError('Invalid goal status')
            revision = None

            def current_evidence(item):
                nonlocal revision
                if revision is None:
                    revision = self.revision()
                return self._current(item, rows, revision)

            if status in {'running', 'completed'}:
                for dep in row['dependencies']:
                    other = json.loads(db.execute('SELECT payload FROM goals WHERE id=?', (dep,)).fetchone()[0])
                    if other['status'] != 'completed' or not current_evidence(other):
                        raise ValueError('Dependency needs current completion evidence')
            if evidence is not _UNSET:
                row['evidence'] = evidence
                if evidence is not None:
                    refs = row['dependencies'] + [item['id'] for item in rows.values() if item.get('parent') == identity]
                    row['evidence'] = {**evidence, 'generation': uuid.uuid4().hex,
                                      'upstream': {ref: self._proof_id(rows[ref].get('evidence')) for ref in refs}}
            elif status == 'running' and row['status'] != 'running':
                row['evidence'] = None
            if status == 'completed':
                children = [json.loads(r[0]) for r in db.execute('SELECT payload FROM goals WHERE parent=?', (identity,))]
                if children:
                    if not all(c['status'] == 'completed' and current_evidence(c) for c in children):
                        raise ValueError('Complete and verify child tasks first')
                    if evidence is _UNSET and not current_evidence(row):
                        row['evidence'] = {'passed': True, 'revision': revision,
                                           'checks': [check for c in children for check in c['evidence']['checks']],
                                           'generation': uuid.uuid4().hex,
                                           'upstream': {ref: self._proof_id(rows[ref].get('evidence'))
                                                        for ref in row['dependencies'] + [c['id'] for c in children]}}
                if any(item['status'] != 'passed' for item in self._acceptance(row)):
                    raise ValueError('Acceptance criteria need qualified observed checks; unresolved criteria remain')
                if not current_evidence(row):
                    raise ValueError('Completion requires passing checks for the current workspace')
            if status == 'running' and row['status'] != 'running':
                row['attempts'] += 1
            if 'decision' in changes:
                row['decisions'] = (row['decisions'] + [changes['decision']])[-50:]
            row.update({k: v for k, v in changes.items() if k != 'decision'})
            return self._write(db, row, 'updated')

    def finish(self, response, *, verification_revision=_UNSET, acceptance_checks=None):
        evidence = None
        acceptance_checks = {} if acceptance_checks is None else acceptance_checks
        if (not isinstance(acceptance_checks, dict) or len(acceptance_checks) > 100
            or any(not isinstance(key, str) or not isinstance(checks, list) or not checks
                   or any(not isinstance(check, str) or check not in response.tests_run for check in checks)
                   for key, checks in acceptance_checks.items())):
            raise ValueError('Acceptance qualification must reference observed checks')
        if response.tests_run:
            try:
                revision = self.revision() if verification_revision is _UNSET else verification_revision
                if revision is not None:
                    evidence = {'checks': response.tests_run, 'passed': response.tests_passed, 'revision': revision,
                                'acceptance': acceptance_checks}
            except ValueError:
                pass
        status = 'verifying' if response.status.value == 'completed' else 'blocked'
        self.update(response.task_id, {'status': status, 'next_action': response.summary[:8000],
                    'blocker': '' if status == 'verifying' else response.summary[:8000]}, evidence=evidence)
        if status == 'verifying' and evidence and evidence['passed']:
            try:
                self.update(response.task_id, {'status': 'completed'})
            except ValueError as exc:
                self.update(response.task_id, {'status': 'verifying', 'blocker': str(exc)[:8000]})

    def briefing(self, goal_id=None):
        rows = self.list()
        if goal_id:
            selected = {goal_id}
            for _ in range(len(rows)):
                selected.update(r['id'] for r in rows if r['parent'] in selected)
                selected.update(dep for r in rows if r['id'] in selected for dep in r['dependencies'])
            rows = [r for r in rows if r['id'] in selected]
        return '\n'.join(f"{r['id']}: {r['objective']} [{r['status']}{', evidence stale' if r['evidence_stale'] else ''}] "
                         f"Criteria: {r['criteria']} Decisions: {r['decisions'][-3:]} "
                         f"{'Review stalled plan. ' if r['stalled'] else ''}Next: {r['next_action']} Blocker: {r['blocker']}"
                         for r in rows if r['status'] != 'completed' or r['evidence_stale'] or r['id'] == goal_id)[:6000]
