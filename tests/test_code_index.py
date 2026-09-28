"""Tests for the local code retrieval index and the code_search tool."""
import asyncio
import json
import os

import pytest

from local_coder.context.code_index import CodeIndex, chunk_file, tokenize, INDEX_RELPATH
from local_coder.context.repository import RepositoryContext
from local_coder.tools import create_tool_registry
from local_coder.tools.code_search import CodeSearchTool
from local_coder.types import AgentRole, ToolName


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "config_loader.py").write_text(
        '"""Configuration loading."""\n'
        "import yaml\n"
        "\n"
        "DEFAULT_PATH = 'config.yaml'\n"
        "\n"
        "def parse_config(path):\n"
        "    \"\"\"Parse the YAML config file.\"\"\"\n"
        "    with open(path) as f:\n"
        "        return yaml.safe_load(f)\n"
        "\n"
        "class ConfigLoader:\n"
        "    \"\"\"Loads and caches settings.\"\"\"\n"
        "    cache = {}\n"
        "\n"
        "    def load(self, path):\n"
        "        return parse_config(path)\n"
    )
    (src / "retry.py").write_text(
        "import time\n"
        "\n"
        "def retry_with_backoff(fn, attempts=3):\n"
        "    for i in range(attempts):\n"
        "        try:\n"
        "            return fn()\n"
        "        except TimeoutError:\n"
        "            time.sleep(2 ** i)\n"
    )
    web = tmp_path / "web"
    web.mkdir()
    (web / "client.ts").write_text(
        "import { http } from './http';\n"
        "\n"
        "export async function fetchUserProfile(id: string) {\n"
        "  return http.get(`/users/${id}`);\n"
        "}\n"
        "\n"
        "export const renderAvatar = (user) => {\n"
        "  return `<img src=${user.avatar}>`;\n"
        "};\n"
    )
    (tmp_path / "README.md").write_text("# Demo\n\nA demo project about widgets.\n")
    node_modules = tmp_path / "node_modules" / "dep"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("function parseConfig() {}\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\0\0\0binary")
    return tmp_path


class TestTokenize:
    def test_splits_snake_and_camel_case(self):
        tokens = tokenize("parseConfigFile read_file_safe HTTPServer")
        assert {"parseconfigfile", "pars", "config", "fil", "read_file_safe", "read", "saf"} <= set(tokens)
        assert {"http", "server"} <= set(tokens)

    def test_stems_word_forms_together(self):
        assert tokenize("parsing")[0] == tokenize("parse")[0] == tokenize("parsed")[0]

    def test_drops_stopwords(self):
        assert tokenize("the self return") == []


class TestChunking:
    def test_python_functions_methods_and_classes(self, project):
        text = (project / "src" / "config_loader.py").read_text()
        chunks = chunk_file("src/config_loader.py", text)
        symbols = {(c.kind, c.symbol) for c in chunks}
        assert ("function", "parse_config") in symbols
        assert ("class", "ConfigLoader") in symbols
        assert ("function", "ConfigLoader.load") in symbols
        # Imports and module constants land in a module chunk
        assert any(c.kind == "module" and c.start == 1 for c in chunks)
        method = next(c for c in chunks if c.symbol == "ConfigLoader.load")
        assert (method.start, method.end) == (15, 16)

    def test_regex_definitions_for_other_languages(self, project):
        text = (project / "web" / "client.ts").read_text()
        symbols = {c.symbol for c in chunk_file("web/client.ts", text)}
        assert {"fetchUserProfile", "renderAvatar"} <= symbols

    def test_long_definition_is_split(self):
        body = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(200))
        chunks = chunk_file("big.py", body)
        assert len(chunks) >= 3
        assert all(c.end - c.start < 80 for c in chunks)

    def test_invalid_python_falls_back_to_windows(self):
        chunks = chunk_file("bad.py", "def broken(:\n    pass\n")
        assert chunks and chunks[0].kind == "block"


