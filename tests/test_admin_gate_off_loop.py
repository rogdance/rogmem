"""门口那一问不许把事件循环按在磁盘上。

`AdminGate` 对每个 `/admin/*` 请求都要问一次口令：读口令代次、读 cookie 钥匙，
`_secret()` 还要碰文件系统。这一问直接跑在事件循环里的时候，只要它卡一下，
连不经过这道门的 `/healthz` 和读另一本账的 `/admin/costs` 都要一起等——实测
预发布上每个接口（连一字节的 healthz 也一样）都被放大几十倍。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "rogmem" / "app.py"


def _admin_gate(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AdminGate":
            for inner in node.body:
                if isinstance(inner, ast.AsyncFunctionDef) and inner.name == "__call__":
                    return inner
    raise AssertionError("AdminGate.__call__ 不见了")


def test_the_door_check_runs_off_the_event_loop():
    gate = _admin_gate(ast.parse(APP.read_text(encoding="utf-8")))

    mentions = [node for node in ast.walk(gate) if isinstance(node, ast.Name) and node.id == "_admin_ok"]
    threaded = []
    for node in ast.walk(gate):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "to_thread":
            continue
        threaded.extend(
            arg for arg in node.args if isinstance(arg, ast.Name) and arg.id == "_admin_ok"
        )

    assert mentions, "门口还是要问一次口令"
    assert len(threaded) == len(mentions), "这一问必须在线程里问，不能直接 await"
