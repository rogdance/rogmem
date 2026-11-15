"""PR #116 第三轮 (R2) 严审 red tests — 全部**先**失败，再逐条做绿。

覆盖评审 9 条：
P1   ingest_history_for_job 线性化：cancel/reclaim 与 batch commit 可串行化
P2-1 ack 不得累加 posted/duplicates；cancelled ack 不能免 lease 反复加
P2-2 duplicates 单一来源（不再 ingest.duplicates + imported[].duplicate 双计）
P2-3 claim CAS 自身守住 attempts>=max_attempts，不依赖 sweep 即可 failed
P2-4 heartbeat 按时间续租；scanner/upload 中都受 lease deadline 控制
P2-5 scope scanner：有 job 时只读授权 program 目录
P2-6 cursor job/agent/program 隔离；新人工请求不被旧全局 cursor 阻挡
P2-7 服务端在途去重：同 (agent,program) 同 scope 只许一条 active
P2-8 浏览器 light/dark 真实对比 + 390 overflow
P2-9 CSRF scheme+host+effectiveport 规范化

所有测试只用 tempfile + 合成数据；不读真实 HOME。
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
# bootstrap helpers (本文件独立；不复用 e2e 的 Hub 防止意外污染)
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

    def seed_agent(self, *, device_id: str, program: str, capabilities: str = "host_import_jobs:v1"):
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
# P1 · 线性化：ingest_history_for_job 不得允许「cancel + upload 都成功入库」
# ---------------------------------------------------------------------------


def test_ingest_for_job_cancel_vs_upload_is_linearizable(tmp_path):
    """评审 P1：在同一短事务内，cancel 与 batch 必须可串行化。
    并发 cancel 与 upload 的结果必须满足原子二选一不变量：
      - 若 upload.ok，sources += turns AND job.posted += turns AND job.status 仍 active。
      - 若 upload.refused，sources 不变 AND job.posted 不变。
    绝不出现 upload.ok=True 且 job.status=cancelled 且 sources 未增 的脱缝状态。
    """
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devAtm", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="atm")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)

    barrier = threading.Barrier(2)
    results = {}

    def do_upload():
        barrier.wait()
        results["upload"] = engine.ingest_history_for_job(
            agent_id=aid, job_id=r["job"]["id"], lease_token=job["lease_token"],
            program="Cursor",
            turns=[_turn("sX", 1), _turn("sX", 2, role="assistant")],
        )

    def do_cancel():
        barrier.wait()
        results["cancel"] = engine.cancel_host_import_job(job_id=r["job"]["id"])

    t1 = threading.Thread(target=do_cancel)
    t2 = threading.Thread(target=do_upload)
    t1.start(); t2.start()
    t1.join(); t2.join()

    srcs = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    row = engine.db().execute("SELECT status, posted, duplicates, refused FROM host_jobs").fetchone()
    upload = results["upload"]
    if upload.get("ok"):
        assert srcs == 2, f"upload.ok 时必须 sources += 2；实际 {srcs}"
        assert row["posted"] == 2
        assert row["status"] in {"running", "cancelled"}  # cancel 可能在 commit 之后才生效
        # 关键不变量：若 cancel 后 upload.ok，posted 一定对齐 sources
        assert row["posted"] == srcs
    else:
        assert upload["refused"] in {"cancelled", "terminal", "stale-lease", "not-active"}
        assert srcs == 0, f"upload refused 时 sources 不得变化；实际 {srcs}"
        assert row["posted"] == 0 and row["duplicates"] == 0 and row["refused"] == 0


def test_ingest_for_job_refuses_when_lease_reclaimed_before_commit(tmp_path):
    """原过期 reclaim 场景：claim-A → lease 过期 → claim-B 换 token → claim-A 的 worker
    带旧 lease 提交 batch → 必须整批拒，不得入库任何行；B 的 token 不受污染。
    """
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devReclaim", program="Cursor")
    engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="rc")
    [first] = engine.claim_host_import_jobs(agent_id=aid, lease_s=60)
    # 过期
    engine.db().execute("UPDATE host_jobs SET lease_until='2000-01-01T00:00:00+00:00'")
    engine.db().commit()
    # 第二次 claim 换 token
    [second] = engine.claim_host_import_jobs(agent_id=aid, lease_s=60)
    assert second["lease_token"] != first["lease_token"]
    before = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    stale_result = engine.ingest_history_for_job(
        agent_id=aid, job_id=first["id"], lease_token=first["lease_token"],
        program="Cursor",
        turns=[_turn("sr", 1), _turn("sr", 2, role="assistant")],
    )
    after = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert stale_result["ok"] is False
    assert stale_result["refused"] == "stale-lease"
    assert after == before, "stale worker 的 batch 绝不能入库"
    row = engine.db().execute("SELECT posted, duplicates FROM host_jobs").fetchone()
    assert row["posted"] == 0 and row["duplicates"] == 0


# ---------------------------------------------------------------------------
# P2-1 · ack 不得加 posted/duplicates；cancelled ack 不得免 lease 反复加
# ---------------------------------------------------------------------------


def test_ack_cannot_self_report_posted(tmp_path):
    """评审 P2-1：空 batch 用 ack(status=completed, posted=7) 也只能是 posted=0；
    计数仅由 upload 实际入库决定。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devAckLie", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="ap")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    out = engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                      lease_token=job["lease_token"],
                                      status="completed",
                                      posted=7, duplicates=3, refused=2, failed=0)
    assert out["ok"]
    row = engine.db().execute("SELECT status, posted, duplicates, refused FROM host_jobs").fetchone()
    assert row["status"] == "completed"
    assert row["posted"] == 0, f"ack 不得累加 posted；实际 {row['posted']}"
    assert row["duplicates"] == 0, f"ack 不得累加 duplicates；实际 {row['duplicates']}"
    assert row["refused"] == 0, f"ack 不得累加 refused；实际 {row['refused']}"


