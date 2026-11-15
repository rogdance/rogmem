"""How long a provider was available, degraded, or down inside each chart slot.

A probe is a point. It stands for the state until the next probe, but not across
a silence longer than twice the probe interval: a gap that long means nobody
looked, and that stretch stays unknown instead of inheriting an old colour.

A slot then keeps four durations that add up to the slot itself: ok, warn, down,
unknown. The square uses the longer impairment, or available when none was seen.
Its depth is that state's share of the slot, so a partly observed hour stays
paler than an hour that was in that state the whole time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_STATES = ("ok", "warn", "down")


def note(con: sqlite3.Connection, provider_id: str, health: str, at: str) -> None:
    if health not in _STATES or not str(provider_id or "").strip() or not str(at or "").strip():
        return
    con.execute(
        "INSERT INTO provider_health(provider_id, at, health) VALUES(?,?,?)",
        (provider_id, at, health),
    )


def hold_for(interval_seconds: float) -> timedelta:
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError):
        interval = 300.0
    if interval < 1:
        interval = 300.0
    return timedelta(seconds=interval * 2)


def tone(ok: int, warn: int, down: int, unknown: int) -> tuple[str, float]:
    """Hue is the longer impairment, or available when nothing impaired was seen.

    Depth is that state's share of the whole slot. A full slot is a solid color.
    A slot only partly observed stays pale. Nothing observed stays hollow.
    """

    span = ok + warn + down + unknown
    if span <= 0 or ok + warn + down <= 0:
        return "unknown", 0.0
    if down <= 0 and warn <= 0:
        return "ok", ok / span
    if down >= warn:
        return "down", down / span
    return "warn", warn / span


def _parse(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ms(moment: datetime) -> int:
    return int(round(moment.timestamp() * 1000))


def _model_name(provider: dict, hinted: str) -> str:
    models = [str(name) for name in (provider.get("models") or []) if str(name).strip()]
    if hinted and hinted in models:
        return hinted
    kinds = provider.get("model_kinds") if isinstance(provider.get("model_kinds"), dict) else {}
    for name in models:
        if kinds.get(name) != "embed":
            return name
    return models[0] if models else ""


def choose_target(providers: list[dict], distill: str, preferred_id: str = "minimax-cn") -> tuple[dict, str] | None:
    """The provider the pipeline is calling.

    Throughput can only be compared with the model that is actually assigned.
    A fresh install has no assignment yet, so the built-in MiniMax row stands in.
    """

    rows = [item for item in providers if isinstance(item, dict)]
    ident, hinted = "", ""
    if distill and "|" in distill:
        ident, hinted = distill.split("|", 1)
    by_id = {str(item.get("id") or ""): item for item in rows}
    if ident and ident in by_id:
        return by_id[ident], _model_name(by_id[ident], hinted)
    for item in rows:
        if str(item.get("id") or "") == preferred_id:
            return item, _model_name(item, "")
    return None


# The ledger's embed worker is a different model from the one the pipeline is calling.
_EMBED_WORKER = "向量嵌入"


def tenures(
    calls: list[tuple[datetime, str, str]],
    start: datetime,
    end: datetime,
    fallback: tuple[str, str] | None = None,
) -> list[tuple[datetime, datetime, str, str]]:
    """Who was being called across [start, end).

    A chat call is evidence that this provider and model were invoked then.
    That choice stays in force until a later call names a different one.
    Before any call, the configured assignment is the only evidence.
    A later call is not painted backward over the time that preceded it.
    """

    changes: list[tuple[datetime, str, str]] = []
    for moment, provider_id, label in sorted(calls, key=lambda item: item[0]):
        if not provider_id:
            continue
        if changes and changes[-1][1] == provider_id and changes[-1][2] == label:
            continue
        changes.append((moment, provider_id, label))
    if not changes:
        if fallback and fallback[0]:
            return [(start, end, fallback[0], fallback[1])]
        return []
    in_force: tuple[str, str] | None = None
    later: list[tuple[datetime, str, str]] = []
    for moment, provider_id, label in changes:
        if moment <= start:
            in_force = (provider_id, label)
        else:
            later.append((moment, provider_id, label))
    if in_force is not None:
        points = [(start, in_force[0], in_force[1])]
    elif fallback and fallback[0]:
        points = [(start, fallback[0], fallback[1])]
    else:
        points = []
    for moment, provider_id, label in later:
        if not points or provider_id != points[-1][1] or label != points[-1][2]:
            points.append((moment, provider_id, label))
    spans: list[tuple[datetime, datetime, str, str]] = []
    for index, (moment, provider_id, label) in enumerate(points):
        lo = moment if moment > start else start
        hi = points[index + 1][0] if index + 1 < len(points) else end
        if hi > end:
            hi = end
        if hi > lo:
            spans.append((lo, hi, provider_id, label))
    return spans


def cover(
    samples: list[tuple[datetime, str]],
    start: datetime,
    end: datetime,
    hold: timedelta,
) -> list[tuple[int, int, str]]:
    """Non-overlapping [lo, hi) millisecond spans inside [start, end)."""

    # A sample exactly at `end` adds no time inside the window, but it still
    # stops the previous sample. Dropping it would let that previous state run on.
    usable = [(moment, health) for moment, health in samples if health in _STATES and moment <= end]
    usable.sort(key=lambda item: item[0])
    start_ms = _ms(start)
    end_ms = _ms(end)
    hold_ms = max(0, int(round(hold.total_seconds() * 1000)))
    spans: list[tuple[int, int, str]] = []
    for index, (moment, health) in enumerate(usable):
        opened = _ms(moment)
        closed = opened + hold_ms
        if index + 1 < len(usable):
            nxt = _ms(usable[index + 1][0])
            if nxt < closed:
                closed = nxt
        lo = opened if opened > start_ms else start_ms
        hi = closed if closed < end_ms else end_ms
        if hi > lo:
            spans.append((lo, hi, health))
    return spans


def _pack(span_ms: int, acc: dict[str, int]) -> dict[str, int]:
    span_s = int(round(span_ms / 1000))
    parts = {name: int(round(acc.get(name, 0) / 1000)) for name in _STATES}
    used = sum(parts.values())
    if used > span_s and parts:
        largest = max(parts, key=lambda name: parts[name])
        parts[largest] -= used - span_s
        used = span_s
    unknown = span_s - used
    if unknown < 0:
        unknown = 0
    return {"ok": parts["ok"], "warn": parts["warn"], "down": parts["down"], "unknown": unknown, "span": span_s}


def mix(spans: list[tuple[int, int, str]], start: datetime, end: datetime) -> dict[str, int]:
    lo = _ms(start)
    hi = _ms(end)
    span_ms = hi - lo if hi > lo else 0
    acc = {name: 0 for name in _STATES}
    if span_ms:
        for opened, closed, health in spans:
            left = opened if opened > lo else lo
            right = closed if closed < hi else hi
            if right > left and health in acc:
                acc[health] += right - left
    return _pack(span_ms, acc)


def _grain(window: dict, series: list[dict]) -> str:
    grain = str((window or {}).get("grain") or "")
    if grain not in {"hour", "day"} and series:
        grain = str(series[0].get("grain") or "")
    return grain if grain in {"hour", "day"} else "hour"


def _slots(series: list[dict], window: dict) -> list[tuple[str, datetime, datetime]]:
    step = timedelta(hours=1) if _grain(window, series) == "hour" else timedelta(days=1)
    w0 = _parse(str((window or {}).get("start") or ""))
    w1 = _parse(str((window or {}).get("end") or ""))
    found: list[tuple[str, datetime, datetime]] = []
    for item in series or []:
        if not isinstance(item, dict):
            continue
        at = str(item.get("at") or "")
        opened = _parse(at)
        if opened is None:
            continue
        closed = opened + step
        if w0 is not None and opened < w0:
            opened = w0
        if w1 is not None and closed > w1:
            closed = w1
        if closed <= opened:
            continue
        found.append((at, opened, closed))
    return found


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    return (
        con.execute(f"SELECT 1 FROM pragma_table_info('{table}') WHERE name=?", (column,)).fetchone()
        is not None
    )


def _load(con: sqlite3.Connection, provider_id: str, start: datetime, end: datetime, hold: timedelta) -> list[tuple[datetime, str]]:
    """Probes for one provider. Instants are compared after parsing, not as text.

    The stored instant limits the read to this window. Parsing still decides
    membership, so a clock shape the index understood loosely can be dropped.
    """

    lo = start - hold
    if _has_column(con, "provider_health", "probed_jd"):
        pad = timedelta(seconds=2)
        rows = con.execute(
            """SELECT at, health FROM provider_health
               WHERE provider_id=? AND probed_jd IS NOT NULL
                 AND probed_jd >= julianday(?) AND probed_jd <= julianday(?)
               ORDER BY probed_jd, id""",
            (provider_id, (lo - pad).isoformat(), (end + pad).isoformat()),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT at, health FROM provider_health WHERE provider_id=? ORDER BY id",
            (provider_id,),
        ).fetchall()
    lo = start - hold
    found: list[tuple[datetime, str]] = []
    for row in rows:
        moment = _parse(str(row["at"]))
        health = str(row["health"] or "")
        if moment is None or health not in _STATES or moment < lo or moment > end:
            continue
        found.append((moment, health))
    found.sort(key=lambda item: item[0])
    return found


def measure(
    samples: list[tuple[datetime, str]],
    slots: list[tuple[str, datetime, datetime]],
    hold: timedelta,
) -> list[dict]:
    if not samples or not slots:
        return []
    start = min(item[1] for item in slots)
    end = max(item[2] for item in slots)
    spans = cover(samples, start - hold, end, hold)
    return [{"at": at, **mix(spans, opened, closed)} for at, opened, closed in slots]


def _setting(con: sqlite3.Connection, key: str) -> str:
    row = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return str(row["v"]) if row else ""


def _stored_providers(con: sqlite3.Connection) -> list[dict]:
    raw = _setting(con, "providers")
    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _unknown_buckets(slots: list[tuple[str, datetime, datetime]]) -> list[dict]:
    return [{"at": at, **mix([], opened, closed)} for at, opened, closed in slots]


def _call_label(providers: list[dict], provider_id: str, model: str) -> str:
    provider = next((item for item in providers if str(item.get("id") or "") == provider_id), None)
    name = str(provider.get("name") or "") if provider else ""
    if not name:
        name = provider_id
    if model and model != name:
        return f"{name} · {model}"
    return name or model or provider_id


def _invocations(start: datetime, end: datetime) -> list[tuple[datetime, str, str]]:
    """Chat calls that show which model was actually invoked, including the one still in force at start."""

    from rogmem.engine import _DATA

    if _DATA is None:
        return []
    path = Path(_DATA) / "cost.sqlite"
    if not path.exists():
        return []
    con = sqlite3.connect(path, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone() is None:
            return []
        kept = "IFNULL(provider_id,'') != '' AND IFNULL(worker,'') != ?"
        if _has_column(con, "model_calls", "at_jd"):
            pad = timedelta(seconds=2)
            prior = con.execute(
                f"""SELECT id, at, provider_id, model FROM model_calls
                    WHERE {kept} AND at_jd IS NOT NULL AND at_jd < julianday(?)
                    ORDER BY at_jd DESC, id DESC LIMIT 1""",
                (_EMBED_WORKER, start.isoformat()),
            ).fetchall()
            inside = con.execute(
                f"""SELECT id, at, provider_id, model FROM model_calls
                    WHERE {kept} AND at_jd IS NOT NULL
                      AND at_jd >= julianday(?) AND at_jd < julianday(?)
                    ORDER BY at_jd, id""",
                (_EMBED_WORKER, (start - pad).isoformat(), (end + pad).isoformat()),
            ).fetchall()
            rows = []
            seen: set[int] = set()
            for row in list(prior) + list(inside):
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                rows.append(row)
        else:
            rows = con.execute(
                f"""SELECT id, at, provider_id, model FROM model_calls
                    WHERE {kept}
                    ORDER BY id""",
                (_EMBED_WORKER,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    parsed: list[tuple[datetime, str, str]] = []
    for row in rows:
        moment = _parse(str(row["at"]))
        provider_id = str(row["provider_id"] or "")
        if moment is not None and provider_id:
            parsed.append((moment, provider_id, str(row["model"] or "")))
    parsed.sort(key=lambda item: item[0])
    prior = [item for item in parsed if item[0] < start]
    inside = [item for item in parsed if start <= item[0] < end]
    return ([prior[-1]] if prior else []) + inside


def _mix_slots(
    slots: list[tuple[str, datetime, datetime]],
    spans: list[tuple[datetime, datetime, str, str]],
    probes: dict[str, list[tuple[datetime, str]]],
    hold: timedelta,
) -> list[dict]:
    buckets = []
    for at, opened, closed in slots:
        span_ms = _ms(closed) - _ms(opened)
        acc = {name: 0 for name in _STATES}
        who_ms: dict[str, int] = {}
        if span_ms > 0:
            for t0, t1, provider_id, label in spans:
                lo = t0 if t0 > opened else opened
                hi = t1 if t1 < closed else closed
                overlap = _ms(hi) - _ms(lo)
                if overlap <= 0:
                    continue
                for sample_lo, sample_hi, health in cover(probes.get(provider_id, []), lo, hi, hold):
                    acc[health] += sample_hi - sample_lo
                if label:
                    who_ms[label] = who_ms.get(label, 0) + overlap
        packed = _pack(span_ms, acc)
        buckets.append(
            {
                "at": at,
                **packed,
                "who": [
                    {"name": name, "seconds": int(round(millis / 1000))}
                    for name, millis in who_ms.items()
                    if millis > 0
                ],
            }
        )
    return buckets


def for_chart(series: list[dict] | None, window: dict | None) -> dict:
    """Availability aligned to chart buckets.

    Slot length comes from the chart window, whether or not a probe was stored.
    A closed database still returns those slots as unobserved.
    """

    slots = _slots(series or [], window or {})
    if not slots:
        return {"name": "MiniMax", "model": "", "buckets": []}
    unobserved = {"name": "MiniMax", "model": "", "buckets": _unknown_buckets(slots)}
    try:
        from rogmem.engine import DEFAULT_MINIMAX_ID, db, probe_interval_seconds

        con = db()
    except (RuntimeError, sqlite3.OperationalError):
        return unobserved
    try:
        providers = _stored_providers(con)
        chosen = choose_target(providers, _setting(con, "model_distill"), DEFAULT_MINIMAX_ID)
        name = "MiniMax"
        model = ""
        fallback = None
        if chosen is not None:
            provider, model = chosen
            name = str(provider.get("name") or "MiniMax")
            provider_id = str(provider.get("id") or "")
            if provider_id:
                fallback = (provider_id, _call_label(providers, provider_id, model))
        window_start = min(item[1] for item in slots)
        window_end = max(item[2] for item in slots)
        calls = [
            (moment, provider_id, _call_label(providers, provider_id, called))
            for moment, provider_id, called in _invocations(window_start, window_end)
        ]
        spans = tenures(calls, window_start, window_end, fallback)
        if not spans:
            return {"name": name, "model": model, "buckets": _unknown_buckets(slots)}
        hold = hold_for(probe_interval_seconds())
        probes = {
            provider_id: _load(con, provider_id, window_start, window_end, hold)
            for provider_id in {item[2] for item in spans}
        }
        buckets = _mix_slots(slots, spans, probes, hold)
    except sqlite3.OperationalError:
        return unobserved
    return {"name": name, "model": model, "buckets": buckets}
