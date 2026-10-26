"""Tests for the ranked repo map and the repo_map tool."""
import asyncio

import pytest

from local_coder.context.code_index import CodeIndex
from local_coder.context.repo_map import RepoMap, _pagerank, ref_key
from local_coder.orchestrator.coordinator import Coordinator
from local_coder.tools import create_tool_registry
from local_coder.tools.repo_map import RepoMapTool
from local_coder.types import AgentRole, AgentTask, ModelResponse, ProjectConfig, TaskContext, ToolName


@pytest.fixture
def project(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    # storage.py is the hub: every other module uses DocumentStore.
    (app / "storage.py").write_text(
        '"""Persistence."""\n'
        "\n"
        "class DocumentStore:\n"
        "    def save_document(self, doc):\n"
        "        return _encode_record(doc)\n"
        "\n"
        "    def load_document(self, key):\n"
        "        return None\n"
        "\n"
        "def _encode_record(doc):\n"
        "    return str(doc)\n"
    )
    (app / "api.py").write_text(
        "from app.storage import DocumentStore\n"
        "\n"
        "def handle_upload(request):\n"
        "    store = DocumentStore()\n"
        "    return store.save_document(request.body)\n"
        "\n"
        "def handle_download(request):\n"
        "    return DocumentStore().load_document(request.key)\n"
    )
    (app / "worker.py").write_text(
        "from app.storage import DocumentStore\n"
        "\n"
        "def reindex_documents():\n"
        "    store = DocumentStore()\n"
        "    store.load_document('all')\n"
    )
    (app / "cli.py").write_text(
        "from app.api import handle_upload\n"
        "from app.storage import DocumentStore\n"
        "\n"
        "def main():\n"
        "    handle_upload(DocumentStore())\n"
    )
    # An island nothing references, about something else entirely.
    (app / "billing.py").write_text(
        "@decorator\n"
        "def compute_invoice_total(line_items, tax_rate):\n"
        "    subtotal = sum(i.price for i in line_items)\n"
        "    return subtotal * (1 + tax_rate)\n"
    )
    (tmp_path / "README.md").write_text("# Docs app\n")
    return tmp_path


def make_map(project) -> RepoMap:
    return RepoMap(str(project), index=CodeIndex(str(project), persist=False))


def test_ref_key_matches_how_the_index_stores_mentions():
    assert ref_key("DocumentStore") == "documentstore"
    assert ref_key("_encode_record") == "encode_record"
    assert ref_key("save") == "sav"  # one-word names are stemmed
    assert ref_key("__init__") is None


def test_most_referenced_definition_ranks_first(project):
    ranked, file_rank = make_map(project).rank()
    assert ranked[0].path == "app/storage.py"
    assert ranked[0].symbol == "DocumentStore"
    assert max(file_rank, key=file_rank.get) == "app/storage.py"
    # Nothing references billing, so it trails the storage hub.
    billing = next(d for d in ranked if d.symbol == "compute_invoice_total")
    assert billing.rank < ranked[0].rank


def test_query_pulls_matching_code_to_the_top(project):
    ranked, _ = make_map(project).rank("fix the invoice tax total calculation")
    assert ranked[0].symbol == "compute_invoice_total"


def test_mentioned_path_is_focused(project):
    ranked, _ = make_map(project).rank("tidy up app/worker.py")
    top_paths = [d.path for d in ranked[:2]]
    assert "app/worker.py" in top_paths


def test_focus_files_rank_higher(project):
    rm = make_map(project)
    plain = {d.symbol: d.rank for d in rm.rank()[0]}
    focused = {d.symbol: d.rank for d in rm.rank(focus_files=["./app/cli.py"])[0]}
    assert focused["main"] / focused["DocumentStore"] > plain["main"] / plain["DocumentStore"]


def test_render_shows_signature_lines_with_numbers(project):
    text = make_map(project).build(max_tokens=1024)
    assert "app/storage.py:" in text
    assert "    3| class DocumentStore:" in text
    # A method is shown under its class, indented as in the source.
    assert "    4|     def save_document(self, doc):" in text
    # Decorators are skipped in favor of the line naming the function.
    assert "    2| def compute_invoice_total(line_items, tax_rate):" in text
    assert text.index("app/storage.py:") < text.index("app/billing.py:")


def test_each_line_appears_once(project):
    lines = make_map(project).build(max_tokens=1024).splitlines()
    assert len(lines) == len(set(lines))


def test_map_respects_token_budget(project):
    rm = make_map(project)
    full = rm.build(max_tokens=4096)
    small = rm.build(max_tokens=40)
    assert len(small) <= 40 * 4
    assert len(small) < len(full)
    # What fits is the top of the ranking.
    assert small.startswith("app/storage.py:")
    assert rm.build(max_tokens=0) == ""


def test_empty_project(tmp_path):
    assert make_map(tmp_path).build() == ""


def test_pagerank_follows_links_and_sums_to_one():
    edges = {"a": {("c", "x"): 5.0}, "b": {("c", "x"): 5.0}}
    rank = _pagerank(["a", "b", "c"], edges, {})
    assert rank["c"] > rank["a"] == rank["b"]
    assert abs(sum(rank.values()) - 1.0) < 1e-6


def test_pagerank_weak_links_pass_less_rank():
    strong = _pagerank(["a", "b"], {"a": {("b", "x"): 5.0}}, {})
    weak = _pagerank(["a", "b"], {"a": {("b", "x"): 0.1}}, {})
    assert strong["b"] > weak["b"]


class TestRepoMapTool:
    def test_output(self, project):
        tool = RepoMapTool(str(project), index=CodeIndex(str(project), persist=False))
        result = asyncio.run(tool.execute(query="invoice total", max_tokens=512))
        assert result.success
        assert result.output.startswith("app/billing.py:")

    def test_rejects_file_outside_workspace(self, project):
        tool = RepoMapTool(str(project), index=CodeIndex(str(project), persist=False))
        result = asyncio.run(tool.execute(files=["../../etc/passwd"]))
        assert not result.success

    def test_registered_and_shares_the_code_search_index(self, project):
        registry = create_tool_registry(str(project))
        repo_map = registry.get_tool(ToolName.REPO_MAP)
        code_search = registry.get_tool(ToolName.CODE_SEARCH)
        assert repo_map.repo_map.index is code_search.index
        for role in (AgentRole.EXPLORER, AgentRole.CODER, AgentRole.PLANNER):
            assert registry.has_permission(role, ToolName.REPO_MAP)


class RecordingModel:
    def __init__(self):
        self.messages = []

    async def generate(self, messages, **kwargs):
        self.messages.append(messages)
        return ModelResponse(content="Explored the repo.")


def test_explorer_starts_with_the_repo_map(project):
    coordinator = Coordinator(config=ProjectConfig(project_root=str(project)), project_root=str(project))
    model = RecordingModel()
    coordinator.model_manager.get_model = lambda role, *a: asyncio.sleep(0, result=model)

    asyncio.run(coordinator._explore("Speed up saving documents"))

    user_msg = model.messages[0][1].content
    assert "## Repository Map" in user_msg
    assert "class DocumentStore:" in user_msg


def test_repo_map_can_be_turned_off(project):
    config = ProjectConfig(project_root=str(project))
    config.agentic.repo_map_tokens = 0
    coordinator = Coordinator(config=config, project_root=str(project))
    assert asyncio.run(coordinator._repo_map("anything")) == ""


def test_format_task_omits_empty_map():
    from local_coder.agents.base import BaseAgent

    task = AgentTask(role=AgentRole.CODER, objective="x")
    assert "Repository Map" not in BaseAgent._format_task(None, task)
    task.context = TaskContext(architecture="app/a.py:\n    1| def f():")
    assert "## Repository Map\napp/a.py:" in BaseAgent._format_task(None, task)
