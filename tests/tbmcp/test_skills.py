"""Skills stay in sync with the tools: valid frontmatter, only real tools (ported from
thunderbird-mcp 4921d1e; the tool names come from tern's registry instead of the FastMCP server)."""

import re
import typing
from pathlib import Path

import pytest

from tern.tools.builtin.tb import TB_TOOL_NAMES, SkillName
from tern.tools.registry import Registry, builtin_tools

SKILLS = sorted(
    (Path(__file__).parents[2] / "src" / "tern" / "tbmcp" / "skills").glob("*/SKILL.md")
)


def _tools():
    return set(Registry(builtin_tools()).names())


def test_skills_exist():
    assert len(SKILLS) >= 6


def test_skill_tool_names_match_the_files():
    assert sorted(p.parent.name for p in SKILLS) == sorted(typing.get_args(SkillName))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fields = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    assert fields["name"].strip() == path.parent.name
    desc = fields["description"].strip()
    assert 50 < len(desc) <= 1024 and "Use " in desc  # says when to use it


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_references_real_tools(path):
    mentioned = set(re.findall(r"\btb_[a-z_]+\b", path.read_text(encoding="utf-8")))
    assert mentioned, "a skill should use the server's tools"
    assert mentioned <= _tools(), f"unknown tools: {mentioned - _tools()}"
    assert mentioned <= set(TB_TOOL_NAMES)


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_has_required_sections(path):
    text = path.read_text(encoding="utf-8")
    heads = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert heads == ["Name", "Description", "Trigger phrases", "Instructions"]
    name_match = re.search(r"^## Name\n\n(.+)$", text, re.MULTILINE)
    assert name_match is not None
    assert name_match.group(1) == path.parent.name
    triggers_match = re.search(r"## Trigger phrases\n\n((?:- .+\n)+)", text)
    assert triggers_match is not None
    assert len(triggers_match.group(1).splitlines()) >= 4
