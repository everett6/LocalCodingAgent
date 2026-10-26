"""Transactional project goals, evidence, decisions and resumable briefings."""
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from local_coder.memory.context_store import ContextMemory, _clean


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
            result = subprocess.run(['git', '-C', str(self.memory.project_root), *args], capture_output=True, timeout=5)
            if result.returncode:
                raise ValueError('Goal verification requires a Git workspace')
            return result.stdout
        digest = hashlib.sha256(git('rev-parse', 'HEAD') + git('diff', 'HEAD', '--binary'))
        for name in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if name:
                path = self.memory.project_root / name.decode()
                if path.is_symlink():
                    digest.update(name + str(path.readlink()).encode())
                elif path.is_file():
                    digest.update(name + path.read_bytes())
        return digest.hexdigest()

    def list(self):
        with self.memory._connection() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM goals ORDER BY rowid')]
        try:
            revision = self.revision()
        except ValueError:
            revision = None
        for row in rows:
            row['stalled'] = row['attempts'] >= 3 and row['status'] != 'completed'
            row['evidence_stale'] = bool(row.get('evidence')) and row['evidence']['revision'] != revision
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

    def update(self, identity, changes, *, evidence=None):
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
            status = changes.get('status', row['status'])
            if status not in {'pending', 'ready', 'running', 'blocked', 'verifying', 'completed'}:
                raise ValueError('Invalid goal status')
            if status in {'running', 'completed'}:
                for dep in row['dependencies']:
                    other = json.loads(db.execute('SELECT payload FROM goals WHERE id=?', (dep,)).fetchone()[0])
                    if other['status'] != 'completed' or not other.get('evidence') or other['evidence']['revision'] != self.revision():
                        raise ValueError('Dependency needs current completion evidence')
            if evidence:
                row['evidence'] = evidence
            if status == 'completed' and not row.get('evidence'):
                children = [json.loads(r[0]) for r in db.execute('SELECT payload FROM goals WHERE parent=?', (identity,))]
                if children and all(c['status'] == 'completed' and c.get('evidence') and c['evidence']['passed'] and c['evidence']['revision'] == self.revision() for c in children):
                    row['evidence'] = {'passed': True, 'revision': self.revision(),
                                       'checks': [check for c in children for check in c['evidence']['checks']]}
            if status == 'completed' and (not row.get('evidence') or not row['evidence']['passed'] or row['evidence']['revision'] != self.revision()):
                raise ValueError('Completion requires passing checks for the current workspace')
            if status == 'completed':
                for child in db.execute('SELECT payload FROM goals WHERE parent=?', (identity,)):
                    item = json.loads(child[0])
                    if item['status'] != 'completed' or not item.get('evidence') or item['evidence']['revision'] != self.revision():
                        raise ValueError('Complete and verify child tasks first')
            if status == 'running' and row['status'] != 'running':
                row['attempts'] += 1
            if 'decision' in changes:
                row['decisions'] = (row['decisions'] + [changes['decision']])[-50:]
            row.update({k: v for k, v in changes.items() if k != 'decision'})
            return self._write(db, row, 'updated')

    def finish(self, response):
        evidence = None
        if response.tests_run:
            try:
                evidence = {'checks': response.tests_run, 'passed': response.tests_passed, 'revision': self.revision()}
            except ValueError:
                pass
        status = 'verifying' if response.status.value == 'completed' else 'blocked'
        self.update(response.task_id, {'status': status, 'next_action': response.summary[:8000],
                    'blocker': '' if status == 'verifying' else response.summary[:8000]}, evidence=evidence)
        if status == 'verifying' and evidence and evidence['passed']:
            self.update(response.task_id, {'status': 'completed'})

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