def test_ack_cancelled_cannot_bump_posted_without_lease(tmp_path):
    """评审 P2-1 续：cancelled 后反复 ack 也不能无中生有 posted。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devCanLie", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="cl")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    engine.cancel_host_import_job(job_id=r["job"]["id"])
    for _ in range(5):
        engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                    lease_token=job["lease_token"], status="completed",
                                    posted=9999, duplicates=9999)
    row = engine.db().execute("SELECT posted, duplicates FROM host_jobs").fetchone()
    assert row["posted"] == 0 and row["duplicates"] == 0


def test_ack_terminal_is_idempotent(tmp_path):
    """评审 P2-1：terminal 后重放 ack 必须幂等（不增计数、不换状态）。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devIdem", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="ix")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                lease_token=job["lease_token"], status="completed",
                                posted=0, duplicates=0, refused=0, failed=0)
    row1 = dict(engine.db().execute("SELECT * FROM host_jobs").fetchone())
    for _ in range(3):
        engine.ack_host_import_job(agent_id=aid, job_id=r["job"]["id"],
                                    lease_token=job["lease_token"], status="completed",
                                    posted=5, failed=5)
    row2 = dict(engine.db().execute("SELECT * FROM host_jobs").fetchone())
    assert row1["posted"] == row2["posted"] == 0
    assert row1["status"] == row2["status"] == "completed"


# ---------------------------------------------------------------------------
# P2-2 · duplicates 单一来源：重放整批 server 侧只算 duplicates 一次
# ---------------------------------------------------------------------------


