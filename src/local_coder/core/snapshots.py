"""Private pre-edit file snapshots with compare-and-restore conflict checks."""
from __future__ import annotations
import difflib
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid
from local_coder.workspace import Workspace


def _hash(data):return hashlib.sha256(data).hexdigest() if data is not None else None


class FileSnapshots:
    def __init__(self,project_root):
        self.workspace=Workspace(project_root);self.root=self.workspace.root
        from local_coder.statedir import state_path
        self.storage=state_path(project_root,'.local-coder/file-snapshots/.keep').parent
        if self.storage.is_symlink():raise ValueError('Snapshot directory cannot be a symlink')
        self.storage.mkdir(parents=True,exist_ok=True,mode=0o700);self.storage.chmod(0o700)
        self._lock=threading.RLock()
    def _target(self,path):
        target=self.workspace.resolve(path);relative=target.relative_to(self.root)
        if not relative.parts or relative.parts[0] in {'.git','.local-coder'}:raise ValueError('Protected state is not an editable file')
        if (self.root/Path(path)).is_symlink():raise ValueError('Snapshot targets cannot be symlinks')
        if target.exists() and not target.is_file():raise ValueError('Snapshot target must be a file')
        if target.exists() and target.stat().st_size>2_000_000:raise ValueError('Snapshot file exceeds 2 MB limit')
        return target
    def _directory(self,identity):
        if not isinstance(identity,str) or len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):raise ValueError('Invalid snapshot ID')
        directory=self.storage/identity
        if directory.is_symlink() or directory.resolve().parent!=self.storage:raise ValueError('Invalid snapshot path')
        return directory
    def _read(self,identity):
        file=self._directory(identity)/'metadata.json'
        if file.is_symlink() or file.stat().st_size>1_000_000:raise ValueError('Invalid snapshot metadata')
        record=json.loads(file.read_text())
        if record.get('id')!=identity or not isinstance(record.get('files'),list) or len(record['files'])>50:raise ValueError('Invalid snapshot metadata')
        for item in record['files']:
            if item.get('blob') is not None and (not isinstance(item['blob'],str) or not item['blob'].isdigit() or int(item['blob'])>49):raise ValueError('Invalid snapshot blob')
        return record
    def _save(self,record):
        directory=self._directory(record['id']);temporary=directory/('metadata.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as output:json.dump(record,output)
        os.replace(temporary,directory/'metadata.json')
    def capture(self,paths):
        if not isinstance(paths,list) or not 1<=len(paths)<=50:raise ValueError('Snapshot requires 1-50 file paths')
        with self._lock:
            targets={str(self._target(path).relative_to(self.root)):self._target(path) for path in paths}
            existing=sum(p.stat().st_size for p in self.storage.rglob('*') if p.is_file() and not p.is_symlink())
            added=sum(target.stat().st_size if target.exists() else 0 for target in targets.values())+64000
            if existing+added>256_000_000:raise ValueError('Snapshot storage limit reached')
            record={'id':uuid.uuid4().hex,'created_at':time.time(),'status':'captured','paths':list(targets),'files':[]}
            directory=self._directory(record['id']);directory.mkdir(mode=0o700)
            for index,(relative,target) in enumerate(targets.items()):
                before=target.read_bytes() if target.exists() else None
                if before is not None:
                    fd=os.open(directory/str(index),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as output:output.write(before)
                record['files'].append({'path':relative,'blob':str(index) if before is not None else None,
                                        'before_hash':_hash(before),'after_hash':None,'mode':target.stat().st_mode&0o777 if target.exists() else 0o644})
            self._save(record);return {k:v for k,v in record.items() if k!='files'}
    def complete(self,identity):
        with self._lock:
            record=self._read(identity)
            for item in record['files']:
                target=self._target(item['path']);item['after_hash']=_hash(target.read_bytes() if target.exists() else None)
            record['status']='completed';self._save(record)
    def list(self):
        records=[]
        for directory in self.storage.iterdir():
            try:
                record=self._read(directory.name);records.append({k:v for k,v in record.items() if k!='files'})
            except (OSError,ValueError,KeyError):continue
        return sorted(records,key=lambda r:r['created_at'],reverse=True)[:100]
    def diff(self,identity):
        record=self._read(identity);files=[];diffs=[]
        for item in record['files']:
            target=self._target(item['path']);blob=self._directory(identity)/str(item['blob'])
            if item['blob'] is not None and (blob.is_symlink() or blob.stat().st_size>2_000_000):raise ValueError('Invalid snapshot blob')
            before=blob.read_bytes() if item['blob'] is not None else b''
            if item['blob'] is not None and _hash(before)!=item['before_hash']:raise ValueError('Snapshot integrity check failed')
            after=target.read_bytes() if target.exists() else b''
            old=before.decode('utf-8',errors='replace');new=after.decode('utf-8',errors='replace')
            files.append({'path':item['path'],'before':old[:200000],'after':new[:200000]})
            diffs.append(''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='before/'+item['path'],tofile='after/'+item['path'])))
        return {'id':identity,'files':files,'diff':'\n'.join(diffs)[:500000]}
    def restore(self,identity):
        with self._lock:
            record=self._read(identity)
            if record['status']!='completed':raise ValueError('Only completed snapshots can be restored')
            ready=[]
            for item in record['files']:
                target=self._target(item['path'])
                if _hash(target.read_bytes() if target.exists() else None)!=item['after_hash']:raise ValueError('File changed since agent edit; refusing to overwrite user work')
                before=None
                if item['blob'] is not None:
                    blob=self._directory(identity)/item['blob']
                    if blob.is_symlink():raise ValueError('Invalid snapshot blob')
                    before=blob.read_bytes()
                    if _hash(before)!=item['before_hash']:raise ValueError('Snapshot integrity check failed')
                ready.append((target,before,item['mode']))
            for target,before,mode in ready:
                if before is None:target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True)
                    temporary=target.with_name(target.name+'.restore-'+uuid.uuid4().hex)
                    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode)
                    with os.fdopen(fd,'wb') as output:output.write(before)
                    os.replace(temporary,target)
            record['status']='restored';self._save(record)
            return {'restored':identity,'paths':record['paths']}
