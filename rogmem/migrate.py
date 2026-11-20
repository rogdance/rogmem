"""SQLite 版本化迁移（Hindsight P2 吸收）。

历史库的列补齐仍由 ``engine.connect`` 里幂等的 ALTER 引导（视为版本 0 基线）。
本模块用 ``PRAGMA user_version`` 登记从 1 起的显式步骤；新 schema 变更只加这里，
不再往 connect 堆无名 ALTER。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

# 当前库应达到的版本。新增步骤时只加一档，并写进 _STEPS。
SCHEMA_VERSION = 1


def current_version(con: sqlite3.Connection) -> int:
    row = con.execute("PRAGMA user_version").fetchone()
    return int(row[0] if row else 0)


def _set_version(con: sqlite3.Connection, version: int) -> None:
    # PRAGMA user_version 不接受绑定参数。
    con.execute(f"PRAGMA user_version = {int(version)}")


def _v1_defense_hits(con: sqlite3.Connection) -> None:
    """审计命中指纹（永不存原值）+ 决策计数。"""
    con.execute(
        """CREATE TABLE IF NOT EXISTS defense_hits (
             id INTEGER PRIMARY KEY,
             at TEXT NOT NULL,
             action TEXT NOT NULL,
             detector TEXT NOT NULL DEFAULT '',
             preview TEXT NOT NULL DEFAULT '',
             source_kind TEXT NOT NULL DEFAULT ''
           )"""
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS defense_hits_at ON defense_hits(at DESC, id DESC)"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS defense_stats (
             action TEXT PRIMARY KEY,
             count INTEGER NOT NULL DEFAULT 0
           )"""
    )
    for action in ("allow", "redact", "block"):
        con.execute(
            "INSERT OR IGNORE INTO defense_stats(action, count) VALUES(?, 0)",
            (action,),
        )


_STEPS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _v1_defense_hits),
]


def apply(con: sqlite3.Connection) -> int:
    """跑到 SCHEMA_VERSION。返回本轮新执行的步骤数。"""
    ver = current_version(con)
    ran = 0
    for target, step in _STEPS:
        if ver >= target:
            continue
        if target > SCHEMA_VERSION:
            break
        step(con)
        _set_version(con, target)
        ver = target
        ran += 1
    if ver < SCHEMA_VERSION:
        # 无对应步骤的版本空洞时直接抬到目标，避免卡死。
        _set_version(con, SCHEMA_VERSION)
    return ran
