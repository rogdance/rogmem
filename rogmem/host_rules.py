"""Global host rules. The wording is the standing order; core carries the packet."""

from __future__ import annotations

from pathlib import Path

RULES_REV = 2


def _bundle_rev() -> str:
    """The rogmem plugin version. One version for everything the hub installs on a computer."""
    from rogmem.plugin import version

    return version() or str(RULES_REV)


# Kept under the old name for callers that still import it; same value as rogmem.plugin.version().
HOST_BUNDLE_REV = _bundle_rev()
MARKER_START = "<!-- rogmem-rule:start -->"
MARKER_END = "<!-- rogmem-rule:end -->"
BLOCK_START = f"【rogmem 规则版本：{RULES_REV}】"
BLOCK_END = "【rogmem 规则结束】"

_FORBIDDEN = {
    "Cursor": "绝对禁止把别的检索、本地笔记或压缩摘要当成这次 core。",
    "Trae": "绝对禁止用会话压缩、本机记忆摘要或本地笔记代替这次 core。",
    "Hermes": "绝对禁止用 MEMORY.md、USER.md、预取结果或压缩摘要代替这次 core。那些都不是历史包。",
    "Codex": "绝对禁止用自动压缩摘要或本机另存的笔记代替这次 core。",
    "WorkBuddy": "绝对禁止用压缩摘要或本地笔记代替这次 core。",
    "CodeBuddy": "绝对禁止用压缩摘要或本地笔记代替这次 core。",
    "DSH": "绝对禁止用会话压缩或本地技能笔记代替这次 core。",
}


def rule_body(program: str) -> str:
    """The standing order for one host. Obligations match; only the false substitute changes."""
    forbidden = _FORBIDDEN[program]
    tool = "rogmem MCP 的 core" if program == "Hermes" else "rogmem 的 core"
    return f"""{BLOCK_START}

本段是全局规则。对所有项目、所有会话、每一次回答都生效。绝对禁止把它当成某一个仓库的项目规则。绝对禁止在别的项目里跳过。

每一次准备对用户作出可见回复之前，先按本段执行。

一、先看本对话里有没有历史包。历史包是 {tool} 的成功返回，或者开头已经给出的「rogmem 历史包」，而且必须此刻仍在你的上下文里。

还没有，或者已经不在上下文里（被宿主压缩、被摘要替换、或已被丢弃）：必须立刻调用 core，并且必须等到返回，读完再回答。绝对禁止先回答再补调。绝对禁止因为问题很短、看起来和记忆无关、你觉得自己记得、或工具可有可无，就跳过。{forbidden}
开场包写明没有工程速写，而你知道仓库远程地址或 40 位根提交，才必须再调一次 core，只把这个身份传进去，用来装上工程速写。绝对禁止把本机路径传进去。

core 调不到，或调用失败，或通道没开：用一句话说明这次没有装入记忆，然后按宿主自己已有的上下文把这件事做完。绝对禁止因为记忆没装上就停手。绝对禁止假装已经装入。没送上的会话留在本机原来的会话里，接通后由本机续传。绝对禁止在对话里把原文再贴一遍。

project 只用于装入这一仓库的工程速写。你知道仓库的远程地址或 40 位根提交，就必须传入。你只知道本机路径时，必须省略 project。绝对禁止把本机路径传进去。绝对禁止猜一个地址。最近摘录不靠这个参数，按时间一起给出。

这次返回是本对话的历史包，里面是人物速写、工程速写，以及配额内的最近摘录。有仓库标记的摘录带着标记，没有就空着。必须读完再回答。

二、历史包此刻在你的上下文里。
优先用它和用户本场已经说出口的话回答。绝对禁止手里有这包，却只凭通用知识回答这个人的约定、偏好或这个工程的现状。
优先顺序必须遵守。用户本场原话最高。身份、偏好、工程规范以速写为准。近况若摘录里的原句更新，以摘录为准。
这时禁止把整包在同一会话里重复拉一遍。

这个人的约定、偏好，这个工程的现状、历史决策，本来就该多查：凡碰到下面任一条，必须先 search 核对，再回答，宁查勿猜。
1. 速写与摘录里没有这件事；
2. 这件事发生在速写的状态截点之后；
3. 你只是"大概记得"，并不确定。
search 只查这一问，绝对禁止用它顶替整包。

历史包写明被截断，而这一问需要更早的原文：必须按返回给出的位置再调用 core 续读。这次只取更早的摘录。绝对禁止用 search 代替这次续读。绝对禁止把没有装进来的内容当成已知。

返回里的规则版本高于本块版本：必须用返回的整块替换从「rogmem 规则版本」到「rogmem 规则结束」的这一段。绝对禁止改动这一段以外的任何规则。版本相同，绝对禁止重写这一段。

已经在本场说清楚、或包与摘录里已有的事实，不要再取。绝对禁止为了保险把整包再调一遍。

core 调用失败时，必须告诉用户这次没有装入记忆，然后把这件事做完。绝对禁止假装已经装入。绝对禁止停手。

{BLOCK_END}
"""