def test_duplicates_counted_from_single_source(tmp_path):
    """评审 P2-2：第一次 upload 2 条入库；第二次重放同 2 条，
    job.duplicates 必须 == 2（不是 4 的双计）；sources 不再增。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devDup", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="dp")
    [job] = engine.claim_host_import_jobs(agent_id=aid, lease_s=600)
    turns = [_turn("sdup", 1), _turn("sdup", 2, role="assistant")]
    first = engine.ingest_history_for_job(agent_id=aid, job_id=r["job"]["id"],
                                           lease_token=job["lease_token"],
                                           program="Cursor", turns=turns)
    second = engine.ingest_history_for_job(agent_id=aid, job_id=r["job"]["id"],
                                            lease_token=job["lease_token"],
                                            program="Cursor", turns=turns)
    assert first["posted"] == 2 and first["duplicates"] == 0
    assert second["posted"] == 0 and second["duplicates"] == 2, \
        f"第二次重放应 duplicates=2，不能双计；实际 {second['duplicates']}"
    row = engine.db().execute("SELECT posted, duplicates FROM host_jobs").fetchone()
    assert row["posted"] == 2 and row["duplicates"] == 2
    srcs = engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert srcs == 2


# ---------------------------------------------------------------------------
# P2-3 · claim CAS 自身守 attempts，无需 sweep 即触顶 failed
# ---------------------------------------------------------------------------


def test_claim_cas_respects_max_attempts_without_sweep(tmp_path):
    """评审 P2-3：max_attempts=2，连续两次 claim+lease 过期（模拟崩溃），
    下一次 claim 必须直接 failed 不再领。不依赖 sweep。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devAtt", program="Cursor")
    r = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="at",
                                       max_attempts=2)
    [c1] = engine.claim_host_import_jobs(agent_id=aid, lease_s=1)
    engine.db().execute("UPDATE host_jobs SET lease_until='2000-01-01T00:00:00+00:00'")
    engine.db().commit()
    [c2] = engine.claim_host_import_jobs(agent_id=aid, lease_s=1)
    engine.db().execute("UPDATE host_jobs SET lease_until='2000-01-01T00:00:00+00:00'")
    engine.db().commit()
    # 第三次 claim 必须 0 行（本 job 已达 attempts 上限）
    third = engine.claim_host_import_jobs(agent_id=aid, lease_s=60)
    assert third == [], f"attempts 触顶后 claim 不得再领；实际 {third}"
    row = engine.db().execute("SELECT status, attempts FROM host_jobs").fetchone()
    assert row["status"] == "failed", f"attempts 触顶必须直接 failed；实际 {row['status']}"
    assert row["attempts"] >= 2


# ---------------------------------------------------------------------------
# P2-4 · heartbeat 按时间而非 batch 数；长扫描中 lease 不丢
# ---------------------------------------------------------------------------


def test_worker_renews_lease_by_time_during_long_scan(hub, tmp_path, monkeypatch):
    """评审 P2-4：有界 heartbeat。
    模拟 scan 耗时 >= heartbeat 间隔；worker 必须在 scan 之前/途中续租。
    """
    from rogmem import cli
    aid, tok = hub.seed_agent(device_id="devHB", program="Cursor")
    home = _build_fake_cursor_home(tmp_path, token=tok, hub_base=hub.base, n=1,
                                    device_id="devHB")
    hub.create_job(aid, "Cursor", request_id="hb")

    # 把 scanner 拖慢
    from installer import rogmem_installer as inst
    real_read = inst.read_program_history
    def slow(home, program, allow_real=False):
        time.sleep(1.2)
        yield from real_read(home, program, allow_real=allow_real)
    monkeypatch.setattr(inst, "read_program_history", slow)

    # claim 时 lease 只给 2 秒；若 worker 不在 scan 后立即续租，上传时 lease 已过期。
    monkeypatch.setattr(cli, "_INITIAL_LEASE_S", 2, raising=False)
    cli.run_host_import_jobs(home)
    row = hub.engine.db().execute("SELECT status, posted FROM host_jobs").fetchone()
    assert row["status"] == "completed", f"长扫描中 heartbeat 失败；实际 {row['status']}"
    assert row["posted"] == 2


# ---------------------------------------------------------------------------
# P2-5 · scope scanner：有 job 时只读授权 program 目录
# ---------------------------------------------------------------------------


def _build_fake_cursor_home(root: Path, *, token: str, hub_base: str, n: int = 1,
                            with_hermes: bool = False, device_id: str | None = None) -> Path:
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
    dev = device_id or home.name.replace("home-", "dev-")
    (home / ".cursor" / "mcp.json").write_text(json.dumps({
        "mcpServers": {"rogmem": {
            "url": f"{hub_base}/mcp",
            "headers": {"Authorization": f"Bearer {token}",
                         "X-Rogmem-Device": dev}}
        }}), encoding="utf-8")
    if with_hermes:
        import sqlite3
        db = home / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.executescript("""
          CREATE TABLE sessions(id TEXT PRIMARY KEY, parent_session_id TEXT,
                                source TEXT, cwd TEXT, git_repo_root TEXT);
          CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT,
                                timestamp TEXT, active INTEGER);
          INSERT INTO sessions VALUES('hs','','', '/t', '');
          INSERT INTO messages VALUES('hs','user','ht','2026-11-02T00:00:00Z',1);
        """)
        con.commit(); con.close()
    return home


