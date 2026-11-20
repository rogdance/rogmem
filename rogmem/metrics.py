"""结构化指标导出（Hindsight P2 吸收）。

产出 Prometheus 文本与 JSON。不引入 OpenTelemetry / prometheus_client 依赖；
单机 NAS 场景足够 scrape。系列不含密钥原值。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from rogmem import migrate


def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = con.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    if row is None:
        return 0
    return int(row[0] or 0)


def snapshot(con: sqlite3.Connection) -> dict[str, Any]:
    """读库快照。调用方负责提供已打开的连接。"""
    defense = {"allow": 0, "redact": 0, "block": 0}
    try:
        for row in con.execute("SELECT action, count FROM defense_stats"):
            action = str(row[0] or "")
            if action in defense:
                defense[action] = int(row[1] or 0)
    except sqlite3.Error:
        pass

    distill_paused = 0
    distill_concurrency = 0
    try:
        from rogmem import engine

        distill_paused = 1 if engine.distill_paused() else 0
        distill_concurrency = int(engine.distill_concurrency() or 0)
    except Exception:
        pass

    model_calls = 0
    try:
        from rogmem import cost as cost_mod

        with cost_mod._store(write=False) as ledger:
            model_calls = _count(ledger, "SELECT COUNT(*) FROM model_calls")
    except Exception:
        model_calls = _count(con, "SELECT COUNT(*) FROM model_calls")

    return {
        "schema_version": migrate.current_version(con),
        "sources": _count(con, "SELECT COUNT(*) FROM sources"),
        "crystals_active": _count(
            con,
            "SELECT COUNT(*) FROM crystallizations WHERE status='active'",
        ),
        "queue_leased": _count(con, "SELECT COUNT(*) FROM queue WHERE state='leased'"),
        "distill_paused": distill_paused,
        "distill_concurrency": distill_concurrency,
        "defense_decisions": defense,
        "model_calls": model_calls,
    }


def render_prometheus(snap: dict[str, Any]) -> str:
    lines = [
        "# HELP rogmem_schema_version SQLite user_version after migrations",
        "# TYPE rogmem_schema_version gauge",
        f"rogmem_schema_version {int(snap.get('schema_version') or 0)}",
        "# HELP rogmem_sources Total source rows",
        "# TYPE rogmem_sources gauge",
        f"rogmem_sources {int(snap.get('sources') or 0)}",
        "# HELP rogmem_crystals_active Active crystallizations",
        "# TYPE rogmem_crystals_active gauge",
        f"rogmem_crystals_active {int(snap.get('crystals_active') or 0)}",
        "# HELP rogmem_queue_leased Leased distill queue rows",
        "# TYPE rogmem_queue_leased gauge",
        f"rogmem_queue_leased {int(snap.get('queue_leased') or 0)}",
        "# HELP rogmem_distill_paused Whether distill is paused",
        "# TYPE rogmem_distill_paused gauge",
        f"rogmem_distill_paused {int(snap.get('distill_paused') or 0)}",
        "# HELP rogmem_distill_concurrency Configured distill worker slots",
        "# TYPE rogmem_distill_concurrency gauge",
        f"rogmem_distill_concurrency {int(snap.get('distill_concurrency') or 0)}",
        "# HELP rogmem_model_calls_total Recorded model calls",
        "# TYPE rogmem_model_calls_total counter",
        f"rogmem_model_calls_total {int(snap.get('model_calls') or 0)}",
        "# HELP rogmem_defense_decisions_total Memory Defense decisions by action",
        "# TYPE rogmem_defense_decisions_total counter",
    ]
    decisions = snap.get("defense_decisions") or {}
    for action in ("allow", "redact", "block"):
        lines.append(
            f'rogmem_defense_decisions_total{{action="{action}"}} {int(decisions.get(action) or 0)}'
        )
    lines.append("")
    return "\n".join(lines)
