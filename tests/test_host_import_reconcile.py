"""导入对账：待导入会话数按幂等键终态算，不再用两个不相干计数相减。

回归的是「反复点击导入历史始终留残数」：中枢按 idempotency_key 判重，
判重的条目已经存在于库里，但只挂在另一个宿主名下，本机 history_count 不增长；
旧口径用「本机扫描总数 − 本机 history_count」做减法，于是这批终态永远被算成待导入。
本文件用临时 sqlite，不访问真实 HOME、不连 MCP、不读正文。
"""

from __future__ import annotations

import os
from pathlib import Path


def _open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROGMEM_DATA", str(tmp_path))
    monkeypatch.setenv("ROGMEM_SCAN", str(tmp_path / "scan"))
    monkeypatch.setenv("ROGMEM_EMBED", "off")
    monkeypatch.setenv("ROGMEM_NO_PASSWORD", "1")
    os.environ.setdefault("ROGMEM_SKIP_SCHEDULER", "1")
    from rogmem import engine

    engine.connect(tmp_path, tmp_path / "scan")
    engine.set_embed_mode("off")
    return engine


def _agent(engine, device_id: str, program: str) -> str:
    from rogmem import crypto

    public = crypto.b64encode(crypto.new_keypair()[1])
    applied = engine.enroll_apply(device_id, device_id, program, public)
    assert applied["ok"], applied
    assert engine.enroll_approve(applied["id"])["ok"]
    assert engine.enroll_claim(public)["ok"]
    assert engine.set_agent_capabilities(
        agent_id=applied["id"], capabilities="host_import_jobs:v1"
    )["ok"]
    return applied["id"]


def _turns(prefix: str, n: int, *, refused: int = 0) -> list[dict]:
    out = []
    for i in range(n):
        out.append({
            "role": "user",
            "text": f"{prefix}-正文-{i}",
            "event_at": "2026-11-24T00:00:00+00:00",
            "session_id": f"{prefix}-s{i}",
            "seq": i,
        })
    for i in range(refused):
        # 缺时间或缺正文：可扫到、可被计数，但中枢确认无法导入。
        out.append({
            "role": "user",
            "text": f"{prefix}-拒收-{i}",
            "event_at": "",
            "session_id": f"{prefix}-bad{i}",
            "seq": 100 + i,
        })
    return out


def _run_job(engine, agent_id: str, program: str, turns: list[dict], request_id: str) -> dict:
    created = engine.create_host_import_job(
        agent_id=agent_id, program=program, request_id=request_id
    )
    assert created["ok"], created
    claimed = engine.claim_host_import_jobs(agent_id=agent_id)
    assert claimed, "作业没有被认领"
    job = claimed[0]
    result = engine.ingest_history_for_job(
        agent_id=agent_id,
        job_id=job["id"],
        lease_token=job["lease_token"],
        program=program,
        turns=turns,
    )
    assert result["ok"], result
    acked = engine.ack_host_import_job(
        agent_id=agent_id,
        job_id=job["id"],
        lease_token=job["lease_token"],
        status="completed",
    )
    assert acked["ok"], acked
    return result


def _row(engine, device_id: str) -> dict:
    rows = engine.list_connected_hosts(viewer_device_id=device_id)["rows"]
    return next(item for item in rows if item["device_id"] == device_id)


def _pending_span(engine, device_id: str) -> str:
    from rogmem import ui

    page = ui.hosts_page(
        engine.list_hosts(),
        engine.list_connected_hosts(viewer_device_id=device_id),
        {},
    )
    row = page.split(f"data-device-id='{device_id}'", 1)[1].split("</tr>", 1)[0]
    segment = row.split("class='scan-pending'", 1)[1].split("</span>", 1)[0]
    return "class='scan-pending'" + segment + "</span>"


