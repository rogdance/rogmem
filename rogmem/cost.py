"""What a model call cost.

Calls, prices and the exchange rate live in their own file, so writing a call
never joins the memory transaction and never waits on it. A call stores tokens.
Money is computed when someone looks. A total exists only when every call in
it has a price; otherwise the amount is unknown. Cache tokens are already
inside the input count: they are priced on their own and are not added again.
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
PER_MILLION = 1_000_000
PAGE = 50
WORKERS = ("初始蒸馏", "并页", "速写", "专名核对", "身份合并", "向量嵌入")
SPANS = {"all", "24h", "7d", "30d"}
DETAIL_SPANS = {"all", "24h", "30d"}
_WORKER: contextvars.ContextVar[str] = contextvars.ContextVar("rogmem_cost_worker", default="")
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_SCHEMA_PATH: Path | None = None
_CLOCK_READY = False
_CLOCK_PATH: Path | None = None


class BadBook(ValueError):
    pass


@dataclass(frozen=True)
class Price:
    currency: str
    input: float
    output: float
    cache: float


@dataclass(frozen=True)
class Call:
    at: datetime
    worker: str
    provider_id: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_tokens: int | None
    prompt: str
    answer: str


def current_worker() -> str:
    return _WORKER.get()


@contextmanager
def bind_worker(name: str):
    token = _WORKER.set(name)
    try:
        yield
    finally:
        _WORKER.reset(token)


def charging(worker: str):
    """Mark every model call made while this function runs."""

    def deco(fn):
        def wrapped(*args, **kwargs):
            with bind_worker(worker):
                return fn(*args, **kwargs)

        wrapped.__name__ = fn.__name__
        wrapped.__doc__ = fn.__doc__
        wrapped.__wrapped__ = fn
        return wrapped

    return deco


def clip(text: str, limit: int = 36) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _whole(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def read_usage(body: dict) -> tuple[int, int, int] | None:
    """Normalize a vendor usage object to input, output, cache.

    Input already includes cache. OpenAI reports that total as prompt_tokens.
    The Anthropic-shaped field input_tokens is only the part that missed cache,
    so the cache read is added back.
    """

    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    cache = 0
    if isinstance(details, dict):
        cache = _whole(details.get("cached_tokens"))
    cache = max(cache, _whole(usage.get("cache_read_input_tokens")), _whole(usage.get("cached_tokens")))
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        prompt = _whole(usage.get("prompt_tokens"))
        return prompt, _whole(usage.get("completion_tokens")), min(cache, prompt)
    if "input_tokens" in usage or "output_tokens" in usage:
        fresh = _whole(usage.get("input_tokens")) + _whole(usage.get("cache_creation_input_tokens"))
        return fresh + cache, _whole(usage.get("output_tokens")), cache
    if "total_tokens" in usage:
        return _whole(usage.get("total_tokens")), 0, cache
    return None


def total_tokens(input_tokens: int | None, output_tokens: int | None) -> int | None:
    if input_tokens is None and output_tokens is None:
        return None
    return int(input_tokens or 0) + int(output_tokens or 0)


def money_cny(
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int,
    price: Price | None,
    usd_cny: float | None,
) -> float | None:
    """Yuan for one call. None when the price, or the dollar rate it needs, is missing."""

    if price is None:
        return None
    fresh = max(0, int(input_tokens) - int(cache_tokens))
    cache = min(int(cache_tokens), int(input_tokens))
    native = (fresh * price.input + cache * price.cache + int(output_tokens) * price.output) / PER_MILLION
    if price.currency == "USD":
        if usd_cny is None:
            return None
        return native * usd_cny
    return native


def _parse_at(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _floor(moment: datetime, grain: str) -> datetime:
    local = moment.astimezone(SHANGHAI)
    if grain == "hour":
        local = local.replace(minute=0, second=0, microsecond=0)
    else:
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local


def span_bounds(span: str, now: datetime, earliest: datetime | None) -> tuple[datetime, str]:
    """Start (inclusive) and bucket grain for a named window ending at now."""

    moment = now.astimezone(timezone.utc)
    if span == "24h":
        return moment - timedelta(hours=24), "hour"
    if span == "7d":
        return moment - timedelta(days=7), "day"
    if span == "30d":
        return moment - timedelta(days=30), "day"
    if earliest is None:
        return moment, "day"
    return earliest.astimezone(timezone.utc), "day"


def buckets(start: datetime, end: datetime, grain: str) -> list[datetime]:
    step = timedelta(hours=1) if grain == "hour" else timedelta(days=1)
    cur = _floor(start, grain)
    last = _floor(end, grain)
    found: list[datetime] = []
    while cur <= last:
        found.append(cur)
        cur += step
    return found


def bucket_of(moment: datetime, grain: str) -> datetime:
    return _floor(moment, grain)


def _number(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise BadBook("单价要是数字") from exc
    if number < 0:
        raise BadBook("单价不能为负")
    return number


def _currency(value: str) -> str:
    text = (value or "").strip().upper()
    if text in {"USD", "CNY"}:
        return text
    raise BadBook("币种只能是美元或人民币")


def _rate(value) -> float | None:
    text = "" if value is None else str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise BadBook("汇率要是数字") from exc
    if number <= 0:
        raise BadBook("汇率要是正数")
    return number


def _rate_of(con: sqlite3.Connection) -> float | None:
    row = con.execute("SELECT v FROM settings WHERE k='usd_cny'").fetchone()
    if row is None or str(row[0]).strip() == "":
        return None
    try:
        number = float(row[0])
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _prices(con: sqlite3.Connection) -> dict[tuple[str, str], Price]:
    found: dict[tuple[str, str], Price] = {}
    rows = con.execute(
        "SELECT provider_id, model, currency, input_price, output_price, cache_price FROM model_prices"
    ).fetchall()
    for row in rows:
        if row[3] is None or row[4] is None or row[5] is None:
            continue
        found[(str(row[0]), str(row[1]))] = Price(str(row[2] or "CNY"), float(row[3]), float(row[4]), float(row[5]))
    return found


def _save_book(con: sqlite3.Connection, rate, items: list[dict]) -> None:
    """Replace the rate and every submitted model price. A blank row clears that model."""

    usd = _rate(rate)
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            raise BadBook("价格不是一份清单")
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider or not model:
            raise BadBook("价格缺少模型")
        key = (provider, model)
        if key in seen:
            raise BadBook("同一个模型写了两次")
        seen.add(key)
        incoming = (_number(item.get("input")), _number(item.get("output")), _number(item.get("cache")))
        if all(part is None for part in incoming):
            con.execute("DELETE FROM model_prices WHERE provider_id=? AND model=?", key)
            continue
        if any(part is None for part in incoming):
            raise BadBook("一个模型的三项单价要一起填")
        con.execute(
            """INSERT INTO model_prices(provider_id, model, currency, input_price, output_price, cache_price)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(provider_id, model) DO UPDATE SET
                 currency=excluded.currency,
                 input_price=excluded.input_price,
                 output_price=excluded.output_price,
                 cache_price=excluded.cache_price""",
            (provider, model, _currency(str(item.get("currency") or "")), *incoming),
        )
    if usd is None:
        con.execute("DELETE FROM settings WHERE k='usd_cny'")
    else:
        con.execute(
            "INSERT INTO settings(k,v) VALUES('usd_cny',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(usd),),
        )


def last_user(messages) -> str:
    text = ""
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), str):
            text = message["content"]
    return text


_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  worker TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_tokens INTEGER,
  prompt TEXT NOT NULL DEFAULT '',
  answer TEXT NOT NULL DEFAULT '',
  at_jd REAL
);
CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls(at);
CREATE INDEX IF NOT EXISTS model_calls_jd ON model_calls(at_jd);
CREATE TABLE IF NOT EXISTS model_prices (
  provider_id TEXT NOT NULL,
  model TEXT NOT NULL,
  currency TEXT NOT NULL,
  input_price REAL,
  output_price REAL,
  cache_price REAL,
  PRIMARY KEY (provider_id, model)
);
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


def _store_path() -> Path | None:
    """The ledger lives beside the memory database and never shares its transaction."""

    from rogmem.engine import _DATA

    if _DATA is None:
        return None
    return Path(_DATA) / "cost.sqlite"


def _tables_ready(con: sqlite3.Connection) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_calls'").fetchone()
        is not None
    )


def migrate_call_clock(con: sqlite3.Connection) -> None:
    """Give each call a stored instant so a chart can stop at its window.

    A read does not call this. DDL here would wait behind the writer that is
    appending the next call.
    """

    global _CLOCK_READY, _CLOCK_PATH
    path = _store_path()
    if _CLOCK_READY and _CLOCK_PATH == path:
        return
    if not _tables_ready(con):
        con.executescript(_SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(model_calls)")}
    if "at_jd" not in columns:
        con.execute("ALTER TABLE model_calls ADD COLUMN at_jd REAL")
    con.execute("DROP TRIGGER IF EXISTS model_calls_at_jd_insert")
    con.execute("DROP TRIGGER IF EXISTS model_calls_at_jd_update")
    con.execute(
        """CREATE TRIGGER model_calls_at_jd_insert
           AFTER INSERT ON model_calls
           BEGIN
             UPDATE model_calls
                SET at_jd = julianday(replace(trim(NEW.at), 't', 'T'))
              WHERE id = NEW.id;
           END"""
    )
    con.execute(
        """CREATE TRIGGER model_calls_at_jd_update
           AFTER UPDATE OF at ON model_calls
           BEGIN
             UPDATE model_calls
                SET at_jd = julianday(replace(trim(NEW.at), 't', 'T'))
              WHERE id = NEW.id;
           END"""
    )
    con.execute(
        """UPDATE model_calls
              SET at_jd = julianday(replace(trim(at), 't', 'T'))
            WHERE at_jd IS NULL
              AND julianday(replace(trim(at), 't', 'T')) IS NOT NULL"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS model_calls_jd ON model_calls(at_jd)")
    _CLOCK_READY = True
    _CLOCK_PATH = path


