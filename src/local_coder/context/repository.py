"""Context management for repository understanding."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from local_coder.types import TaskContext

logger = logging.getLogger(__name__)

# Directories to always exclude from scanning
EXCLUDED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", "*.egg-info", ".local-coder",
    ".next", ".nuxt", "target", "vendor",
}

# Binary file extensions to skip
BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".xz",
    ".pdf", ".doc", ".docx",
    ".db", ".sqlite", ".sqlite3",
}


class RepositoryContext:
    """Builds and manages context about a repository."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self._file_tree_cache: str | None = None
        self._file_tree_cache_time: float = 0

    async def get_file_tree(self, max_depth: int = 4) -> str:
        """Get a tree representation of the project structure."""
        now = asyncio.get_event_loop().time()
        if self._file_tree_cache and (now - self._file_tree_cache_time) < 30:
            return self._file_tree_cache

        tree = await asyncio.to_thread(self._build_file_tree, max_depth)
        self._file_tree_cache = tree
        self._file_tree_cache_time = now
        return tree

    def _build_file_tree(self, max_depth: int) -> str:
        """Build a file tree string."""
        lines: list[str] = [str(self.project_root.name) + "/"]
        self._walk_tree(self.project_root, "", max_depth, 0, lines)
        return "\n".join(lines)

    def _walk_tree(
        self, path: Path, prefix: str, max_depth: int, depth: int, lines: list[str]
    ) -> None:
        if depth >= max_depth:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return

        # Filter excluded directories
        entries = [
            e for e in entries
            if e.name not in EXCLUDED_DIRS
            and not any(e.name.endswith(ext) for ext in BINARY_EXTENSIONS)
        ]

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = prefix + ("    " if is_last else "│   ")

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                self._walk_tree(entry, child_prefix, max_depth, depth + 1, lines)
            else:
                size = self._format_size(entry.stat().st_size)
                lines.append(f"{prefix}{connector}{entry.name} ({size})")

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    async def get_git_info(self) -> dict[str, Any]:
        """Get current git repository information."""
        info: dict[str, Any] = {}
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, cwd=str(self.project_root),
            )
            info["commit"] = result.stdout.strip() if result.returncode == 0 else None

            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, cwd=str(self.project_root),
            )
            info["branch"] = result.stdout.strip() if result.returncode == 0 else None

            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=str(self.project_root),
            )
            if result.returncode == 0:
                lines = [l for l in result.stdout.strip().split("\n") if l]
                info["modified_files"] = [l[3:] for l in lines if l.startswith(" M")]
                info["new_files"] = [l[3:] for l in lines if l.startswith("??")]
                info["staged_files"] = [l[3:] for l in lines if l[0] in "MADRC"]
                info["is_clean"] = len(lines) == 0
            else:
                info["is_clean"] = None

        except FileNotFoundError:
            logger.warning("git not found")
            info["error"] = "git not available"

        return info

    async def get_relevant_files(
        self,
        query: str,
        max_files: int = 20,
    ) -> list[str]:
        """Find files relevant to a query using grep-based search."""
        files: list[str] = []

        # Search for files matching query terms
        terms = query.lower().split()
        for term in terms[:5]:  # Limit terms to search
            found = await self._grep_files(term, max_results=10)
            files.extend(found)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_files: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)
                if len(unique_files) >= max_files:
                    break

        return unique_files

    async def _grep_files(self, pattern: str, max_results: int = 20) -> list[str]:
        """Search for pattern in files using ripgrep or fallback."""
        try:
            # Try ripgrep first
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "rg", "-l", "--max-count", "1",
                    "--max-filesize", "1M",
                    "-g", "!.git",
                    "-g", "!node_modules",
                    "-g", "!__pycache__",
                    "-g", "!*.pyc",
                    pattern,
                ],
                capture_output=True, text=True,
                cwd=str(self.project_root),
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[:max_results]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback to Python grep
        return await asyncio.to_thread(
            self._python_grep, pattern, max_results
        )

    def _python_grep(self, pattern: str, max_results: int) -> list[str]:
        """Fallback grep using Python."""
        matches: list[str] = []
        pattern_lower = pattern.lower()

        for root, dirs, files in os.walk(self.project_root):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]

            for fname in files:
                if any(fname.endswith(ext) for ext in BINARY_EXTENSIONS):
                    continue

                fpath = Path(root) / fname
                try:
                    content = fpath.read_text(errors="ignore")
                    if pattern_lower in content.lower():
                        rel = str(fpath.relative_to(self.project_root))
                        matches.append(rel)
                        if len(matches) >= max_results:
                            return matches
                except (PermissionError, OSError):
                    continue

        return matches

    async def read_file_safe(self, path: str, max_chars: int = 10000) -> str | None:
        """Read a file safely with size limits."""
        full_path = self.project_root / path
        if not full_path.is_file():
            return None
        try:
            content = await asyncio.to_thread(full_path.read_text, "utf-8")
            if len(content) > max_chars:
                content = content[:max_chars] + f"\n... (truncated, {len(content)} total chars)"
            return content
        except (PermissionError, UnicodeDecodeError, OSError):
            return None

    async def detect_project_type(self) -> dict[str, Any]:
        """Detect the type of project and its technologies."""
        info: dict[str, Any] = {
            "languages": [],
            "frameworks": [],
            "build_system": None,
            "test_framework": None,
            "package_manager": None,
        }

        root = self.project_root

        # Python
        if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            info["languages"].append("python")
            info["package_manager"] = "pip"
            if (root / "pyproject.toml").exists():
                content = await self.read_file_safe("pyproject.toml")
                if content:
                    if "pytest" in content:
                        info["test_framework"] = "pytest"
                    if "hatchling" in content:
                        info["build_system"] = "hatch"
                    elif "setuptools" in content:
                        info["build_system"] = "setuptools"
                    elif "poetry" in content:
                        info["build_system"] = "poetry"
                        info["package_manager"] = "poetry"

        if (root / "requirements.txt").exists():
            info["languages"].append("python") if "python" not in info["languages"] else None
            info["package_manager"] = info["package_manager"] or "pip"

        # JavaScript/TypeScript
        if (root / "package.json").exists():
            info["languages"].append("javascript")
            content = await self.read_file_safe("package.json")
            if content:
                if "typescript" in content:
                    info["languages"].append("typescript")
                if "react" in content:
                    info["frameworks"].append("react")
                if "vue" in content:
                    info["frameworks"].append("vue")
                if "next" in content:
                    info["frameworks"].append("nextjs")
                if "jest" in content:
                    info["test_framework"] = "jest"
                elif "vitest" in content:
                    info["test_framework"] = "vitest"
                elif "mocha" in content:
                    info["test_framework"] = "mocha"
            if (root / "yarn.lock").exists():
                info["package_manager"] = "yarn"
            elif (root / "pnpm-lock.yaml").exists():
                info["package_manager"] = "pnpm"
            else:
                info["package_manager"] = "npm"

        # Rust
        if (root / "Cargo.toml").exists():
            info["languages"].append("rust")
            info["build_system"] = "cargo"
            info["test_framework"] = "cargo test"

        # Go
        if (root / "go.mod").exists():
            info["languages"].append("go")
            info["build_system"] = "go"
            info["test_framework"] = "go test"

        # C/C++
        if (root / "CMakeLists.txt").exists():
            info["languages"].append("c/c++")
            info["build_system"] = "cmake"
        elif (root / "Makefile").exists():
            if not info["build_system"]:
                info["build_system"] = "make"

        # Java
        if (root / "pom.xml").exists():
            info["languages"].append("java")
            info["build_system"] = "maven"
        elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
            info["languages"].append("java")
            info["build_system"] = "gradle"

        return info

    async def build_task_context(
        self,
        objective: str,
        files: list[str] | None = None,
        include_tree: bool = True,
        include_git: bool = True,
    ) -> TaskContext:
        """Build a TaskContext for an agent task."""
        ctx = TaskContext()

        # Add architecture info
        if include_tree:
            tree = await self.get_file_tree(max_depth=3)
            project_info = await self.detect_project_type()
            ctx.architecture = (
                f"Project structure:\n{tree}\n\n"
                f"Project info: {project_info}"
            )

        # Add git info
        if include_git:
            git_info = await self.get_git_info()
            if git_info.get("branch"):
                ctx.git_history.append(f"Branch: {git_info['branch']}")
            if git_info.get("commit"):
                ctx.git_history.append(f"HEAD: {git_info['commit']}")
            if git_info.get("modified_files"):
                ctx.git_history.append(
                    f"Modified: {', '.join(git_info['modified_files'])}"
                )

        # Read specified files
        if files:
            for f in files[:10]:  # Limit files to read
                content = await self.read_file_safe(f)
                if content:
                    ctx.file_contents[f] = content

        # Find relevant files if no specific files given
        if not files and objective:
            relevant = await self.get_relevant_files(objective, max_files=5)
            for f in relevant:
                content = await self.read_file_safe(f)
                if content:
                    ctx.file_contents[f] = content
            ctx.relevant_symbols = relevant

        return ctx
