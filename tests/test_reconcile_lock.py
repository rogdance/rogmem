"""整库巡检不许抱着 engine.LOCK 走。

`reconcile_new_crystals` 与 `patrol_wikilinks` 都要走遍每一篇结晶。这几段被
``with LOCK:`` 整个包住的时候，实测一次抱锁 136 秒，控制台里的每一次读都在
锁后面排队，页面就跟着巡检一起停。三个过程本身都是一页一提交的，锁里只需要
留下最后那条记账。
"""

from __future__ import annotations

import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "rogmem" / "engine.py"
WALKS = {"_patrol_wikilinks", "_fold_label_overlaps", "_reconcile_ask_queue"}
GUARDED = ("reconcile_new_crystals", "patrol_wikilinks", "_fold_same_names")


def _tree() -> ast.Module:
    return ast.parse(ENGINE.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _is_lock(node: ast.withitem) -> bool:
    target = node.context_expr
    return isinstance(target, ast.Name) and target.id == "LOCK"


def _inside_lock(node: ast.AST, names: set[str]) -> list[str]:
    """Calls to `names` that sit anywhere inside a `with LOCK:` block."""

    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.With) or not any(_is_lock(item) for item in child.items):
            continue
        for inner in ast.walk(child):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id in names:
                found.append(inner.func.id)
    return found


def test_no_walk_runs_inside_the_lock():
    functions = _functions(_tree())
    for name in GUARDED:
        assert name in functions, name
        held = _inside_lock(functions[name], WALKS)
        assert held == [], f"{name} 在锁里走了整库巡检：{held}"


def test_the_whole_library_patrol_takes_no_lock_at_all():
    functions = _functions(_tree())
    body = functions["patrol_wikilinks"]
    assert not any(isinstance(node, ast.With) and any(_is_lock(item) for item in node.items) for node in ast.walk(body))


def test_the_patrol_hands_its_index_to_every_page():
    """一页一次重读整张 crystallizations 拼索引，是巡检慢的主因之一。"""

    functions = _functions(_tree())
    calls = [
        node
        for node in ast.walk(functions["_patrol_wikilinks"])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sync_wikilinks"
    ]
    assert calls, "巡检要真的同步链接"
    for call in calls:
        assert any(keyword.arg == "index" for keyword in call.keywords), ast.dump(call)