def test_duplicates_bound_to_another_host_no_longer_look_pending(tmp_path, monkeypatch):
    """核心回归：别的宿主先入库，本机再点导入 → posted=0 全量判重，但待导入必须归零。"""
    engine = _open(tmp_path, monkeypatch)
    owner = _agent(engine, "dev-owner", "Cursor")
    viewer = _agent(engine, "dev-viewer", "Cursor")
    turns = _turns("shared", 4)
    engine.report_hosts([{
        "program": "Cursor", "found": True, "access": "已接入",
        "health": "正常", "detail": "", "importable": 4,
    }])

    # 先在另一台电脑上把这 4 条导入，sources 挂在 dev-owner 名下。
    first = _run_job(engine, owner, "Cursor", turns, "r-owner-1")
    assert first["posted"] == 4 and first["duplicates"] == 0

    # 本机第一次点：服务端全部判重，本机 history_count 仍然是 0。
    second = _run_job(engine, viewer, "Cursor", turns, "r-viewer-1")
    assert second["posted"] == 0 and second["duplicates"] == 4
    assert _row(engine, "dev-viewer")["history_count"] == 0

    # 账本认的是「已确认在库」，不是「挂在谁名下」。
    assert _row(engine, "dev-viewer")["confirmed"] == 4
    assert ">0<" in _pending_span(engine, "dev-viewer")

    # 反复点击：账本不重复膨胀，待导入保持 0。
    third = _run_job(engine, viewer, "Cursor", turns, "r-viewer-2")
    assert third["posted"] == 0 and third["duplicates"] == 4
    assert _row(engine, "dev-viewer")["confirmed"] == 4
    assert ">0<" in _pending_span(engine, "dev-viewer")
    assert engine.db().execute(
        "SELECT COUNT(*) FROM host_import_receipts WHERE device_id='dev-viewer'"
    ).fetchone()[0] == 4


def test_refused_turns_are_terminal_not_pending(tmp_path, monkeypatch):
    """可扫到但中枢拒收的条目是终态，必须单独说明，不能永远算待导入。"""
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-refuse", "Hermes")
    turns = _turns("mixed", 3, refused=2)
    engine.report_hosts([{
        "program": "Hermes", "found": True, "access": "已接入",
        "health": "正常", "detail": "", "importable": 5,
    }])

    first = _run_job(engine, agent, "Hermes", turns, "r1")
    assert first["posted"] == 3 and first["refused_count"] == 2
    row = _row(engine, "dev-refuse")
    assert row["confirmed"] == 3 and row["refused"] == 2
    assert ">0<" in _pending_span(engine, "dev-refuse")

    from rogmem import ui

    page = ui.hosts_page(
        engine.list_hosts(),
        engine.list_connected_hosts(viewer_device_id="dev-refuse"),
        {},
    )
    assert "跳过 2" in page

    second = _run_job(engine, agent, "Hermes", turns, "r2")
    assert second["posted"] == 0 and second["duplicates"] == 3
    assert _row(engine, "dev-refuse")["refused"] == 2
    assert ">0<" in _pending_span(engine, "dev-refuse")


