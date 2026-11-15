"""R4 严审窄修红测试（前）。

逐条针对 R3 未闭环项：
R4-1 真正扫描中心跳：lease=2s，scan 真 sleep 3s（>lease），heartbeat 必须续租让作业 complete。
R4-2 UI/jobs 列表按 agent 分页/摘要：>50 条老作业不得挤掉其他设备的新作业显示。
R4-3 cancelled ack 幂等：stale token 拒；重放 ack 不反复累加 failed；completed 空扫描不假。
R4-4 cross-process scope dedup：Python LOCK 之外也需 SQLite UNIQUE 索引守『in-flight 唯一』。
R4-5 ingest_history_for_job 在 inline 异常时必须 rollback 事务。

只合成数据，不碰真实 HOME。
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
    from rogmem import engine
    engine.connect(data, scan)
    return engine


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


def _build_fake_cursor_home(root: Path, *, token: str, hub_base: str, n: int,
                             device_id: str) -> Path:
    home = root / f"home-{secrets.token_hex(4)}"
    (home / ".cursor" / "projects" / "r").mkdir(parents=True)
    lines = []
    for i in range(n):
        lines.append(json.dumps({"type": "message", "role": "user",
                                  "session_id": "cs", "content": f"合成-{i}",
                                  "timestamp": f"2026-11-01T00:00:{i:02d}Z"}))
        lines.append(json.dumps({"type": "message", "role": "assistant",
                                  "session_id": "cs", "content": f"回-{i}",
                                  "timestamp": f"2026-11-01T00:00:{i:02d}Z"}))
    (home / ".cursor" / "projects" / "r" / "s.jsonl").write_text(
        "\n".join(lines), encoding="utf-8")
    (home / ".cursor" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"rogmem": {
            "url": f"{hub_base}/mcp",
            "headers": {"Authorization": f"Bearer {token}",
                         "X-Rogmem-Device": device_id}}
        }}), encoding="utf-8")
    return home


# ---------------------------------------------------------------------------
# R4-1 · 真·长扫描心跳
# ---------------------------------------------------------------------------


def _force_short_server_lease(monkeypatch, lease_s: int) -> None:
    """强制 server 端 claim/renew 都用这么短的 lease，让测试能真正跨 lease。"""
    from rogmem import engine as eng_mod

    real_claim = eng_mod.claim_host_import_jobs
    def claim_short(*, agent_id, lease_s=lease_s):
        return real_claim(agent_id=agent_id, lease_s=lease_s)
    monkeypatch.setattr(eng_mod, "claim_host_import_jobs", claim_short)

    real_renew = eng_mod.renew_host_import_lease
    def renew_short(*, agent_id, job_id, lease_token, lease_s=lease_s):
        return real_renew(agent_id=agent_id, job_id=job_id,
                           lease_token=lease_token, lease_s=lease_s)
    monkeypatch.setattr(eng_mod, "renew_host_import_lease", renew_short)


def test_worker_heartbeat_survives_scan_longer_than_lease(hub, tmp_path, monkeypatch):
    """R4-1：server 端 lease=2s + scan sleep 3s，必须 heartbeat 在 scan 途中续租，
    作业 complete。覆盖「只在 scan 前后 renew」这种近似失效的真场景。
    """
    from rogmem import cli
    _force_short_server_lease(monkeypatch, 2)
    aid, tok = hub.seed_agent(device_id="devHB2", program="Cursor")
    home = _build_fake_cursor_home(tmp_path, token=tok, hub_base=hub.base, n=1,
                                    device_id="devHB2")
    hub.create_job(aid, "Cursor", request_id="hb2")

    from installer import rogmem_installer as inst
    real_read = inst.read_program_history
    def slow(home, program, allow_real=False):
        time.sleep(3.0)  # 严格 > server lease
        yield from real_read(home, program, allow_real=allow_real)
    monkeypatch.setattr(inst, "read_program_history", slow)

    # 让 client 也把 lease 期限视为 2s，才会触发续租
    monkeypatch.setattr(cli, "_INITIAL_LEASE_S", 2, raising=False)
    monkeypatch.setattr(cli, "_RENEW_MARGIN_S", 1, raising=False)
    cli.run_host_import_jobs(home)
    row = hub.engine.db().execute("SELECT status, posted FROM host_jobs").fetchone()
    assert row["status"] == "completed", f"长扫描心跳必须续租；实际 {row['status']}"
    assert row["posted"] == 2


def test_worker_heartbeat_stops_on_cancel_without_leaking_threads(hub, tmp_path, monkeypatch):
    """R4-1 反面：scan 途中 cancel，heartbeat 必须真实观察到取消并停止后续上传。
    函数返回后 heartbeat 线程/定时器必须收敛，不留泄漏。
    """
    from rogmem import cli
    _force_short_server_lease(monkeypatch, 2)
    aid, tok = hub.seed_agent(device_id="devHBCan", program="Cursor")
    home = _build_fake_cursor_home(tmp_path, token=tok, hub_base=hub.base, n=10,
                                    device_id="devHBCan")
    job_id = hub.create_job(aid, "Cursor", request_id="hbcan")

    from installer import rogmem_installer as inst
    real_read = inst.read_program_history
    def slow(home, program, allow_real=False):
        time.sleep(0.5)
        hub.engine.cancel_host_import_job(job_id=job_id)
        time.sleep(1.5)
        yield from real_read(home, program, allow_real=allow_real)
    monkeypatch.setattr(inst, "read_program_history", slow)

    uploads = {"n": 0}
    real_post = cli._post_json_with_headers
    def spy(url, payload, headers):
        uploads["n"] += 1
        return real_post(url, payload, headers)
    monkeypatch.setattr(cli, "_post_json_with_headers", spy)

    monkeypatch.setattr(cli, "_INITIAL_LEASE_S", 2, raising=False)
    monkeypatch.setattr(cli, "_RENEW_MARGIN_S", 1, raising=False)

    threads_before = threading.active_count()
    cli.run_host_import_jobs(home)
    time.sleep(0.3)  # heartbeat 清理窗口
    threads_after = threading.active_count()

    # heartbeat 真正感知 cancel → 后续 upload 应被阻止
    # 允许 0 次（理想）或 1 次（在 heartbeat 感知前的首个 batch 拿到 refused=cancelled 立即退）
    assert uploads["n"] <= 1, f"cancel 后最多 1 次 upload（并被服务端拒）；实际 {uploads['n']}"
    srcs = hub.engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert srcs == 0
    assert threads_after <= threads_before + 1, (
        f"heartbeat 必须收敛不泄漏；before={threads_before} after={threads_after}")


# ---------------------------------------------------------------------------
# R4-2 · UI /admin/api/hosts/jobs 按 agent 摘要/分页
# ---------------------------------------------------------------------------


def test_jobs_endpoint_per_agent_summary_covers_all_devices(hub):
    """R4-2：50+ 条老作业的 devHeavy 不得挤掉另一台 devRare 的当前作业显示。
    UI 用 per-agent 摘要/有界分页，任何设备的当前作业都可见。
    """
    import urllib.request as U
    heavy_aid, _ = hub.seed_agent(device_id="devHeavy", program="Cursor")
    rare_aid, _ = hub.seed_agent(device_id="devRare", program="Hermes")
    # devHeavy 60 条已完成（老）作业
    for i in range(60):
        hub.engine.create_host_import_job(agent_id=heavy_aid, program="Cursor",
                                            request_id=f"old-{i}")
        hub.engine.db().execute(
            "UPDATE host_jobs SET status='completed', completed_at='2026-01-01T00:00:00+00:00' "
            "WHERE request_id=?",
            (f"old-{i}",),
        )
    hub.engine.db().commit()
    # devRare 1 条当前 pending
    hub.create_job(rare_aid, "Hermes", request_id="rare-current")

    # UI 侧请求：按 agent 分别拉应可见 devRare 当前作业
    for aid in (heavy_aid, rare_aid):
        with U.urlopen(f"{hub.base}/admin/api/hosts/jobs?agent_id={aid}", timeout=5) as r:
            body = json.loads(r.read().decode())
        agent_ids = {j["agent_id"] for j in body["jobs"]}
        # 该请求必须只回本 agent 的作业
        assert agent_ids <= {aid}, f"per-agent 请求不得回其他 agent 作业；{agent_ids}"
    # devRare 当前作业必须可见
    with U.urlopen(f"{hub.base}/admin/api/hosts/jobs?agent_id={rare_aid}", timeout=5) as r:
        rare_jobs = json.loads(r.read().decode())["jobs"]
    assert any(j["request_id"] == "rare-current" and j["status"] == "pending"
               for j in rare_jobs), "devRare 当前作业必须可见"

    # 聚合入口要么按 agent 返回『active summary』，要么支持 limit/状态过滤；
    # 不得默认 limit=50 把 rare 的当前作业挤出去。
    with U.urlopen(f"{hub.base}/admin/api/hosts/jobs?status=pending", timeout=5) as r:
        pending_only = json.loads(r.read().decode())["jobs"]
    assert any(j["agent_id"] == rare_aid for j in pending_only), (
        "按 status=pending 过滤时 devRare 的当前作业必须可见；"
        f"实际返回 {len(pending_only)} 条 pending")


# ---------------------------------------------------------------------------
# R4-3 · cancelled 态 ack 真正幂等
# ---------------------------------------------------------------------------


def test_cancelled_ack_rejects_stale_token(tmp_path):
    """R4-3：stale token 的 ack 对 cancelled 作业必须拒，不能静默累加 failed。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devCS", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="cs")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    engine.cancel_host_import_job(job_id=r["job"]["id"])
    out = engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                      lease_token="bogus-token",
                                      status="partial", failed=1)
    assert out["ok"] is False and out["refused"] == "stale-lease"
    row = engine.db().execute("SELECT failed FROM host_jobs").fetchone()
    assert row["failed"] == 0, "stale token 的 ack 不能累加 failed"