def cursor_rule() -> str:
    return (
        "---\n"
        "description: rogmem。每次回答前必须执行。全局规则，所有项目生效。\n"
        "alwaysApply: true\n"
        "---\n\n"
        + rule_body("Cursor")
    )


def _mdc_rule(program: str) -> str:
    return (
        "---\n"
        "description: rogmem。每次回答前必须执行。全局规则，所有项目生效。\n"
        "alwaysApply: true\n"
        "---\n\n"
        + rule_body(program)
    )


def workbuddy_rule() -> str:
    return _mdc_rule("WorkBuddy")


def trae_rule() -> str:
    return _mdc_rule("Trae")


def codebuddy_rule() -> str:
    return _mdc_rule("CodeBuddy")


def dsh_rule() -> str:
    return rule_body("DSH")


def hermes_rule() -> str:
    return rule_body("Hermes")


def codex_rule() -> str:
    return rule_body("Codex")


def rule_path(home: Path, program: str) -> Path | None:
    home = Path(home)
    if program == "Cursor":
        return home / ".cursor" / "rules" / "rogmem.mdc"
    if program == "Trae":
        return home / ".trae" / "user_rules" / "rogmem.md"
    if program == "Hermes":
        return home / ".hermes" / "rogmem-rule.md"
    if program == "Codex":
        return home / ".codex" / "AGENTS.md"
    if program == "WorkBuddy":
        return home / ".workbuddy" / "rules" / "rogmem.mdc"
    if program == "CodeBuddy":
        return home / ".codebuddy" / "rules" / "rogmem.mdc"
    if program == "DSH":
        return home / ".dsh" / "skills" / "rogmem" / "SKILL.md"
    return None


def upsert_marked(existing: str, block: str) -> str:
    """Replace the rogmem block. Text outside the markers stays."""
    wrapped = f"{MARKER_START}\n{block.strip()}\n{MARKER_END}\n"
    start = existing.find(MARKER_START)
    end = existing.find(MARKER_END)
    if start >= 0 and end > start:
        tail = existing[end + len(MARKER_END) :]
        if tail.startswith("\n"):
            tail = tail[1:]
        return existing[:start] + wrapped + tail
    if existing.strip():
        return existing.rstrip() + "\n\n" + wrapped
    return wrapped


def write_host_rule(home: Path, program: str) -> Path | None:
    path = rule_path(home, program)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if program == "Cursor":
        path.write_text(cursor_rule(), encoding="utf-8")
    elif program == "Trae":
        path.write_text(trae_rule(), encoding="utf-8")
    elif program == "WorkBuddy":
        path.write_text(workbuddy_rule(), encoding="utf-8")
    elif program == "CodeBuddy":
        path.write_text(codebuddy_rule(), encoding="utf-8")
    elif program == "DSH":
        front = "---\nname: rogmem\ndescription: 每次回答前必须执行的全局规则。所有会话生效。\n---\n\n"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        body = upsert_marked(existing, dsh_rule())
        if not existing.strip():
            body = front + body
        path.write_text(body, encoding="utf-8")
    elif program == "Hermes":
        path.write_text(hermes_rule(), encoding="utf-8")
    elif program == "Codex":
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(upsert_marked(existing, codex_rule()), encoding="utf-8")
    return path