def test_receipts_are_isolated_by_device_and_program(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    a = _agent(engine, "dev-a", "Cursor")
    b = _agent(engine, "dev-b", "Cursor")
    c = _agent(engine, "dev-c", "Hermes")

    _run_job(engine, a, "Cursor", _turns("a", 3), "ra")
    assert _row(engine, "dev-b")["confirmed"] is None  # 没账本 → 旧口径
    assert _row(engine, "dev-c")["confirmed"] is None

    _run_job(engine, b, "Cursor", _turns("b", 2), "rb")
    _run_job(engine, c, "Hermes", _turns("c", 1), "rc")
    summary = engine.host_receipt_summary()
    assert summary[("dev-a", "Cursor")]["confirmed"] == 3
    assert summary[("dev-b", "Cursor")]["confirmed"] == 2
    assert summary[("dev-c", "Hermes")]["confirmed"] == 1
    assert ("dev-a", "Hermes") not in summary


def test_identity_is_stable_for_accepted_and_refused(tmp_path, monkeypatch):
    engine = _open(tmp_path, monkeypatch)
    from rogmem.engine import _history_idem, _prepare_history_jobs

    jobs, refused, keys = _prepare_history_jobs(_turns("x", 2, refused=1), "Cursor", None)
    assert len(jobs) == 2 and refused == 1 and len(keys) == 1
    assert jobs[0]["idem"] == _history_idem("x-s0", 0, "x-正文-0") or any(
        job["idem"] == _history_idem("x-s0", 0, "x-正文-0") for job in jobs
    )
    assert keys[0] == _history_idem("x-bad0", 100, "x-拒收-0")
    again_jobs, again_refused, again_keys = _prepare_history_jobs(
        _turns("x", 2, refused=1), "Cursor", None
    )
    assert [j["idem"] for j in again_jobs] == [j["idem"] for j in jobs]
    assert again_keys == keys and again_refused == refused


def test_backfill_recovers_existing_sources_without_duplicates(tmp_path, monkeypatch):
    """老库升级：账本为空时，已存在的 sources 一次回填成已确认，重复启动不重写。"""
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-old", "Cursor")
    _run_job(engine, agent, "Cursor", _turns("legacy", 3), "r1")
    assert engine.db().execute(
        "SELECT COUNT(*) FROM host_import_receipts"
    ).fetchone()[0] == 3

    engine.db().execute("DELETE FROM host_import_receipts")
    engine.db().commit()
    assert engine.host_receipt_summary() == {}

    engine.connect(tmp_path, tmp_path / "scan")
    assert engine.host_receipt_summary()[("dev-old", "Cursor")]["confirmed"] == 3
    before = engine.db().execute(
        "SELECT COUNT(*) FROM host_import_receipts"
    ).fetchone()[0]
    engine.connect(tmp_path, tmp_path / "scan")
    after = engine.db().execute(
        "SELECT COUNT(*) FROM host_import_receipts"
    ).fetchone()[0]
    assert after == before == 3


def test_page_and_js_contract_use_the_reconciliation_basis(tmp_path, monkeypatch):
    """服务端渲染与浏览器刷新必须用同一套减法；完成态文案不能只写 posted。"""
    from rogmem import ui

    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-js", "WorkBuddy")
    _run_job(engine, agent, "WorkBuddy", _turns("js", 2), "r1")
    page = ui.hosts_page(
        engine.list_hosts(),
        engine.list_connected_hosts(viewer_device_id="dev-js"),
        {},
    )
    row = page.split("data-device-id='dev-js'", 1)[1].split("</tr>", 1)[0]
    assert "data-confirmed='2'" in row
    assert "data-refused='0'" in row
    assert "confirmedAttr" in page          # JS 走账本口径
    assert "data-confirmed" in page         # 进度接口写回行属性
    assert "完成 已确认 " in page            # 全量判重也是完成
    assert "item.confirmed" in page
    assert "完成 posted=" not in page

    # 没有账本的行保留旧减法，不把升级前的数字突然放大。
    assert ">7<" in ui._import_cell({"importable": 10}, 3)
    assert ">2<" in ui._import_cell({"importable": 10}, 3, confirmed=5, refused=3)
    assert ">0<" in ui._import_cell({"importable": 10}, 3, confirmed=10, refused=0)
    assert "跳过 3" in ui._import_cell({"importable": 10}, 3, confirmed=5, refused=3)


# ---------------------------------------------------------------------------
# 第二层：本机 importable 数的是「原始行」，账本按「会话身份」去重。
# 同一条原文在历史里出现两次时，账本只能记 1 个身份，于是永远留下清不掉的残数。
# 完成作业的 posted+duplicates+refused 才是同一口径的原始行数。
# ---------------------------------------------------------------------------

def _report(engine, program: str, importable: int) -> None:
    engine.report_hosts([{
        "program": program, "found": True, "access": "已接入",
        "health": "正常", "detail": "", "importable": importable,
    }])


def test_repeated_identical_rows_no_longer_stay_pending(tmp_path, monkeypatch):
    """历史里同一条原文出现两次：账本记 3 个身份，作业核销 5 行，待导入必须为 0。"""
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-dup", "Hermes")
    base = _turns("dup", 3)
    turns = base + [dict(base[0]), dict(base[1])]  # 5 行原文，只有 3 个身份
    _report(engine, "Hermes", 5)

    first = _run_job(engine, agent, "Hermes", turns, "dup-1")
    assert first["posted"] == 3 and first["duplicates"] == 2
    row = _row(engine, "dev-dup")
    assert row["confirmed"] == 3        # 账本按身份去重，永远到不了 5
    assert row["reconciled"] == 5       # 作业按原始行核销
    assert ">0<" in _pending_span(engine, "dev-dup")

    # 反复点击：全量判重，残数不能回来。
    second = _run_job(engine, agent, "Hermes", turns, "dup-2")
    assert second["posted"] == 0 and second["duplicates"] == 5
    assert _row(engine, "dev-dup")["reconciled"] == 5
    assert ">0<" in _pending_span(engine, "dev-dup")


def test_pending_only_counts_rows_added_since_last_job(tmp_path, monkeypatch):
    """作业核销的是当时的扫描快照；之后新增的历史只显示新增的那几行。"""
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-grow", "WorkBuddy")
    _report(engine, "WorkBuddy", 4)
    _run_job(engine, agent, "WorkBuddy", _turns("grow", 4), "grow-1")
    assert _row(engine, "dev-grow")["reconciled"] == 4
    assert ">0<" in _pending_span(engine, "dev-grow")

    _report(engine, "WorkBuddy", 7)
    assert ">3<" in _pending_span(engine, "dev-grow")

    _run_job(engine, agent, "WorkBuddy", _turns("grow", 7), "grow-2")
    assert _row(engine, "dev-grow")["reconciled"] == 7
    assert ">0<" in _pending_span(engine, "dev-grow")


def test_no_completed_job_falls_back_to_ledger(tmp_path, monkeypatch):
    """没完成过作业的设备不虚构 reconciled，继续用身份账本，数字不放大。"""
    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-nojob", "Cursor")
    _report(engine, "Cursor", 4)
    assert engine.host_scan_reconciliation() == {}
    assert _row(engine, "dev-nojob")["reconciled"] is None

    created = engine.create_host_import_job(
        agent_id=agent, program="Cursor", request_id="nojob-1"
    )
    assert created["ok"]
    claimed = engine.claim_host_import_jobs(agent_id=agent)
    assert claimed
    engine.ingest_history_for_job(
        agent_id=agent, job_id=claimed[0]["id"],
        lease_token=claimed[0]["lease_token"], program="Cursor",
        turns=_turns("nojob", 4),
    )
    # 作业还在 running，没有终态，reconciled 仍缺席，账本口径照常工作。
    assert engine.host_scan_reconciliation() == {}
    row = _row(engine, "dev-nojob")
    assert row["reconciled"] is None and row["confirmed"] == 4
    assert ">0<" in _pending_span(engine, "dev-nojob")

    engine.ack_host_import_job(
        agent_id=agent, job_id=claimed[0]["id"],
        lease_token=claimed[0]["lease_token"], status="completed",
    )
    assert engine.host_scan_reconciliation()[("dev-nojob", "Cursor")] == 4


def test_reconciled_contract_is_rendered_and_refreshed(tmp_path, monkeypatch):
    """服务端渲染、进度接口前端写回、浏览器本地刷新必须都认 reconciled。"""
    from rogmem import ui

    engine = _open(tmp_path, monkeypatch)
    agent = _agent(engine, "dev-contract", "Hermes")
    base = _turns("c", 3)
    _report(engine, "Hermes", 5)
    _run_job(engine, agent, "Hermes", base + [dict(base[0]), dict(base[1])], "c-1")

    page = ui.hosts_page(
        engine.list_hosts(),
        engine.list_connected_hosts(viewer_device_id="dev-contract"),
        {},
    )
    row = page.split("data-device-id='dev-contract'", 1)[1].split("</tr>", 1)[0]
    assert "data-reconciled='5'" in row
    assert "reconciledAttr" in page and "item.reconciled" in page
    assert ">0<" in _pending_span(engine, "dev-contract")

    # 三种证据取最大值，不叠加、不重复减 refused。
    assert ">0<" in ui._import_cell({"importable": 5}, 3, confirmed=3, refused=0, reconciled=5)
    assert ">2<" in ui._import_cell({"importable": 7}, 3, confirmed=3, refused=0, reconciled=5)
    assert ">0<" in ui._import_cell({"importable": 5}, 3, confirmed=2, refused=1, reconciled=5)
    assert ">7<" in ui._import_cell({"importable": 10}, 3, reconciled=None)
