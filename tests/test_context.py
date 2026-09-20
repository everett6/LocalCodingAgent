"""Tests for repository context."""
import asyncio
import os

import pytest

from local_coder.context.repository import RepositoryContext


@pytest.fixture
def sample_project(tmp_path):
    """Create a sample project structure for testing."""
    # Create project files
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]')
    (tmp_path / "README.md").write_text("# Test Project")
    
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "main.py").write_text("def main():\n    print('hello')\n")
    (src / "utils.py").write_text("def helper():\n    return 42\n")
    
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_main.py").write_text("def test_main():\n    assert True\n")
    
    # Create .git to make it look like a git repo
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    
    return tmp_path


class TestFileTree:
    def test_build_tree(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        tree = asyncio.run(ctx.get_file_tree(max_depth=3))
        assert "src/" in tree
        assert "main.py" in tree
        assert "tests/" in tree
        assert ".git" not in tree  # Should be excluded

    def test_tree_caching(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        tree1 = asyncio.run(ctx.get_file_tree())
        tree2 = asyncio.run(ctx.get_file_tree())
        assert tree1 == tree2  # Should be cached


class TestProjectDetection:
    def test_detect_python_project(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        info = asyncio.run(ctx.detect_project_type())
        assert "python" in info["languages"]
        assert info["test_framework"] == "pytest"

    def test_detect_empty_project(self, tmp_path):
        ctx = RepositoryContext(str(tmp_path))
        info = asyncio.run(ctx.detect_project_type())
        assert info["languages"] == []


class TestFileReading:
    def test_read_file(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        content = asyncio.run(ctx.read_file_safe("src/main.py"))
        assert content is not None
        assert "def main" in content

    def test_read_nonexistent(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        content = asyncio.run(ctx.read_file_safe("nonexistent.py"))
        assert content is None

    def test_read_truncation(self, sample_project):
        # Create a large file
        large = sample_project / "large.txt"
        large.write_text("x" * 20000)
        
        ctx = RepositoryContext(str(sample_project))
        content = asyncio.run(ctx.read_file_safe("large.txt", max_chars=100))
        assert len(content) < 200  # Should be truncated
        assert "truncated" in content


class TestContextBuilding:
    def test_build_context(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        task_ctx = asyncio.run(ctx.build_task_context(
            "add a new function",
            files=["src/main.py"],
            include_git=False,
        ))
        assert task_ctx.architecture  # Should have project info
        assert "src/main.py" in task_ctx.file_contents

    def test_build_context_no_files(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        task_ctx = asyncio.run(ctx.build_task_context(
            "general improvement",
            include_git=False,
        ))
        assert task_ctx.architecture


class TestRelevantFiles:
    def test_find_relevant(self, sample_project):
        ctx = RepositoryContext(str(sample_project))
        files = asyncio.run(ctx.get_relevant_files("main"))
        # Should find main.py or test_main.py
        assert any("main" in f for f in files)