def test_cancelled_ack_repeated_with_valid_token_is_idempotent(tmp_path):
    """R4-3：持 valid token 的 cancelled ack 允许一次性记录 failed，但重放幂等——
    failed 不得随每次 ack 继续增长。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devCRe", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="cre")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    engine.cancel_host_import_job(job_id=r["job"]["id"])
    for _ in range(5):
        engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                    lease_token=job["lease_token"],
                                    status="cancelled_ack", failed=3,
                                    last_error_class="cancelled")
    row = engine.db().execute("SELECT status, posted, duplicates, failed FROM host_jobs").fetchone()
    assert row["status"] == "cancelled"
    assert row["posted"] == 0 and row["duplicates"] == 0
    assert row["failed"] == 3, (
        f"cancelled ack 重放必须幂等（failed 不得累加）；实际 {row['failed']}")


def test_completed_zero_scan_is_not_penalized(tmp_path):
    """R4-3 合理性：空扫描 ack(status=completed) 合法，posted=0 不代表假装完成全量；
    服务端只承认『这一次作业已终结』，不自报『全量已导入』。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devZero", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="z")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    # 无 upload；直接 ack completed（例如本机没有任何该 program 的会话文件）
    out = engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                      lease_token=job["lease_token"],
                                      status="completed", failed=0)
    assert out["ok"]
    row = engine.db().execute("SELECT status, posted FROM host_jobs").fetchone()
    assert row["status"] == "completed"
    assert row["posted"] == 0  # 空扫描合法


