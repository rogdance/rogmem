"""The public README stays aligned with the product it describes."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
UI = (ROOT / "rogmem" / "ui.py").read_text(encoding="utf-8")
APP = (ROOT / "rogmem" / "app.py").read_text(encoding="utf-8")
ENGINE = (ROOT / "rogmem" / "engine.py").read_text(encoding="utf-8")

HEADINGS = (
    "它解决什么",
    "一次会话怎么用记忆",
    "记忆长什么样",
    "从原文到结晶",
    "已经接上的宿主",
    "在自己的机器上跑起来",
    "智能体怎么调用",
    "版本与联系",
)


def _about_lead() -> str:
    match = re.search(r"<p class='about-lead'>(.*?)</p>", UI)
    assert match, "about page lost its lead sentence"
    return match.group(1)


def test_readme_keeps_the_about_sentence_and_contacts():
    from scripts.public_tree import contact_needles

    assert _about_lead() in README
    assert "https://github.com/rogdance/rogmem" in README
    for needle in contact_needles():
        assert needle.lower() not in README.lower()
        assert needle.lower() not in UI.lower()


def test_readme_names_every_host_and_mcp_tool():
    hosts = re.search(r"HOST_PROGRAMS = \(([^)]*)\)", ENGINE)
    assert hosts
    for name in re.findall(r'"([^"]+)"', hosts.group(1)):
        assert name in README
    tools = re.findall(r"@mcp\.tool\(\)\n(?:@[^\n]+\n)*def (\w+)\(", APP)
    assert tools
    for name in tools:
        assert f"`{name}`" in README


def test_readme_keeps_its_section_order_and_mascot():
    cursor = 0
    for heading in HEADINGS:
        found = README.find(f"## {heading}", cursor)
        assert found > cursor, heading
        cursor = found
    assert 'src="docs/brand/rogmem-lockup.svg"' in README
    image = ROOT / "docs" / "brand" / "rogmem-lockup.svg"
    assert image.is_file()
    svg = image.read_text(encoding="utf-8")
    assert "prefers-color-scheme" in svg
    assert "#FF3763" in svg and "#FF7038" in svg
    skill = (ROOT / "skills" / "rogmem-readme" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: rogmem-readme" in skill


def test_readme_leaves_out_private_operations():
    lowered = README.lower()
    assert "18081" not in README
    assert "remote-access" not in lowered
    assert "192.168." not in README
    assert "admin.password" not in README
    assert "138-0000" not in README
    assert "license" not in lowered
