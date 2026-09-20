"""Local Git-backed checkpoints for reversible agent edits."""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from local_coder.workspace import Workspace


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    created_at: str
    head: str
    patch_file: str
    untracked_dir: str


class CheckpointManager:
    """Create and restore snapshots of a Git working tree."""

    def __init__(self, project_root: str, state_dir: str = ".local-coder/checkpoints"):
        self.workspace = Workspace(project_root)
        self.root = self.workspace.root
        self.storage = self.workspace.resolve(state_dir)
        self.storage.mkdir(parents=True, exist_ok=True)

    def create(self) -> Checkpoint:
        """Save tracked changes and untracked files without changing Git state."""
        self._git(["rev-parse", "HEAD"])
        checkpoint_id = f"cp_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        checkpoint_dir = self.storage / checkpoint_id
        untracked_dir = checkpoint_dir / "untracked"
        untracked_dir.mkdir(parents=True)

        patch_file = checkpoint_dir / "working-tree.patch"
        diff = self._git_bytes(["diff", "--binary", "HEAD"])
        patch_file.write_bytes(diff)

        storage_relative = str(self.storage.relative_to(self.root))
        untracked = [
            relative
            for relative in self._git(["ls-files", "--others", "--exclude-standard"]).splitlines()
            if relative != storage_relative and not relative.startswith(f"{storage_relative}/")
        ]
        for relative in untracked:
            source = self.workspace.resolve(relative)
            destination = untracked_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, destination)

        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            head=self._git(["rev-parse", "HEAD"]),
            patch_file=str(patch_file.relative_to(self.root)),
            untracked_dir=str(untracked_dir.relative_to(self.root)),
        )
        (checkpoint_dir / "metadata.json").write_text(
            json.dumps(asdict(checkpoint), indent=2) + "\n",
            encoding="utf-8",
        )
        return checkpoint

    def list(self) -> list[Checkpoint]:
        """List checkpoints from newest to oldest."""
        checkpoints = []
        for metadata in self.storage.glob("*/metadata.json"):
            checkpoints.append(Checkpoint(**json.loads(metadata.read_text(encoding="utf-8"))))
        return sorted(checkpoints, key=lambda item: item.created_at, reverse=True)

    def get(self, checkpoint_id: str) -> Checkpoint:
        """Load a checkpoint by ID."""
        checkpoint_path = self.storage / checkpoint_id / "metadata.json"
        if not checkpoint_path.is_file():
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")
        return Checkpoint(**json.loads(checkpoint_path.read_text(encoding="utf-8")))

    def rollback(self, checkpoint_id: str) -> Checkpoint:
        """Restore a checkpoint, replacing current tracked and untracked changes."""
        checkpoint = self.get(checkpoint_id)
        self._git(["reset", "--hard", checkpoint.head])
        storage_relative = str(self.storage.relative_to(self.root))
        self._git(["clean", "-fd", "-e", f"/{storage_relative}/"])

        patch_path = self.workspace.resolve(checkpoint.patch_file)
        if patch_path.stat().st_size:
            self._run(["git", "apply", "--binary", str(patch_path)], check=True)

        untracked_dir = self.workspace.resolve(checkpoint.untracked_dir)
        if untracked_dir.is_dir():
            for source in untracked_dir.rglob("*"):
                if source.is_file():
                    relative = source.relative_to(untracked_dir)
                    destination = self.workspace.resolve(relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
        return checkpoint

    def _git(self, args: list[str]) -> str:
        return self._run(["git", *args], check=True).stdout.decode().strip()

    def _git_bytes(self, args: list[str]) -> bytes:
        return self._run(["git", *args], check=True).stdout

    def _run(self, command: list[str], check: bool = False):
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=False)
        if check and result.returncode != 0:
            error = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"Command failed ({' '.join(command)}): {error}")
        return result