def _ensure_schema(con: sqlite3.Connection, *, write: bool = False) -> None:
    """Create tables when missing. A read must not repeat DDL or it waits behind every call write."""

    global _SCHEMA_READY, _SCHEMA_PATH
    path = _store_path()
    if _SCHEMA_READY and _SCHEMA_PATH == path:
        if write:
            migrate_call_clock(con)
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and _SCHEMA_PATH == path:
            if write:
                migrate_call_clock(con)
            return
        if _tables_ready(con):
            if write:
                migrate_call_clock(con)
            _SCHEMA_READY = True
            _SCHEMA_PATH = path
            return
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(_SCHEMA)
        migrate_call_clock(con)
        _SCHEMA_READY = True
        _SCHEMA_PATH = path


@contextmanager
def _store(*, write: bool = False):
    path = _store_path()
    if path is None:
        raise RuntimeError("cost store is not open")
    con = sqlite3.connect(path, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        if write or not (_SCHEMA_READY and _SCHEMA_PATH == path):
            _ensure_schema(con, write=write)
        yield con
    finally:
        con.close()


def note_response(*, at: str, worker: str, provider_id: str, model: str, body: dict, prompt: str, answer: str) -> None:
    """Append one finished call. This commit cannot see the caller's transaction."""

    if _store_path() is None:
        return
    with _store(write=True) as con:
        remember(
            con,
            at=at,
            worker=worker,
            provider_id=provider_id,
            model=model,
            usage=read_usage(body),
            prompt=prompt,
            answer=answer,
        )
        con.commit()
    _bump_inputs()


def record(
    *,
    at: str,
    worker: str,
    provider_id: str,
    model: str,
    usage: tuple[int, int, int] | None,
    prompt: str = "",
    answer: str = "",
) -> None:
    with _store(write=True) as con:
        remember(
            con,
            at=at,
            worker=worker,
            provider_id=provider_id,
            model=model,
            usage=usage,
            prompt=prompt,
            answer=answer,
        )
        con.commit()
    _bump_inputs()


def remember(
    con: sqlite3.Connection,
    *,
    at: str,
    worker: str,
    provider_id: str,
    model: str,
    usage: tuple[int, int, int] | None,
    prompt: str,
    answer: str,
) -> None:
    tokens = usage if usage is not None else (None, None, None)
    con.execute(
        """INSERT INTO model_calls(at, worker, provider_id, model, input_tokens, output_tokens, cache_tokens, prompt, answer)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (at, worker or "模型调用", provider_id or "", model or "", tokens[0], tokens[1], tokens[2], clip(prompt), clip(answer)),
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _window_sql(start: datetime | None, end: datetime | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start is not None:
        clauses.append("at >= ?")
        params.append(_stamp(start))
    if end is not None:
        clauses.append("at <= ?")
        params.append(_stamp(end))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _calls(
    con: sqlite3.Connection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    with_text: bool = True,
) -> list[Call]:
    """Load calls in a window. A page must not pull the whole ledger to draw one span."""

    where, params = _window_sql(start, end)
    cols = "at, worker, provider_id, model, input_tokens, output_tokens, cache_tokens"
    if with_text:
        cols += ", prompt, answer"
    rows = con.execute(
        f"SELECT {cols} FROM model_calls{where} ORDER BY at, id",
        params,
    ).fetchall()
    found: list[Call] = []
    for row in rows:
        prompt = str(row[7] or "") if with_text else ""
        answer = str(row[8] or "") if with_text else ""
        found.append(
            Call(
                _parse_at(str(row[0])),
                str(row[1] or ""),
                str(row[2] or ""),
                str(row[3] or ""),
                None if row[4] is None else int(row[4]),
                None if row[5] is None else int(row[5]),
                None if row[6] is None else int(row[6]),
                prompt,
                answer,
            )
        )
    return found


def _detail_rows(
    con: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    worker: str,
    model: str,
    page: int,
) -> tuple[list[Call], int]:
    """One page of call text. Aggregation never needs these strings."""

    where, params = _window_sql(start, end)
    clauses = []
    if worker:
        clauses.append("worker = ?")
        params.append(worker)
    if model:
        provider, _, name = model.partition("\t")
        clauses.append("provider_id = ?")
        params.append(provider)
        clauses.append("model = ?")
        params.append(name)
    if clauses:
        where = where + (" AND " if where else " WHERE ") + " AND ".join(clauses)
    total = con.execute(f"SELECT COUNT(*) FROM model_calls{where}", params).fetchone()[0]
    pages = max(1, (int(total) + PAGE - 1) // PAGE)
    current = page if page >= 1 else 1
    if current > pages:
        current = pages
    offset = (current - 1) * PAGE
    rows = con.execute(
        f"""SELECT at, worker, provider_id, model, input_tokens, output_tokens, cache_tokens, prompt, answer
            FROM model_calls{where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?""",
        [*params, PAGE, offset],
    ).fetchall()
    calls = [
        Call(
            _parse_at(str(row[0])),
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or ""),
            None if row[4] is None else int(row[4]),
            None if row[5] is None else int(row[5]),
            None if row[6] is None else int(row[6]),
            str(row[7] or ""),
            str(row[8] or ""),
        )
        for row in rows
    ]
    return calls, int(total)


def _earliest_at(con: sqlite3.Connection) -> datetime | None:
    row = con.execute("SELECT at FROM model_calls ORDER BY at, id LIMIT 1").fetchone()
    if row is None:
        return None
    return _parse_at(str(row[0]))


def _catalog(con: sqlite3.Connection) -> tuple[list[str], list[dict]]:
    workers = list(WORKERS)
    seen_workers = set(workers)
    for row in con.execute("SELECT DISTINCT worker FROM model_calls ORDER BY worker"):
        name = str(row[0] or "")
        if name and name not in seen_workers:
            seen_workers.add(name)
            workers.append(name)
    models: list[dict] = []
    for row in con.execute(
        "SELECT DISTINCT provider_id, model FROM model_calls WHERE IFNULL(model,'') != '' ORDER BY provider_id, model"
    ):
        models.append({"provider": str(row[0] or ""), "model": str(row[1] or "")})
    return workers, models


def _money_from_parts(fresh: int, output: int, cache_billed: int, price: Price | None, usd_cny: float | None) -> float | None:
    """Yuan for a group of calls whose per-call fresh/cache parts are already summed.

    Summing tokens first and then applying min(cache, input) does not equal the
    sum of each call. The parts have to be folded per call, then priced once.
    """

    if price is None:
        return None
    native = (fresh * price.input + cache_billed * price.cache + output * price.output) / PER_MILLION
    if price.currency == "USD":
        if usd_cny is None:
            return None
        return native * usd_cny
    return native


def _merge_sum(into: dict, piece: dict) -> None:
    into["input"] += piece["input"]
    into["output"] += piece["output"]
    into["cache"] += piece["cache"]
    into["bare"] += piece["bare"]
    into["unpriced"] += piece["unpriced"]
    if piece.get("priced"):
        into["money"] += piece["money"]
        into["priced"] = True


def _slot_expr(grain: str) -> str:
    clock = "datetime((julianday(at) - 2440587.5) * 86400, 'unixepoch', '+8 hours')"
    shape = "%Y-%m-%d %H:00:00" if grain == "hour" else "%Y-%m-%d 00:00:00"
    return f"strftime('{shape}', {clock})"


def _group_sums(
    con: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    grain: str,
    worker: str,
    model: str,
) -> list[sqlite3.Row]:
    """One row per bucket and price identity. The page never sees each call."""

    where, params = _window_sql(start, end)
    if worker:
        where = where + (" AND " if where else " WHERE ") + "worker = ?"
        params.append(worker)
    if model:
        provider, _, name = model.partition("\t")
        where = where + (" AND " if where else " WHERE ") + "provider_id = ? AND model = ?"
        params.extend([provider, name])
    metered = "input_tokens IS NULL AND output_tokens IS NULL"
    return con.execute(
        f"""SELECT {_slot_expr(grain)} AS slot,
                   provider_id, model,
                   SUM(CASE WHEN {metered} THEN 1 ELSE 0 END) AS bare,
                   SUM(CASE WHEN {metered} THEN 0 ELSE COALESCE(input_tokens, 0) END) AS input_tokens,
                   SUM(CASE WHEN {metered} THEN 0 ELSE COALESCE(output_tokens, 0) END) AS output_tokens,
                   SUM(CASE WHEN {metered} THEN 0 ELSE COALESCE(cache_tokens, 0) END) AS cache_tokens,
                   SUM(CASE WHEN {metered} THEN 0 ELSE max(0, COALESCE(input_tokens, 0) - COALESCE(cache_tokens, 0)) END) AS fresh,
                   SUM(CASE WHEN {metered} THEN 0 ELSE min(COALESCE(cache_tokens, 0), COALESCE(input_tokens, 0)) END) AS cache_billed,
                   SUM(CASE WHEN {metered} THEN 0 ELSE 1 END) AS metered
            FROM model_calls{where}
            GROUP BY slot, provider_id, model""",
        params,
    ).fetchall()


def _piece_from_group(row: sqlite3.Row, prices, rate) -> dict:
    piece = _empty()
    piece["input"] = int(row["input_tokens"] or 0)
    piece["output"] = int(row["output_tokens"] or 0)
    piece["cache"] = int(row["cache_tokens"] or 0)
    piece["bare"] = int(row["bare"] or 0)
    metered = int(row["metered"] or 0)
    if metered:
        amount = _money_from_parts(
            int(row["fresh"] or 0),
            piece["output"],
            int(row["cache_billed"] or 0),
            prices.get((str(row["provider_id"] or ""), str(row["model"] or ""))),
            rate,
        )
        if amount is None:
            piece["unpriced"] = metered
        else:
            piece["money"] = amount
            piece["priced"] = True
    return piece


def _kept(call: Call, start: datetime, end: datetime, worker: str, model: str) -> bool:
    if call.at < start or call.at > end:
        return False
    if worker and call.worker != worker:
        return False
    if model:
        provider, _, name = model.partition("\t")
        if call.provider_id != provider or call.model != name:
            return False
    return True


def _add(call: Call, prices, rate, into: dict) -> None:
    if call.input_tokens is None and call.output_tokens is None:
        into["bare"] += 1
        return
    inn = int(call.input_tokens or 0)
    out = int(call.output_tokens or 0)
    cache = int(call.cache_tokens or 0)
    into["input"] += inn
    into["output"] += out
    into["cache"] += cache
    priced = money_cny(inn, out, cache, prices.get((call.provider_id, call.model)), rate)
    if priced is None:
        into["unpriced"] += 1
    else:
        into["money"] += priced
        into["priced"] = True


def _empty() -> dict:
    return {"money": 0.0, "input": 0, "output": 0, "cache": 0, "priced": False, "unpriced": 0, "bare": 0}


def _public_sum(total: dict) -> dict:
    # `money_known` is the sum of the priced subset. It is retained even when `money` is None
    # (there are unpriced calls in the window): the chart needs to show "¥X.XX known so far"
    # instead of dropping the already-priced part. `money` keeps the historical semantics —
    # None ⇔ 任意未计价 — so existing `_public_sum` readers (summary cards, exports) are untouched.
    priced_sum = total["money"] if total["priced"] else 0.0
    if total["unpriced"]:
        money = None
    elif total["priced"]:
        money = total["money"]
    else:
        money = 0.0
    return {
        "money": money,
        "money_known": priced_sum,
        "tokens": total["input"] + total["output"],
        "input": total["input"],
        "output": total["output"],
        "cache": total["cache"],
        "unpriced": total["unpriced"],
        "bare": total["bare"],
    }


def _overview(
    con: sqlite3.Connection,
    *,
    now: datetime,
    span: str,
    worker: str = "",
    model: str = "",
    detail: str = "30d",
    dworker: str = "",
    dmodel: str = "",
    page: int = 1,
) -> dict:
    span = span if span in SPANS else "30d"
    detail = detail if detail in DETAIL_SPANS else "30d"
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    earliest = _earliest_at(con)
    start, grain = span_bounds(span, moment, earliest)
    dstart, _ = span_bounds(detail, moment, earliest)
    prices = _prices(con)
    rate = _rate_of(con)
    folded = _empty()
    by_bucket: dict[datetime, dict] = {}
    slot_at = {
        (key.strftime("%Y-%m-%d %H:00:00") if grain == "hour" else key.strftime("%Y-%m-%d 00:00:00")): key
        for key in buckets(start, moment, grain)
    }
    for row in _group_sums(con, start=start, end=moment, grain=grain, worker=worker, model=model):
        piece = _piece_from_group(row, prices, rate)
        _merge_sum(folded, piece)
        key = slot_at.get(str(row["slot"] or ""))
        if key is None:
            continue
        _merge_sum(by_bucket.setdefault(key, _empty()), piece)
    series = []
    for key in buckets(start, moment, grain):
        slot = by_bucket.get(key, _empty())
        series.append(
            {
                "at": key.isoformat(),
                "label": key.strftime("%m-%d %H:00") if grain == "hour" else key.strftime("%m-%d"),
                "grain": grain,
                **_public_sum(slot),
            }
        )
    listed, count = _detail_rows(
        con, start=dstart, end=moment, worker=dworker, model=dmodel, page=page
    )
    window = {
        "start": start.isoformat(),
        "end": moment.isoformat(),
        "grain": grain,
        "tz": "UTC+8",
    }
    pages = max(1, (count + PAGE - 1) // PAGE)
    current = page if page >= 1 else 1
    if current > pages:
        current = pages
    rows = []
    for call in listed:
        price = prices.get((call.provider_id, call.model))
        amount = None
        if call.input_tokens is not None or call.output_tokens is not None:
            amount = money_cny(
                int(call.input_tokens or 0),
                int(call.output_tokens or 0),
                int(call.cache_tokens or 0),
                price,
                rate,
            )
        rows.append(
            {
                "at": call.at.astimezone(SHANGHAI).strftime("%m-%d %H:%M"),
                "worker": call.worker,
                "provider_id": call.provider_id,
                "model": call.model,
                "prompt": call.prompt,
                "answer": call.answer,
                "input": call.input_tokens,
                "output": call.output_tokens,
                "cache": call.cache_tokens,
                "total": total_tokens(call.input_tokens, call.output_tokens),
                "money": amount,
            }
        )
    workers, models = _catalog(con)
    return {
        "span": span,
        "worker": worker,
        "model": model,
        "detail": detail,
        "dworker": dworker,
        "dmodel": dmodel,
        "page": current,
        "pages": pages,
        "page_size": PAGE,
        "count": count,
        "summary": _public_sum(folded),
        "series": series,
        "window": window,
        "rows": rows,
        "workers": workers,
        "models": models,
        "rate": rate,
        "observed": True,
    }


_BOARD_SNAP_LOCK = threading.Lock()
_BOARD_SETTLED = threading.Condition(_BOARD_SNAP_LOCK)
_BOARD_SNAP_WANT: dict | None = None
_BOARD_SNAP_THREAD: threading.Thread | None = None
# How long a page may wait for the gather it just asked for. The request itself
# does not open the ledger; it only waits until that identity is published.
VIEW_WAIT_SECONDS = 2.0
_BOARD_SNAP_KEYS = ("span", "worker", "model", "detail", "dworker", "dmodel", "page")
_BOARD_WAKE = threading.Event()
# Identity is the filter combo. Freshness is separate: input generation, the bucket the
# window was computed in, and whether the last gathers failed. A hit is ready only while
# both still match; otherwise the old numbers stay visible and a new gather is scheduled.
_BOARD_OBS: dict[tuple, dict] = {}
_BOARD_OBS_MAX = 8
_BOARD_EPOCH = 0
_OBS_FAIL_LIMIT = 3
_PRICE_BOOK: dict = {"epoch": -1, "prices": None, "rate": None}
_FAILS: dict[tuple, int] = {}
_GAVE_UP: dict[tuple, int] = {}


def _default_board_kwargs(now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    return {
        "now": moment,
        "span": "30d",
        "worker": "",
        "model": "",
        "detail": "30d",
        "dworker": "",
        "dmodel": "",
        "page": 1,
    }


def _board_snap_key(filtered: dict) -> tuple:
    """Identity of a console board view. `now` is not part of the key: the refresher advances it."""

    return tuple(filtered.get(name) for name in _BOARD_SNAP_KEYS)


def _normalize_board_kwargs(kwargs: dict | None = None) -> dict:
    defaults = _default_board_kwargs((kwargs or {}).get("now"))
    filtered = {key: (kwargs or {}).get(key, defaults[key]) for key in defaults}
    filtered["span"] = filtered["span"] if filtered["span"] in SPANS else "30d"
    filtered["detail"] = filtered["detail"] if filtered["detail"] in DETAIL_SPANS else "30d"
    page = filtered["page"]
    try:
        page_no = int(page)
    except (TypeError, ValueError):
        page_no = 1
    filtered["page"] = page_no if page_no >= 1 else 1
    moment = filtered["now"]
    if getattr(moment, "tzinfo", None) is None:
        filtered["now"] = moment.replace(tzinfo=timezone.utc)
    return filtered


def _want_key_locked() -> tuple | None:
    if _BOARD_SNAP_WANT is None:
        return None
    return _board_snap_key(_normalize_board_kwargs(_BOARD_SNAP_WANT))


def _remember_locked(key: tuple, entry: dict) -> None:
    """Store one observation. The newest and the wanted identity are kept."""

    _BOARD_OBS.pop(key, None)
    _BOARD_OBS[key] = entry
    want_key = _want_key_locked()
    while len(_BOARD_OBS) > _BOARD_OBS_MAX:
        victim = next((old for old in _BOARD_OBS if old != key and old != want_key), None)
        if victim is None:
            break
        _BOARD_OBS.pop(victim, None)


def _remember_observation(key: tuple, board: dict, prices: dict) -> None:
    """Test seam: store a ready observation of the current inputs."""

    window = board.get("window") if isinstance(board.get("window"), dict) else {}
    as_of = _parse_at(str(window.get("end"))) if window.get("end") else datetime.now(timezone.utc)
    grain = str(window.get("grain") or "hour")
    with _BOARD_SNAP_LOCK:
        _remember_locked(
            key,
            {
                "epoch": _BOARD_EPOCH,
                "as_of": as_of,
                "grain": grain,
                "board": board,
                "prices": prices,
                "failed": False,
            },
        )


def _clear_observations() -> None:
    global _BOARD_SNAP_WANT, _BOARD_EPOCH
    with _BOARD_SNAP_LOCK:
        _BOARD_OBS.clear()
        _BOARD_SNAP_WANT = None
        _BOARD_EPOCH = 0
        _PRICE_BOOK["epoch"] = -1
        _PRICE_BOOK["prices"] = None
        _PRICE_BOOK["rate"] = None
        _FAILS.clear()
        _GAVE_UP.clear()
    _BOARD_WAKE.clear()


def _bump_inputs(prices: dict | None = None, rate: float | None = None) -> None:
    """Ledger or price-book changed. Old views stay readable, but they are no longer final."""

    global _BOARD_EPOCH
    with _BOARD_SNAP_LOCK:
        _BOARD_EPOCH += 1
        _FAILS.clear()
        _GAVE_UP.clear()
        if prices is not None:
            _PRICE_BOOK["prices"] = prices
            _PRICE_BOOK["rate"] = rate
        if _PRICE_BOOK["prices"] is not None:
            _PRICE_BOOK["epoch"] = _BOARD_EPOCH
    # A write expires the published reading. The fold itself waits until a reader asks.
    _start_board_refresher()


def _is_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _note_hard_fail(key: tuple, epoch: int) -> None:
    with _BOARD_SNAP_LOCK:
        _FAILS[key] = _FAILS.get(key, 0) + 1
        if _FAILS[key] < _OBS_FAIL_LIMIT:
            _BOARD_SETTLED.notify_all()
            return
        _GAVE_UP[key] = epoch
        entry = _BOARD_OBS.get(key)
        if entry is not None and entry["epoch"] <= epoch:
            entry["failed"] = True
        _BOARD_SETTLED.notify_all()


def _publish_observation(
    key: tuple,
    board: dict,
    prices: dict,
    rate: float | None,
    epoch: int,
    as_of: datetime,
    grain: str,
) -> None:
    """Publish one gather. An older generation must not cover a newer one."""

    with _BOARD_SNAP_LOCK:
        try:
            current = _BOARD_OBS.get(key)
            if current is not None:
                if epoch < current["epoch"]:
                    return
                if epoch == current["epoch"] and as_of < current["as_of"]:
                    return
            _remember_locked(
                key,
                {
                    "epoch": epoch,
                    "as_of": as_of,
                    "grain": grain,
                    "board": board,
                    "prices": prices,
                    "failed": False,
                },
            )
            if _PRICE_BOOK["epoch"] <= epoch:
                _PRICE_BOOK["epoch"] = epoch
                _PRICE_BOOK["prices"] = prices
                _PRICE_BOOK["rate"] = rate
            _FAILS.pop(key, None)
            _GAVE_UP.pop(key, None)
        finally:
            _BOARD_SETTLED.notify_all()


def _refresh_board_snap(target: dict | None = None) -> str:
    kwargs = _normalize_board_kwargs(target)
    key = _board_snap_key(kwargs)
    as_of = kwargs["now"]
    _start, grain = span_bounds(kwargs["span"], as_of, None)
    with _BOARD_SNAP_LOCK:
        epoch = _BOARD_EPOCH
        if _GAVE_UP.get(key) == epoch:
            return "skip"
    try:
        with _store() as con:
            board = _overview(con, **kwargs)
            prices = _prices(con)
            rate = board.get("rate") if isinstance(board, dict) else None
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return "locked"
        _note_hard_fail(key, epoch)
        return "fail"
    except Exception:
        _note_hard_fail(key, epoch)
        return "fail"
    _publish_observation(key, board, prices, rate, epoch, as_of, grain)
    return "ok"


def _want_snapshot() -> dict | None:
    if _BOARD_SNAP_WANT is None:
        return None
    return dict(_BOARD_SNAP_WANT)


def _board_refresher() -> None:
    """Fold only after a reader asks. A quiet ledger does not keep a fold warm."""

    while True:
        _BOARD_WAKE.wait()
        _BOARD_WAKE.clear()
        while True:
            with _BOARD_SNAP_LOCK:
                want = _want_snapshot()
                epoch = _BOARD_EPOCH
            outcome = _refresh_board_snap(want)
            with _BOARD_SNAP_LOCK:
                moved = epoch != _BOARD_EPOCH or _want_snapshot() != want
            if outcome == "locked":
                if _BOARD_WAKE.wait(0.5):
                    _BOARD_WAKE.clear()
                continue
            if outcome == "fail":
                with _BOARD_SNAP_LOCK:
                    key = _board_snap_key(_normalize_board_kwargs(want))
                    gave_up = _GAVE_UP.get(key) == _BOARD_EPOCH
                if not gave_up:
                    if _BOARD_WAKE.wait(0.5):
                        _BOARD_WAKE.clear()
                    continue
            # Wakes that arrived during this fold are one more fold, not one per wake.
            if moved or _BOARD_WAKE.is_set():
                _BOARD_WAKE.clear()
                continue
            break


def _start_board_refresher() -> None:
    global _BOARD_SNAP_THREAD
    if _BOARD_SNAP_THREAD is not None and _BOARD_SNAP_THREAD.is_alive():
        return
    _BOARD_SNAP_THREAD = threading.Thread(target=_board_refresher, name="rogmem-cost-snap", daemon=True)
    _BOARD_SNAP_THREAD.start()


def overview(**kwargs) -> dict:
    with _store() as con:
        return _overview(con, **kwargs)


def _shell_board(**kwargs) -> dict:
    """Identity-shaped placeholder for a view that has not been observed.

    Call writers can keep a cold gather past the console switch budget, so the
    request path returns this without opening the ledger. The numbers are an
    empty shape, not a reading: `observed` is false, and there is no zero timeline.
    """

    filtered = _normalize_board_kwargs(kwargs)
    span = filtered["span"]
    detail = filtered["detail"]
    moment = filtered["now"]
    start, grain = span_bounds(span, moment, None)
    empty = _public_sum(_empty())
    return {
        "span": span,
        "worker": filtered["worker"],
        "model": filtered["model"],
        "detail": detail,
        "dworker": filtered["dworker"],
        "dmodel": filtered["dmodel"],
        "page": filtered["page"],
        "pages": 1,
        "page_size": PAGE,
        "count": 0,
        "observed": False,
        "summary": empty,
        "series": [],
        "window": {
            "start": start.isoformat(),
            "end": moment.isoformat(),
            "grain": grain,
            "tz": "UTC+8",
        },
        "rows": [],
        "workers": [],
        "models": [],
        "rate": None,
    }


def view_key(**kwargs) -> tuple:
    """Identity of a cost view. `now` moves the window but does not change the identity."""

    return _board_snap_key(_normalize_board_kwargs(kwargs))


def wait_for_view(key: tuple, timeout: float) -> None:
    """Wait until this identity has been published or the gather has given up.

    Does not open the ledger. A page can block on the refresher without gathering
    on the request that is drawing it.
    """

    if timeout <= 0:
        return
    deadline = time.monotonic() + timeout
    with _BOARD_SETTLED:
        while True:
            if _BOARD_OBS.get(key) is not None or _GAVE_UP.get(key) == _BOARD_EPOCH:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _BOARD_SETTLED.wait(remaining)


def _observation_state(entry: dict | None, now: datetime, epoch: int, gave_up: int | None) -> str:
    """ready is final. stale keeps the old numbers. pending has none. failed stops retrying."""

    if entry is not None and not entry.get("failed"):
        same_bucket = bucket_of(entry["as_of"], entry["grain"]) == bucket_of(now, entry["grain"])
        if entry["epoch"] == epoch and same_bucket:
            return "ready"
    if gave_up == epoch:
        return "failed"
    if entry is None:
        return "pending"
    return "stale"


def board_and_prices(**kwargs) -> tuple[dict, dict[tuple[str, str], Price]]:
    """Read one cost view without gathering the ledger on the request path.

    Identity is the filter combo. A ready hit matches the current inputs and the
    current bucket. Anything older is stale: the previous numbers stay, the price
    book is the latest one, and a gather is scheduled. Missing is pending, not zero.
    A gave-up identity is failed until the inputs change or the reader asks again.
    """

    global _BOARD_SNAP_WANT

    filtered = _normalize_board_kwargs(kwargs)
    key = _board_snap_key(filtered)
    want = {name: filtered[name] for name in _BOARD_SNAP_KEYS}
    now = filtered["now"]
    with _BOARD_SNAP_LOCK:
        changed = _BOARD_SNAP_WANT != want
        _BOARD_SNAP_WANT = want
        entry = _BOARD_OBS.get(key)
        if entry is not None:
            _BOARD_OBS.pop(key, None)
            _BOARD_OBS[key] = entry
        epoch = _BOARD_EPOCH
        gave_up = _GAVE_UP.get(key)
        book_prices = _PRICE_BOOK["prices"]
        book_rate = _PRICE_BOOK["rate"]
        book_known = _PRICE_BOOK["epoch"] >= 0 and book_prices is not None
    state = _observation_state(entry, now, epoch, gave_up)
    # A new identity or a stale reading asks for one fold. Repeating a view that
    # is still pending must not ask again, or every probe would start another fold.
    if changed or state == "stale":
        _BOARD_WAKE.set()
    _start_board_refresher()
    prices = book_prices if book_known else (entry["prices"] if entry is not None else {})
    if entry is None:
        shell = _shell_board(**filtered)
        shell["observation"] = {"state": state, "epoch": None, "as_of": None}
        shell["observed"] = False
        if book_known:
            shell["rate"] = book_rate
        return shell, prices
    board = entry["board"]
    board["observation"] = {
        "state": state,
        "epoch": entry["epoch"],
        "as_of": entry["as_of"].isoformat(),
    }
    if book_known:
        board["rate"] = book_rate
    if state == "pending":
        board["observed"] = False
    return board, prices


def load_rate() -> float | None:
    with _store() as con:
        return _rate_of(con)


def load_prices() -> dict[tuple[str, str], Price]:
    with _store() as con:
        return _prices(con)


def save_book(rate, items: list[dict]) -> None:
    with _store(write=True) as con:
        try:
            _save_book(con, rate, items)
            con.commit()
            prices = _prices(con)
            usd = _rate_of(con)
        except Exception:
            con.rollback()
            raise
    _bump_inputs(prices=prices, rate=usd)