def test_scanner_scope_limits_reads_to_authorized_program(hub, tmp_path, monkeypatch):
    """评审 P2-5：Cursor 作业绝不能扫 Hermes / Codex 目录。"""
    from rogmem import cli
    aid, tok = hub.seed_agent(device_id="devScope", program="Cursor")
    # 制作一个 fake home 里同时有 .cursor + .hermes；但只把 Cursor 的 mcp 配指向 hub
    home = _build_fake_cursor_home(tmp_path, token=tok, hub_base=hub.base, n=1,
                                    with_hermes=True, device_id="devScope")
    hub.create_job(aid, "Cursor", request_id="scope")

    reads = []
    import builtins
    real_open = builtins.open
    def track_open(path, *a, **kw):
        p = str(path)
        if ".hermes" in p or ".codex" in p or ".dsh" in p:
            reads.append(p)
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", track_open)

    import sqlite3 as _sqlite3
    real_connect = _sqlite3.connect
    def track_connect(dbfile, *a, **kw):
        p = str(dbfile).split("?", 1)[0]
        if ".hermes" in p or ".codex" in p:
            reads.append(p)
        return real_connect(dbfile, *a, **kw)
    monkeypatch.setattr(_sqlite3, "connect", track_connect)

    cli.run_host_import_jobs(home)
    off_scope = [p for p in reads if "hub-data" not in p and "hub-scan" not in p]
    assert off_scope == [], f"评审 P2-5：授权 scope 之外不得读；实际读到 {off_scope!r}"


# ---------------------------------------------------------------------------
# P2-6 · 新人工请求不被旧全局 cursor 阻挡
# ---------------------------------------------------------------------------


def test_new_job_is_not_blocked_by_global_sent_cursor(hub, tmp_path):
    """评审 P2-6：先跑完一次作业（client 可能写了 ~/.rogmem/history.sent），
    再下达新作业，新作业必须完整重算 scope 内所有 turns，让服务端 dedup 决定。
    posted/duplicates 必须对应实际 sources 变化（第一次 posted=2，第二次 posted=0 duplicates=2）。
    """
    from rogmem import cli
    aid, tok = hub.seed_agent(device_id="devCur", program="Cursor")
    home = _build_fake_cursor_home(tmp_path, token=tok, hub_base=hub.base, n=1,
                                    device_id="devCur")
    hub.create_job(aid, "Cursor", request_id="c1")
    cli.run_host_import_jobs(home)
    before = hub.engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert before == 2
    # 新人工作业：必须真正重新尝试全部 turns，由 server dedup 决定
    hub.create_job(aid, "Cursor", request_id="c2")
    cli.run_host_import_jobs(home)
    after = hub.engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert after == before
    row2 = hub.engine.db().execute(
        "SELECT posted, duplicates FROM host_jobs WHERE request_id='c2'"
    ).fetchone()
    assert row2["posted"] == 0
    assert row2["duplicates"] == 2, (
        f"评审 P2-6：新人工请求不能被旧 cursor 跳过；duplicates 必须由 server 真实回；"
        f"实际 {row2['duplicates']}")


# ---------------------------------------------------------------------------
# P2-7 · 服务端在途去重：同 (agent, program) 只许一条 active
# ---------------------------------------------------------------------------


def test_server_dedupes_inflight_job_per_scope(tmp_path):
    """评审 P2-7：两 tab 不同 request_id 建同 (agent, program) 作业，
    服务端必须在途去重，第二次返回同一个 job（或 refused: in-flight），绝不建两条。"""
    engine = _bootstrap(tmp_path)
    aid, _ = _seed_agent(engine, device_id="devInf", program="Cursor")
    a = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="tab1")
    b = engine.create_host_import_job(agent_id=aid, program="Cursor", request_id="tab2")
    assert a["ok"]
    assert b["ok"]
    rows = engine.db().execute(
        "SELECT COUNT(*) FROM host_jobs WHERE agent_id=? AND status IN ('pending','claimed','running','partial')",
        (aid,),
    ).fetchone()[0]
    assert rows == 1, f"评审 P2-7：同 scope 在途只许 1 条；实际 {rows}"
    # 两次返回同一个 job id（幂等命中）
    assert a["job"]["id"] == b["job"]["id"]


