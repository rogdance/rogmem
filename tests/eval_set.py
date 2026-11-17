"""Multi-item eval floor. Scores are 0–2 per dimension; FLOOR is not a trivial all-1s gate."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_PATH = Path(__file__).with_name("eval_baseline.json")

SCALE_MAX = 2
FLOOR = {"召回": 2, "先后": 2, "更新": 2, "矛盾": 2}
DIMENSIONS = ("召回", "先后", "更新", "矛盾")


def load_baseline() -> dict:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {key: int(data[key]) for key in DIMENSIONS}


def judge(scores: dict, baseline: dict, floor: dict = FLOOR) -> None:
    """Fail when baseline is below FLOOR, or current scores regress below baseline."""
    missing = [key for key, need in floor.items() if int(baseline.get(key, 0)) < need]
    if missing:
        raise AssertionError(f"baseline below FLOOR: {missing} baseline={baseline} floor={floor}")
    low = [key for key, need in baseline.items() if int(scores.get(key, 0)) < need]
    if low:
        raise AssertionError(f"scores below baseline: {low} scores={scores} baseline={baseline}")


def _core_field(name: str) -> str:
    from rogmem import engine

    loaded = engine.core_memory(budget=8000).get("loaded") or []
    return next((item["text"] for item in loaded if item["name"] == name), "")


def _put_source(kind: str, body: str, event_at: str):
    from rogmem import engine

    sid = secrets.token_hex(8)
    con = engine.db()
    con.execute(
        """INSERT INTO sources(id,kind,ingested_at,content_hash,body,status,lane,event_at)
           VALUES(?,?,?,?,?,'已蒸馏','fast',?)""",
        (sid, kind, event_at, hashlib.sha256(body.encode()).hexdigest(), body, event_at),
    )
    con.commit()
    return con.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()


def _write(kind: str, field: str, value: str, quote: str, event_at: str, extra: str = "") -> None:
    from rogmem import engine

    body = extra + quote
    source = _put_source(kind, body, event_at)
    engine._write_field(
        source,
        {"field": field, "value": value, "quote": quote, "crystal": "记忆主人", "trust": "written"},
        engine.rank_of(source),
        body,
    )


def _recall_name() -> bool:
    from rogmem import engine

    _write("user", "真名", "韩跑跑", "韩跑跑", "2026-10-24T01:00:00+00:00", "我叫")
    found = engine.search("韩跑跑")
    blob = json.dumps(found, ensure_ascii=False)
    return "韩跑跑" in blob and bool(found.get("crystals"))


def _recall_raw() -> bool:
    from rogmem import engine

    source = _put_source("file", "筐里还有白菜没写进结晶", "2026-10-24T01:05:00+00:00")
    engine._index_text(engine.db(), source["id"], source["body"])
    engine.db().commit()
    found = engine.search("白菜")
    return any("白菜" in item.get("text", "") for item in found.get("raw", []))


def _order_style() -> bool:
    _write("user", "沟通风格", "先看结论", "先看结论", "2026-10-24T04:10:00+00:00", "我喜欢")
    _write("user", "沟通风格", "写长文", "写长文", "2026-10-24T04:20:00+00:00", "我后来喜欢")
    text = _core_field("沟通风格")
    return "写长文" in text and "先看结论" not in text


def _order_job() -> bool:
    _write("user", "出生时间", "1990-01-01", "1990-01-01", "2026-10-24T06:00:00+00:00", "出生时间是")
    _write("user", "出生时间", "1991-02-02", "1991-02-02", "2026-10-24T06:10:00+00:00", "出生时间改成")
    text = _core_field("出生时间")
    return "1991-02-02" in text and "1990-01-01" not in text


def _update_user_over_summary() -> bool:
    _write("summary", "出生时间", "1980-03-03", "1980-03-03", "2026-10-24T07:00:00+00:00", "出生时间是")
    _write("user", "出生时间", "1990-01-01", "1990-01-01", "2026-10-24T07:10:00+00:00", "出生时间是")
    text = _core_field("出生时间")
    return "1990-01-01" in text and "1980-03-03" not in text


def _update_file_cannot_override_user() -> bool:
    _write("user", "所在地", "深圳", "深圳", "2026-10-24T07:20:00+00:00", "人在")
    _write("file", "所在地", "上海", "上海", "2026-10-24T07:30:00+00:00", "档案写所在地是")
    text = _core_field("所在地")
    return "深圳" in text and "上海" not in text


def _conflict_jobs() -> bool:
    from rogmem import engine

    _write("summary", "出生时间", "1980-03-03", "1980-03-03", "2026-10-24T08:00:00+00:00", "出生时间是")
    _write("summary", "出生时间", "1991-02-02", "1991-02-02", "2026-10-24T08:01:00+00:00", "出生时间是")
    text = _core_field("出生时间")
    kinds = [row[0] for row in engine.db().execute("SELECT kind FROM decisions")]
    return "尚无来源" in text and "无法说定" in kinds


def _conflict_sex() -> bool:
    from rogmem import engine

    _write("summary", "性别", "男", "男", "2026-10-24T08:10:00+00:00", "性别是")
    _write("summary", "性别", "女", "女", "2026-10-24T08:11:00+00:00", "性别是")
    text = _core_field("性别")
    kinds = [row[0] for row in engine.db().execute("SELECT kind FROM decisions")]
    return "尚无来源" in text and "无法说定" in kinds


ITEMS = {
    "召回": (_recall_name, _recall_raw),
    "先后": (_order_style, _order_job),
    "更新": (_update_user_over_summary, _update_file_cannot_override_user),
    "矛盾": (_conflict_jobs, _conflict_sex),
}


_ENV_KEYS = (
    "ROGMEM_DATA",
    "ROGMEM_SCAN",
    "ROGMEM_NO_PASSWORD",
    "ROGMEM_EMBED",
    "ROGMEM_LLM_BASE",
    "ROGMEM_LLM_MODEL",
)


def _snapshot_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in _ENV_KEYS}


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _start_stub() -> object:
    try:
        from tests.llm_stub import serve
    except ModuleNotFoundError:
        from llm_stub import serve  # type: ignore

    stub = serve(0)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    host, port = stub.server_address[:2]
    os.environ["ROGMEM_LLM_BASE"] = f"http://{host}:{port}/v1"
    os.environ["ROGMEM_LLM_MODEL"] = "stub"
    os.environ["ROGMEM_NO_PASSWORD"] = "1"
    os.environ["ROGMEM_EMBED"] = "hash"
    return stub


def _connect(tmp: Path) -> None:
    from rogmem import engine

    os.environ["ROGMEM_DATA"] = str(tmp)
    os.environ["ROGMEM_SCAN"] = str(tmp / "scan")
    engine.connect(tmp, tmp / "scan")
    engine.set_embed_mode("hash")


def score_all() -> dict[str, int]:
    root = Path(tempfile.mkdtemp(prefix="rogmem-eval-"))
    saved = _snapshot_env()
    stub = _start_stub()
    scores = {key: 0 for key in DIMENSIONS}
    try:
        n = 0
        for dim, cases in ITEMS.items():
            for case in cases:
                n += 1
                _connect(root / f"case-{n}")
                if case():
                    scores[dim] += 1
        return scores
    finally:
        stub.shutdown()
        _restore_env(saved)


def main() -> int:
    scores = score_all()
    baseline = load_baseline()
    print(json.dumps({"scores": scores, "baseline": baseline, "floor": FLOOR}, ensure_ascii=False))
    judge(scores, baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