class TestSearch:
    def test_finds_by_description(self, project):
        index = CodeIndex(str(project), persist=False)
        hits = index.search("retry when a call times out with backoff")
        assert hits[0].path == "src/retry.py"
        assert hits[0].symbol == "retry_with_backoff"

    def test_identifier_query_matches_other_casing(self, project):
        index = CodeIndex(str(project), persist=False)
        hits = index.search("parseConfig")
        assert hits[0].symbol == "parse_config"

    def test_skips_vendored_and_binary_files(self, project):
        index = CodeIndex(str(project), persist=False)
        index.refresh()
        paths = {c.path for c in index._chunks}
        assert not any(p.startswith("node_modules") for p in paths)
        assert "logo.png" not in paths

    def test_path_prefix_filter(self, project):
        index = CodeIndex(str(project), persist=False)
        hits = index.search("user profile config", path_prefix="web")
        assert hits and all(h.path.startswith("web/") for h in hits)

    def test_search_files_ranks_files(self, project):
        index = CodeIndex(str(project), persist=False)
        assert index.search_files("load yaml config")[0] == "src/config_loader.py"

    def test_no_terms_returns_nothing(self, project):
        index = CodeIndex(str(project), persist=False)
        assert index.search("the of and") == []

    def test_snippets_include_signature_and_matching_lines(self, project):
        index = CodeIndex(str(project), persist=False)
        hits = index.search("yaml safe_load")
        index.add_snippets(hits, "yaml safe_load")
        lines = dict(hits[0].snippet)
        assert hits[0].start in lines
        assert any("safe_load" in text for text in lines.values())


class TestIncremental:
    def test_persists_and_reuses_unchanged_files(self, project):
        index = CodeIndex(str(project))
        first = index.refresh()
        assert first["updated"] >= 3
        assert (project / INDEX_RELPATH).is_file()
        data = json.loads((project / INDEX_RELPATH).read_text())
        assert "src/retry.py" in data["files"]

        fresh = CodeIndex(str(project))
        second = fresh.refresh()
        assert second["updated"] == 0 and second["unchanged"] == first["updated"]
        assert fresh.search("backoff")[0].path == "src/retry.py"

    def test_picks_up_edits_and_deletions(self, project):
        index = CodeIndex(str(project), persist=False)
        index.refresh()
        target = project / "src" / "retry.py"
        target.write_text("def circuit_breaker():\n    return 'open'\n")
        os.utime(target, ns=(1, 1))
        (project / "README.md").unlink()

        stats = index.refresh()
        assert stats["updated"] == 1 and stats["removed"] == 1
        assert index.search("circuit breaker")[0].path == "src/retry.py"
        assert not index.search("backoff")

    def test_corrupt_index_file_is_rebuilt(self, project):
        path = project / INDEX_RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        index = CodeIndex(str(project))
        assert index.search("backoff")[0].path == "src/retry.py"


class TestCodeSearchTool:
    def test_output_format(self, project):
        tool = CodeSearchTool(str(project))
        result = asyncio.run(tool.execute(query="load yaml config", max_results=3))
        assert result.success
        first = result.output.splitlines()[0]
        assert first.startswith("src/config_loader.py:")
        assert "[function " in first and "score=" in first

    def test_rejects_path_outside_workspace(self, project):
        tool = CodeSearchTool(str(project))
        result = asyncio.run(tool.execute(query="config", path="../"))
        assert not result.success

    def test_no_results_message(self, project):
        tool = CodeSearchTool(str(project))
        result = asyncio.run(tool.execute(query="zzzqqq"))
        assert result.success and "No matching code" in result.output

    def test_registered_for_roles_that_can_grep(self, project):
        registry = create_tool_registry(str(project))
        for role in (AgentRole.EXPLORER, AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.REVIEWER):
            assert registry.has_permission(role, ToolName.CODE_SEARCH)
        names = [s["function"]["name"] for s in registry.get_schemas_for_role(AgentRole.CODER)]
        assert "code_search" in names


class TestRepositoryContext:
    def test_relevant_files_use_index(self, project):
        ctx = RepositoryContext(str(project))
        files = asyncio.run(ctx.get_relevant_files("retry backoff timeout", max_files=2))
        assert files[0] == "src/retry.py"
