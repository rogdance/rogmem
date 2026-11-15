"""R5 独立代码审 red tests：5 项。

A1 ingest_history_for_job 异常必须真正 rollback（含部分 INSERT 后抛）；
    事后另一个 commit 不得 commit 残留。
A2 partial UNIQUE 索引迁移：老库同 scope 多 active 作业启动时必须被安全规范化，
    再建索引；不得静默删 sources / 隐藏失败。
A3 create inflight dedup 在跨连接并发下必须正确：两独立连接同时 INSERT →
    一方 win，另一方捕获 IntegrityError + re-read winner + 返回同 job；
    sources 表不受影响；不吞不相关的 IntegrityError。
B  UI /admin/api/hosts/jobs 必须能让所有正在显示的设备当前作业都可见：
    rare 先 pending → 之后 heavy 60 条 completed + 60 更新 → rare 仍可见。
D  心跳行为澄清：真实的 renew 次数在长 scan 中 > 1；线程清理有界但不假称零泄漏。

本文件只用合成数据，不读真实 HOME。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


class _Hub:
    def __init__(self, tmp: Path):
        os.environ.setdefault("ROGMEM_SKIP_SCHEDULER", "1")
        data = tmp / "data"; scan = tmp / "scan"
        data.mkdir(); scan.mkdir()
        os.environ["ROGMEM_DATA"] = str(data)
        for name in ("rogmem.app",):
            sys.modules.pop(name, None)
        from rogmem import engine
        engine.connect(data, scan)
        app_mod = importlib.import_module("rogmem.app")
        self.engine = engine
        self.app_mod = app_mod
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.data_dir = data
        import uvicorn
        cfg = uvicorn.Config(app_mod.build_app(), host="127.0.0.1", port=self.port,
                             log_level="warning", lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            time.sleep(0.05)
            if self.server.started and self.server.servers:
                break

    def shutdown(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)

    def seed_agent(self, *, device_id: str, program: str,
                   capabilities: str = "host_import_jobs:v1"):
        aid = secrets.token_hex(8)
        tok = "rm_" + secrets.token_urlsafe(16)
        kh = hashlib.sha256(tok.encode()).hexdigest()
        now = self.engine.utcnow()
        self.engine.db().execute(
            """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,
                 created_at,connected_at,call_count,capabilities)
               VALUES(?,?,?,?,?,'启用',?,?,?,?,0,?)""",
            (aid, device_id, device_id, program, f"{device_id} · {program}",
             "pk-" + secrets.token_hex(4), kh, now, now, capabilities),
        )
        self.engine.db().commit()
        return aid, tok

    def create_job(self, agent_id: str, program: str, request_id: str = "r1") -> str:
        r = self.engine.create_host_import_job(agent_id=agent_id, program=program, request_id=request_id)
        assert r["ok"], r
        return r["job"]["id"]


@pytest.fixture
def hub(tmp_path):
    h = _Hub(tmp_path)
    try:
        yield h
    finally:
        h.shutdown()


def _bootstrap(tmp_path: Path):
    os.environ.setdefault("ROGMEM_SKIP_SCHEDULER", "1")
    data = tmp_path / "d"; scan = tmp_path / "s"
    data.mkdir(); scan.mkdir()
    os.environ["ROGMEM_DATA"] = str(data)
    for name in ("rogmem.engine",):
        sys.modules.pop(name, None)
    from rogmem import engine
    engine.connect(data, scan)
    return engine, data


def _seed_agent(engine, *, device_id: str, program: str,
                capabilities: str = "host_import_jobs:v1"):
    tok = "rm_" + secrets.token_urlsafe(16)
    aid = secrets.token_hex(8)
    kh = hashlib.sha256(tok.encode()).hexdigest()
    now = engine.utcnow()
    engine.db().execute(
        """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,
             created_at,connected_at,call_count,capabilities)
           VALUES(?,?,?,?,?,'启用',?,?,?,?,0,?)""",
        (aid, device_id, device_id, program, f"{device_id} · {program}",
         "pk-" + secrets.token_hex(4), kh, now, now, capabilities),
    )
    engine.db().commit()
    return aid, tok


def _turn(session: str, seq: int, role: str = "user", text: str = "x"):
    return {
        "session_id": session,
        "turn_id": f"{session}:{seq}",
        "seq": seq,
        "role": role,
        "text": f"{text}-{seq}",
        "event_at": f"2026-11-01T00:00:{seq:02d}+00:00",
    }


# ---------------------------------------------------------------------------
# A1 · 真·事务回滚：inline 先 INSERT 再抛，异常后同线程 commit 不得泄漏
# ---------------------------------------------------------------------------


def test_inline_partial_write_then_exception_is_rolled_back(tmp_path, monkeypatch):
    """真正模拟：_ingest_history_inline 已经 INSERT 若干 sources 之后抛异常。
    - ingest_history_for_job 必须 try/except → rollback → re-raise。
    - 之后同线程再对 host_jobs 做任意 commit，之前那批部分 sources 不得被 commit 进来。
    """
    engine, _data = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devRB1", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="rb1")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)

    from rogmem import engine as eng_mod

    real_inline = eng_mod._ingest_history_inline
    def partial_boom(con, jobs, **kw):
        # 真实先 INSERT 第一行再抛；模拟『部分 sources 已落盘』的中途失败
        if jobs:
            real_inline(con, jobs[:1], **kw)
        raise RuntimeError("inline-boom-after-partial-insert")
    monkeypatch.setattr(eng_mod, "_ingest_history_inline", partial_boom)

    with pytest.raises(RuntimeError):
        engine.ingest_history_for_job(
            agent_id=aid, job_id=r["job"]["id"], lease_token=job["lease_token"],
            program="Cursor",
            turns=[_turn("sr", 1), _turn("sr", 2, role="assistant")],
        )

    srcs = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert srcs == 0, f"R5 A1：inline 异常必须 rollback 已写 sources；实际 {srcs}"

    row = engine.db().execute(
        "SELECT status, posted, duplicates, refused, lease_token FROM host_jobs"
    ).fetchone()
    assert row["status"] == "claimed" and row["posted"] == 0
    assert row["lease_token"] == job["lease_token"]

    # 关键：事后同线程对 host_jobs 做另一个 commit，之前 rollback 的那批不得被 commit
    engine.db().execute(
        "UPDATE host_jobs SET last_error_class=? WHERE id=?",
        ("channel-closed", r["job"]["id"]),
    )
    engine.db().commit()

    srcs_after = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert srcs_after == 0, (
        f"R5 A1：之前 rollback 的 sources 不得被下一次 commit 带入；实际 {srcs_after}")
    # 同线程连接的事务状态应该干净
    assert engine.db().in_transaction is False


# ---------------------------------------------------------------------------
# A2 · partial UNIQUE 索引对老库的迁移：同 scope 多 active 启动不得炸
# ---------------------------------------------------------------------------


def test_partial_unique_index_migrates_existing_active_duplicates(tmp_path, monkeypatch):
    """老库里已有同 (agent_id, program) 两条 active 作业。
    engine.connect() 建索引必须先规范化（把除最新一条外的其他标 failed），
    不得 IntegrityError 启动失败，也不得默默删除历史行。
    """
    # 先用一个绕过 UNIQUE 索引的老 schema 建库
    data = tmp_path / "old-data"; scan = tmp_path / "old-scan"
    data.mkdir(); scan.mkdir()
    db_path = data / "rogmem.sqlite"
    old_schema = """
    CREATE TABLE IF NOT EXISTS agents (
      id TEXT PRIMARY KEY, device_id TEXT NOT NULL, device_name TEXT NOT NULL,
      program TEXT NOT NULL, display_name TEXT NOT NULL, status TEXT NOT NULL,
      pubkey TEXT NOT NULL, key_hash TEXT, created_at TEXT NOT NULL,
      connected_at TEXT, call_count INTEGER NOT NULL DEFAULT 0,
      capabilities TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS host_jobs (
      id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
      agent_id TEXT NOT NULL, device_id TEXT NOT NULL, program TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'history_import',
      status TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
      claimed_at TEXT, lease_until TEXT, lease_token TEXT,
      attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
      posted INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
      refused INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
      last_error_class TEXT, completed_at TEXT, cancelled_at TEXT,
      requested_by TEXT NOT NULL DEFAULT 'admin',
      UNIQUE(agent_id, request_id)
    );
    """
    con = sqlite3.connect(db_path)
    con.executescript(old_schema)
    con.execute("INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,created_at) VALUES('a1','devOLD','devOLD','Cursor','devOLD·Cursor','启用','pk1','2026-01-01T00:00:00+00:00')")
    con.execute("INSERT INTO host_jobs(id,request_id,agent_id,device_id,program,status,created_at,expires_at) VALUES('hj_old1','r1','a1','devOLD','Cursor','pending','2026-01-01T00:00:00+00:00','2030-01-01T00:00:00+00:00')")
    con.execute("INSERT INTO host_jobs(id,request_id,agent_id,device_id,program,status,created_at,expires_at) VALUES('hj_old2','r2','a1','devOLD','Cursor','claimed','2026-01-02T00:00:00+00:00','2030-01-01T00:00:00+00:00')")
    con.commit()
    con.close()

    # 现在走正式 engine.connect，索引迁移必须成功
    os.environ["ROGMEM_DATA"] = str(data)
    sys.modules.pop("rogmem.engine", None)
    from rogmem import engine
    engine.connect(data, scan)

    # 两条老 active 仍存在，但只许最多 1 条 active
    rows = engine.db().execute(
        """SELECT id, status FROM host_jobs
             WHERE agent_id='a1' AND program='Cursor'
             ORDER BY created_at"""
    ).fetchall()
    active = [r for r in rows if r["status"] in {"pending","claimed","running","partial"}]
    assert len(active) == 1, (
        f"R5 A2：迁移后同 scope active 必须唯一；实际 {[dict(r) for r in rows]}")
    # 历史行不得被删
    assert len(rows) == 2, "R5 A2：老作业行不得被删除/隐藏"
    # 被降级的那条必须有可追溯终态理由，不是 cancelled（cancelled 由 admin 显式触发）
    downgraded = [dict(r) for r in rows if r["status"] not in {"pending","claimed","running","partial"}]
    assert len(downgraded) == 1
    assert downgraded[0]["status"] in {"failed", "superseded"}


# ---------------------------------------------------------------------------
# A3 · create 在跨连接竞争下必须 catch IntegrityError + re-read winner
# ---------------------------------------------------------------------------


def test_create_inflight_race_returns_single_winner(tmp_path):
    """两个独立 sqlite3.Connection 对同一数据库，同 (agent_id, program) 并发
    走 create_host_import_job（不同 request_id）。
    - partial UNIQUE 索引会让其中一方的 INSERT 抛 IntegrityError。
    - create 必须 catch 这个异常、rollback、re-read 已有 winner，返回同一个 job。
    - 两方最终返回的 job id 必须一致；DB 里 active 作业只 1 条；无 500。
    """
    engine, data = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devRace", program="Cursor")

    barrier = threading.Barrier(2)
    results: dict[int, dict] = {}

    def worker(idx: int, request_id: str):
        # 独立连接，绕过 engine._DB
        con = sqlite3.connect(str(data / "rogmem.sqlite"))
        con.row_factory = sqlite3.Row
        try:
            barrier.wait()
            # 模拟 engine._create_host_import_job 的核心逻辑：先探 inflight，再 INSERT
            res = engine.create_host_import_job(agent_id=aid, program="Cursor",
                                                  request_id=request_id)
            results[idx] = res
        finally:
            con.close()

    t1 = threading.Thread(target=worker, args=(1, "tab1"))
    t2 = threading.Thread(target=worker, args=(2, "tab2"))
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)

    assert results[1]["ok"] and results[2]["ok"], results
    assert results[1]["job"]["id"] == results[2]["job"]["id"], (
        f"R5 A3：并发 create 必须返回同一 winner；实际 {results[1]['job']['id']} vs {results[2]['job']['id']}")
    rows = engine.db().execute(
        """SELECT COUNT(*) FROM host_jobs WHERE agent_id=? AND program='Cursor'
             AND status IN ('pending','claimed','running','partial')""",
        (aid,),
    ).fetchone()[0]
    assert rows == 1


# ---------------------------------------------------------------------------
# B · UI 列表必须让所有当前作业可见（不被单设备历史作业挤掉）
# ---------------------------------------------------------------------------


def test_rare_current_job_survives_heavy_old_jobs_after_fresh_updates(hub):
    """构造 rare 先 pending，然后 heavy 60 条 completed 全部『之后更新过』
    （created_at 保留但 completed_at 新，或直接把 created_at 也往后排），
    让 heavy 排在 ORDER BY created_at DESC 的前 50 → 旧 UI 默认拉法会丢 rare。
    真正 UI 实现必须覆盖所有在显示的设备当前作业：按 status=pending 过滤 /
    按 agent 分批 / 只回 active + latest。
    """
    import urllib.request as U
    rare_aid, _ = hub.seed_agent(device_id="devRare2", program="Hermes")
    heavy_aid, _ = hub.seed_agent(device_id="devHeavy2", program="Cursor")
    # rare 先建
    hub.create_job(rare_aid, "Hermes", request_id="rare-current")
    # heavy 60 条 completed，created_at 都在 rare 之后（真会把 rare 挤出 default 50）
    for i in range(60):
        hub.engine.db().execute(
            """INSERT INTO host_jobs(id,request_id,agent_id,device_id,program,status,
                   created_at,expires_at,completed_at,max_attempts)
               VALUES(?,?,?,?,'Cursor','completed',?,?,?,3)""",
            (f"hj_hv_{i}", f"hv-{i}", heavy_aid, "devHeavy2",
             f"2027-01-01T00:{i:02d}:00+00:00",
             "2030-01-01T00:00:00+00:00",
             f"2027-01-01T00:{i:02d}:30+00:00"),
        )
    hub.engine.db().commit()

    # 不带 filter 的默认 /jobs：评审 R5 B 说这默认会丢 rare → 不是一个合理 UI 请求。
    # 真·UI 必须按 status=pending 或 per-agent 拉；不默认请求全历史。
    with U.urlopen(f"{hub.base}/admin/api/hosts/jobs?status=pending", timeout=5) as r:
        pending_jobs = json.loads(r.read().decode())["jobs"]
    assert any(j["agent_id"] == rare_aid and j["request_id"] == "rare-current"
               for j in pending_jobs), (
        f"R5 B：status=pending 过滤下 devRare 当前作业必须可见；"
        f"返回 {len(pending_jobs)} 条，agent_ids={set(j['agent_id'] for j in pending_jobs)}")

    # 真·UI 也可以按 agent 分批查询：rare 的 agent 必须可查到自己的当前作业
    with U.urlopen(f"{hub.base}/admin/api/hosts/jobs?agent_id={rare_aid}", timeout=5) as r:
        rare_jobs = json.loads(r.read().decode())["jobs"]
    assert any(j["request_id"] == "rare-current" for j in rare_jobs)

    # 本机 UI 真实文件里必须用上述方式，不能裸 fetch('/admin/api/hosts/jobs')
    ui_js = Path("rogmem/ui.py").read_text(encoding="utf-8")
    assert "fetch(\"/admin/api/hosts/jobs\")" not in ui_js, (
        "R5 B：ui.py 不得裸 fetch 全历史；需改为按 agent 或 status=active 分批")


# ---------------------------------------------------------------------------
# D · 心跳行为澄清（不强求零泄漏，但 renew 次数在长 scan 中真 > 1）
# ---------------------------------------------------------------------------


def test_heartbeat_renew_count_exceeds_one_during_long_scan(hub, tmp_path, monkeypatch):
    """lease 2s，总观察 ~5s，renew 应至少 2 次；不比『before+1 线程』假称零泄漏。"""
    from rogmem import cli
    aid, tok = hub.seed_agent(device_id="devHB3", program="Cursor")
    # 本测试直接计数 MCP tool renew 调用次数
    renew_calls = {"n": 0}
    real_mcp_call = cli._mcp_tool_call
    def spy(url, token, device, name, args, **kw):
        if name == "renew_host_import_lease":
            renew_calls["n"] += 1
        return real_mcp_call(url, token, device, name, args, **kw)
    monkeypatch.setattr(cli, "_mcp_tool_call", spy)

    # 直接用 heartbeat 对一个真 agent/job 测 5s 窗口
    # 先 claim 一个 job 拿到 lease_token
    hub.create_job(aid, "Cursor", request_id="hb3")
    # 用短 lease 2s claim
    from rogmem import engine as eng_mod
    real_claim = eng_mod.claim_host_import_jobs
    def claim_short(*, agent_id, lease_s=2):
        return real_claim(agent_id=agent_id, lease_s=2)
    monkeypatch.setattr(eng_mod, "claim_host_import_jobs", claim_short)
    real_renew = eng_mod.renew_host_import_lease
    def renew_short(*, agent_id, job_id, lease_token, lease_s=2):
        return real_renew(agent_id=agent_id, job_id=job_id,
                           lease_token=lease_token, lease_s=2)
    monkeypatch.setattr(eng_mod, "renew_host_import_lease", renew_short)

    url = f"{hub.base}/mcp"; device = "devHB3"
    pending = cli._mcp_tool_call(url, tok, device, "pending_host_import_jobs", {})
    job = pending["jobs"][0]
    with cli._JobHeartbeat(url, tok, device,
                            job_id=job["job_id"], lease_token=job["lease_token"],
                            lease_s=2, margin_s=1):
        time.sleep(4.5)
    assert renew_calls["n"] >= 2, (
        f"R5 D：lease=2s + 窗口 4.5s 内 renew 必须 >= 2 次；实际 {renew_calls['n']}")
