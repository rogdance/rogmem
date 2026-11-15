"""Time a card fold before and after closed-hour seals.

Synthetic ledger only. The baseline is the old whole-window group. The warm
read should touch model_calls only for the open hour.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROWS = 80_000
HOURS = 400


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="rogmem-bench-"))
    os.environ["ROGMEM_DATA"] = str(root)
    os.environ["ROGMEM_SCAN"] = str(root / "scan")
    os.environ["ROGMEM_NO_PASSWORD"] = "1"
    os.environ["ROGMEM_EMBED"] = "off"
    os.environ["ROGMEM_SKIP_SCHEDULER"] = "1"
    from rogmem import cost, engine

    engine.connect(root, root / "scan")
    engine.set_embed_mode("off")
    now = datetime(2026, 11, 15, 4, 30, tzinfo=timezone.utc)
    start = now - timedelta(hours=HOURS)
    with cost._store(write=True) as con:
        payload = []
        for index in range(ROWS):
            hour = index % HOURS
            moment = start + timedelta(hours=hour, minutes=index % 50)
            payload.append(
                (
                    moment.isoformat(),
                    "速写" if index % 2 == 0 else "并页",
                    "厂",
                    "甲",
                    10 + (index % 7),
                    3,
                    1,
                    "问",
                    "答",
                )
            )
        # A few rows in the still-open hour.
        for index in range(30):
            moment = now - timedelta(minutes=index + 1)
            payload.append((moment.isoformat(), "速写", "厂", "甲", 5, 1, 0, "问", "答"))
        con.executemany(
            """INSERT INTO model_calls(at, worker, provider_id, model, input_tokens, output_tokens, cache_tokens, prompt, answer)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        con.commit()
    window_start = start
    with cost._store() as con:
        began = time.perf_counter()
        scanned = cost._group_sums(con, start=window_start, end=now, grain="hour", worker="", model="")
        baseline_ms = (time.perf_counter() - began) * 1000
        baseline_tokens = sum(int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) for row in scanned)
        began = time.perf_counter()
        cold = cost._window_groups(con, start=window_start, end=now, grain="hour", worker="", model="")
        cold_ms = (time.perf_counter() - began) * 1000
        cold_tokens = sum(int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) for row in cold)
        seen: list[str] = []
        con.set_trace_callback(lambda sql: seen.append(" ".join(sql.split())))
        began = time.perf_counter()
        warm = cost._window_groups(con, start=window_start, end=now, grain="hour", worker="", model="")
        warm_ms = (time.perf_counter() - began) * 1000
        warm_tokens = sum(int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) for row in warm)
    groups = [sql for sql in seen if "model_calls" in sql and "GROUP BY" in sql]
    print(
        {
            "rows": len(payload),
            "closed_hours": HOURS,
            "baseline_ms": round(baseline_ms, 1),
            "cold_ms": round(cold_ms, 1),
            "warm_ms": round(warm_ms, 1),
            "baseline_tokens": baseline_tokens,
            "cold_tokens": cold_tokens,
            "warm_tokens": warm_tokens,
            "warm_model_call_groups": len(groups),
            "warm_groups_are_bounded": all("at_jd" in sql for sql in groups),
        }
    )
    if baseline_tokens != warm_tokens or cold_tokens != warm_tokens:
        raise SystemExit("token totals diverged")
    if not groups or not all("at_jd" in sql for sql in groups):
        raise SystemExit("warm read scanned model_calls without an hour bound")


if __name__ == "__main__":
    main()
