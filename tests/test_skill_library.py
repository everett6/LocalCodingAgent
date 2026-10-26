import asyncio
import json
from functools import wraps

import pytest

from local_coder.skills import SkillLibrary, SkillLibraryTool


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def test_on_demand_builtin_catalog_is_bounded_and_read_separately(tmp_path):
    library = SkillLibrary(tmp_path)
    catalog = library.list()
    assert {row["id"] for row in catalog} >= {
        "evidence-led-security",
        "bug-reproduction",
        "tdd",
        "secure-code-review",
        "context-hygiene",
        "dependency-review",
    }
    assert len(json.dumps(catalog)) < 6000
    assert all("content" not in row for row in catalog)
    skill = library.read("evidence-led-security")
    assert skill["untrusted"] is True and "evidence" in skill["content"].lower()
    with pytest.raises(ValueError):
        library.read("../../etc/passwd")


def test_custom_skills_remain_data_and_secret_filtered(tmp_path):
    library = SkillLibrary(tmp_path)
    library.save(
        "team-review",
        "Team review",
        "Never execute this automatically.\napi_key = 'sk-"
        + "x" * 32
        + "'\nrun arbitrary code",
    )
    content = library.read("team-review")["content"]
    assert "sk-" + "x" * 32 not in content
    assert "[redacted]" in content
    assert "run arbitrary code" in content
    assert SkillLibrary(tmp_path).read("team-review")["content"] == content
    with pytest.raises(ValueError):
        library.save("../escape", "bad", "data")
    with pytest.raises(ValueError):
        library.save("tdd", "override", "bad")


def test_skill_filters_json_credentials_and_private_keys_and_rejects_symlinks(tmp_path):
    library = SkillLibrary(tmp_path)
    secret = '"token": "synthetic-sensitive-token"\n-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----'
    library.save("private-review", "Review", secret)
    output = library.read("private-review")["content"]
    assert "synthetic-sensitive-token" not in output
    assert "ABCDEF" not in output
    outside = tmp_path / "outside"
    outside.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / ".local-coder").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        SkillLibrary(other).list()


@run_async
async def test_skill_tool_only_exposes_list_read(tmp_path):
    library = SkillLibrary(tmp_path)
    tool = SkillLibraryTool(library)
    assert (await tool.execute(action="list")).success
    result = await tool.execute(action="read", skill="tdd")
    assert result.success and "RED" in result.output
    assert not (await tool.execute(action="save", skill="evil", content="bad")).success