# ---------------------------------------------------------------------------
# P2-9 · CSRF 以 scheme+host+effectiveport 规范化
# ---------------------------------------------------------------------------


def test_csrf_check_uses_scheme_and_effective_port(tmp_path):
    """评审 P2-9：仅比 netloc 会漏 scheme/端口差异。
    - Origin https://evil.com 攻 http://127.0.0.1:P 必须 refused:cross-origin。
    - Origin http://127.0.0.1:P（同源）必须通过。
    - Origin http://127.0.0.1:OTHER 必须拒。
    """
    hub = _Hub(tmp_path)
    try:
        aid, _ = hub.seed_agent(device_id="devCsrfP", program="Cursor")
        import urllib.request as U, urllib.error
        body = json.dumps({"agent_id": aid, "program": "Cursor"}).encode()

        def post(origin):
            req = U.Request(f"{hub.base}/admin/api/hosts/jobs", data=body,
                             headers={"Content-Type": "application/json", "Origin": origin},
                             method="POST")
            try:
                with U.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        s, b = post("https://evil.example.com")
        assert s == 403 and b["refused"] == "cross-origin"
        s, b = post("http://127.0.0.1:99999")
        assert s == 403 and b["refused"] == "cross-origin"
        s, b = post(hub.base)  # 同源 http://127.0.0.1:P
        assert s == 200 and b["ok"]
        # 清掉 inflight 让下一次新建可以成功（否则 in-flight dedup 会命中）
        hub.engine.db().execute("UPDATE host_jobs SET status='cancelled'")
        hub.engine.db().commit()
        other_port = _free_port()
        s, b = post(f"http://127.0.0.1:{other_port}")
        assert s == 403 and b["refused"] == "cross-origin"
        # 空 Origin：历史 CLI 兼容，由 base_url 判断不是作业端点时放行；作业端点要求同源
        req = U.Request(f"{hub.base}/admin/api/hosts/jobs", data=body,
                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with U.urlopen(req, timeout=5) as r:
                s2 = r.status; b2 = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            s2 = exc.code; b2 = json.loads(exc.read().decode())
        assert s2 == 403 and b2["refused"] == "cross-origin"
    finally:
        hub.shutdown()


def test_admin_button_through_local_door_is_not_cross_origin(tmp_path):
    """管理台经门口或远程入口打开时，浏览器 Origin 不是中枢自己的地址。

    浏览器同源 fetch 带 Sec-Fetch-Site: same-origin，页面改不了这个头。这必须收下。
    同一父域的另一个源是 same-site，仍然拒绝。
    没有这个头时，伪造的门口 Origin、以及自己填的 X-Forwarded-Host，都拒绝。
    """
    hub = _Hub(tmp_path)
    try:
        aid, _ = hub.seed_agent(device_id="devDoor", program="Cursor")
        import urllib.request as U, urllib.error

        body = json.dumps({"agent_id": aid, "program": "Cursor", "request_id": "door-1"}).encode()
        door = "http://127.0.0.1:18780"

        def post(headers):
            req = U.Request(
                f"{hub.base}/admin/api/hosts/jobs",
                data=body,
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            try:
                with U.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        status, payload = post({"Origin": door, "Sec-Fetch-Site": "same-origin"})
        assert status == 200 and payload["ok"], payload
        hub.engine.db().execute("UPDATE host_jobs SET status='cancelled'")
        hub.engine.db().commit()
        status, payload = post({"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        assert status == 403 and payload["refused"] == "cross-origin"
        status, payload = post({"Origin": door, "Sec-Fetch-Site": "cross-site"})
        assert status == 403 and payload["refused"] == "cross-origin"
        status, payload = post({"Origin": "https://sibling.example", "Sec-Fetch-Site": "same-site"})
        assert status == 403 and payload["refused"] == "cross-origin"
        status, payload = post({"Origin": door})
        assert status == 403 and payload["refused"] == "cross-origin"
        status, payload = post({
            "Origin": door,
            "X-Forwarded-Host": "127.0.0.1:18780",
            "X-Forwarded-Proto": "http",
        })
        assert status == 403 and payload["refused"] == "cross-origin"
    finally:
        hub.shutdown()
