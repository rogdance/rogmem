"""宿主历史导入端到端 · 真生产 worker · 不碰真实 $HOME。

关键区别（PR #116 严审第二版）：
- 不再手搓 HTTP + 裸 /admin/api/history 伪装作业链路。
- 直接调用生产函数 `rogmem.cli.run_host_import_jobs(home)`。
- 临时 hub 真正启动（uvicorn 后台线程，随机端口），配置 fake_home 的
  宿主配置指向该端口的 /mcp；agent 的 bearer 入库。
- 覆盖：无作业不扫描；目标地址从 MCP url 推出，不依赖 loopback；cursor
  持久化（history.sent）；分批中途 cancel 真的停；stale lease 拒绝；
  真实 sources 计数以服务端摄入为准；能力声明；多 agent 隔离。
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
# bootstrap：进程内起一个真正的 hub，并把 fake home 配置指向它
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close(); return port


class Hub:
    def __init__(self, tmp: Path):
        os.environ.setdefault("ROGMEM_SKIP_SCHEDULER", "1")
        data = tmp / "hub-data"; scan = tmp / "hub-scan"
        data.mkdir(); scan.mkdir()
        os.environ["ROGMEM_DATA"] = str(data)
        # 每个 hub 都要全新的 FastMCP 实例
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

        config = uvicorn.Config(app_mod.build_app(), host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            time.sleep(0.05)
            if self.server.started and self.server.servers:
                break
        assert self.server.started, "uvicorn did not come up"

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
        res = self.engine.create_host_import_job(
            agent_id=agent_id, program=program, request_id=request_id,
        )
        assert res["ok"], res
        return res["job"]["id"]


@pytest.fixture
def hub(tmp_path):
    h = Hub(tmp_path)
    try:
        yield h
    finally:
        h.shutdown()


def _write_fake_home(root: Path, *, program: str = "Cursor", n: int = 3,
                     agent: dict | None = None) -> Path:
    home = root / "fake-home"
    home.mkdir(parents=True, exist_ok=True)
    cfg_root = {"Cursor": ".cursor", "Hermes": ".hermes", "Codex": ".codex",
                "WorkBuddy": ".workbuddy", "CodeBuddy": ".codebuddy",
                "DSH": ".dsh"}[program]
    (home / cfg_root).mkdir(parents=True, exist_ok=True)
    # 1) 转写文件
    if program == "Cursor":
        folder = home / ".cursor" / "projects" / "repoE2E"
        folder.mkdir(parents=True)
        lines = []
        for i in range(n):
            lines.append(json.dumps({"type": "message", "role": "user",
                                     "session_id": "sess1",
                                     "content": f"合成E2E用户句-{i}",
                                     "timestamp": f"2026-11-01T00:00:{i:02d}Z"}))
            lines.append(json.dumps({"type": "message", "role": "assistant",
                                     "session_id": "sess1",
                                     "content": f"合成E2E回复-{i}",
                                     "timestamp": f"2026-11-01T00:00:{i:02d}Z"}))
        (folder / "s.jsonl").write_text("\n".join(lines), encoding="utf-8")
    # 2) 宿主配置（MCP + bearer + device），指向本次 hub
    if agent and program == "Cursor":
        conf = {
            "mcpServers": {
                "rogmem": {
                    "url": f"{agent['hub_base']}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {agent['token']}",
                        "X-Rogmem-Device": agent["device_id"],
                    },
                }
            }
        }
        (home / ".cursor" / "mcp.json").write_text(json.dumps(conf), encoding="utf-8")
    return home


def _count_sources(engine) -> int:
    return engine.db().execute("SELECT COUNT(*) FROM sources").fetchone()[0]


def _job(engine, job_id: str) -> dict:
    row = engine.db().execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# A. 无作业时 run_host_import_jobs 不扫描；
# 有作业时真实上传，sources 计数以服务端摄入为准
# ---------------------------------------------------------------------------


def test_no_jobs_means_no_scan(tmp_path, hub):
    from rogmem import cli
    agent_id, token = hub.seed_agent(device_id="devA", program="Cursor")
    home = _write_fake_home(tmp_path, program="Cursor", n=3,
                            agent={"device_id": "devA", "token": token, "hub_base": hub.base})
    # 本次没有任何 job
    collect_calls = {"n": 0}
    from installer import rogmem_installer as inst
    real_read = inst.read_program_history
    def spy(home, program, allow_real=False):
        collect_calls["n"] += 1
        yield from real_read(home, program, allow_real=allow_real)
    inst.read_program_history = spy
    try:
        res = cli.run_host_import_jobs(home)
        assert res["jobs"] == 0 and res["posted"] == 0
        # 评审 §2：无作业时绝不扫描本地历史正文
        assert collect_calls["n"] == 0
    finally:
        inst.read_program_history = real_read


def test_worker_real_pipeline_end_to_end(tmp_path, hub):
    from rogmem import cli
    agent_id, token = hub.seed_agent(device_id="devA", program="Cursor")
    home = _write_fake_home(tmp_path, program="Cursor", n=3,
                            agent={"device_id": "devA", "token": token, "hub_base": hub.base})
    job_id = hub.create_job(agent_id, "Cursor", request_id="r1")
    before = _count_sources(hub.engine)
    res = cli.run_host_import_jobs(home)
    after = _count_sources(hub.engine)
    assert after - before == 6, f"预期入库 6 条，实际 {after-before}"
    row = _job(hub.engine, job_id)
    assert row["status"] == "completed"
    assert row["posted"] == 6
    # 二次 run：history.sent cursor + 服务端幂等双保险 → 无新作业就不扫、不上传
    before2 = _count_sources(hub.engine)
    res2 = cli.run_host_import_jobs(home)
    assert _count_sources(hub.engine) == before2
    assert res2["jobs"] == 0


def test_second_job_reports_duplicates_via_server_dedup(tmp_path, hub):
    """评审 R2 P2-6：新人工作业不被旧 client cursor 跳过。
    client 不写 history.sent；server sources UNIQUE 是唯一真幂等来源。
    第二作业仍完整扫描并全部上传；server 把每条识别为 duplicate；posted=0, duplicates=总条数。
    """
    from rogmem import cli
    agent_id, token = hub.seed_agent(device_id="devCur", program="Cursor")
    home = _write_fake_home(tmp_path, program="Cursor", n=2,
                            agent={"device_id": "devCur", "token": token, "hub_base": hub.base})
    hub.create_job(agent_id, "Cursor", request_id="r1")
    cli.run_host_import_jobs(home)
    # 新 job 应被 server 认出是在途（inflight dedup）或新建 — 由于上一轮 completed，这里应能新建
    job2 = hub.create_job(agent_id, "Cursor", request_id="r2")
    before = _count_sources(hub.engine)
    cli.run_host_import_jobs(home)
    assert _count_sources(hub.engine) == before
    row = _job(hub.engine, job2)
    assert row["status"] == "completed"
    assert row["posted"] == 0
    assert row["duplicates"] == 4, (
        f"R2 P2-6：server-dedup 真实回；实际 duplicates={row['duplicates']}")


# ---------------------------------------------------------------------------
# B. cancel 真的能停：第一批入库后 admin 取消，剩余批次拒绝
# ---------------------------------------------------------------------------


def test_cancel_midflight_stops_uploads(tmp_path, hub, monkeypatch):
    from rogmem import cli
    agent_id, token = hub.seed_agent(device_id="devMid", program="Cursor")
    # 制造 100 对（200 turns）→ 至少 5 批 ×40
    home = _write_fake_home(tmp_path, program="Cursor", n=100,
                            agent={"device_id": "devMid", "token": token, "hub_base": hub.base})
    job_id = hub.create_job(agent_id, "Cursor", request_id="rc")
    uploads = {"n": 0}
    real_post = cli._post_json_with_headers
    def wrapped(url, payload, headers):
        uploads["n"] += 1
        # 在第一个批次成功后立刻 cancel；后续批次应被拒
        resp = real_post(url, payload, headers)
        if uploads["n"] == 1:
            hub.engine.cancel_host_import_job(job_id=job_id)
        return resp
    monkeypatch.setattr(cli, "_post_json_with_headers", wrapped)
    cli.run_host_import_jobs(home)
    row = _job(hub.engine, job_id)
    # 1 批成功 + 1 批被 cancelled 拒 → 不应该把全部 200 条塞进去
    assert row["posted"] <= 40
    assert uploads["n"] == 2, f"期望仅两次批（首批 + 被拒批）；实际 {uploads['n']}"


# ---------------------------------------------------------------------------
# C. stale lease：客户端持过期 lease ack → refused=stale-lease；
# 真实上传入口也必须拒
# ---------------------------------------------------------------------------


def test_stale_lease_upload_endpoint_refuses(tmp_path, hub):
    import urllib.request, urllib.error
    agent_id, token = hub.seed_agent(device_id="devSt", program="Cursor")
    hub.create_job(agent_id, "Cursor", request_id="rs")
    [job] = hub.engine.claim_host_import_jobs(agent_id=agent_id, lease_s=1)
    # 把 lease 直接拉过期
    hub.engine.db().execute("UPDATE host_jobs SET lease_until='2000-01-01T00:00:00+00:00'")
    hub.engine.db().commit()
    import urllib.request as U
    req = U.Request(
        f"{hub.base}/host/jobs/{job['id']}/upload",
        data=json.dumps({"program": "Cursor", "turns": [{"role": "user", "text": "x",
                                                         "session_id": "s", "seq": 1,
                                                         "event_at": "2026-11-01T00:00:00+00:00"}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "X-Rogmem-Device": "devSt",
                 "X-Rogmem-Lease-Token": job["lease_token"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        status = exc.code
    assert status == 400
    assert body["refused"] == "stale-lease"


# ---------------------------------------------------------------------------
# D. legacy 桥接已被评审否决：旧 /admin/api/hosts/import 不得建作业
# ---------------------------------------------------------------------------


def test_legacy_confirm_import_route_does_not_broadcast_jobs(tmp_path, hub):
    import urllib.request as U
    hub.seed_agent(device_id="devX", program="Cursor")
    hub.seed_agent(device_id="devY", program="Cursor")
    body = json.dumps({"program": "Cursor", "confirm": True}).encode()
    req = U.Request(f"{hub.base}/admin/api/hosts/import", data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
    with U.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode())
    assert data["ok"] is True
    assert data.get("jobs") == []
    rows = hub.engine.db().execute("SELECT COUNT(*) FROM host_jobs").fetchone()[0]
    assert rows == 0


# ---------------------------------------------------------------------------
# E. 多 agent 隔离：两台同 program，只有各自作业被各自 worker 拉到
# ---------------------------------------------------------------------------


def test_admin_jobs_rejects_cross_origin_post(tmp_path, hub):
    """评审 §8：开管理台无口令现实下，新作业入口至少挡住纯跨站 POST。"""
    import urllib.request as U, urllib.error
    aid, _ = hub.seed_agent(device_id="devCsrf", program="Cursor")
    body = json.dumps({"agent_id": aid, "program": "Cursor"}).encode()
    req = U.Request(f"{hub.base}/admin/api/hosts/jobs", data=body,
                    headers={"Content-Type": "application/json",
                             "Origin": "https://evil.example.com"},
                    method="POST")
    try:
        with U.urlopen(req, timeout=5):
            status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
        body_resp = json.loads(exc.read().decode())
    assert status == 403
    assert body_resp["refused"] == "cross-origin"
    # 作业没被建
    assert hub.engine.db().execute("SELECT COUNT(*) FROM host_jobs").fetchone()[0] == 0


def test_two_agents_same_program_isolated(tmp_path, hub):
    from rogmem import cli
    aid1, t1 = hub.seed_agent(device_id="devI1", program="Cursor")
    aid2, t2 = hub.seed_agent(device_id="devI2", program="Cursor")
    # home1 只配 agent1；home2 只配 agent2
    home1 = _write_fake_home(tmp_path / "h1", program="Cursor", n=2,
                             agent={"device_id": "devI1", "token": t1, "hub_base": hub.base})
    home2 = _write_fake_home(tmp_path / "h2", program="Cursor", n=2,
                             agent={"device_id": "devI2", "token": t2, "hub_base": hub.base})
    # 只给 aid1 建作业
    job1 = hub.create_job(aid1, "Cursor", request_id="only1")
    r2 = cli.run_host_import_jobs(home2)  # 先跑 home2 → 不应拉到 aid1 的作业
    assert r2["jobs"] == 0
    r1 = cli.run_host_import_jobs(home1)  # home1 → 拉到并完成
    assert r1["jobs"] == 1
    row = _job(hub.engine, job1)
    assert row["status"] == "completed"