# ---------------------------------------------------------------------------
# R4-4 · 跨进程 in-flight scope 唯一（SQLite 层）
# ---------------------------------------------------------------------------


def test_db_schema_has_inflight_partial_unique_index(tmp_path):
    """R4-4：仅 Python LOCK 不够。SQLite 层必须有 partial UNIQUE INDEX 守
    『同 (agent_id, program) 只许一条 pending/claimed/running/partial』。
    这样即使跨进程并发 INSERT 也拒双建。
    """
    engine = _bootstrap(tmp_path)
    rows = engine.db().execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='host_jobs'"
    ).fetchall()
    sql_texts = " ".join((r["sql"] or "") for r in rows)
    assert "UNIQUE" in sql_texts.upper(), (
        f"host_jobs 必须有 partial UNIQUE 索引（跨进程保险）；实际索引：{[(r['name'], r['sql']) for r in rows]}")
    # partial 条件：status IN (pending, claimed, running, partial)
    low = sql_texts.lower()
    assert all(s in low for s in ("pending", "claimed", "running", "partial")), (
        "partial unique index 必须以 active 状态集合作为 WHERE 条件；实际 SQL：" + sql_texts)


def test_db_rejects_inflight_duplicate_at_sql_layer(tmp_path):
    """R4-4：绕过 Python LOCK 直接 INSERT，SQLite 唯一约束必须拒第二条。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devDbUnique", program="Cursor")
    engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="u1")
    # 直接 INSERT 第二条同 scope active
    con = engine.db()
    import secrets as _s
    try:
        con.execute(
            """INSERT INTO host_jobs(id, request_id, agent_id, device_id, program, kind,
                 status, created_at, expires_at, max_attempts, requested_by)
               VALUES(?,?,?,?,?,'history_import','pending',?,?,3,'admin')""",
            ("hj_direct", "u2", aid, "devDbUnique", "Cursor",
             engine.utcnow(), engine.utcnow()),
        )
        con.commit()
        raised = False
    except sqlite3.IntegrityError:
        con.rollback()
        raised = True
    assert raised, "SQLite 必须在 SQL 层拒同 scope 第二条 active；只靠 Python LOCK 不够"


# ---------------------------------------------------------------------------
# R4-5 · 事务异常回滚
# ---------------------------------------------------------------------------


def test_ingest_for_job_rolls_back_on_inline_failure(tmp_path, monkeypatch):
    """R4-5：若 _ingest_history_inline 中途异常，host_jobs 的 counter/status 更新
    必须一并回滚；sources 已 INSERT 的行一并回滚；lease 不变。
    """
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devRB", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="rb")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)

    from rogmem import engine as eng_mod
    def boom(*a, **kw):
        raise RuntimeError("inline-boom")
    monkeypatch.setattr(eng_mod, "_ingest_history_inline", boom)

    with pytest.raises(RuntimeError):
        engine.ingest_history_for_job(
            agent_id=aid, job_id=r["job"]["id"], lease_token=job["lease_token"],
            program="Cursor",
            turns=[{"session_id": "s", "role": "user", "text": "x",
                    "event_at": "2026-11-01T00:00:00+00:00", "seq": 1}],
        )
    row = engine.db().execute(
        "SELECT status, posted, duplicates, refused, lease_token FROM host_jobs"
    ).fetchone()
    assert row["status"] == "claimed", f"异常后 status 不得变 running；实际 {row['status']}"
    assert row["posted"] == 0 and row["duplicates"] == 0 and row["refused"] == 0
    assert row["lease_token"] == job["lease_token"]
    srcs = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert srcs == 0, "异常必须回滚 sources 的 INSERT"
