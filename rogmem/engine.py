"""Storage and distillation rules. Behavior follows the product spec."""

from __future__ import annotations

import contextvars
import difflib
import hashlib
import ipaddress
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jieba

from rogmem import crypto
from rogmem.cost import charging
from rogmem.exchange import (
    Exchange,
    Side,
    exchange_ready,
    flag_updates,
    _instant_key,
    form_exchanges,
    is_history_lane,
    join_exchange_text,
    order_history_newest_first,
)
from rogmem.device_identity import (
    BANNED_LABELS,
    UNNAMED_LABEL,
    device_label,
    display_device_name,
    given_device_name,
    normalize_device_name,
)
from rogmem.progress import (
    STAGES as PIPELINE_STAGES,
    retire_polish,
    ST_CRYSTAL,
    ST_DISTILL_FAIL,
    ST_DISTILLING,
    ST_NO_BODY,
    ST_NO_CRYSTAL,
    ST_PARSED,
    ST_PARSE_FAIL,
    ST_VEC_FAIL,
    ST_VEC_READY,
    canonical_status,
    mark_is_no_crystal,
    matches as source_progress_matches,
)
from rogmem.prompts import (
    MERGE_ENTITY_SYSTEM,
    PROPER_NAME_SYSTEM,
    INTEGRATE_PAGE_SYSTEM,
    with_kept_relations,
    distill_scope,
    distill_system,
    proper_name_user,
)
from rogmem.queue_priority import (
    ORIGIN_BATCH,
    ORIGIN_HISTORY,
    ORIGIN_LIVE,
    ORIGIN_REBENCH,
    ORIGIN_RETRY,
    ORIGIN_SCAN,
    QueueCandidate,
    choose_next,
    lane_for,
    normalize_lane,
    read_high_streak,
    write_high_streak,
)
from rogmem.redact import redact_text, find_secrets
from rogmem.crystal_store import (
    DEFERRED_RETRY_LIMIT,
    DISTILLER_STORE_WRITE_ENABLED,
    CrystalStore,
    Fact,
    Inference,
    IntegrateWriteNotLive,
    LeaseConflict,
    ThinCrystal,
    apply_op_supported,
    chapter_update_ops,
    field_update_ops,
    hydrate,
    parse_valid_until,
)
from rogmem.distill_gate import (
    PAYLOAD_KIND_EXTRACT,
    PAYLOAD_KIND_INTEGRATE,
    check_core_module_complete,
    parse_integrate_payload,
    stamp_integrate_payload,
)
from rogmem.distill_match import list_draft_crystals, match_integrate_parts
from rogmem.slot_chain import (
    origin_links,
    style_delta,
)


def _shared_apply_connection(store, con) -> None:
    """CTO: statements/edges must share the CrystalStore connection."""
    store_con = getattr(store, "con", None)
    if store_con is not None and store_con is not con:
        raise RuntimeError("statements/edges must share the CrystalStore connection")


def _store_acquire_lease(store, crystal_id: str, holder: str, *, commit: bool = True) -> bool:
    """Acquire a CrystalStore write lease. Fakes without the method are skipped."""
    acquire = getattr(store, "acquire_lease", None)
    if not callable(acquire):
        return False
    acquire(crystal_id, holder, purpose="distill", commit=commit)
    return True


def _store_release_lease(store, crystal_id: str, holder: str, *, commit: bool = True) -> None:
    release = getattr(store, "release_lease", None)
    if not callable(release):
        return
    try:
        release(crystal_id, holder, commit=commit)
    except LeaseConflict:
        return


def _apply_then_sidecar(
    target_store,
    con,
    *,
    crystal_id: str,
    expected_revision: int,
    ops: list[dict],
    holder: str,
    sidecar,
    injected: bool,
    before=None,
    commit: bool = True,
) -> None:
    """Apply first, then sidecar, then same-connection commit.

    Optional ``before`` (persist_unresolved) shares this try. Field and
    chapter writers share this helper. Statements / edges / revision
    must not land before ``apply_ops``. Sidecar or other failure rolls
    back the whole shared ``db()`` transaction, including ``before``.
    Injected store is apply-only; the engine connection rolls back.

    A CrystalStore write lease is taken in this same transaction (queue
    lease / orphan-release pattern) so concurrent distill workers cannot
    share a crystal. Injected fakes skip the lease.

    ``IntegrateWriteNotLive`` (write switch killed to False): apply is
    fail-closed and sidecar does not run; ``before`` (persist_unresolved)
    still commits on this connection. Production default is on. Do not
    flip ``DISTILLER_STORE_WRITE_ENABLED`` here.
    """
    acquired = False
    try:
        _shared_apply_connection(target_store, con)
        if not injected:
            acquired = _store_acquire_lease(target_store, crystal_id, holder, commit=False)
        if before is not None:
            before()
        target_store.apply_ops(
            crystal_id,
            expected_revision,
            ops,
            holder=holder,
            commit=False,
        )
        if injected:
            con.rollback()
            return
        sidecar()
        if acquired:
            _store_release_lease(target_store, crystal_id, holder, commit=False)
            acquired = False
        if commit:
            con.commit()
    except IntegrateWriteNotLive:
        if injected:
            con.rollback()
            return
        if before is not None and commit:
            if acquired:
                _store_release_lease(target_store, crystal_id, holder, commit=False)
                acquired = False
            con.commit()
            return
        if before is not None:
            if acquired:
                _store_release_lease(target_store, crystal_id, holder, commit=False)
                acquired = False
            return
        con.rollback()
        return
    except LeaseConflict:
        con.rollback()
        if injected:
            return
        raise
    except Exception:
        con.rollback()
        raise

LOCK = threading.RLock()
_CHUNK_SEQ: contextvars.ContextVar[int | None] = contextvars.ContextVar("rogmem_chunk_seq", default=None)
# The queue row this distill thread is allowed to finish. Empty for a direct call.
_ATTEMPT: contextvars.ContextVar[int | None] = contextvars.ContextVar("rogmem_attempt", default=None)
# Rows leased together for the distill this thread is running.
_EXCHANGE: contextvars.ContextVar[tuple] = contextvars.ContextVar("rogmem_exchange", default=())
_LIVE_LEASES: set[int] = set()
_LIVE_LOCK = threading.Lock()
COMMON = {"石头", "木头", "自行车"}
ORG_NAMES = {"周生生", "捷安特", "腾讯"}
PRODUCT_NAMES = {"和平精英"}
PLACE_NAMES = {"深圳"}
PERSON_FIELDS = [
    "姓名",
    "别名",
    "性别",
    "出生时间",
    "语言",
    "与记忆主人的关系",
    "所属组织及职务",
    "所在地",
    "概述",
    "兴趣爱好",
    "沟通风格",
    "决策风格",
]
OWNER_FIELDS = ["真名" if f == "姓名" else f for f in PERSON_FIELDS] + ["当前关注", "约定待办"]
PROJECT_FIELDS = ["名称", "别名", "仓库地址", "与记忆主人的关系", "概述", "工程规范", "当前状态"]
ORG_FIELDS = ["名称", "别名", "类型", "所在地", "与记忆主人的关系", "概述"]
PRODUCT_FIELDS = ["名称", "别名", "类型", "所属组织", "与记忆主人的关系", "概述"]
CONCEPT_FIELDS = ["名称", "别名", "种类", "适用对象", "与记忆主人的关系", "概述"]
CONCEPT_KINDS = {"规则", "方法", "框架", "原则"}
NOT_PRODUCT = ("模板", "框架", "方法论", "量表", "题库", "制度", "规范", "原则", "流程")
SINGLE = {"真名", "姓名", "性别", "出生时间", "所属组织", "所属组织及职务", "所在地"}
STYLE = {"兴趣爱好", "决策风格", "沟通风格"}
EMBED_PRESETS = (
    {"id": "BAAI/bge-small-zh-v1.5", "label": "bge-small-zh", "note": "中文，体积小。镜像里已经装好，默认用这个。"},
    {"id": "BAAI/bge-base-zh-v1.5", "label": "bge-base-zh", "note": "中文，更准，体积更大。第一次切换时下载。"},
    {"id": "BAAI/bge-small-en-v1.5", "label": "bge-small-en", "note": "英文。第一次切换时下载。"},
)
PERSON_CHAPTERS = ["教育背景", "事业经历", "代表作品", "参与项目", "个人生活", "社交关系", "健康档案"]
VERBATIM_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{4}年\d{1,2}月\d{1,2}日"
    r"|v\d+(?:\.\d+){1,3}"
    r"|\d+(?:\.\d+)?(?:元|美元|万)"
    r"|\d{4,}"
)
STATUTE_RE = re.compile(r"第[0-9０-９一二三四五六七八九十百千零〇]+条")
INSTRUMENT_RE = re.compile(r"规章|条例|章程|办法|规定|合同|协议")
COMMAND_RE = re.compile(
    r"(?:git|npm|pnpm|yarn|docker|curl|ssh|python3?|pip3?|brew|kubectl|cargo|go)\s+"
    r"[A-Za-z0-9_./:=-]{1,40}(?:\s+--?[A-Za-z0-9_.=/-]+)*"
)
CONFIG_RE = re.compile(
    r"[A-Za-z_][\w.-]{0,32}\s*=\s*(?:\"[^\"\n]{1,80}\"|'[^'\n]{1,80}'|[A-Za-z0-9_.:/@+-]{1,80})"
)
FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9/])[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fff\w]{0,12}[：:](?!/)[^，。；\n]{1,40}"
)
RECALL_RE = re.compile(r"<relevant-memories>[\s\S]*?</relevant-memories>", re.I)

_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_LOCAL = threading.local()
_DATA: Path | None = None
_SCAN: Path | None = None
_VECS: list[tuple[int, list[float]]] = []
_EMBED = "off"
_MODEL = None
_EMBED_GATE = threading.Lock()
_VECTOR_CATCHUP = threading.Lock()
_VECTOR_BATCH = 64
_LAST_SCAN = 0.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _keep_instant(con: sqlite3.Connection, table: str, clock: str, dest: str) -> None:
    """Store a parsed instant beside a clock so a windowed read can use an index.

    Writers do not all use one ISO shape. A string bound is not a superset of them.
    """

    known = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    if dest not in known:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {dest} REAL")
    expr = f"julianday(replace(trim(NEW.{clock}), 't', 'T'))"
    stored = f"julianday(replace(trim({clock}), 't', 'T'))"
    con.execute(f"DROP TRIGGER IF EXISTS {table}_{dest}_insert")
    con.execute(f"DROP TRIGGER IF EXISTS {table}_{dest}_update")
    con.execute(
        f"""CREATE TRIGGER {table}_{dest}_insert
            AFTER INSERT ON {table}
            BEGIN
              UPDATE {table} SET {dest} = {expr} WHERE id = NEW.id;
            END"""
    )
    con.execute(
        f"""CREATE TRIGGER {table}_{dest}_update
            AFTER UPDATE OF {clock} ON {table}
            BEGIN
              UPDATE {table} SET {dest} = {expr} WHERE id = NEW.id;
            END"""
    )
    con.execute(
        f"""UPDATE {table} SET {dest} = {stored}
            WHERE {dest} IS NULL
              AND IFNULL(trim({clock}), '') != ''
              AND {stored} IS NOT NULL"""
    )


def connect(data: Path, scan: Path) -> None:
    global _DB, _DB_PATH, _DATA, _SCAN
    from rogmem.runtime_files import assert_runtime_files

    assert_runtime_files()
    data.mkdir(parents=True, exist_ok=True)
    scan.mkdir(parents=True, exist_ok=True)
    _DATA = data
    _SCAN = scan
    con = _open_connection(data / "rogmem.sqlite")
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    con.executescript(schema)
    columns = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    if "last_used" not in columns:
        con.execute("ALTER TABLE agents ADD COLUMN last_used TEXT")
    if "connected_at" not in columns:
        con.execute("ALTER TABLE agents ADD COLUMN connected_at TEXT")
    if "call_count" not in columns:
        con.execute("ALTER TABLE agents ADD COLUMN call_count INTEGER NOT NULL DEFAULT 0")
    queue_columns = {row[1] for row in con.execute("PRAGMA table_info(queue)")}
    if "cursor" not in queue_columns:
        con.execute("ALTER TABLE queue ADD COLUMN cursor INTEGER NOT NULL DEFAULT 0")
    if "leased_until" not in queue_columns:
        con.execute("ALTER TABLE queue ADD COLUMN leased_until TEXT")
    if "superseded" not in queue_columns:
        con.execute("ALTER TABLE queue ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
    for name in ("enqueued_at", "started_at", "finished_at"):
        if name not in queue_columns:
            con.execute(f"ALTER TABLE queue ADD COLUMN {name} TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS queue_state_finished ON queue(state, finished_at)")
    _keep_instant(con, "queue", "finished_at", "finished_jd")
    con.execute("CREATE INDEX IF NOT EXISTS queue_done_jd ON queue(state, finished_jd)")
    con.execute(
        """CREATE INDEX IF NOT EXISTS queue_other_state ON queue(id)
           WHERE state NOT IN ('queued', 'leased', 'failed', 'done')"""
    )
    con.execute(
        """UPDATE queue SET enqueued_at = (
             SELECT ingested_at FROM sources WHERE sources.id = queue.source_id
           )
           WHERE IFNULL(enqueued_at, '') = ''"""
    )
    revision_columns = {row[1] for row in con.execute("PRAGMA table_info(revisions)")}
    for name in ("source_id", "quote", "event_at", "statement_id", "actor", "change", "basis", "snapshot", "page_json", "scope", "gist"):
        if name not in revision_columns:
            con.execute(f"ALTER TABLE revisions ADD COLUMN {name} TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS revisions_source ON revisions(source_id)")
    con.execute("CREATE INDEX IF NOT EXISTS revisions_crystal ON revisions(crystal_id, id)")
    con.execute("CREATE INDEX IF NOT EXISTS revisions_created ON revisions(created_at DESC, id DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS statements_source ON statements(source_id)")
    con.execute("CREATE INDEX IF NOT EXISTS statements_crystal ON statements(crystal_id, invalid_at)")
    con.execute(
        """CREATE INDEX IF NOT EXISTS edges_mention ON edges(src, dst)
           WHERE ended=0 AND kind='提及'"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS edges_mention_dst ON edges(dst, src)
           WHERE ended=0 AND kind='提及'"""
    )
    con.execute(
        """CREATE INDEX IF NOT EXISTS edges_live_other ON edges(statement_id, src, dst, kind)
           WHERE ended=0 AND kind!='提及'"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS sources_ingested ON sources(ingested_at, id)")
    con.execute("CREATE INDEX IF NOT EXISTS alerts_created ON alerts(created_at DESC, id DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created_at DESC, id DESC)")
    run_columns = {row[1] for row in con.execute("PRAGMA table_info(run_log)")}
    if run_columns and "step" not in run_columns:
        con.execute("ALTER TABLE run_log ADD COLUMN step TEXT NOT NULL DEFAULT ''")
    if run_columns:
        con.execute("CREATE INDEX IF NOT EXISTS run_log_created ON run_log(created_at DESC, id DESC)")
    source_columns = {row[1] for row in con.execute("PRAGMA table_info(sources)")}
    if "coverage" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN coverage TEXT")
    if "residual" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN residual TEXT")
    if "pipeline" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN pipeline TEXT")
    if "stage_json" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN stage_json TEXT")
    if "event_basis" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN event_basis TEXT")
    if "body_chars" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN body_chars INTEGER NOT NULL DEFAULT 0")
    if "body_preview" not in source_columns:
        con.execute("ALTER TABLE sources ADD COLUMN body_preview TEXT NOT NULL DEFAULT ''")
    con.execute(
        """CREATE TABLE IF NOT EXISTS source_cards (
             id TEXT PRIMARY KEY,
             kind TEXT NOT NULL DEFAULT '',
             filename TEXT NOT NULL DEFAULT '',
             ingested_at TEXT NOT NULL DEFAULT '',
             pipeline TEXT NOT NULL DEFAULT '',
             status TEXT NOT NULL DEFAULT '',
             body_chars INTEGER NOT NULL DEFAULT 0,
             body_preview TEXT NOT NULL DEFAULT ''
           )"""
    )
    for name in (
        "sources_body_chars_insert",
        "sources_body_chars_update",
        "sources_body_note_insert",
        "sources_body_note_update",
        "source_cards_insert",
        "source_cards_status",
        "source_cards_body",
    ):
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
    con.execute(
        """CREATE TRIGGER source_cards_insert
           AFTER INSERT ON sources
           BEGIN
             INSERT INTO source_cards(id, kind, filename, ingested_at, pipeline, status, body_chars, body_preview)
             VALUES (
               NEW.id,
               IFNULL(NEW.kind, ''),
               IFNULL(NEW.filename, ''),
               IFNULL(NEW.ingested_at, ''),
               IFNULL(NEW.pipeline, ''),
               IFNULL(NEW.status, ''),
               length(NEW.body),
               replace(replace(substr(NEW.body, 1, 80), char(10), ' '), char(13), ' ')
             );
           END"""
    )
    con.execute(
        """CREATE TRIGGER source_cards_status
           AFTER UPDATE OF status, pipeline, filename, kind, ingested_at ON sources
           BEGIN
             INSERT INTO source_cards(id, kind, filename, ingested_at, pipeline, status, body_chars, body_preview)
             VALUES (
               NEW.id,
               IFNULL(NEW.kind, ''),
               IFNULL(NEW.filename, ''),
               IFNULL(NEW.ingested_at, ''),
               IFNULL(NEW.pipeline, ''),
               IFNULL(NEW.status, ''),
               length(NEW.body),
               replace(replace(substr(NEW.body, 1, 80), char(10), ' '), char(13), ' ')
             )
             ON CONFLICT(id) DO UPDATE SET
               kind=excluded.kind,
               filename=excluded.filename,
               ingested_at=excluded.ingested_at,
               pipeline=excluded.pipeline,
               status=excluded.status;
           END"""
    )
    con.execute(
        """CREATE TRIGGER source_cards_body
           AFTER UPDATE OF body ON sources
           BEGIN
             INSERT INTO source_cards(id, kind, filename, ingested_at, pipeline, status, body_chars, body_preview)
             VALUES (
               NEW.id,
               IFNULL(NEW.kind, ''),
               IFNULL(NEW.filename, ''),
               IFNULL(NEW.ingested_at, ''),
               IFNULL(NEW.pipeline, ''),
               IFNULL(NEW.status, ''),
               length(NEW.body),
               replace(replace(substr(NEW.body, 1, 80), char(10), ' '), char(13), ' ')
             )
             ON CONFLICT(id) DO UPDATE SET
               kind=excluded.kind,
               filename=excluded.filename,
               ingested_at=excluded.ingested_at,
               pipeline=excluded.pipeline,
               status=excluded.status,
               body_chars=excluded.body_chars,
               body_preview=excluded.body_preview;
           END"""
    )
    statement_columns = {row[1] for row in con.execute("PRAGMA table_info(statements)")}
    if "home_crystal" not in statement_columns:
        con.execute("ALTER TABLE statements ADD COLUMN home_crystal TEXT")
    if "valid_until" not in statement_columns:
        con.execute("ALTER TABLE statements ADD COLUMN valid_until TEXT")
    edge_columns = {row[1] for row in con.execute("PRAGMA table_info(edges)")}
    if "home_src" not in edge_columns:
        con.execute("ALTER TABLE edges ADD COLUMN home_src TEXT")
    if "home_dst" not in edge_columns:
        con.execute("ALTER TABLE edges ADD COLUMN home_dst TEXT")
    host_columns = {row[1] for row in con.execute("PRAGMA table_info(hosts)")}
    if host_columns and "importable" not in host_columns:
        con.execute("ALTER TABLE hosts ADD COLUMN importable INTEGER")
    agent_columns = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    if agent_columns and "capabilities" not in agent_columns:
        con.execute("ALTER TABLE agents ADD COLUMN capabilities TEXT NOT NULL DEFAULT ''")
    job_columns = {row[1] for row in con.execute("PRAGMA table_info(host_jobs)")}
    if job_columns and "lease_token" not in job_columns:
        con.execute("ALTER TABLE host_jobs ADD COLUMN lease_token TEXT")
    if job_columns:
        # R5 A2：先把老库里同 (agent_id, program) 多条 active 的历史数据规范化，
        # 保留最新一条（created_at DESC, id DESC），其余置 failed + last_error_class
        # 记为 'superseded'（保审计痕迹；不删行、不隐藏历史）。
        # 没有 ROW_NUMBER() 时（SQLite < 3.25），下面的 window function 兼容走 EXISTS 子查询。
        active_states = ("pending", "claimed", "running", "partial")
        placeholders = ",".join("?" for _ in active_states)
        now_iso = utcnow()
        con.execute(
            f"""UPDATE host_jobs
                  SET status='failed', completed_at=?, last_error_class='superseded'
                WHERE status IN ({placeholders})
                  AND EXISTS (
                    SELECT 1 FROM host_jobs winner
                     WHERE winner.agent_id = host_jobs.agent_id
                       AND winner.program  = host_jobs.program
                       AND winner.status IN ({placeholders})
                       AND (winner.created_at > host_jobs.created_at
                            OR (winner.created_at = host_jobs.created_at
                                AND winner.id > host_jobs.id))
                  )""",
            (now_iso, *active_states, *active_states),
        )
        # R4-4：跨进程 in-flight 唯一（partial UNIQUE 索引）。幂等；新库也会命中。
        con.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS host_jobs_one_active_per_scope
                 ON host_jobs(agent_id, program)
                 WHERE status IN ('pending','claimed','running','partial')"""
        )
    chunk_columns = {row[1] for row in con.execute("PRAGMA table_info(chunks)")}
    if "role" not in chunk_columns:
        con.execute("ALTER TABLE chunks ADD COLUMN role TEXT NOT NULL DEFAULT 'child'")
    if "parent_id" not in chunk_columns:
        con.execute("ALTER TABLE chunks ADD COLUMN parent_id INTEGER")
    if "embed_text" not in chunk_columns:
        con.execute("ALTER TABLE chunks ADD COLUMN embed_text TEXT")
    con.execute(
        """CREATE TABLE IF NOT EXISTS chunk_revisions(
             id INTEGER PRIMARY KEY,
             chunk_id INTEGER NOT NULL,
             previous TEXT NOT NULL,
             created_at TEXT NOT NULL
           )"""
    )
    from rogmem.crystal_store import migrate_crystal_store

    migrate_crystal_store(con)
    _DB = con
    _DB_PATH = (data / "rogmem.sqlite").resolve()
    _LOCAL.con = con
    _LOCAL.path = _DB_PATH
    jieba.initialize()
    _ensure_owner(con)
    _ensure_default_minimax()
    _ensure_model_catalog()
    _load_vecs(con)
    migrate_source_statuses(con)
    backfill_revision_scopes(con)
    backfill_revision_basis(con)
    settle_file_statuses()
    withdraw_projects_without_repo()
    rebuild_overviews()
    _link_relations(con)
    _backfill_undated_focus_statements(con)
    from rogmem.crystal_index import reindex_crystals

    reindex_crystals(con)
    con.commit()
    release_orphaned_leases()
    ensure_password()
    _attribute_loose_turns()
    _start_operation_log_refresher()
    _keep_instant(con, "provider_health", "at", "probed_jd")
    con.execute("CREATE INDEX IF NOT EXISTS provider_health_jd ON provider_health(provider_id, probed_jd)")
    try:
        from rogmem import cost as cost_mod

        with cost_mod._store(write=True) as ledger:
            ledger.commit()
        cost_mod._start_board_refresher()
    except Exception:
        pass
    # Leave autocommit. A later request thread mints hub_id; an open write here blocks it.
    con.commit()


def _open_connection(path: Path) -> sqlite3.Connection:
    """One connection per thread. Sharing one connection across threads is what made the admin wait and the distill raise SQLITE_MISUSE."""
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _sqlite_path() -> Path:
    if _DATA is None:
        raise RuntimeError("database is not open")
    return (_DATA / "rogmem.sqlite").resolve()


def _bind_thread_connection(con: sqlite3.Connection, path: Path) -> sqlite3.Connection:
    _LOCAL.con = con
    _LOCAL.path = path
    return con


def db() -> sqlite3.Connection:
    """The database for the current data directory.

    A thread keeps its connection, but not across a switch to another data
    directory. Otherwise a request handled on a reused thread checks the
    previous database's password and rejects the new one.
    """
    path = _sqlite_path()
    current = getattr(_LOCAL, "con", None)
    if current is not None and getattr(_LOCAL, "path", None) == path:
        return current
    if current is not None:
        try:
            current.close()
        except Exception:
            pass
        _LOCAL.con = None
        _LOCAL.path = None
    global _DB, _DB_PATH
    if threading.current_thread() is threading.main_thread():
        if _DB is None or _DB_PATH != path:
            if _DB is not None:
                try:
                    _DB.close()
                except Exception:
                    pass
            _DB = _open_connection(path)
            _DB_PATH = path
        return _bind_thread_connection(_DB, path)
    opened = _open_connection(path)
    return _bind_thread_connection(opened, path)


def password_ready() -> bool:
    return bool(get_setting("admin_password"))


def _data_dir() -> Path:
    if _DATA is not None:
        return _DATA
    raw = os.environ.get("ROGMEM_DATA") or "/data"
    return Path(raw)


INITIAL_ADMIN_PASSWORD = "Rogmem@2026"


def password_gaps(value: str) -> list[str]:
    """What a new password still lacks. Empty means it can be saved."""
    text = value or ""
    gaps: list[str] = []
    if len(text) < 8:
        gaps.append("至少 8 位")
    if not any(char.isupper() for char in text):
        gaps.append("大写")
    if not any(char.islower() for char in text):
        gaps.append("小写")
    if not any(char.isdigit() for char in text):
        gaps.append("数字")
    if not any(not char.isalnum() for char in text):
        gaps.append("符号")
    return gaps


def ensure_password() -> None:
    from rogmem import auth

    if auth.allow_password_bypass(_data_dir()):
        return
    if get_setting("admin_password"):
        return
    raw = INITIAL_ADMIN_PASSWORD
    marker = _data_dir() / "admin.password"
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (raw + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(marker, 0o600)
    set_setting("admin_password", auth.hash_password(raw))
    set_setting("admin_password_generation", "1")
    set_setting("admin_password_pending", "1")
    db().commit()


def admin_login(password: str) -> bool:
    from rogmem import auth

    if auth.allow_password_bypass(_data_dir()):
        return True
    encoded = get_setting("admin_password") or ""
    return bool(encoded) and auth.verify_password(password or "", encoded)


def passwordless_enabled() -> bool:
    """免密登录。没存过就是开。关掉之后管理写操作要检查口令。"""
    raw = (get_setting("passwordless") or "").strip().lower()
    if raw in {"0", "off", "false", "no"}:
        return False
    return True


def user_password_set() -> bool:
    """用户在基础设置里自己设过口令。初始口令 rogmem 不算。"""
    return (get_setting("admin_password_user_set") or "") == "1"


def admin_password_required() -> bool:
    """设了密码，或关掉免密，管理写操作才检查口令。"""
    return user_password_set() or not passwordless_enabled()


def take_password_reveal() -> str:
    if get_setting("admin_password_pending") != "1":
        return ""
    path = _data_dir() / "admin.password"
    secret = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    set_setting("admin_password_pending", "0")
    db().commit()
    return secret


def change_password(current: str, new: str, confirm: str | None = None) -> dict:
    from rogmem import auth

    nxt = new or ""
    again = nxt if confirm is None else confirm
    gaps = password_gaps(nxt)
    problems: list[str] = []
    if gaps:
        problems.append("还缺" + "、".join(gaps))
    if nxt != again:
        problems.append("两次输入的新口令不一致")
    if problems:
        return {"ok": False, "error": "。".join(problems)}
    if not admin_login(current or ""):
        return {"ok": False, "error": "口令不对"}
    generation = int(get_setting("admin_password_generation") or "1") + 1
    with LOCK:
        set_setting("admin_password", auth.hash_password(nxt))
        set_setting("admin_password_generation", str(generation))
        set_setting("admin_password_pending", "0")
        set_setting("admin_password_user_set", "1")
        db().commit()
    marker = _data_dir() / "admin.password"
    if marker.is_file():
        marker.unlink()
    return {"ok": True}


def save_webhook(url: str) -> dict:
    from rogmem.webhook_policy import validate_webhook_url

    cleaned = (url or "").strip()
    if not cleaned:
        with LOCK:
            set_setting("alert_webhook", "")
            db().commit()
        return {"ok": True, "webhook": ""}
    error = validate_webhook_url(cleaned)
    if error:
        return {"ok": False, "error": error}
    with LOCK:
        set_setting("alert_webhook", cleaned)
        db().commit()
    return {"ok": True, "webhook": cleaned}


def _release_pause_messages(*messages: str) -> int:
    marks = tuple(message for message in messages if message)
    if not marks:
        return 0
    placeholders = ",".join("?" * len(marks))
    with LOCK:
        parked = db().execute(
            f"SELECT id FROM queue WHERE last_error IN ({placeholders})",
            marks,
        ).fetchall()
        alerts_open = db().execute(
            f"SELECT id FROM alerts WHERE open=1 AND message IN ({placeholders})",
            marks,
        ).fetchall()
        if not parked and not alerts_open:
            return 0
        db().execute(
            f"UPDATE alerts SET open=0 WHERE open=1 AND message IN ({placeholders})",
            marks,
        )
        db().execute(
            f"""UPDATE queue SET state='queued', next_at=NULL, attempts=0, last_error=NULL
                WHERE last_error IN ({placeholders})""",
            marks,
        )
        db().execute(
            f"UPDATE sources SET status=?, last_error=NULL, attempt=0 WHERE last_error IN ({placeholders})",
            (ST_VEC_READY, *marks),
        )
        db().commit()
    return len(parked)


def release_model_pause() -> int:
    """Resume units parked only because no distill model was configured."""
    if llm_target("distill") is None:
        return 0
    return _release_pause_messages(MISSING_DISTILL_MODEL)


def recover_auth_alerts() -> int:
    """Close open model-auth alerts and notify once that they recovered."""
    with LOCK:
        rows = db().execute(
            "SELECT id, message FROM alerts WHERE open=1 AND message LIKE '外部模型鉴权失败%'"
        ).fetchall()
        if not rows:
            return 0
        db().execute("UPDATE alerts SET open=0 WHERE open=1 AND message LIKE '外部模型鉴权失败%'")
        db().commit()
        messages = [row["message"] for row in rows]
    for message in messages:
        notify_alert("恢复：" + message)
    return len(messages)


def notify_alert(message: str) -> None:
    url = get_setting("alert_webhook")
    if not url:
        return
    from rogmem import webhook_policy

    try:
        webhook_policy.deliver(url, webhook_policy.alert_payload(message))
    except Exception:
        return


def machine_budget() -> float:
    """Fraction of this machine the hub may use. The rest stays for the NAS."""
    raw = (os.environ.get("ROGMEM_BUDGET") or "0.75").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.75
    if value <= 0 or value > 1:
        return 0.75
    return value


def embed_threads() -> int:
    """Threads for one embedding. Callers share one model, so this is the whole CPU budget."""
    cores = os.cpu_count() or 1
    return max(1, min(cores, int(cores * machine_budget())))


def distill_concurrency() -> int:
    """How many distill units may run at once. 1 restores the old serial worker."""
    raw = (os.environ.get("ROGMEM_DISTILL_CONCURRENCY") or "3").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(1, min(n, 32))


def release_orphaned_leases() -> int:
    """Leases from a previous process are stale. A call still running in this process is not."""
    if _DB is None:
        return 0
    live = _live_lease_ids()
    skip = ""
    if live:
        skip = " AND id NOT IN (" + ",".join("?" * len(live)) + ")"
    with LOCK:
        cur = db().execute(
            f"UPDATE queue SET state='queued', leased_until=NULL WHERE state='leased'{skip}",
            live,
        )
        CrystalStore(db()).release_orphaned()
        db().commit()
        return int(cur.rowcount)


def hub_id() -> str:
    """Which memory this process is. Stable across entrances; minted once."""
    current = (get_setting("hub_id") or "").strip()
    if current:
        return current
    with LOCK:
        current = (get_setting("hub_id") or "").strip()
        if current:
            return current
        minted = "rh_" + secrets.token_hex(16)
        set_setting("hub_id", minted)
        db().commit()
        return minted


def health() -> dict:
    ok = True
    leased = 0
    identity = ""
    try:
        db().execute("SELECT 1").fetchone()
        leased = db().execute("SELECT COUNT(*) AS n FROM queue WHERE state='leased'").fetchone()["n"]
        identity = hub_id()
    except Exception:
        ok = False
    from rogmem.version import product_version

    return {
        "ok": ok,
        "service": "rogmem",
        "hub": identity,
        "version": product_version(),
        "embed": _EMBED,
        "scan": str(_SCAN),
        "password_ready": password_ready() if ok else False,
        "distill_concurrency": distill_concurrency(),
        "distill_leased": leased if ok else 0,
    }


def _embed_cache() -> Path:
    root = os.environ.get("ROGMEM_DATA") or (str(_DATA) if _DATA else "")
    path = Path(root) / "fastembed" if root else Path(tempfile.gettempdir()) / "fastembed_cache"
    path.mkdir(parents=True, exist_ok=True)
    baked = Path(os.environ.get("ROGMEM_EMBED_BAKED", "/opt/fastembed"))
    if baked.is_dir() and any(baked.glob("models--*")) and not any(path.glob("models--*")):
        import shutil

        shutil.copytree(baked, path, dirs_exist_ok=True)
    (path / ".rogmem-embed-cache").write_text("fastembed\n", encoding="utf-8")
    return path


def embed_model_id() -> str:
    try:
        saved = get_setting("embed_model")
    except Exception:
        saved = ""
    ids = {item["id"] for item in EMBED_PRESETS}
    if saved in ids:
        return saved
    return os.environ.get("ROGMEM_EMBED_MODEL", EMBED_PRESETS[0]["id"])


def _embedder_ready() -> bool:
    """A local weight is loaded, or a remote assignment already has a key."""
    return _MODEL is not None or _embed_is_remote()


def _release_local_weights() -> None:
    """The machine is not the embedder. Drop the local weights and their thread claim."""
    global _MODEL
    _MODEL = None
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"


def set_embed_mode(mode: str) -> None:
    global _EMBED, _MODEL
    _EMBED = mode
    if mode == "fastembed" and _embed_is_remote():
        _release_local_weights()
        return
    if mode == "fastembed":
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        threads = embed_threads()
        for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[key] = str(threads)
        cache = str(_embed_cache())
        os.environ["FASTEMBED_CACHE_PATH"] = cache
        os.environ.setdefault("HF_HUB_CACHE", cache)
        from fastembed import TextEmbedding

        name = embed_model_id()
        try:
            _MODEL = TextEmbedding(name, cache_dir=cache, threads=threads)
        except TypeError:
            _MODEL = TextEmbedding(name, cache_dir=cache)
    elif mode == "hash":
        name = "hash"
        _MODEL = None
    else:
        name = "off"
        _MODEL = None
    _note_embed_model(name)


def _embed_one(text: str) -> list[float] | None:
    if _EMBED == "off":
        return None
    if _EMBED == "hash":
        digest = hashlib.sha256(text.encode()).digest()
        vals = [(b / 127.5) - 1 for b in digest]
        norm = sum(v * v for v in vals) ** 0.5 or 1
        return [v / norm for v in vals]
    if _MODEL is None and not _embed_is_remote():
        return None
    found = _run_model([text])
    return found[0] if found else None


def _embed_is_remote() -> bool:
    """A registered embedding model with a key is called remotely. An empty key stays on this machine."""
    try:
        token = get_setting("model_embed") or ""
    except Exception:
        return False
    if "|" not in token:
        return False
    ident, model = token.split("|", 1)
    if ident == LOCAL_EMBED_ID or model in {item["id"] for item in EMBED_PRESETS}:
        return False
    try:
        provider = next((item for item in _providers() if item.get("id") == ident), None)
    except Exception:
        return False
    if provider is None or not str(provider.get("api_key") or "").strip():
        return False
    return model in (provider.get("models") or []) and model_kind(provider, model) == MODEL_KIND_EMBED


# One embeddings request carries a short list. A longer passage is several requests.
_REMOTE_EMBED_BATCH = 16


def _remote_embed(texts: list[str]) -> list[list[float] | None]:
    token = get_setting("model_embed") or ""
    ident, model = token.split("|", 1)
    provider = next((item for item in _providers() if item.get("id") == ident), None)
    if provider is None or not str(provider.get("api_key") or "").strip():
        return [None for _ in texts]
    found: list[list[float] | None] = []
    for start in range(0, len(texts), _REMOTE_EMBED_BATCH):
        found.extend(_remote_embed_call(provider, model, texts[start : start + _REMOTE_EMBED_BATCH]))
    return found


@charging("向量嵌入")
def _remote_embed_call(provider: dict, model: str, texts: list[str]) -> list[list[float] | None]:
    """One call. A rejected list is split; a single rejected text stays empty."""
    payload = {"model": model, "input": texts}
    req = urllib.request.Request(
        str(provider.get("base_url") or "").rstrip("/") + "/embeddings",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + str(provider.get("api_key") or "")},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode())
        _meter_chat(
            str(provider.get("base_url") or ""),
            model,
            [{"role": "user", "content": "\n".join(str(text) for text in texts)}],
            "",
            raw,
            "",
        )
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 413} and len(texts) > 1:
            mid = len(texts) // 2
            return _remote_embed_call(provider, model, texts[:mid]) + _remote_embed_call(provider, model, texts[mid:])
        return [None for _ in texts]
    except Exception:
        return [None for _ in texts]
    found: list[list[float] | None] = [None for _ in texts]
    for item in raw.get("data") or []:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            continue
        index = int(item.get("index") or 0)
        if 0 <= index < len(found):
            found[index] = [float(value) for value in item["embedding"]]
    return found


def _run_model(texts: list[str]) -> list[list[float] | None]:
    """One embedding at a time. A remote model does not take the machine's cores."""
    if not texts:
        return []
    if _embed_is_remote():
        with _EMBED_GATE:
            return _remote_embed(texts)
    if _MODEL is None:
        return [None for _ in texts]
    with _EMBED_GATE:
        try:
            return [[float(value) for value in vec] for vec in _MODEL.embed(texts)]
        except Exception:
            return [None for _ in texts]


def rebuild_chunk_vectors() -> int:
    """Re-embed stored chunk text. Does not change source status or the distill queue.

    Each vector is written and committed before the next embed. A write left open
    across the slow call is what makes a later retry see database is locked.
    """
    if _EMBED == "off":
        return 0
    rows = db().execute("SELECT id, text FROM chunks WHERE text != ''").fetchall()
    changed = 0
    for row in rows:
        vec = _embed_one(row["text"])
        if not vec:
            continue
        with LOCK:
            db().execute("UPDATE chunks SET vec_json=? WHERE id=?", (json.dumps(vec), row["id"]))
            db().commit()
        changed += 1
    _load_vecs(db())
    return changed


def _note_embed_model(name: str) -> int:
    if _DB is None or not name:
        return 0
    previous = get_setting("embed_model")
    rebuilt = rebuild_chunk_vectors() if previous and previous != name else 0
    set_setting("embed_model", name)
    db().commit()
    return rebuilt


def _load_vecs(con: sqlite3.Connection) -> None:
    global _VECS
    _VECS = []
    for row in con.execute("SELECT id, vec_json FROM chunks WHERE vec_json IS NOT NULL AND text != ''"):
        _VECS.append((row["id"], json.loads(row["vec_json"])))


def _tokens(text: str) -> str:
    from rogmem.crystal_index import tokens

    return tokens(text)


def _match_query(text: str) -> str:
    parts = [p.replace('"', "") for p in jieba.cut_for_search(text) if p.strip()]
    if not parts:
        return ""
    return " OR ".join(f'"{p}"' for p in parts[:12])


def _chunk(text: str) -> list[tuple[str, str]]:
    from rogmem.chunker import split_text

    pieces = split_text(text)
    return [(str(index), piece.content) for index, piece in enumerate(pieces, start=1) if piece.content.strip()]


def strip_recall(text: str) -> str | None:
    cleaned = RECALL_RE.sub("", text).strip()
    if not cleaned:
        return None
    return cleaned


def resolve_device_name(name: str | None = None) -> str:
    text = given_device_name(name)
    if text:
        return text
    return normalize_device_name(None)


def enroll_apply(device_id: str, device_name: str, program: str, pubkey_b64: str) -> dict:
    pubkey = crypto.b64decode(pubkey_b64)
    code = crypto.code_for(pubkey)
    program = _label(program)
    label = resolve_device_name(device_name)
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM agents WHERE pubkey=?", (pubkey_b64,)).fetchone()
        if row:
            return {"ok": True, "id": row["id"], "status": row["status"], "code": code, "duplicate": True}
        agent_id = secrets.token_hex(8)
        display = f"{label} · {program}" if label else program
        con.execute(
            """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at)
               VALUES(?,?,?,?,?,'待放行',?,NULL,?)""",
            (agent_id, device_id, label, program, display, pubkey_b64, utcnow()),
        )
        con.commit()
    return {"ok": True, "id": agent_id, "status": "待放行", "code": code, "duplicate": False}


def enroll_approve(agent_id: str) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row or row["status"] not in {"待放行", "待领取"}:
            return {"ok": False, "error": "不能放行"}
        con.execute("UPDATE agents SET status='待领取' WHERE id=?", (agent_id,))
        con.commit()
    return {"ok": True, "status": "待领取"}


def approve_device(device_id: str) -> dict:
    """Release every pending agent on one device. Other devices stay pending."""
    device_id = (device_id or "").strip()
    if not device_id:
        return {"ok": False, "error": "没有设备", "ids": []}
    with LOCK:
        con = db()
        rows = con.execute(
            "SELECT id FROM agents WHERE device_id=? AND status='待放行' ORDER BY created_at",
            (device_id,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return {"ok": False, "error": "没有待放行", "ids": []}
        con.execute(
            "UPDATE agents SET status='待领取' WHERE device_id=? AND status='待放行'",
            (device_id,),
        )
        con.commit()
    return {"ok": True, "status": "待领取", "ids": ids}


def rename_agent(agent_id: str, display_name: str) -> dict:
    """Change the label only. Device and program stay the identity."""
    name = (display_name or "").strip()
    if not name:
        return {"ok": False, "error": "显示名不能空"}
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "没有这一条"}
        con.execute("UPDATE agents SET display_name=? WHERE id=?", (name, agent_id))
        con.commit()
        fresh = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return {
        "ok": True,
        "id": fresh["id"],
        "device_id": fresh["device_id"],
        "program": fresh["program"],
        "display_name": fresh["display_name"],
    }


def enroll_reclaim(pubkey_b64: str, device_id: str = "", program: str = "") -> dict:
    """Send an issued key back to review. The same row keeps its identity and the old key dies."""
    device_id = (device_id or "").strip()
    program = (program or "").strip()
    try:
        crypto.b64decode(pubkey_b64)
    except Exception:
        return {"ok": False, "error": "没有这条申请"}
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM agents WHERE pubkey=?", (pubkey_b64,)).fetchone()
        rotate = False
        if row is None and device_id and program:
            row = con.execute(
                "SELECT * FROM agents WHERE device_id=? AND program=?",
                (device_id, program),
            ).fetchone()
            rotate = row is not None
        if row is None:
            return {"ok": False, "error": "没有这条申请"}
        if device_id and row["device_id"] != device_id:
            return {"ok": False, "error": "不是这台设备"}
        if program and row["program"] != program:
            return {"ok": False, "error": "不是这个程序"}
        if row["status"] not in {"启用", "停用"}:
            return {"ok": False, "error": "还没有领取过"}
        if rotate:
            taken = con.execute(
                "SELECT id FROM agents WHERE pubkey=? AND id!=?",
                (pubkey_b64, row["id"]),
            ).fetchone()
            if taken:
                return {"ok": False, "error": "这把公钥已经有一条申请"}
            con.execute(
                "UPDATE agents SET status='待放行', key_hash=NULL, pubkey=? WHERE id=?",
                (pubkey_b64, row["id"]),
            )
        else:
            con.execute("UPDATE agents SET status='待放行', key_hash=NULL WHERE id=?", (row["id"],))
        con.commit()
    return {
        "ok": True,
        "id": row["id"],
        "device_id": row["device_id"],
        "program": row["program"],
        "status": "待放行",
        "code": crypto.code_for(crypto.b64decode(pubkey_b64)),
    }


_GRANT_TTL = timedelta(minutes=20)
_AGENT_RANK = {"启用": 0, "停用": 1, "已拒绝": 2, "待领取": 3, "待放行": 4}


def create_enroll_grant() -> dict:
    """Mint a short-lived enrollment secret. The raw value is returned once.

    This does not take the distillation lock. One insert is already serialized by
    the database, and the admin button must not wait out a model call.
    """
    secret = "rg_" + secrets.token_urlsafe(32)
    grant_id = secrets.token_hex(8)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires = now + _GRANT_TTL
    con = db()
    con.execute(
        """INSERT INTO enroll_grants(id, secret_hash, created_at, expires_at, device_id, device_name)
           VALUES(?,?,?,?, '', '')""",
        (grant_id, digest, now.isoformat(), expires.isoformat()),
    )
    con.commit()
    return {"ok": True, "id": grant_id, "secret": secret, "expires_at": expires.isoformat()}


def _grant_error(error: str) -> dict:
    return {"ok": False, "error": error, "status": "", "already": False, "wrapped": "", "id": ""}


def _seal_for(pubkey_b64: str) -> tuple[str, str]:
    token = "rm_" + secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode()).hexdigest()
    wrapped = crypto.b64encode(crypto.seal(crypto.b64decode(pubkey_b64), token.encode()))
    return digest, wrapped


def _remember_redemption(con: sqlite3.Connection, grant_id: str, program: str, agent_id: str, now: str, wrapped: str = "") -> None:
    con.execute(
        """INSERT INTO enroll_grant_redemptions(grant_id, program, agent_id, redeemed_at, wrapped)
           VALUES(?,?,?,?,?)
           ON CONFLICT(grant_id, program) DO NOTHING""",
        (grant_id, program, agent_id, now, wrapped),
    )


def redeem_enroll_grant(secret: str, device_id: str, device_name: str, program: str, pubkey_b64: str) -> dict:
    """Exchange one enrollment grant for this program's own key. An enabled host is left as it is."""
    device_id = (device_id or "").strip()
    program = (program or "").strip()
    presented = (secret or "").strip()
    if program not in HOST_PROGRAMS:
        return _grant_error("不是这个程序")
    if not device_id or not presented.startswith("rg_"):
        return _grant_error("凭证无效")
    try:
        crypto.b64decode(pubkey_b64)
    except Exception:
        return _grant_error("没有公钥")
    label = resolve_device_name(device_name)
    digest = hashlib.sha256(presented.encode()).hexdigest()
    with LOCK:
        con = db()
        grant = con.execute("SELECT * FROM enroll_grants WHERE secret_hash=?", (digest,)).fetchone()
        if grant is None or not hmac.compare_digest(grant["secret_hash"], digest):
            return _grant_error("凭证无效")
        expires = datetime.fromisoformat(grant["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return _grant_error("凭证过期")
        bound = (grant["device_id"] or "").strip()
        if bound and bound != device_id:
            return _grant_error("不是这台设备")
        if not bound:
            con.execute(
                "UPDATE enroll_grants SET device_id=?, device_name=? WHERE id=? AND device_id=''",
                (device_id, label, grant["id"]),
            )
            bound_now = con.execute("SELECT device_id FROM enroll_grants WHERE id=?", (grant["id"],)).fetchone()
            if bound_now is None or bound_now["device_id"] != device_id:
                con.rollback()
                return _grant_error("不是这台设备")
        prior = con.execute(
            "SELECT agent_id, wrapped FROM enroll_grant_redemptions WHERE grant_id=? AND program=?",
            (grant["id"], program),
        ).fetchone()
        if prior:
            con.commit()
            sealed = prior["wrapped"] or ""
            return {
                "ok": True,
                "error": "",
                "status": "启用" if sealed else "已接入",
                "already": not sealed,
                "wrapped": sealed,
                "id": prior["agent_id"],
            }
        rows = con.execute(
            "SELECT * FROM agents WHERE device_id=? AND program=? ORDER BY created_at",
            (device_id, program),
        ).fetchall()
        current = min(rows, key=lambda row: _AGENT_RANK.get(row["status"], 9)) if rows else None
        now = utcnow()
        if current is not None and current["status"] == "启用":
            _remember_redemption(con, grant["id"], program, current["id"], now)
            con.commit()
            return {"ok": True, "error": "", "status": "已接入", "already": True, "wrapped": "", "id": current["id"]}
        if current is not None and current["status"] == "停用":
            con.commit()
            return _grant_error("已停用")
        if current is not None and current["status"] == "已拒绝":
            con.commit()
            return _grant_error("已拒绝")
        taken = con.execute(
            "SELECT id FROM agents WHERE pubkey=? AND NOT (device_id=? AND program=?)",
            (pubkey_b64, device_id, program),
        ).fetchone()
        if taken:
            con.commit()
            return _grant_error("这把公钥已经有一条申请")
        key_hash, wrapped = _seal_for(pubkey_b64)
        if current is None:
            agent_id = secrets.token_hex(8)
            con.execute(
                """INSERT INTO agents(id,device_id,device_name,program,display_name,status,pubkey,key_hash,created_at,connected_at)
                   VALUES(?,?,?,?,?,'启用',?,?,?,?)""",
                (agent_id, device_id, label, program, f"{label} · {program}" if label else program, pubkey_b64, key_hash, now, now),
            )
        else:
            agent_id = current["id"]
            con.execute(
                """UPDATE agents
                   SET status='启用', pubkey=?, key_hash=?,
                       connected_at=COALESCE(NULLIF(connected_at, ''), ?)
                   WHERE id=?""",
                (pubkey_b64, key_hash, now, agent_id),
            )
        _remember_redemption(con, grant["id"], program, agent_id, now, wrapped)
        con.commit()
    return {"ok": True, "error": "", "status": "启用", "already": False, "wrapped": wrapped, "id": agent_id}


def enroll_claim(pubkey_b64: str) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM agents WHERE pubkey=? AND status='待领取'", (pubkey_b64,)).fetchone()
        if not row:
            return {"ok": False, "error": "不能领取"}
        token = "rm_" + secrets.token_urlsafe(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        wrapped = crypto.seal(crypto.b64decode(pubkey_b64), token.encode())
        now = utcnow()
        con.execute(
            "UPDATE agents SET status='启用', key_hash=?, connected_at=COALESCE(NULLIF(connected_at, ''), ?) WHERE id=?",
            (digest, now, row["id"]),
        )
        con.commit()
    return {"ok": True, "wrapped": crypto.b64encode(wrapped), "id": row["id"]}


def agent_by_token(token: str, device_id: str | None) -> sqlite3.Row | None:
    """Resolve an enabled agent by key and device.

    Updating last_used is bookkeeping. When the distill line holds the
    database, that write may wait or fail. The door still opens: a locked
    clock must not become an unauthorized or broken MCP call.
    """
    digest = hashlib.sha256(token.encode()).hexdigest()
    con = db()
    row = con.execute("SELECT * FROM agents WHERE key_hash=? AND status='启用'", (digest,)).fetchone()
    if not row:
        return None
    device = (device_id or "").strip()
    if not device or device != row["device_id"]:
        return None
    try:
        con.execute("PRAGMA busy_timeout=50")
        try:
            con.execute("UPDATE agents SET last_used=? WHERE id=?", (utcnow(), row["id"]))
            con.commit()
        finally:
            con.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        try:
            con.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
    return row


def disable_agent(agent_id: str) -> None:
    with LOCK:
        db().execute("UPDATE agents SET status='停用' WHERE id=?", (agent_id,))
        db().commit()


def enable_agent(agent_id: str) -> dict:
    """Turn a stopped agent back on. The same key keeps working."""
    with LOCK:
        con = db()
        row = con.execute("SELECT status, key_hash FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "没有这一条"}
        if row["status"] == "启用":
            return {"ok": True, "status": "启用"}
        if row["status"] != "停用":
            return {"ok": False, "error": "现在不能通过授权"}
        if not row["key_hash"]:
            return {"ok": False, "error": "没有可恢复的授权"}
        con.execute("UPDATE agents SET status='启用' WHERE id=?", (agent_id,))
        con.commit()
    return {"ok": True, "status": "启用"}


def note_memory_call(agent_id: str) -> None:
    """Count one successful memory read. Writes and internal distill are not calls."""
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return
    with LOCK:
        db().execute(
            "UPDATE agents SET call_count = COALESCE(call_count, 0) + 1 WHERE id=? AND status='启用'",
            (agent_id,),
        )
        db().commit()


def list_agents() -> list[dict]:
    rows = db().execute(
        "SELECT id,display_name,program,device_name,device_id,status,pubkey,last_used FROM agents ORDER BY created_at"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["code"] = crypto.code_for(crypto.b64decode(item.pop("pubkey")))
        except Exception:
            item.pop("pubkey", None)
            item["code"] = ""
        out.append(item)
    return out


def reject_agent(agent_id: str) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row or row["status"] != "待放行":
            return {"ok": False, "error": "只能拒绝待放行"}
        con.execute("UPDATE agents SET status='已拒绝' WHERE id=?", (agent_id,))
        con.commit()
    return {"ok": True}


def delete_agent(agent_id: str) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "没有这一条"}
        if row["status"] not in {"待放行", "已拒绝"}:
            return {"ok": False, "error": "已绑定的只能停用"}
        con.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        con.commit()
    return {"ok": True}


def _fill_session_project(con: sqlite3.Connection, session_id: str, project: str | None) -> None:
    """Same rule as a history upload: fill an empty repository, do not replace a different one."""
    from rogmem.hermes_memory import choose_project_ref

    session_id = (session_id or "").strip()
    project = (project or "").strip()
    if not session_id or not project:
        return
    rows = con.execute(
        "SELECT id, IFNULL(project_ref,'') AS project_ref FROM sources WHERE session_id=?",
        (session_id,),
    ).fetchall()
    for row in rows:
        chosen = choose_project_ref(row["project_ref"], project)
        if chosen and chosen != row["project_ref"]:
            con.execute("UPDATE sources SET project_ref=? WHERE id=?", (chosen, row["id"]))


def _stored_basis(value: str | None) -> str | None:
    """utterance is when the words were said. file is only the file's last write."""
    text = str(value or "").strip()
    if text in {"utterance", "file"}:
        return text
    return None


def _parse_event(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _speech_instant(event_at, event_basis, ingested_at) -> datetime | None:
    """When the words were said. A file's last write, or the moment a row was stored, is not that."""
    basis = _stored_basis(event_basis) or ""
    if basis == "file":
        return None
    event = _parse_event(event_at)
    if event is None:
        return None
    if not basis:
        arrived = _parse_event(ingested_at)
        if arrived is not None and event == arrived:
            return None
    return event


def _model_clock(event_at: str, ingested_at: str, event_basis: str) -> tuple[str, str]:
    """The event clock the prompt knows. Ingest time is left blank rather than worn as speech."""
    basis = _stored_basis(event_basis) or ""
    event = str(event_at or "").strip()
    if basis == "file" and event:
        return event, "file"
    if _speech_instant(event, basis, ingested_at) is None:
        return "", ""
    return event, basis


def _can_settle_current(source, page_event_at: str) -> bool:
    """Whether this source's speech time is later than one stored field value.

    Revising a page is the model's job. This only keeps a dated field value
    from being replaced by an earlier clock or by a clock that is not speech.
    """
    speech = _speech_instant(
        _source_column(source, "event_at"),
        _source_column(source, "event_basis"),
        _source_column(source, "ingested_at"),
    )
    if speech is None:
        return False
    latest = _parse_event(page_event_at)
    if latest is None:
        return True
    return speech > latest


def _event_cannot_reopen(source, statement) -> bool:
    """Current state that a speech time already settled stays until a later speech time."""
    current = statement["event_at"] if statement is not None and "event_at" in statement.keys() else ""
    if _parse_event(current) is None:
        return False
    return not _can_settle_current(source, str(current or ""))


def submit_text(
    *,
    kind: str,
    text: str,
    session_id: str,
    turn_id: str,
    seq: int,
    agent: sqlite3.Row,
    event_at: str | None,
    project: str | None,
    lane: str = "fast",
    event_basis: str = "",
) -> dict:
    stored_lane = lane_for(
        kind=kind,
        origin=ORIGIN_LIVE if kind != "summary" and lane in {"fast", "p0", "0", ""} else None,
        legacy_lane=lane,
    )
    if kind == "summary":
        stored_lane = lane_for(kind=kind, origin=ORIGIN_BATCH)
    cleaned = strip_recall(text)
    if cleaned is None:
        return {"ok": False, "error": "只有召回内容，未入库"}
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    idem = f"{session_id}:{seq}:{content_hash}"
    planned = _planned_chunks(cleaned, "", 0)[0]
    with LOCK:
        con = db()
        old = con.execute("SELECT id FROM sources WHERE idempotency_key=?", (idem,)).fetchone()
        if old:
            _fill_session_project(con, session_id, project)
            con.commit()
            return {"ok": True, "id": old["id"], "duplicate": True}
        source_id = secrets.token_hex(8)
        now = utcnow()
        wait = turn_wait_seconds()
        if kind in {"user", "agent"}:
            mate = con.execute(
                "SELECT id FROM sources WHERE turn_id=? AND kind!=? AND kind IN ('user','agent')",
                (turn_id, kind),
            ).fetchone()
            pair = 1 if mate else 0
            from datetime import timedelta

            wait_until = None if pair else (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat()
        else:
            mate = None
            pair = 1
            wait_until = None
        con.execute(
            """INSERT INTO sources(id,kind,session_id,turn_id,msg_seq,device_id,device_name,agent_id,agent_name,
               project_ref,event_at,event_basis,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,wait_until)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'已解析待向量化',?,?,?)""",
            (
                source_id,
                kind,
                session_id,
                turn_id,
                seq,
                agent["device_id"],
                agent["device_name"],
                agent["id"],
                _shown_host_name(agent),
                project,
                event_at or None,
                _stored_basis(event_basis),
                now,
                idem,
                content_hash,
                cleaned,
                stored_lane,
                pair,
                wait_until,
            ),
        )
        if mate:
            con.execute("UPDATE sources SET pair_ready=1, wait_until=NULL WHERE turn_id=?", (turn_id,))
        _write_planned(con, source_id, planned)
        _finish_vector_gate(con, source_id, stored_lane)
        _fill_session_project(con, session_id, project)
        _settle_sessions(con, {session_id})
        con.commit()
    return {"ok": True, "id": source_id, "duplicate": False}


def _prepare_history_jobs(
    turns: list[dict], program: str, device_name: str | None
) -> tuple[list[dict], int]:
    """解析并哈希。不切块、不嵌入。切块留给已经在跑的向量补齐。
    返回 (待插入 jobs, 已拒绝条数)。被 ingest_history / ingest_history_for_job 共用。
    """
    jobs: list[dict] = []
    refused = 0
    for turn in turns or []:
        role = turn.get("role")
        event_at = str(turn.get("event_at") or "")
        cleaned = strip_recall(str(turn.get("text") or ""))
        if role not in {"user", "assistant"} or not event_at or cleaned is None:
            refused += 1
            continue
        kind = "user" if role == "user" else "agent"
        session_id = str(turn.get("session_id") or program)
        turn_id = str(turn.get("turn_id") or session_id)
        seq = int(turn.get("seq") or 0)
        content_hash = hashlib.sha256(cleaned.encode()).hexdigest()
        jobs.append({
            "kind": kind,
            "session_id": session_id,
            "turn_id": turn_id,
            "seq": seq,
            "event_at": event_at,
            "event_basis": _stored_basis(str(turn.get("event_basis") or "")),
            "label": resolve_device_name(turn.get("device_name") or device_name),
            "idem": f"{session_id}:{seq}:{content_hash}",
            "content_hash": content_hash,
            "cleaned": cleaned,
            "project": str(turn.get("project") or "").strip(),
        })
    return order_history_newest_first(jobs), refused


def _claim_unassigned_source(con, source_id: str, host) -> None:
    """A stored line that never named a host belongs to the agent uploading it."""
    if host is None:
        return
    current = con.execute(
        "SELECT IFNULL(agent_id, '') AS agent_id FROM sources WHERE id=?",
        (source_id,),
    ).fetchone()
    if current is None or str(current["agent_id"] or "").strip():
        return
    con.execute(
        "UPDATE sources SET agent_id=?, device_id=?, device_name=?, agent_name=? WHERE id=?",
        (host["id"], host["device_id"], host["device_name"], _shown_host_name(host), source_id),
    )


def _ingest_history_inline(
    con,
    jobs: list[dict],
    *,
    program: str,
    history_lane: str,
    now: str,
    bound_host=None,
    settle: bool = True,
) -> tuple[list[dict], int]:
    """在已有事务里把原文写入 sources。不切块、不嵌入。调用方负责 commit。

    返回 (imported 列表, 其中 duplicate 数)。sources 的 UNIQUE 约束是唯一真值来源。
    bound_host 是这次作业已经认下的宿主。有它时不再按设备名猜。
    settle 为假时调用方会把这场按住，等作业结束再蒸馏。
    """
    hosts = [] if bound_host is not None else _hosts_for_program(con, program)
    chosen: dict[str, sqlite3.Row | None] = {}
    imported: list[dict] = []
    dup_count = 0
    provisional_wait = (datetime.now(timezone.utc) + timedelta(seconds=turn_wait_seconds())).isoformat()
    for job in jobs:
        old = con.execute("SELECT id FROM sources WHERE idempotency_key=?", (job["idem"],)).fetchone()
        if old:
            dup_count += 1
            if bound_host is not None:
                _claim_unassigned_source(con, old["id"], bound_host)
            if job["project"]:
                from rogmem.hermes_memory import choose_project_ref

                current = con.execute(
                    "SELECT IFNULL(project_ref,'') AS project_ref FROM sources WHERE id=?",
                    (old["id"],),
                ).fetchone()
                chosen_proj = choose_project_ref(current["project_ref"] if current else "", job["project"])
                if chosen_proj and chosen_proj != (current["project_ref"] if current else ""):
                    con.execute(
                        "UPDATE sources SET project_ref=? WHERE id=?",
                        (chosen_proj, old["id"]),
                    )
            imported.append({"id": old["id"], "duplicate": True})
            continue
        if bound_host is not None:
            host = bound_host
        else:
            label = job["label"]
            if label not in chosen:
                chosen[label] = _pick_host(hosts, label)
            host = chosen[label]
        source_id = secrets.token_hex(8)
        con.execute(
            """INSERT INTO sources(id,kind,session_id,turn_id,msg_seq,device_id,device_name,agent_id,agent_name,
               project_ref,event_at,event_basis,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,wait_until)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'已解析待向量化',?,0,?)""",
            (
                source_id,
                job["kind"],
                job["session_id"],
                job["turn_id"],
                job["seq"],
                host["device_id"] if host else None,
                host["device_name"] if host else job["label"],
                host["id"] if host else None,
                _shown_host_name(host) if host else _label(program),
                job["project"],
                job["event_at"],
                job["event_basis"],
                now,
                job["idem"],
                job["content_hash"],
                job["cleaned"],
                history_lane,
                provisional_wait,
            ),
        )
        _queue_source(con, source_id, history_lane)
        _set_pipeline(con, source_id, "解析", "ok")
        _set_pipeline(con, source_id, "向量", "run")
        imported.append({"id": source_id, "duplicate": False})
    if settle:
        _settle_sessions(con, {str(job.get("session_id") or "") for job in jobs})
    return imported, dup_count


def ingest_history(turns: list[dict], program: str = "历史导入", device_name: str | None = None) -> dict:
    """Enqueue original turns as P1. Missing time or text is refused, not stored.

    评审 R2 P2-2：duplicates 计数**唯一来源**是 imported[].duplicate 的个数
    （sources 表的 UNIQUE 幂等），不另加 helper 里的 dup_count 以免双计。
    """
    history_lane = lane_for(kind="user", origin=ORIGIN_HISTORY)
    jobs, refused = _prepare_history_jobs(turns, program, device_name)
    with LOCK:
        con = db()
        now = utcnow()
        imported, _dup_count = _ingest_history_inline(
            con, jobs, program=program, history_lane=history_lane, now=now
        )
        con.commit()
    duplicates = sum(1 for item in imported if item.get("duplicate"))
    return {"ok": True, "imported": imported, "duplicates": duplicates, "refused": refused}


def bind_session_projects(items: list[dict]) -> dict:
    """Write the repository a session was uploaded with.

    An empty field is filled. The same repository with a repaired owner replaces
    the old address. A different repository is left as it was.
    """
    updated = 0
    with LOCK:
        con = db()
        before = con.total_changes
        for item in items or []:
            if not isinstance(item, dict):
                continue
            _fill_session_project(
                con,
                str(item.get("session_id") or ""),
                str(item.get("project") or ""),
            )
        updated = con.total_changes - before
        con.commit()
    return {"ok": True, "updated": updated}


NAS_PAGE = 200
NAS_CAP = 5000
NAS_IMPORT_MAX = 50


def _nas_real(candidate: Path) -> Path | None:
    """Where this path settles. A loop, a break, or a missing target settles nowhere."""
    try:
        return candidate.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None


def nas_configured_path() -> str:
    stored = ""
    try:
        stored = (get_setting("nas_path") or "").strip()
    except Exception:
        stored = ""
    if stored:
        return stored
    raw = (os.environ.get("ROGMEM_NAS", "/nas") or "").strip()
    return raw or "/nas"


def nas_root() -> Path | None:
    resolved = _nas_real(Path(nas_configured_path()))
    if resolved is None or not resolved.is_dir():
        return None
    return resolved


def _nas_skip_name(name: str) -> bool:
    return (not name) or name in {"Dockers", "#recycle"} or name.startswith(".")


def _nas_placed(root: Path, candidate: Path) -> Path | None:
    """The settled path when it is the root or still inside it."""
    real = _nas_real(candidate)
    if real is None:
        return None
    try:
        rel_real = real.relative_to(root)
    except ValueError:
        return None
    if any(_nas_skip_name(part) for part in rel_real.parts):
        return None
    return real


def _nas_resolve(root: Path, rel: str) -> tuple[Path, str] | None:
    if not isinstance(rel, str):
        return None
    text = rel.strip()
    if "\\" in text or "\x00" in text or text.startswith("/"):
        return None
    text = text.strip("/")
    parts = [] if text == "" else text.split("/")
    for part in parts:
        if _nas_skip_name(part):
            return None
    candidate = root.joinpath(*parts) if parts else root
    real = _nas_placed(root, candidate)
    if real is None:
        return None
    return real, text


def _nas_offset(offset) -> int:
    try:
        value = int(offset)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _nas_collect(root: Path, directory: Path) -> tuple[list[dict], bool] | None:
    try:
        children = list(directory.iterdir())
    except OSError:
        return None
    allowed = accepted_suffixes()
    rows = []
    for child in children:
        name = child.name
        if _nas_skip_name(name):
            continue
        real = _nas_placed(root, child)
        if real is None:
            continue
        if real.is_dir():
            rows.append((0, name.casefold(), {"name": name, "kind": "dir"}))
        elif real.is_file():
            suffix = Path(name).suffix.lower().lstrip(".")
            rows.append(
                (
                    1,
                    name.casefold(),
                    {"name": name, "kind": "file", "accepted": suffix in allowed, "_path": real},
                )
            )
    rows.sort(key=lambda item: (item[0], item[1]))
    truncated = len(rows) > NAS_CAP
    return [item[2] for item in rows[:NAS_CAP]], truncated


def list_nas(rel: str = "", offset: int = 0) -> dict:
    root = nas_root()
    if root is None:
        return {"ok": False, "error": "极空间目录还没挂进容器"}
    found = _nas_resolve(root, "" if rel is None else str(rel))
    if found is None or not found[0].is_dir():
        return {"ok": False, "error": "不能从这里导入"}
    collected = _nas_collect(root, found[0])
    if collected is None:
        return {"ok": False, "error": "不能从这里导入"}
    rows, truncated = collected
    start = _nas_offset(offset)
    page = []
    for item in rows[start : start + NAS_PAGE]:
        page.append({key: value for key, value in item.items() if key != "_path"})
    return {
        "ok": True,
        "path": found[1],
        "entries": page,
        "offset": start,
        "limit": NAS_PAGE,
        "more": start + NAS_PAGE < len(rows),
        "truncated": truncated,
    }


def _nas_read(path: Path) -> bytes:
    return path.read_bytes()


def _nas_import_file(path: Path, filename: str, device_name: str | None) -> dict:
    try:
        if path.stat().st_size > DOCUMENT_MAX_BYTES:
            return {"ok": False, "error": "文件太大", "filename": filename}
        data = _nas_read(path)
    except OSError:
        return {"ok": False, "error": "没有这个文件", "filename": filename}
    return import_bytes(filename, data, device_name)


def _nas_outcome(imported: list[dict]) -> dict:
    failed = [item for item in imported if not item.get("ok")]
    return {"ok": not failed or any(item.get("ok") for item in imported), "imported": imported}


def import_nas(paths: list, device_name: str | None = None) -> dict:
    root = nas_root()
    if root is None:
        return {"ok": False, "error": "极空间目录还没挂进容器"}
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "没有文件"}
    if len(paths) > NAS_IMPORT_MAX:
        return {"ok": False, "error": "一次最多导入 50 个文件"}
    imported = []
    for rel in paths:
        if not isinstance(rel, str):
            imported.append({"ok": False, "error": "不能从这里导入", "filename": ""})
            continue
        found = _nas_resolve(root, rel)
        if found is None or not found[0].is_file():
            imported.append({"ok": False, "error": "不能从这里导入", "filename": Path(str(rel)).name})
            continue
        imported.append(_nas_import_file(found[0], Path(rel).name, device_name))
    return _nas_outcome(imported)


def import_nas_dir(rel: str, device_name: str | None = None) -> dict:
    root = nas_root()
    if root is None:
        return {"ok": False, "error": "极空间目录还没挂进容器"}
    found = _nas_resolve(root, "" if rel is None else str(rel))
    if found is None or not found[0].is_dir():
        return {"ok": False, "error": "不能从这里导入"}
    collected = _nas_collect(root, found[0])
    if collected is None:
        return {"ok": False, "error": "不能从这里导入"}
    rows, _truncated = collected
    files = [item for item in rows if item.get("kind") == "file" and item.get("accepted")]
    files.sort(key=lambda item: item["name"].casefold())
    if not files:
        return {"ok": False, "error": "这一层没有可导入的文件"}
    if len(files) > NAS_IMPORT_MAX:
        return {"ok": False, "error": f"这一层有 {len(files)} 个可导入文件，一次最多 50 个"}
    return _nas_outcome(
        [_nas_import_file(item["_path"], item["name"], device_name) for item in files]
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_saved(
    filename: str,
    source: Path,
    device_name: str | None = None,
    *,
    origin: str = ORIGIN_BATCH,
) -> dict:
    """Copy one document into the scan directory and read it. The source file stays."""
    safe = Path(filename).name
    suffix = Path(safe).suffix.lower().lstrip(".")
    if not safe or suffix not in accepted_suffixes():
        return {"ok": False, "error": "不接受的文件类型", "filename": safe}
    try:
        size = source.stat().st_size
    except OSError:
        return {"ok": False, "error": "没有这个文件", "filename": safe}
    if size > DOCUMENT_MAX_BYTES:
        return {"ok": False, "error": "文件太大", "filename": safe}
    root = active_scan()
    if root is None:
        return {"ok": False, "error": "没有扫描目录", "filename": safe}
    root.mkdir(parents=True, exist_ok=True)
    digest = _sha256_file(source)
    dest = root / safe
    if dest.exists() and _sha256_file(dest) != digest:
        dest = root / f"{Path(safe).stem}-{digest[:8]}{Path(safe).suffix}"
    if source.resolve() != dest.resolve():
        shutil.copyfile(source, dest)
    result = ingest_file(dest, device_name=device_name, origin=origin)
    if not result.get("duplicate"):
        result["filename"] = dest.name
    return result


def import_bytes(filename: str, data: bytes, device_name: str | None = None) -> dict:
    safe = Path(filename).name
    suffix = Path(safe).suffix.lower().lstrip(".")
    if not safe or suffix not in accepted_suffixes():
        return {"ok": False, "error": "不接受的文件类型", "filename": safe}
    if len(data) > DOCUMENT_MAX_BYTES:
        return {"ok": False, "error": "文件太大", "filename": safe}
    root = active_scan()
    if root is None:
        return {"ok": False, "error": "没有扫描目录", "filename": safe}
    root.mkdir(parents=True, exist_ok=True)
    dest = root / safe
    digest = hashlib.sha256(data).hexdigest()
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
        dest = root / f"{dest.stem}-{digest[:8]}{dest.suffix}"
    dest.write_bytes(data)
    result = ingest_file(dest, device_name=device_name, origin=ORIGIN_BATCH)
    if not result.get("duplicate"):
        result["filename"] = dest.name
    if result.get("ok") and not result.get("error"):
        row = db().execute("SELECT body FROM sources WHERE id=?", (result["id"],)).fetchone()
        result["stored"] = bool(row and row["body"])
    return result


def ingest_file(path: Path, device_name: str | None = None, *, origin: str = ORIGIN_SCAN) -> dict:
    data = path.read_bytes()
    content_hash = hashlib.sha256(data).hexdigest()
    label = resolve_device_name(device_name)
    file_lane = lane_for(kind="file", origin=origin)
    dedup = hash_dedup_enabled()
    if dedup:
        with LOCK:
            old = db().execute(
                "SELECT id, filename FROM sources WHERE content_hash=? AND kind='file'",
                (content_hash,),
            ).fetchone()
            if old:
                return {"ok": True, "id": old["id"], "duplicate": True, "filename": old["filename"] or path.name}
    try:
        from rogmem.sections import join_sections, read_sections

        sections = read_sections(path)
        text = join_sections(sections)
        error = ""
    except Exception as exc:
        sections = []
        text = ""
        error = str(exc)[:200]
    try:
        written_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        written_at = ""
    planned = [] if error else _plan_document(path, sections)
    with LOCK:
        con = db()
        if dedup:
            old = con.execute(
                "SELECT id, filename FROM sources WHERE content_hash=? AND kind='file'",
                (content_hash,),
            ).fetchone()
            if old:
                return {"ok": True, "id": old["id"], "duplicate": True, "filename": old["filename"] or path.name}
        source_id = secrets.token_hex(8)
        now = utcnow()
        idem = "file:" + content_hash if dedup else f"file:{content_hash}:{source_id}"
        if error:
            con.execute(
                """INSERT INTO sources(id,kind,device_name,agent_name,event_at,event_basis,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename,last_error)
                   VALUES(?,'file',?,'极空间',?,'file',?,?,?,?,'解析失败',?,1,?,?)""",
                (source_id, label, written_at or None, now, idem, content_hash, "", file_lane, path.name, error),
            )
            _set_pipeline(con, source_id, "解析", "fail")
            record_run(
                con,
                "蒸馏",
                "失败",
                path.name,
                error,
                step="解析",
                ref_kind="source",
                ref_id=source_id,
            )
            con.commit()
            return {"ok": False, "id": source_id, "error": "解析失败"}
        con.execute(
            """INSERT INTO sources(id,kind,device_name,agent_name,event_at,event_basis,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,filename)
               VALUES(?,'file',?,'极空间',?,'file',?,?,?,?,'已解析待向量化',?,1,?)""",
            (source_id, label, written_at or None, now, idem, content_hash, text, file_lane, path.name),
        )
        _write_planned(con, source_id, planned)
        _close_stale_file_quotes(con, path.name, source_id, text)
        if _too_thin(text):
            con.execute(
                "UPDATE sources SET status=?, last_error='去掉标记后没有文字，不调用模型', pipeline=? WHERE id=?",
                (ST_NO_BODY, "解析:fail", source_id),
            )
            record_run(
                con,
                "蒸馏",
                "跳过",
                path.name,
                "去掉标记后没有文字，不调用模型",
                step="解析",
                ref_kind="source",
                ref_id=source_id,
            )
        else:
            _finish_vector_gate(con, source_id, file_lane)
        con.commit()
    return {"ok": True, "id": source_id, "duplicate": False}


def _too_thin(text: str) -> bool:
    """True when markers and punctuation leave no words. A short sentence still distills.

    A picture embed is not those words. Its filename must not keep an otherwise
    empty body on the distill path.
    """
    from rogmem.weknora_read import picture_links_removed

    stripped = picture_links_removed(text or "")
    stripped = re.sub(r"[#>*_`~\-=|\[\]()!]+", " ", stripped)
    words = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(words) == 0


def _queue_source(con: sqlite3.Connection, source_id: str, lane: str) -> None:
    existing = con.execute("SELECT id FROM queue WHERE source_id=? AND state!='done'", (source_id,)).fetchone()
    if existing:
        return
    con.execute(
        "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES(?,?, 'queued', ?)",
        (source_id, lane, utcnow()),
    )


def _fill_missing_vectors(con: sqlite3.Connection, source_id: str) -> None:
    if _EMBED == "fastembed" and not _embedder_ready():
        return
    rows = con.execute(
        "SELECT id, text, vec_json FROM chunks WHERE source_id=? AND role!='parent'",
        (source_id,),
    ).fetchall()
    for row in rows:
        if row["vec_json"] or not (row["text"] or "").strip():
            continue
        vec = _embed_one(row["text"])
        if vec:
            con.execute("UPDATE chunks SET vec_json=? WHERE id=?", (json.dumps(vec), row["id"]))


def _vector_outcome(con: sqlite3.Connection, source_id: str) -> str:
    """ready, wait for the local model, or fail after a real attempt."""
    if _EMBED in {"", "off"} or _vectors_ready(con, source_id):
        return "ready"
    if _EMBED == "fastembed" and not _embedder_ready():
        return "wait"
    return "fail"


def _finish_vector_gate(con: sqlite3.Connection, source_id: str, lane: str) -> str:
    """Queue distill as soon as the text is real. Vector work lights its own lamp."""
    outcome = _vector_outcome(con, source_id)
    row = con.execute("SELECT status FROM sources WHERE id=?", (source_id,)).fetchone()
    current = row["status"] if row else ""
    busy = {ST_DISTILLING, ST_CRYSTAL, ST_NO_CRYSTAL, ST_DISTILL_FAIL, ST_VEC_READY}
    queued = con.execute("SELECT 1 FROM queue WHERE source_id=? LIMIT 1", (source_id,)).fetchone()
    if queued is None:
        _queue_source(con, source_id, lane)
    if current not in busy:
        _set_pipeline(con, source_id, "解析", "ok")
    if outcome == "ready":
        _set_pipeline(con, source_id, "向量", "ok")
        if current not in busy:
            con.execute(
                "UPDATE sources SET status=?, last_error=NULL WHERE id=?",
                (ST_VEC_READY, source_id),
            )
        else:
            con.execute(
                "UPDATE sources SET last_error=NULL WHERE id=? AND last_error LIKE '向量%'",
                (source_id,),
            )
        return ST_VEC_READY
    if outcome == "wait":
        _set_pipeline(con, source_id, "向量", "run")
        if current not in busy:
            con.execute(
                "UPDATE sources SET status=?, last_error=? WHERE id=?",
                (ST_PARSED, "向量还在补，蒸馏先走", source_id),
            )
        return ST_PARSED
    _set_pipeline(con, source_id, "向量", "fail")
    if current not in busy:
        con.execute(
            "UPDATE sources SET status=?, last_error=? WHERE id=?",
            (ST_VEC_FAIL, "向量没有写上", source_id),
        )
    return ST_VEC_FAIL


def _release_parsed_without_model() -> int:
    """The embed model is still loading. Distill does not wait for it."""
    if not _VECTOR_CATCHUP.acquire(blocking=False):
        return 0
    try:
        with LOCK:
            con = db()
            rows = con.execute(
                """SELECT id, lane FROM sources
                   WHERE status=? AND id NOT IN (SELECT source_id FROM queue WHERE state!='done')
                   ORDER BY ingested_at LIMIT ?""",
                (ST_PARSED, _VECTOR_BATCH),
            ).fetchall()
            for row in rows:
                _queue_source(con, row["id"], row["lane"] or "slow")
                _set_pipeline(con, row["id"], "解析", "ok")
                _set_pipeline(con, row["id"], "向量", "run")
            if rows:
                con.commit()
            return len(rows)
    finally:
        _VECTOR_CATCHUP.release()


def ensure_pending_vectors() -> int:
    """Fill a small batch of missing vectors and release them to distill.

    One catch-up runs at a time. Other distill workers keep leasing instead of
    embedding the same backlog. The batch is committed before the next one,
    so the list moves instead of waiting for every waiting row.
    """
    if _EMBED == "fastembed" and not _embedder_ready():
        return _release_parsed_without_model()
    if not _VECTOR_CATCHUP.acquire(blocking=False):
        return 0
    try:
        with LOCK:
            con = db()
            con.commit()
            rows = con.execute(
                """SELECT id, lane, body FROM sources
                   WHERE status NOT IN (?, ?)
                     AND IFNULL(wait_until, '') NOT LIKE 'hold:%'
                     AND (
                       status=?
                       OR EXISTS (
                         SELECT 1 FROM chunks
                         WHERE chunks.source_id=sources.id AND role!='parent'
                           AND vec_json IS NULL AND TRIM(IFNULL(text, ''))!=''
                       )
                     )
                   ORDER BY ingested_at LIMIT ?""",
                (ST_NO_BODY, ST_PARSE_FAIL, ST_PARSED, _VECTOR_BATCH),
            ).fetchall()
            waiting = []
            for row in rows:
                if _too_thin(row["body"] or ""):
                    waiting.append({"id": row["id"], "thin": True})
                    continue
                chunks = con.execute(
                    """SELECT id, text FROM chunks
                       WHERE source_id=? AND role!='parent' AND vec_json IS NULL""",
                    (row["id"],),
                ).fetchall()
                if not chunks:
                    waiting.append({"id": row["id"], "lane": row["lane"] or "slow", "body": row["body"] or ""})
                else:
                    missing = [
                        (chunk["id"], chunk["text"])
                        for chunk in chunks
                        if (chunk["text"] or "").strip()
                    ]
                    waiting.append({"id": row["id"], "lane": row["lane"] or "slow", "missing": missing})
            con.commit()
        texts: list[str] = []
        owners: list[tuple[dict, int]] = []
        for item in waiting:
            if item.get("thin") or "body" in item:
                continue
            start = len(texts)
            for _cid, text in item.get("missing") or []:
                texts.append(text)
            owners.append((item, start))
        vectors = _embed_many(texts) if texts else []
        for item, start in owners:
            missing = item.get("missing") or []
            item["vectors"] = [
                (cid, vectors[start + index] if start + index < len(vectors) else None)
                for index, (cid, _text) in enumerate(missing)
            ]
        for item in waiting:
            if item.get("thin") or "missing" in item:
                continue
            item["planned"] = _planned_chunks(item["body"], "", 0)[0]
        if not waiting:
            return 0
        changed = 0
        with LOCK:
            con = db()
            early = {"", ST_PARSED, ST_VEC_FAIL, ST_VEC_READY}
            for item in waiting:
                if item.get("thin"):
                    current = con.execute("SELECT status FROM sources WHERE id=?", (item["id"],)).fetchone()
                    if current and current["status"] in early:
                        con.execute(
                            "UPDATE sources SET status=?, last_error=? WHERE id=?",
                            (ST_NO_BODY, "去掉标记后没有文字，不调用模型", item["id"]),
                        )
                        label = con.execute(
                            "SELECT id, filename, session_id FROM sources WHERE id=?",
                            (item["id"],),
                        ).fetchone()
                        record_run(
                            con,
                            "蒸馏",
                            "跳过",
                            _source_subject(label),
                            "去掉标记后没有文字，不调用模型",
                            step="解析",
                            ref_kind="source",
                            ref_id=item["id"],
                        )
                        changed += 1
                    continue
                if "planned" in item:
                    if not con.execute("SELECT 1 FROM chunks WHERE source_id=? LIMIT 1", (item["id"],)).fetchone():
                        _write_planned(con, item["id"], item["planned"])
                wrote = False
                for cid, vec in item.get("vectors") or []:
                    if vec:
                        con.execute("UPDATE chunks SET vec_json=? WHERE id=?", (json.dumps(vec), cid))
                        wrote = True
                before = con.execute("SELECT status FROM sources WHERE id=?", (item["id"],)).fetchone()["status"]
                _finish_vector_gate(con, item["id"], item.get("lane") or "slow")
                after = con.execute("SELECT status FROM sources WHERE id=?", (item["id"],)).fetchone()["status"]
                if wrote or after != before:
                    changed += 1
            con.commit()
        return changed
    finally:
        _VECTOR_CATCHUP.release()


def select_embed_model(model_id: str) -> dict:
    """Switch among the preset local embed models and rebuild vectors. Does not re-distill."""
    allowed = {item["id"]: item for item in EMBED_PRESETS}
    if model_id not in allowed:
        return {"ok": False, "error": "只能切换已经预设的向量模型"}
    os.environ["ROGMEM_EMBED_MODEL"] = model_id
    try:
        set_embed_mode("fastembed")
    except Exception:
        return {"ok": False, "error": "这个向量模型没装上"}
    return {"ok": True, "model": model_id, "label": allowed[model_id]["label"]}


def embed_public() -> dict:
    current = ""
    try:
        current = embed_model_id()
    except Exception:
        current = EMBED_PRESETS[0]["id"]
    return {"current": current, "presets": [dict(item) for item in EMBED_PRESETS]}


def _retire_stage_token(name: str) -> str:
    """The lamp now called 并页 was stored as 整合. Only the migration reads the old token."""
    return "并页" if name == "整合" else name


def _rewrite_pipeline_stages(text: str) -> str:
    """Rename stage tokens. The vector side-lamp and the separators stay put."""
    if not text or "整合" not in text:
        return text

    def piece(part: str) -> str:
        name, sep, state = part.partition(":")
        name = _retire_stage_token(name)
        return f"{name}:{state}" if sep else name

    def join(section: str) -> str:
        return ",".join(piece(part) for part in section.split(",") if part)

    main, tilde, side = text.rpartition("~")
    if not (tilde and side.startswith("向量:")):
        main, side = text, ""
    memory, bar, display = main.partition("|")
    if not bar:
        display, memory = main, ""
    body = join(display)
    if memory:
        body = f"{join(memory)}|{body}"
    if side:
        body += "~" + side
    return body


def migrate_source_statuses(con: sqlite3.Connection) -> None:
    """Rename statuses from the old list onto the parse / vector / distill list."""
    con.execute("UPDATE sources SET status=? WHERE status='正文不足'", (ST_NO_BODY,))
    con.execute("UPDATE sources SET status=? WHERE status='向量未就绪'", (ST_VEC_FAIL,))
    con.execute("UPDATE sources SET status=? WHERE status='覆盖完成'", (ST_CRYSTAL,))
    con.execute("UPDATE sources SET status=? WHERE status='队列中'", (ST_VEC_READY,))
    con.execute(
        f"""UPDATE sources SET status=? WHERE status='已蒸馏'
            AND id IN (SELECT source_id FROM statements WHERE source_id IS NOT NULL)""",
        (ST_CRYSTAL,),
    )
    con.execute(
        f"""UPDATE sources SET status=? WHERE status='已蒸馏'
            AND id NOT IN (SELECT source_id FROM statements WHERE source_id IS NOT NULL)""",
        (ST_NO_CRYSTAL,),
    )
    con.execute(
        f"""UPDATE sources SET status=? WHERE kind='file' AND status='已解析'
            AND id NOT IN (SELECT source_id FROM queue WHERE state!='done')
            AND id NOT IN (SELECT source_id FROM statements WHERE source_id IS NOT NULL)""",
        (ST_NO_CRYSTAL,),
    )
    for row in con.execute("SELECT id, pipeline FROM sources WHERE IFNULL(pipeline, '') LIKE '%整合%'"):
        rewritten = _rewrite_pipeline_stages(row["pipeline"] or "")
        if rewritten != (row["pipeline"] or ""):
            con.execute("UPDATE sources SET pipeline=? WHERE id=?", (rewritten, row["id"]))
    _settle_run_log(con)


def _vectors_ready(con: sqlite3.Connection, source_id: str) -> bool:
    if _EMBED in {"", "off"}:
        return True
    rows = con.execute(
        "SELECT text, vec_json FROM chunks WHERE source_id=? AND role='child'",
        (source_id,),
    ).fetchall()
    usable = [row for row in rows if (row["text"] or "").strip()]
    if not usable:
        return False
    return all(row["vec_json"] for row in usable)


def _close_stale_file_quotes(con: sqlite3.Connection, filename: str, new_id: str, new_body: str) -> int:
    if not filename:
        return 0
    closed = 0
    now = utcnow()
    olds = con.execute(
        "SELECT id FROM sources WHERE kind='file' AND filename=? AND id!=?",
        (filename, new_id),
    ).fetchall()
    for old in olds:
        rows = con.execute(
            "SELECT id, quote FROM statements WHERE source_id=? AND invalid_at IS NULL",
            (old["id"],),
        ).fetchall()
        for row in rows:
            quote = row["quote"] or ""
            if quote and quote not in distill_scope(filename, new_body or ""):
                con.execute("UPDATE statements SET invalid_at=? WHERE id=?", (now, row["id"]))
                closed += 1
    return closed


def scan_dir(force: bool = True) -> int:
    global _LAST_SCAN
    import time

    root = active_scan()
    if root is None or not root.exists():
        return 0
    interval = int(get_setting("scan_interval") or "0")
    if not force and interval and time.time() - _LAST_SCAN < interval:
        return 0
    allowed = accepted_suffixes()
    n = 0
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower().lstrip(".") not in allowed:
            continue
        ingest_file(path)
        n += 1
    _LAST_SCAN = time.time()
    return n


def _plan_document(path: Path, sections: list[tuple[str, str]]) -> list[dict]:
    """Chunk and embed before any database lock. The insert is a separate, short write."""
    structured = any(heading for heading, _body in sections)
    if not structured:
        text = "\n".join(body for _heading, body in sections if body)
        header = f"文件：{path.name}" if path.name else ""
        return _planned_chunks(text, header, 0)[0]
    planned: list[dict] = []
    seq = 0
    for heading, body in sections:
        if not body:
            continue
        header = "\n".join(bit for bit in (f"文件：{path.name}", f"标题：{heading}" if heading else "") if bit)
        chunk, seq = _planned_chunks(body, header, seq)
        planned.extend(chunk)
    return planned


def _index_document(con: sqlite3.Connection, source_id: str, path: Path, sections: list[tuple[str, str]]) -> None:
    _write_planned(con, source_id, _plan_document(path, sections))


def _index_text(con: sqlite3.Connection, source_id: str, text: str, filename: str = "") -> None:
    header = f"文件：{filename}" if filename else ""
    _write_planned(con, source_id, _planned_chunks(text, header, 0)[0])


def _planned_chunks(text: str, header: str, seq: int) -> tuple[list[dict], int]:
    """Embed one passage. Callers must not be holding the write lock."""
    from rogmem.chunker import split_headed

    parents, children = split_headed(text, context_header=header)
    planned: list[dict] = []
    for index, parent in enumerate(parents):
        planned.append(
            {
                "kind": "parent",
                "index": index,
                "seq": seq,
                "loc": f"{parent.start}-{parent.end}",
                "text": parent.content,
            }
        )
        seq += 1
    child_rows = [(child, child.embedding_content()) for child in children]
    vectors = _embed_many([text for _child, text in child_rows]) if child_rows else []
    for index, (child, embed_text) in enumerate(child_rows):
        planned.append(
            {
                "kind": "child",
                "parent_index": child.parent_index,
                "seq": seq,
                "loc": str(seq),
                "text": child.content,
                "vec": vectors[index] if index < len(vectors) else None,
                "embed_text": embed_text if embed_text != child.content.strip() else None,
                "tokens": embed_text,
            }
        )
        seq += 1
    return planned, seq


def _write_planned(con: sqlite3.Connection, source_id: str, planned: list[dict]) -> None:
    parent_ids: dict[int, int] = {}
    for item in planned:
        if item["kind"] == "parent":
            cur = con.execute(
                "INSERT INTO chunks(source_id,seq,loc,text,role) VALUES(?,?,?,?, 'parent')",
                (source_id, item["seq"], item["loc"], item["text"]),
            )
            parent_ids[item["index"]] = int(cur.lastrowid)
            continue
        parent_id = parent_ids.get(item["parent_index"]) if item.get("parent_index") is not None else None
        vec = item.get("vec")
        cur = con.execute(
            "INSERT INTO chunks(source_id,seq,loc,text,vec_json,role,parent_id,embed_text) VALUES(?,?,?,?,?,'child',?,?)",
            (
                source_id,
                item["seq"],
                item["loc"],
                item["text"],
                json.dumps(vec) if vec else None,
                parent_id,
                item.get("embed_text"),
            ),
        )
        cid = int(cur.lastrowid)
        con.execute(
            "INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)",
            (str(cid), _tokens(item.get("tokens") or item["text"])),
        )
        if vec:
            _VECS.append((cid, vec))


def _hold_leases(qids: list[int]) -> None:
    with _LIVE_LOCK:
        _LIVE_LEASES.update(int(qid) for qid in qids)


def _drop_leases(qids: list[int]) -> None:
    with _LIVE_LOCK:
        _LIVE_LEASES.difference_update(int(qid) for qid in qids)


def _live_lease_ids() -> list[int]:
    with _LIVE_LOCK:
        return list(_LIVE_LEASES)


def _member_qids(primary: int) -> list[int]:
    members = _EXCHANGE.get() or ()
    qids = [int(item["qid"]) for item in members if item.get("qid")]
    if primary and int(primary) not in qids:
        qids.insert(0, int(primary))
    return qids or ([int(primary)] if primary else [])


def _member_source_ids(primary: str) -> list[str]:
    members = _EXCHANGE.get() or ()
    source_ids = [str(item["source_id"]) for item in members if item.get("source_id")]
    if primary and primary not in source_ids:
        source_ids.insert(0, primary)
    return source_ids or ([primary] if primary else [])


def _side_from_queue(row) -> Side:
    return Side(
        source_id=row["source_id"],
        session_id=row["session_id"] or "",
        turn_id=row["turn_id"] or "",
        kind=row["kind"] or "",
        event_at=row["event_at"] or "",
        ingested_at=row["ingested_at"] or "",
        rowid=int(row["rowid"] or 0),
        pair_ready=int(row["pair_ready"] or 0),
        wait_until=row["wait_until"] or "",
        qid=int(row["qid"] or 0),
        lane=row["lane"] or "",
        attempts=int(row["attempts"] or 0),
        chars=int(row["chars"] or 0) if "chars" in row.keys() else 0,
    )


def _exchange_candidate(exchange: Exchange) -> QueueCandidate:
    def rank(side: Side) -> tuple:
        return (normalize_lane(side.lane, side.kind), side.ingested_at or "", side.rowid)

    lane_side = min(exchange.sides, key=rank)
    users = [side for side in exchange.sides if side.kind == "user"]
    primary = users[0] if users else exchange.sides[0]
    earliest = min(exchange.sides, key=lambda side: (side.ingested_at or "9999", side.rowid))
    return QueueCandidate(
        qid=min(side.qid for side in exchange.sides),
        source_id=primary.source_id,
        lane=lane_side.lane,
        kind=lane_side.kind,
        ingested_at=earliest.ingested_at,
        attempts=max(side.attempts for side in exchange.sides),
    )


def _queued_work(con, now: str) -> list:
    return con.execute(
        """SELECT q.id AS qid, q.source_id, q.lane, q.attempts,
                  s.kind, s.ingested_at, IFNULL(s.session_id, '') AS session_id,
                  IFNULL(s.turn_id, '') AS turn_id, IFNULL(s.event_at, '') AS event_at,
                  s.pair_ready, IFNULL(s.wait_until, '') AS wait_until, s.rowid AS rowid,
                  length(IFNULL(s.body, '')) AS chars
           FROM queue q JOIN sources s ON s.id=q.source_id
           WHERE q.state='queued' AND (q.next_at IS NULL OR q.next_at<=?)""",
        (now,),
    ).fetchall()


def _schedule(con, now: str) -> tuple[list[QueueCandidate], dict[str, Exchange | None]]:
    """One candidate per distill unit. A session run is one candidate, not one per row."""
    clock = datetime.fromisoformat(now)
    conversation: list[Side] = []
    other = []
    for row in _queued_work(con, now):
        if row["kind"] in {"user", "agent"}:
            conversation.append(_side_from_queue(row))
        else:
            other.append(row)
    candidates: list[QueueCandidate] = []
    units: dict[str, Exchange | None] = {}
    for exchange in form_exchanges(conversation):
        if not exchange_ready(exchange, clock):
            continue
        candidate = _exchange_candidate(exchange)
        candidates.append(candidate)
        units[candidate.source_id] = exchange
    for row in other:
        candidate = QueueCandidate(
            qid=int(row["qid"]),
            source_id=row["source_id"],
            lane=row["lane"],
            kind=row["kind"],
            ingested_at=row["ingested_at"] or "",
            attempts=int(row["attempts"] or 0),
        )
        candidates.append(candidate)
        units[candidate.source_id] = None
    return candidates, units


def _settle_sessions(con, session_ids: set[str]) -> None:
    """Close runs that already have both roles. A one-role run keeps its deadline."""
    now = utcnow()
    wait = turn_wait_seconds()
    for session_id in session_ids:
        if not str(session_id or "").strip():
            continue
        rows = con.execute(
            """SELECT q.id AS qid, s.id AS source_id, q.lane, q.attempts, s.kind, s.ingested_at,
                      IFNULL(s.session_id, '') AS session_id, IFNULL(s.turn_id, '') AS turn_id,
                      IFNULL(s.event_at, '') AS event_at, s.pair_ready,
                      IFNULL(s.wait_until, '') AS wait_until, s.rowid AS rowid,
                      length(IFNULL(s.body, '')) AS chars
               FROM sources s JOIN queue q ON q.source_id=s.id
               WHERE s.session_id=? AND s.kind IN ('user','agent') AND q.state='queued'""",
            (session_id,),
        ).fetchall()
        if not rows:
            continue
        updates = flag_updates(
            form_exchanges([_side_from_queue(row) for row in rows]),
            now_iso=now,
            wait_seconds=wait,
        )
        for source_id, ready, wait_until, keep in updates:
            if keep:
                con.execute("UPDATE sources SET pair_ready=? WHERE id=?", (ready, source_id))
            else:
                con.execute(
                    "UPDATE sources SET pair_ready=?, wait_until=? WHERE id=?",
                    (ready, wait_until, source_id),
                )


def _reclaim_expired(con, now: str) -> None:
    """Give back a lease whose worker is gone. A worker still inside the call keeps it."""
    live = _live_lease_ids()
    skip = ""
    if live:
        skip = " AND id NOT IN (" + ",".join("?" * len(live)) + ")"
    con.execute(
        f"""UPDATE queue SET state='queued', leased_until=NULL
            WHERE state='leased' AND superseded=0 AND leased_until IS NOT NULL AND leased_until<=?{skip}""",
        (now, *live),
    )
    con.execute(
        """UPDATE queue SET state='queued'
           WHERE state='failed' AND (next_at IS NULL OR next_at<=?)""",
        (now,),
    )
    con.execute(
        f"""UPDATE queue SET state='queued', superseded=0, attempts=0, last_error=NULL,
            next_at=NULL, leased_until=NULL, cursor=0
            WHERE state='leased' AND superseded=1 AND leased_until IS NOT NULL AND leased_until<=?{skip}""",
        (now, *live),
    )


def _lease_members(exchange: Exchange | None, picked: QueueCandidate) -> list[tuple[int, str]]:
    if exchange is None:
        return [(picked.qid, picked.source_id)]
    ordered = [(side.qid, side.source_id) for side in exchange.sides]
    ordered.sort(key=lambda item: 0 if item[1] == picked.source_id else 1)
    return ordered


def lease_next() -> dict | None:
    """Lease one distill unit. A session run leases every row in that run."""
    ensure_pending_vectors()
    now = utcnow()
    with LOCK:
        con = db()
        if con.execute(
            """SELECT 1 FROM alerts WHERE open=1 AND (
                 message LIKE '外部模型鉴权失败%' OR message=? OR message=?
               )""",
            (MISSING_DISTILL_MODEL, MISSING_PROVIDER_KEY),
        ).fetchone():
            return None
        _reclaim_expired(con, now)
        candidates, units = _schedule(con, now)
        picked, streak = choose_next(
            candidates,
            now=datetime.fromisoformat(now),
            high_streak=read_high_streak(con),
        )
        if not picked:
            con.commit()
            return None
        members = _lease_members(units.get(picked.source_id), picked)
        until = _after(600)
        for qid, _source_id in members:
            taken = con.execute(
                """UPDATE queue SET state='leased', leased_until=?,
                   started_at=COALESCE(NULLIF(started_at, ''), ?)
                   WHERE id=? AND state='queued'""",
                (until, now, qid),
            )
            if taken.rowcount != 1:
                con.rollback()
                return None
        write_high_streak(con, streak)
        for _qid, source_id in members:
            previous = con.execute("SELECT pipeline FROM sources WHERE id=?", (source_id,)).fetchone()
            con.execute(
                "UPDATE sources SET status=?, pipeline=? WHERE id=?",
                (ST_DISTILLING, _begin_attempt((previous["pipeline"] if previous else "") or ""), source_id),
            )
        con.commit()
        source = con.execute("SELECT * FROM sources WHERE id=?", (members[0][1],)).fetchone()
    qids = [qid for qid, _source_id in members]
    source_ids = [source_id for _qid, source_id in members]
    return {
        "qid": qids[0],
        "qids": qids,
        "source_id": source_ids[0],
        "source_ids": source_ids,
        "lane": picked.lane,
        "source": source,
        "members": [{"qid": qid, "source_id": source_id} for qid, source_id in members],
    }


def pump_one() -> str | None:
    leased = lease_next()
    if not leased:
        return None
    qid = leased["qid"]
    source_id = leased["source_id"]
    source = leased["source"]
    qids = [int(item) for item in (leased.get("qids") or [qid])]
    members = tuple(leased.get("members") or [{"qid": qid, "source_id": source_id}])
    _hold_leases(qids)
    token = _ATTEMPT.set(qid)
    exchange = _EXCHANGE.set(members)
    try:
        return _pump_leased(qid, source_id, source)
    finally:
        _ATTEMPT.reset(token)
        _EXCHANGE.reset(exchange)
        _drop_leases(qids)


def _mirror_exchange_outcome(con, primary: str) -> None:
    """The rest of the run finished with this call. They do not start another one."""
    others = [source_id for source_id in _member_source_ids(primary) if source_id != primary]
    if not others:
        return
    lead = con.execute(
        "SELECT status, coverage, last_error, pipeline FROM sources WHERE id=?",
        (primary,),
    ).fetchone()
    if lead is None:
        return
    for source_id in others:
        con.execute(
            "UPDATE sources SET status=?, coverage=?, last_error=?, pipeline=? WHERE id=?",
            (lead["status"], lead["coverage"], lead["last_error"], lead["pipeline"], source_id),
        )


def _pump_leased(qid: int, source_id: str, source) -> str | None:
    try:
        finished = _distill(source, qid)
        if not finished:
            return source_id
        with LOCK:
            if _attempt_replaced(db(), qid):
                db().commit()
                return source_id
            from rogmem.publish_gate import coverage_state_for_source, set_source_distill_state

            wrote = db().execute("SELECT 1 FROM statements WHERE source_id=? LIMIT 1", (source_id,)).fetchone()
            paged = db().execute("SELECT 1 FROM revisions WHERE source_id=? LIMIT 1", (source_id,)).fetchone()
            finished_at = utcnow()
            for member_qid in _member_qids(qid):
                db().execute(
                    "UPDATE queue SET state='done', last_error=NULL, finished_at=? WHERE id=?",
                    (finished_at, member_qid),
                )
            if wrote:
                state = coverage_state_for_source(db(), source_id, source["body"] or "")
                set_source_distill_state(db(), source_id, state)
                db().execute("UPDATE sources SET last_error=NULL WHERE id=?", (source_id,))
            elif paged:
                db().execute(
                    "UPDATE sources SET status=?, last_error=NULL, coverage='pages' WHERE id=?",
                    (ST_CRYSTAL, source_id),
                )
            else:
                _mark_no_crystal(db(), source_id)
                db().execute("UPDATE sources SET last_error=NULL WHERE id=?", (source_id,))
                record_run(
                    db(),
                    "蒸馏",
                    "无结晶",
                    _source_subject(source),
                    "没有写出结晶",
                    step="草稿",
                    ref_kind="source",
                    ref_id=source_id,
                )
            _mirror_exchange_outcome(db(), source_id)
            db().commit()
        return source_id
    except AuthPause as exc:
        with LOCK:
            if _attempt_replaced(db(), qid):
                db().commit()
                return source_id
            for member_qid in _member_qids(qid):
                db().execute(
                    "UPDATE queue SET state='queued', last_error=?, next_at=? WHERE id=?",
                    (str(exc), "9999-01-01T00:00:00+00:00", member_qid),
                )
            for member_id in _member_source_ids(source_id):
                _fail_pipeline(db(), member_id)
                db().execute(
                    "UPDATE sources SET status=?, last_error=? WHERE id=?",
                    (ST_DISTILL_FAIL, str(exc), member_id),
                )
            opened = False
            if not db().execute("SELECT 1 FROM alerts WHERE open=1 AND message=?", (str(exc),)).fetchone():
                db().execute("INSERT INTO alerts(created_at,message) VALUES(?,?)", (utcnow(), str(exc)))
                opened = True
            db().commit()
        if opened:
            notify_alert(str(exc))
        return source_id
    except Exception as exc:  # noqa: BLE001
        with LOCK:
            if _attempt_replaced(db(), qid):
                db().commit()
                return source_id
            q = db().execute("SELECT attempts FROM queue WHERE id=?", (qid,)).fetchone()
            attempts = (q["attempts"] if q else 0) + 1
            # A call that never came back is not a verdict. It stays on the line
            # and yields, instead of being retired until the next morning.
            incomplete = isinstance(exc, ModelCallFailed)
            state = "queued" if incomplete or attempts < 5 else "failed"
            wait = _after(min(300, 15 * (2 ** (attempts - 1))))
            for member_qid in _member_qids(qid):
                db().execute(
                    "UPDATE queue SET state=?, attempts=?, last_error=?, next_at=? WHERE id=?",
                    (state, attempts, str(exc)[:200], wait, member_qid),
                )
            status = ST_DISTILL_FAIL if state == "failed" else ST_DISTILLING
            for member_id in _member_source_ids(source_id):
                _fail_pipeline(db(), member_id)
                db().execute(
                    "UPDATE sources SET status=?, last_error=?, attempt=? WHERE id=?",
                    (status, str(exc)[:200], attempts, member_id),
                )
            stage = _failed_stage_name(db(), source_id)
            record_run(
                db(),
                "蒸馏",
                "失败" if state == "failed" else "将重试",
                _source_subject(source),
                str(exc)[:200],
                step=stage,
                ref_kind="source",
                ref_id=source_id,
            )
            db().commit()
        return source_id


class AuthPause(RuntimeError):
    pass


class ModelCallFailed(RuntimeError):
    """One model call did not return. The stage stays failed; later stages are separate calls."""


class ProviderRefused(ModelCallFailed):
    """The provider did not take the call. The material was not judged."""


def _call_not_taken(exc: BaseException) -> bool:
    return isinstance(exc, ProviderRefused)


MISSING_DISTILL_MODEL = "没有配置蒸馏模型"
MISSING_PROVIDER_KEY = "供应商密钥还没填"
DEFAULT_MINIMAX_ID = "minimax-cn"
DEFAULT_MINIMAX_BASE = "https://api.minimax.cn/v1"
DEFAULT_MINIMAX_MODEL = "MiniMax-M3.1-Flash"
LOCAL_EMBED_ID = "local-embed"
LOCAL_EMBED_NAME = "中枢服务器本地"
LOCAL_EMBED_PLACE = "中枢服务器本机"
SILICONFLOW_ID = "siliconflow"
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
SILICONFLOW_EMBEDS = (
    "BAAI/bge-m3",
    "Qwen/Qwen3-Embedding-0.6B",
    "BAAI/bge-large-zh-v1.5",
    "BAAI/bge-large-en-v1.5",
    "Pro/BAAI/bge-m3",
)
SILICONFLOW_DEFAULT_EMBED = "BAAI/bge-m3"
MODEL_KIND_CHAT = "chat"
MODEL_KIND_EMBED = "embed"
_MODEL_KINDS = {MODEL_KIND_CHAT, MODEL_KIND_EMBED}
_ASSIGN_KEYS = ("model_distill", "model_merge", "model_embed")
_CHAT_ASSIGN_KEYS = ("model_distill", "model_merge")
_LAND_MISREAD = "integrate/land failed; refuse silent accept"


def _peel_vector(current: str) -> tuple[str, str]:
    """向量 is a side lamp. It must not sit in the distill sequence."""
    text = current or ""
    base, sep, vector = text.rpartition("~")
    if sep and vector.startswith("向量:"):
        return base, vector
    return text, ""


def _with_vector(display: str, vector: str) -> str:
    if not vector:
        return display
    return display + "~" + vector


def _pipeline_value(stage: str, state: str) -> str:
    if stage == "完成":
        return "完成:ok"
    return f"{stage}:{state}"


def _split_memory(current: str) -> tuple[set[str], str]:
    """Stages that have failed stay remembered across retries. Lamps are after '|'."""
    text = retire_polish((current or "").strip())
    if "|" not in text:
        return set(), text
    memory, _, display = text.partition("|")
    failed = {name for name in memory.split(",") if name in PIPELINE_STAGES}
    return failed, display.strip()


def _join_memory(failed: set[str], display: str) -> str:
    names = [name for name in PIPELINE_STAGES if name in failed]
    if not names:
        return display
    return ",".join(names) + "|" + display


def _parse_pipeline(current: str) -> dict:
    """Stage outcomes. A single `草稿:run` means that stage is current and earlier ones passed."""
    text = _peel_vector(_split_memory(current)[1])[0]
    if not text or text.startswith("完成"):
        return {}
    parts = text.split(",")
    if len(parts) == 1:
        stage, sep, state = parts[0].partition(":")
        if not sep or stage not in PIPELINE_STAGES:
            return {}
        index = PIPELINE_STAGES.index(stage)
        marks = {name: "ok" for name in PIPELINE_STAGES[:index]}
        marks[stage] = state or "run"
        return marks
    marks = {}
    for part in parts:
        stage, sep, state = part.partition(":")
        if sep and stage in PIPELINE_STAGES and state in {"ok", "run", "fail", "retry"}:
            marks[stage] = state
    return marks


def _format_pipeline(marks: dict) -> str:
    return ",".join(f"{name}:{marks[name]}" for name in PIPELINE_STAGES if name in marks)


def _compose_pipeline(current: str, stage: str, state: str) -> str:
    """Stages run in order. A failed stage stops the line. Re-entering it breathes yellow."""
    current, vector = _peel_vector(current)
    failed, display = _split_memory(current)
    marks = _parse_pipeline(display)
    for name, value in marks.items():
        if value == "fail":
            failed.add(name)
    if state == "fail":
        failed.add(stage)
    if any(value == "fail" for value in marks.values()):
        return _with_vector(_join_memory(failed, _format_pipeline(marks)), vector)
    if stage == "完成":
        return _with_vector("完成:ok", vector)
    if state == "run" and stage in failed:
        state = "retry"
    index = PIPELINE_STAGES.index(stage)
    for at, name in enumerate(PIPELINE_STAGES):
        if at < index:
            marks[name] = "ok"
            failed.discard(name)
        elif at == index:
            marks[name] = state
            if state == "ok":
                failed.discard(name)
        else:
            marks.pop(name, None)
    return _with_vector(_join_memory(failed, _format_pipeline(marks)), vector)


def _begin_attempt(current: str) -> str:
    """Start again at 草稿. Stages that already failed come back yellow, not white."""
    _, vector = _peel_vector(current)
    failed, display = _split_memory(_peel_vector(current)[0])
    for name, state in _parse_pipeline(display).items():
        if state == "fail":
            failed.add(name)
    draft = "retry" if "草稿" in failed else "run"
    return _with_vector(_join_memory(failed, _compose_pipeline("", "草稿", draft)), vector)


def _mark_no_crystal(con, source_id: str) -> None:
    """Draft ended with nothing written. Move the distill lamps and keep the vector lamp."""
    _set_pipeline(con, source_id, "草稿", "ok")
    con.execute(
        "UPDATE sources SET status=?, coverage='none' WHERE id=?",
        (ST_NO_CRYSTAL, source_id),
    )


def _set_pipeline(con, source_id: str, stage: str, state: str) -> None:
    row = con.execute("SELECT pipeline FROM sources WHERE id=?", (source_id,)).fetchone()
    current = (row["pipeline"] if row else "") or ""
    if stage == "向量":
        body, _vector = _peel_vector(current)
        if state not in {"ok", "run", "fail", "retry", "wait"}:
            state = "run"
        if state == "run" and _vector == "向量:fail":
            state = "retry"
        stored = (body or "解析:ok") + f"~向量:{state}"
    else:
        stored = _compose_pipeline(current, stage, state)
    con.execute(
        "UPDATE sources SET pipeline=? WHERE id=?",
        (stored, source_id),
    )


def _touch_lease(source_id: str) -> None:
    """One model call can outlast the lease. Renew every row in this run before the call."""
    if not source_id or _DB is None:
        return
    source_ids = _member_source_ids(source_id)
    marks = ",".join("?" * len(source_ids))
    try:
        with LOCK:
            db().execute(
                f"UPDATE queue SET leased_until=? WHERE state='leased' AND source_id IN ({marks})",
                (_after(CHAT_SILENCE_CAP + 120), *source_ids),
            )
            db().commit()
    except sqlite3.Error:
        return


def _attempt_replaced(con, qid: int) -> bool:
    """True when this lease must not record an outcome.

    No queue id means a direct call, and it still records. A retry of a running
    attempt marks the row superseded and leaves it leased, so a second worker
    cannot pick the same source up. Coming back here turns that mark into a fresh queue.
    """
    if not qid:
        return False
    row = con.execute("SELECT state, superseded FROM queue WHERE id=?", (qid,)).fetchone()
    if row is None or row["state"] != "leased":
        return True
    if not row["superseded"]:
        return False
    con.execute(
        """UPDATE queue SET state='queued', superseded=0, attempts=0, last_error=NULL,
           next_at=NULL, leased_until=NULL, cursor=0 WHERE id=? AND state='leased'""",
        (qid,),
    )
    return True


def _attempt_current() -> bool:
    """Lamps and notes belong to the attempt that still holds the lease."""
    qid = _ATTEMPT.get()
    if not qid:
        return True
    row = db().execute("SELECT state, superseded FROM queue WHERE id=?", (qid,)).fetchone()
    if row is None:
        return True
    return row["state"] == "leased" and not row["superseded"]


def mark_pipeline(source_id: str, stage: str, state: str) -> None:
    """Remember stage outcomes, and keep the lease alive for this call."""
    with LOCK:
        con = db()
        if not _attempt_current():
            return
        _set_pipeline(con, source_id, stage, state)
        source_ids = _member_source_ids(source_id)
        marks = ",".join("?" * len(source_ids))
        con.execute(
            f"UPDATE queue SET leased_until=? WHERE state='leased' AND source_id IN ({marks})",
            (_after(CHAT_SILENCE_CAP + 120), *source_ids),
        )
        con.commit()


def _note_stage_error(source_id: str, exc: BaseException) -> None:
    with LOCK:
        if not _attempt_current():
            return
        db().execute("UPDATE sources SET last_error=? WHERE id=?", (str(exc)[:200], source_id))
        db().commit()


def _fail_pipeline(con, source_id: str) -> None:
    row = con.execute("SELECT pipeline FROM sources WHERE id=?", (source_id,)).fetchone()
    marks = _parse_pipeline((row["pipeline"] if row else "") or "")
    if any(marks.get(name) == "fail" for name in PIPELINE_STAGES):
        return
    running = [name for name in PIPELINE_STAGES if marks.get(name) == "run"]
    stage = running[-1] if running else "草稿"
    _set_pipeline(con, source_id, stage, "fail")


def _failed_stage_name(con, source_id: str) -> str:
    """The stage whose lamp is already failed. Empty when none is."""
    row = con.execute("SELECT pipeline FROM sources WHERE id=?", (source_id,)).fetchone()
    marks = _parse_pipeline((row["pipeline"] if row else "") or "")
    failed = [name for name in PIPELINE_STAGES if marks.get(name) == "fail"]
    return failed[-1] if failed else ""


def _after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _material_for_distill(source: sqlite3.Row) -> str:
    """The whole unit, as one read.

    Small passages exist so search can hit a sentence. They are not a unit a
    model can judge. A file keeps its filename in view. A session run is every
    side leased with this call, in time order. One side stays the original text.
    """
    members = _EXCHANGE.get() or ()
    source_ids = [str(item["source_id"]) for item in members if item.get("source_id")]
    if len(source_ids) > 1 and source["kind"] != "file":
        marks = ",".join("?" * len(source_ids))
        rows = db().execute(
            f"""SELECT id, kind, body, event_at, ingested_at, rowid AS rowid
                FROM sources WHERE id IN ({marks})""",
            source_ids,
        ).fetchall()
        return join_exchange_text([{key: row[key] for key in row.keys()} for row in rows])
    body = source["body"] or ""
    if source["kind"] != "file":
        return body
    filename = source["filename"] if "filename" in source.keys() else ""
    return distill_scope(filename, body)


def _exchange_source_ids(source) -> list[str]:
    members = _EXCHANGE.get() or ()
    ids = [str(item.get("source_id") or "") for item in members if item.get("source_id")]
    if source is None or isinstance(source, dict):
        return ids
    try:
        own = str(source["id"] or "")
    except Exception:
        own = ""
    if own and own not in ids:
        ids.insert(0, own)
    return ids


def _history_unit_clock(source) -> tuple[str, str]:
    """Latest speech in a finished history dialogue. One row keeps its own clock."""
    if source is None or isinstance(source, dict):
        return "", ""
    ids = _exchange_source_ids(source)
    if len(ids) < 2:
        return "", ""
    marks = ",".join("?" * len(ids))
    rows = db().execute(
        f"""SELECT IFNULL(event_at,'') AS event_at, IFNULL(event_basis,'') AS event_basis,
                   IFNULL(lane,'') AS lane, rowid AS rowid
            FROM sources WHERE id IN ({marks})""",
        ids,
    ).fetchall()
    if len(rows) < 2 or not all(is_history_lane(row["lane"]) for row in rows):
        return "", ""
    best = max(rows, key=lambda row: (_instant_key(row["event_at"]), int(row["rowid"] or 0)))
    return str(best["event_at"] or ""), str(best["event_basis"] or "")


def _cited_material(source) -> str:
    """A quote may come from any line in the dialogue, not only the lead line."""
    if source is None:
        return ""
    if isinstance(source, dict):
        return str(source.get("body") or "")
    ids = _exchange_source_ids(source)
    try:
        kind = str(source["kind"] or "")
        body = str(source["body"] or "")
    except Exception:
        return ""
    if len(ids) > 1 and kind != "file":
        return _material_for_distill(source)
    return body


def _load_stage(source) -> dict | None:
    raw = ""
    if source is not None and "stage_json" in source.keys():
        raw = source["stage_json"] or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        return None
    return data


def _stage_page(item: dict) -> dict:
    """The fields a later stage needs. A bound page keeps the crystal it already is."""
    page = {
        "name": item.get("name") or "",
        "type": item.get("type") or "",
        "body": item.get("body") or "",
        "gist": item.get("gist") or "",
    }
    bound = str(item.get("id") or "").strip()
    if bound and not item.get("separate"):
        page["id"] = bound
    if item.get("separate"):
        page["separate"] = True
    else:
        title = str(item.get("title") or "").strip()
        if title:
            page["title"] = title
    for key in ("conflicts", "merge_note"):
        if key in item:
            page[key] = (item.get(key) or []) if key == "conflicts" else (item.get(key) or "")
    if item.get("settled"):
        page["settled"] = True
    if item.get("landed"):
        page["landed"] = True
    crystal_id = str(item.get("crystal_id") or "").strip()
    if crystal_id:
        page["crystal_id"] = crystal_id
    return page


def _phase_for(pages: list) -> str:
    """draft until every page has been merged, merged until each of those is in the store."""
    usable = [page for page in pages if isinstance(page, dict)]
    if usable and all(page.get("landed") for page in usable):
        return "landed"
    if usable and all(page.get("settled") for page in usable):
        return "merged"
    return "draft"


def _ids_from_pages(pages: list) -> list[str]:
    found: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        crystal_id = str(page.get("crystal_id") or page.get("id") or "").strip()
        if crystal_id and crystal_id not in found:
            found.append(crystal_id)
    return found


def _pending_on_disk(source_id: str) -> list | None:
    """The sketch queue already recorded. None means it has not been opened."""
    row = db().execute("SELECT stage_json FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None or not row["stage_json"]:
        return None
    try:
        data = json.loads(row["stage_json"])
    except json.JSONDecodeError:
        return None
    pending = data.get("pending_sketches") if isinstance(data, dict) else None
    if not isinstance(pending, list):
        return None
    return [str(item) for item in pending if str(item).strip()]


def _checkpoint_body(pages: list, phase: str, *, source_hash: str = "", pending: list | None = None) -> str:
    body: dict = {
        "phase": phase,
        "pages": [_stage_page(item) for item in pages if isinstance(item, dict)],
    }
    if source_hash:
        body["source_hash"] = source_hash
    if phase == "landed":
        body["pending_sketches"] = list(pending) if pending is not None else _ids_from_pages(pages)
    return json.dumps(body, ensure_ascii=False)


def _source_hash_of(source_id: str) -> str:
    row = db().execute("SELECT content_hash FROM sources WHERE id=?", (source_id,)).fetchone()
    return (row["content_hash"] or "") if row is not None else ""


def _save_stage(source_id: str, pages: list, phase: str, *, pending: list | None = None) -> None:
    with LOCK:
        payload = _checkpoint_body(pages, phase, source_hash=_source_hash_of(source_id), pending=pending)
        db().execute("UPDATE sources SET stage_json=? WHERE id=?", (payload, source_id))
        db().commit()


def _clear_stage(source_id: str) -> None:
    with LOCK:
        db().execute("UPDATE sources SET stage_json=NULL WHERE id=?", (source_id,))
        db().commit()


def _source_landed(source_id: str) -> bool:
    return (
        db().execute("SELECT 1 FROM revisions WHERE source_id=? LIMIT 1", (source_id,)).fetchone() is not None
    )


def _checkpoint_usable(source, cache: dict | None) -> bool:
    """A checkpoint belongs to this source text. A missing hash is an older row and still counts."""
    if not cache:
        return False
    stored = str(cache.get("source_hash") or "")
    if not stored:
        return True
    current = _source_column(source, "content_hash")
    if not current:
        return True
    return stored == current


def _stage_pages(cache: dict | None) -> list[dict]:
    if not cache:
        return []
    return [
        page
        for page in (cache.get("pages") or [])
        if isinstance(page, dict) and str(page.get("body") or "").strip() and str(page.get("name") or "").strip()
    ]


def _stored_body(existing: str, contribution: str, *, fragment: bool) -> str:
    """What the crystal should say after this page is stored.

    A page merged on its own replaces the stored text. A later fragment from the
    same run is joined only when it does not already contain that text. Calling
    this again with the same pair returns the same string.
    """
    current = (existing or "").strip()
    piece = (contribution or "").strip()
    if not fragment:
        return piece
    if not piece or piece == current or (current and piece in current):
        return current or piece
    if current and current in piece:
        return piece
    if not current:
        return piece
    return f"{current}\n\n{piece}"


def _next_stage(pages: list[dict], pending, *, phase: str, source_has_revision: bool) -> str:
    """The first unfinished step. Page marks decide; a phase name is only for older rows."""
    if not pages:
        return ""
    if any(page.get("settled") or page.get("landed") for page in pages):
        if any(not page.get("settled") for page in pages):
            return "并页"
        if any(not page.get("landed") for page in pages):
            return "落地"
        if isinstance(pending, list) and not pending:
            return "收尾"
        return "速写"
    if phase == "landed":
        if isinstance(pending, list) and not pending:
            return "收尾"
        return "速写"
    if phase == "merged":
        return "速写" if source_has_revision else "落地"
    return "并页"


def _resume_point(source) -> str:
    """Continue at the first stage whose product is not on disk yet.

    The lamp may have been wound back to 草稿. Each page has one step, and that
    step is what decides. A draft is not a page a reader can open.
    """
    cache = _load_stage(source)
    source_id = source["id"] if source is not None else ""
    if not _checkpoint_usable(source, cache):
        failed, _display = _split_memory(
            (source["pipeline"] if source is not None and "pipeline" in source.keys() else "") or ""
        )
        if "速写" in failed and _source_landed(source_id):
            return "速写"
        return ""
    return _next_stage(
        _stage_pages(cache),
        cache.get("pending_sketches"),
        phase=str(cache.get("phase") or ""),
        source_has_revision=_source_landed(source_id),
    )


def _sketch_pending(source, pending: list[str]) -> None:
    """Write the sketches still missing. A failure leaves the pages already in the store."""
    mark_pipeline(source["id"], "速写", "run")
    for crystal_id in pending:
        try:
            _touch_lease(source["id"])
            _write_sketch(crystal_id, source_id=source["id"])
        except ModelCallFailed as exc:
            mark_pipeline(source["id"], "速写", "fail")
            _note_stage_error(source["id"], exc)
            raise
        except Exception as exc:
            mark_pipeline(source["id"], "速写", "fail")
            _note_stage_error(source["id"], exc)
            raise
    _clear_stage(source["id"])
    mark_pipeline(source["id"], "完成", "ok")


def _resume_sketches(source: sqlite3.Row) -> None:
    cache = _load_stage(source) or {}
    pending = cache.get("pending_sketches")
    if not isinstance(pending, list):
        pending = _ids_from_pages(cache.get("pages") or [])
        if not pending:
            pending = [
                row["crystal_id"]
                for row in db().execute(
                    "SELECT DISTINCT crystal_id FROM revisions WHERE source_id=?",
                    (source["id"],),
                ).fetchall()
            ]
    _sketch_pending(source, [str(item) for item in pending if str(item).strip()])


def _remember_draft(source, pages: list) -> None:
    """The draft is on disk before any later model call. A later failure must not buy it again."""
    if _checkpoint_usable(source, _load_stage(source)):
        return
    kept = [
        item
        for item in pages
        if isinstance(item, dict) and str(item.get("name") or "").strip() and str(item.get("body") or "").strip()
    ]
    if not kept:
        return
    _save_stage(source["id"], kept, "draft")


_LAST_MAINTAIN = 0.0


def maintain() -> None:
    """Repair file status off the request path. Opening a page does not wait for this."""
    global _LAST_MAINTAIN
    now = time.time()
    if now - _LAST_MAINTAIN < 30:
        return
    _LAST_MAINTAIN = now
    settle_file_statuses()
    _attribute_loose_turns()


def _distill(source: sqlite3.Row, qid: int) -> bool:
    """Distill one source in one read. The queue row finishes when this returns."""
    del qid
    point = _resume_point(source)
    if point == "收尾":
        _clear_stage(source["id"])
        mark_pipeline(source["id"], "完成", "ok")
        return True
    if point == "速写":
        _resume_sketches(source)
        return True
    if point in {"落地", "并页"}:
        cached = _load_stage(source) or {}
        _land_markdown_pages(source, cached.get("pages") or [], start=point)
        return True
    body = source["body"] or ""
    _maybe_forget(source)
    if _denial_phrase(body):
        with LOCK:
            _split_denied(db(), body, source["id"])
            db().commit()
    if _is_forget_request(body):
        return True
    text = _material_for_distill(source)
    inject = _prompt_injection(source)
    clock_at, clock_basis = _history_unit_clock(source)
    if clock_at:
        inject["event_at"] = clock_at
        if clock_basis:
            inject["event_basis"] = clock_basis
    integrate_kw = {**inject, **_match_integrate_parts(text)}
    data = _produce_distill_payload(text, source["kind"], **integrate_kw)
    if isinstance(data, dict) and data.get("payload_kind") == "markdown":
        _remember_draft(source, data.get("pages") or [])
        _land_markdown_pages(source, data.get("pages") or [])
        return True
    if _declared_payload_kind(data) == PAYLOAD_KIND_INTEGRATE:
        _raise_if_new_land_unwritten(_route_distill_payload(source, data, text))
        return True
    raise RuntimeError("模型没有交出页面")


def _accept_agreement(source: sqlite3.Row) -> None:
    if source["kind"] != "user":
        return
    if not any(word in source["body"] for word in ("同意", "就这么做")):
        return
    mate = db().execute("SELECT body FROM sources WHERE turn_id=? AND kind='agent'", (source["turn_id"],)).fetchone()
    if not mate:
        return
    existing = db().execute(
        """SELECT 1 FROM statements WHERE field='约定待办' AND quote=? AND invalid_at IS NULL""",
        (mate["body"],),
    ).fetchone()
    if existing:
        return
    _write_field(
        source,
        {"field": "约定待办", "value": mate["body"], "quote": source["body"], "crystal": "记忆主人", "trust": "written"},
        "user",
        source["body"],
    )


def _normalize_extract(data: dict, body: str) -> dict:
    """Map loose model fields onto op/field/chapter, and fill a missing sheet from those items.

    An explicit empty sheet is left empty. A quote that is not continuous in the source is dropped.
    """
    items = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        kind = str(item.get("kind") or "")
        if not item.get("op"):
            if kind == "chapter" or item.get("chapter"):
                item["op"] = "chapter"
            elif kind == "field" or item.get("field") or item.get("key"):
                item["op"] = "field"
        if item.get("op") == "field" and not item.get("field"):
            item["field"] = item.get("key") or ""
        if item.get("op") == "chapter" and not item.get("chapter"):
            item["chapter"] = item.get("key") or ""
        if item.get("op") in {"field", "chapter"} and not item.get("crystal"):
            item["crystal"] = "记忆主人"
        quote = str(item.get("quote") or "")
        if item.get("op") in {"field", "chapter", "open"} and quote and quote not in body and quote.replace("«令牌»", "") not in body:
            continue
        items.append(item)
    _bind_open_types(items)
    data = dict(data)
    data["items"] = items
    if data.get("sheet") == []:
        data["sheet"] = []
    else:
        data["sheet"] = _merge_sheet(data.get("sheet"), items)
    data["payload_kind"] = PAYLOAD_KIND_EXTRACT
    return data


def _complete_sheet(items: list[dict]) -> list[dict]:
    pages: dict[str, dict] = {}

    def page(name: str, ctype: str, owner: bool) -> dict:
        slot = pages.setdefault(name, {"type": ctype, "owner": owner, "writes": {}})
        slot["type"] = ctype or slot["type"]
        slot["owner"] = owner
        return slot

    def verdict(item: dict) -> str:
        value = str(item.get("value") or "")
        trust = item.get("trust") or "written"
        if trust != "written" or any(word in value for word in ("待核实", "可能", "好像")):
            return "待核实"
        return "写入"

    for item in items:
        op = item.get("op")
        if op == "open":
            ctype = item.get("type") or ""
            slot = page(item.get("name") or "", ctype, False)
            title = "姓名" if ctype == "人物" else "名称"
            slot["writes"][("field", title)] = {"verdict": "写入", "value": item.get("name") or "", "quote": item.get("quote") or ""}
        elif op == "field":
            crystal = item.get("crystal") or "记忆主人"
            owner = crystal == "记忆主人"
            field_name = str(item.get("field") or "")
            personish = field_name in set(PERSON_FIELDS) | {"真名", "当前关注", "约定待办", "姓名"}
            fallback = "人物" if personish else "组织"
            slot = page(crystal, "人物" if owner else str(item.get("type") or fallback), owner)
            key = _canonical_field(item.get("field") or "", owner, slot["type"])
            slot["writes"][("field", key)] = {"verdict": verdict(item), "value": item.get("value") or "", "quote": item.get("quote") or ""}
        elif op == "chapter":
            crystal = item.get("crystal") or "记忆主人"
            owner = crystal == "记忆主人"
            chapter_name = str(item.get("chapter") or "")
            fallback = "人物" if chapter_name in PERSON_CHAPTERS else "组织"
            slot = page(crystal, item.get("type") or ("人物" if owner else fallback), owner)
            slot["writes"][("chapter", item.get("chapter") or "")] = {
                "verdict": verdict(item),
                "value": item.get("value") or "",
                "quote": item.get("quote") or "",
            }
    sheets = []
    for name, slot in pages.items():
        if not name or not slot["type"]:
            continue
        entries = []
        for kind, key in checklist(slot["type"], owner=slot["owner"]):
            hit = slot["writes"].get((kind, key))
            if hit:
                entries.append({"kind": kind, "key": key, **hit})
            else:
                entries.append({"kind": kind, "key": key, "verdict": "无", "value": "", "quote": ""})
        sheets.append({"crystal": name, "type": slot["type"], "entries": entries})
    return sheets


# MiniMax-M3.1 上下文约 100 万。max_tokens 含思考，官方输出上限是 524288。
# 不在这里再切一刀，否则正文会在半句停住。
MINIMAX_MAX_COMPLETION = 524288


def _chat_max_tokens(base: str, model: str) -> int:
    """Use the model's own output ceiling. A smaller number is what truncated sketches."""
    if "minimax" in (base or "").lower() or "minimax" in (model or "").lower():
        return MINIMAX_MAX_COMPLETION
    return 4096


def _distill_system(kind: str) -> str:
    """Files and sessions share one prompt. The header says who is speaking."""
    return distill_system(kind)


@charging("初始蒸馏")
def _llm_extract(text: str, kind: str = "") -> dict:
    target = llm_target("distill")
    if not target:
        return _normalize_extract({"items": [], "critic": {}}, text)
    base, key, model = target
    secret_hits = find_secrets(text)
    safe = redact_text(text)
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {
                "role": "system",
                "content": _distill_system(kind),
            },
            {"role": "user", "content": "SOURCE\n" + safe},
        ],
    }
    content, reason = _complete_chat(base, key, payload, role="distill")
    if not content and reason == "length":
        payload["max_tokens"] = max(int(payload["max_tokens"]), _chat_max_tokens(base, model))
        content, reason = _complete_chat(base, key, payload, role="distill")
    if not content:
        raise RuntimeError(f"模型没有给出正文（{reason or '空'}）")
    data = _parse_model_json(content)
    for item in data.get("items") or []:
        quote = item.get("quote") or ""
        value = item.get("value") or ""
        if (quote and any(hit and hit in quote for hit in secret_hits)) or find_secrets(quote) or find_secrets(value):
            item["quote"] = redact_text(quote)
            item["value"] = "有一则令牌"
    return _normalize_extract(data, text)


# One model call, not the whole pipeline. Draft, polish, merge, and sketch
# each open their own request. A long MiniMax draft still exceeds 180s.
CHAT_TIMEOUT_SEC = 360
# A provider often stays silent until the whole answer is ready. The wait grows
# with the request and stops at half an hour, so a long page can finish and a
# dead connection still gives the worker back.
CHAT_SILENCE_CAP = 1800


# Product levels. Flash's own ladder is low / medium / high / xhigh / max.
# max is that model's default, so an unset worker sends nothing.
REASONING_LEVELS = (
    ("off", "关"),
    ("low", "低"),
    ("medium", "中"),
    ("high", "高"),
    ("xhigh", "极高"),
    ("max", "最高"),
)
_REASONING_NAMES = {name for name, _label in REASONING_LEVELS}


def normalize_reasoning_level(raw) -> str:
    text = str(raw or "").strip().lower()
    text = {"0": "off", "1": "max", "false": "off", "true": "max", "": "max"}.get(text, text)
    return text if text in _REASONING_NAMES else "max"


def reasoning_level(role: str) -> str:
    """Unset stays at 最高, which is how MiniMax-M3.1-Flash already behaves."""
    try:
        raw = get_setting(f"thinking_{role}")
    except Exception:
        return "max"
    return normalize_reasoning_level(raw)


def thinking_request(model: str, level: str) -> dict:
    """Request fields for one reasoning level.

    MiniMax-M3.1-Flash rejects a real off, so 关 uses its lowest effort.
    MiniMax-M3 can skip thinking; its higher levels only turn thinking on.
    Other models are left unchanged, because a rejected field would fail the call.
    最高 adds nothing: that is the Flash default.
    """
    chosen = normalize_reasoning_level(level)
    if chosen == "max":
        return {}
    name = (model or "").lower()
    flash = "m3.1" in name or "flash" in name
    if flash:
        effort = "low" if chosen == "off" else chosen
        return {"thinking": {"type": "adaptive"}, "reasoning_effort": effort}
    if "minimax-m3" in name:
        if chosen == "off":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}}
    return {}


def _provider_id_for(base: str) -> str:
    root = (base or "").rstrip("/")
    try:
        providers = _providers()
    except Exception:
        return ""
    for item in providers:
        if str(item.get("base_url") or "").rstrip("/") == root:
            return str(item.get("id") or "")
    return ""


def _meter_chat(base: str, model: str, messages, answer: str, raw, _role: str) -> None:
    """Remember a finished call. Losing the note never fails the call itself."""

    try:
        from rogmem.cost import current_worker, last_user, note_response

        worker = current_worker() or "模型调用"
        note_response(
            at=utcnow(),
            worker=worker,
            provider_id=_provider_id_for(base),
            model=model,
            body=raw if isinstance(raw, dict) else {},
            prompt=last_user(messages),
            answer=answer or "",
        )
    except Exception:
        return


def _chat_silence_seconds(payload: dict) -> int:
    """How long a socket may stay quiet. Short calls keep the old budget.

    The wait adds a second for every 200 characters we send, and never passes
    the cap. A buffered completion of a long page can arrive; a connection
    that has gone quiet still ends.
    """
    chars = 0
    for msg in (payload or {}).get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(str(part.get("text") or "")) for part in content if isinstance(part, dict))
    scaled = CHAT_TIMEOUT_SEC + chars // 200
    return max(CHAT_TIMEOUT_SEC, min(CHAT_SILENCE_CAP, scaled))


def _piece_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in value)
    return ""


def _assemble_chat(text: str) -> tuple[str, str, dict]:
    """One completion, whether the provider sent a JSON body or a stream of events."""
    stripped = (text or "").lstrip()
    if stripped.startswith("{"):
        raw = json.loads(stripped)
        if not isinstance(raw, dict):
            raise ValueError("模型没有返回对象")
        return "", "", raw
    content_parts: list[str] = []
    final_message = ""
    reasoning: list[str] = []
    finish = ""
    usage: dict = {}
    saw_delta = False
    for line in (text or "").splitlines():
        piece = line.strip()
        if not piece.startswith("data:"):
            continue
        data = piece[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        err = chunk.get("error")
        if isinstance(err, dict) and err.get("message"):
            raise ModelCallFailed(str(err.get("message"))[:200])
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish = str(choice["finish_reason"])
        if isinstance(choice.get("delta"), dict):
            saw_delta = True
            delta = choice["delta"]
            content_parts.append(_piece_text(delta.get("content")))
            reason = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reason, str):
                reasoning.append(reason)
        elif isinstance(choice.get("message"), dict):
            message = choice["message"]
            final_message = _piece_text(message.get("content"))
            reason = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reason, str):
                reasoning.append(reason)
    content = "".join(content_parts) if saw_delta else final_message
    if not content and reasoning:
        joined = "".join(reasoning)
        if "{" in joined and "}" in joined:
            content = joined
    raw = {
        "choices": [{"message": {"content": content, "reasoning_content": "".join(reasoning)}, "finish_reason": finish}],
        "usage": usage,
    }
    return content, finish, raw


def _complete_chat(base: str, key: str, payload: dict, *, timeout: int | None = None, role: str = "") -> tuple[str, str]:
    outbound = dict(payload)
    if role and "minimax" in f"{base} {payload.get('model') or ''}".lower():
        outbound.update(thinking_request(str(payload.get("model") or ""), reasoning_level(role)))
    messages = []
    for msg in payload.get("messages") or []:
        copied = dict(msg)
        content = copied.get("content")
        if isinstance(content, str):
            copied["content"] = redact_text(content)
        messages.append(copied)
    outbound["messages"] = messages
    silence = _chat_silence_seconds(outbound) if timeout is None else timeout
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(outbound).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=silence) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthPause("外部模型鉴权失败") from exc
        detail = exc.read().decode("utf-8", "replace")[:200]
        message = f"HTTP {exc.code} {detail}"
        if exc.code == 429:
            raise ProviderRefused(message) from exc
        raise ModelCallFailed(message) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ModelCallFailed(str(reason or exc)[:200]) from exc
    try:
        streamed, streamed_reason, raw = _assemble_chat(body)
    except json.JSONDecodeError as exc:
        raise ModelCallFailed("模型没有返回结果") from exc
    if streamed or streamed_reason:
        answer = re.sub(r"<think>[\s\S]*?</think>", "", streamed, flags=re.I).strip() or streamed
        _meter_chat(base, str(payload.get("model") or ""), messages, answer, raw, role)
        return answer, streamed_reason
    choices = raw.get("choices") or []
    if not choices:
        err = raw.get("error") or {}
        message = err.get("message") if isinstance(err, dict) else ""
        raise RuntimeError(message[:200] or "模型没有返回结果")
    message = choices[0].get("message") or {}
    answer = _message_answer(message)
    _meter_chat(base, str(payload.get("model") or ""), messages, answer, raw, role)
    return answer, str(choices[0].get("finish_reason") or "")


def _message_answer(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str):
        content = ""
    content = re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.I).strip()
    if content:
        return content
    for key in ("reasoning_content", "reasoning"):
        reasoning = message.get(key)
        if isinstance(reasoning, str) and "{" in reasoning and "}" in reasoning:
            return reasoning
    return ""


def _parse_model_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型没有返回对象")
    return data


def checklist(ctype: str, owner: bool = False) -> list[tuple[str, str]]:
    """Fixed fields and default chapters the model must answer before a page is replaced."""
    fields = [name for name in summary_fields(ctype, owner=owner) if name != "概述"]
    return [("field", name) for name in fields] + [("chapter", name) for name in _chapters_for(ctype)]


def _bind_open_types(items: list[dict]) -> None:
    """A fact about a page opened in this same batch inherits that page's type."""
    opened = {
        str(item.get("name") or ""): item.get("type")
        for item in items
        if item.get("op") == "open" and item.get("name") and item.get("type")
    }
    for item in items:
        if item.get("op") not in {"field", "chapter"} or item.get("type"):
            continue
        ctype = opened.get(str(item.get("crystal") or ""))
        if ctype:
            item["type"] = ctype


def _merge_sheet(model_sheet, items: list[dict]) -> list:
    """Keep what the model actually wrote, and let the program fill the untouched slots."""
    completed = _complete_sheet(items)
    if not isinstance(model_sheet, list):
        return completed
    by_name = {block.get("crystal"): block for block in completed if isinstance(block, dict)}
    for block in model_sheet:
        if not isinstance(block, dict) or not block.get("crystal"):
            continue
        name = block["crystal"]
        current = by_name.get(name)
        if current is None:
            by_name[name] = block
            continue
        if block.get("type"):
            current["type"] = block["type"]
        existing = {
            (entry.get("kind"), entry.get("key")): entry
            for entry in current.get("entries") or []
            if isinstance(entry, dict)
        }
        for entry in block.get("entries") or []:
            if isinstance(entry, dict) and entry.get("kind") and entry.get("key"):
                existing[(entry.get("kind"), entry.get("key"))] = entry
        current["entries"] = list(existing.values())
    return list(by_name.values())


def _canonical_field(field: str, owner: bool, ctype: str) -> str:
    if field in {"出生日期", "出生时间"}:
        return "出生时间"
    if owner and field in {"姓名", "真名"}:
        return "真名"
    if ctype == "人物" and field == "所属组织":
        return "所属组织及职务"
    return field


def _uncertain(value: str, trust: str) -> bool:
    if trust not in {"", "written"}:
        return True
    return any(word in (value or "") for word in ("待核实", "可能", "好像"))


def _active_named(name: str):
    row = db().execute("SELECT * FROM crystallizations WHERE name=? AND status='active'", (name,)).fetchone()
    if row is None and name == "记忆主人":
        row = db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    return row


def _quote_in_scope(quote: str, *texts: str) -> bool:
    """A landed quote may live in the full file, not only the current fragment."""
    if not quote:
        return True
    bare = quote.replace("«令牌»", "")
    for text in texts:
        if text and (quote in text or bare in text or "«令牌»" in quote):
            return True
    return False


def _item_scope(item: dict, unit: str, full: str) -> str:
    if item.get("landed") and full:
        return full
    return unit


def _page_type(raw) -> str:
    """Crystal type as stored. The old name 项目 is 工程."""
    from rogmem.pages_md import canonical_page_type

    return canonical_page_type(str(raw or "").strip())


def _open_would_publish(item: dict, critic: dict, body: str, full: str = "") -> bool:
    name = item.get("name") or ""
    ctype = _page_type(item.get("type"))
    quote = item.get("quote") or ""
    if not name or name in COMMON or name in PLACE_NAMES:
        return False
    if name in ORG_NAMES and ctype != "组织":
        return False
    if name in PRODUCT_NAMES and ctype != "产品":
        return False
    if ctype not in {"人物", "工程", "组织", "产品", "概念"}:
        return False
    hay = full if item.get("landed") and full else body
    if name not in hay or name not in quote:
        return False
    if critic.get(name) is False:
        return False
    if ctype == "产品" and any(word in name for word in NOT_PRODUCT):
        return False
    if ctype == "工程":
        repo = _repo_identity(str(item.get("repo") or "")) or _repo_identity(quote)
        if not repo or repo not in body:
            return False
    if ctype == "人物" and re.search(r"我(叫|是)" + re.escape(name), body):
        return False
    if not item.get("landed") and not _proper_name_ok(name, ctype, quote):
        return False
    if _active_named(name):
        return False
    return True


def _field_target(item: dict) -> tuple[str, str, bool]:
    target = item.get("crystal") or "记忆主人"
    row = _active_named(target)
    if row is None:
        hinted = _page_type(item.get("type"))
        if hinted not in {"人物", "工程", "组织", "产品", "概念"}:
            hinted = ""
        if not hinted and str(item.get("field") or "") in _PERSON_CLAIM_FIELDS:
            hinted = "人物"
        if not hinted and str(item.get("chapter") or "") in PERSON_CHAPTERS:
            hinted = "人物"
        if hinted and target not in {"", "记忆主人", "owner"}:
            return target, hinted, False
        return target, "", False
    owner = row["id"] == "owner"
    return row["name"], row["type"], owner


def _require_publish_sheet(source: sqlite3.Row, items: list[dict], critic: dict, sheet, body: str, unit_body: str | None = None):
    """Refuse the whole version before any write when a checklist verdict cannot stand.

    Keys the model never listed are filled as 无. A landed subject may also
    receive an empty sheet block. Forged quotes and conflicting verdicts still fail.
    """
    needed: dict[str, dict] = {}
    backed: dict[tuple[str, str, str], str] = {}
    for item in items:
        op = item.get("op")
        value = str(item.get("value") or "")
        trust = item.get("trust") or "written"
        if op == "chapter" and body.strip() and value.strip() == body.strip() and not item.get("landed"):
            raise RuntimeError("交卷只是把原文追加在末尾")
        if op == "open" and _open_would_publish(item, critic, body, full=source["body"] or ""):
            name = item["name"]
            ctype = _page_type(item["type"])
            needed[name] = {"type": ctype, "owner": False}
            title = "姓名" if ctype == "人物" else "名称"
            backed[(name, "field", title)] = "写入"
            repo = _repo_identity(str(item.get("repo") or "")) or _repo_identity(item.get("quote") or "")
            if ctype == "工程" and repo:
                backed[(name, "field", "仓库地址")] = "写入"
        elif op == "field":
            quote = item.get("quote") or ""
            if quote and not _quote_in_scope(quote, body, (source["body"] or "") if item.get("landed") else ""):
                continue
            crystal, ctype, owner = _field_target(item)
            if not ctype:
                continue
            key = _canonical_field(item.get("field") or "", owner, ctype)
            rank = rank_of(source)
            if key in STYLE and rank in {"agent", "summary"}:
                continue
            if key in STYLE and rank == "file" and owner and not _quote_names_owner(quote):
                continue
            if key == "约定待办" and rank != "user":
                continue
            row = _active_named(crystal)
            if row is None:
                summary = {name: "尚无来源" for name in summary_fields(ctype, owner=owner)}
            else:
                summary = json.loads(row["summary_json"])
            if key not in summary and key not in {"真名", "姓名", "名称", "出生时间", "所属组织及职务"}:
                continue
            needed.setdefault(crystal, {"type": ctype, "owner": owner})
            backed[(crystal, "field", key)] = "待核实" if _uncertain(value, trust) else "写入"
        elif op == "chapter":
            quote = item.get("quote") or ""
            chapter = (item.get("chapter") or "").strip()
            if not chapter or not value or not quote:
                continue
            if not _quote_in_scope(quote, body, (source["body"] or "") if item.get("landed") else ""):
                continue
            crystal, ctype, owner = _field_target(item)
            if not ctype:
                continue
            needed.setdefault(crystal, {"type": ctype, "owner": owner})
            backed[(crystal, "chapter", chapter)] = "待核实" if _uncertain(value, trust) else "写入"
    if not needed:
        return None
    blocks = {}
    for block in sheet or []:
        if isinstance(block, dict) and block.get("crystal"):
            blocks[block["crystal"]] = block
    for name, meta in needed.items():
        block = blocks.get(name)
        if not block:
            landed = any(
                item.get("landed") and (item.get("name") == name or item.get("crystal") == name)
                for item in items
            )
            if not landed:
                raise RuntimeError(f"交卷缺清单：{name}")
            block = {"crystal": name, "type": meta["type"], "entries": []}
            blocks[name] = block
            if isinstance(sheet, list):
                sheet.append(block)
        if block.get("type") and block.get("type") != meta["type"]:
            if any(item.get("landed") and (item.get("name") == name or item.get("crystal") == name) for item in items):
                block["type"] = meta["type"]
            else:
                raise RuntimeError(f"交卷类型不对：{name}")
        got = {}
        for entry in block.get("entries") or []:
            kind = entry.get("kind")
            key = entry.get("key")
            verdict = entry.get("verdict")
            if kind not in {"field", "chapter"} or not key:
                raise RuntimeError(f"交卷结论无效：{name}")
            if verdict not in {"写入", "待核实", "无"}:
                raise RuntimeError(f"交卷结论无效：{name} {key}")
            got[(kind, key)] = entry
        for kind, key in checklist(meta["type"], owner=meta["owner"]):
            if (kind, key) in got:
                continue
            filled = {"kind": kind, "key": key, "verdict": "无", "value": "", "quote": ""}
            block.setdefault("entries", []).append(filled)
            got[(kind, key)] = filled
        for (kind, key), entry in got.items():
            verdict = entry["verdict"]
            expect = backed.get((name, kind, key))
            if verdict == "无" and not expect:
                continue
            if expect and expect != verdict:
                if _landed_backs(items, name, kind, key):
                    entry["verdict"] = expect
                    verdict = expect
                    if not entry.get("quote"):
                        _copy_landed_quote(entry, items, name, kind, key)
                else:
                    raise RuntimeError(f"交卷结论冲突：{name} {key}")
            written = entry.get("value") or ""
            quote = entry.get("quote") or ""
            if verdict == "写入" and _uncertain(written, "written"):
                raise RuntimeError(f"交卷把待核实写进核心摘要：{name} {key}")
            extra = (source["body"] or "") if _landed_backs(items, name, kind, key) else ""
            if verdict in {"写入", "待核实"} and (not quote or not _quote_in_scope(quote, body, extra)):
                if _landed_backs(items, name, kind, key):
                    _copy_landed_quote(entry, items, name, kind, key)
                    quote = entry.get("quote") or ""
                    extra = source["body"] or ""
            if verdict == "写入" and (not quote or not _quote_in_scope(quote, body, extra)):
                raise RuntimeError(f"交卷写入缺少原文：{name} {key}")
            if verdict == "待核实" and (not quote or not _quote_in_scope(quote, body, extra)):
                raise RuntimeError(f"交卷待核实缺少原文：{name} {key}")
    from rogmem.publish_gate import assert_coverage, forbid_synthetic_none, unit_chunks

    scope = unit_body if unit_body is not None else body
    forbid_synthetic_none(sheet, scope, items)
    chunks = [dict(row) for row in db().execute(
        "SELECT seq, text, cited FROM chunks WHERE source_id=? ORDER BY seq",
        (source["id"],),
    )]
    report = assert_coverage(scope, unit_chunks(chunks, scope), items, sheet)
    return report


def _person_is_owner(name: str, aliases=None) -> bool:
    """主人的真名或别名不是另一个人物。对上了就写进记忆主人。"""
    from rogmem.names import name_key, owner_names_match

    labels = _owner_labels()
    incoming = [str(name or "").strip()]
    if isinstance(aliases, list):
        incoming.extend(str(item or "").strip() for item in aliases)
    elif aliases:
        incoming.extend(part.strip() for part in str(aliases).replace("，", "、").split("、"))
    for raw in incoming:
        if len(name_key(raw)) < 2:
            continue
        for label in labels:
            if owner_names_match(raw, label):
                return True
    return False


def _owner_labels() -> list[str]:
    labels = ["记忆主人"]
    row = _active_named("记忆主人")
    if row is None:
        return labels
    try:
        summary = _summary_slots_from_row(row)
    except json.JSONDecodeError:
        summary = {}
    for key in ("真名", "别名"):
        raw = str(summary.get(key) or "")
        if raw in {"", "尚无来源", "暂无来源"}:
            continue
        for part in re.split(r"[、,，]", raw):
            part = part.strip()
            if len(part) < 2 or part in {"尚无来源", "暂无来源"} or part in labels:
                continue
            labels.append(part)
    return labels


def _owner_prompt_fields() -> tuple[str, str]:
    """Owner 真名 / 别名 for approved Draft and Integrate slots. Empty if unknown."""
    extras = [label for label in _owner_labels() if label != "记忆主人"]
    if not extras:
        return "", ""
    return extras[0], ", ".join(extras[1:])


def _source_id(source) -> str:
    if source is None:
        return ""
    try:
        keys = source.keys()
    except Exception:
        return ""
    if "id" in keys:
        return str(source["id"] or "").strip()
    return ""


def _source_filename(source) -> str:
    if source is None:
        return ""
    try:
        keys = source.keys()
    except Exception:
        return ""
    if "filename" in keys:
        return str(source["filename"] or "").strip()
    return ""


def material_kind_label(kind: str) -> str:
    """Map source kind onto the Draft user 材料类型 slot. Session vs file only."""
    if kind in {"user", "agent", "summary"}:
        return "会话"
    if kind == "file":
        return "文件"
    return ""


def _prompt_injection(source=None, **overrides) -> dict:
    """Fill approved program-injection slots from source + owner labels."""
    real, aliases = _owner_prompt_fields()
    kind = str(overrides.get("source_kind") or overrides.get("kind") or "")
    if not kind and source is not None:
        try:
            kind = str(source["kind"] or "")
        except Exception:
            kind = ""
    label = str(overrides.get("material_kind") or "") or material_kind_label(kind)
    ref = str(overrides.get("project_ref") or "") or _source_project_ref(source)
    ingested_at = str(overrides.get("ingested_at") or _source_column(source, "ingested_at") or "")
    event_at, event_basis = _model_clock(
        str(overrides.get("event_at") or _source_column(source, "event_at") or ""),
        ingested_at,
        str(overrides.get("event_basis") or _source_column(source, "event_basis") or ""),
    )
    return {
        "filename": str(overrides.get("filename") or _source_filename(source) or ""),
        "owner_real_name": str(overrides.get("owner_real_name") or real),
        "owner_aliases": str(overrides.get("owner_aliases") or aliases),
        "material_kind": label,
        "project_ref": ref,
        "source_id": str(overrides.get("source_id") or _source_id(source) or ""),
        "event_at": event_at,
        "ingested_at": ingested_at,
        "event_basis": event_basis,
    }


def _source_column(source, name: str) -> str:
    if source is None:
        return ""
    try:
        keys = source.keys()
    except Exception:
        return str(source.get(name) or "") if isinstance(source, dict) else ""
    if name not in keys:
        return ""
    return str(source[name] or "")


_OWNER_OTHER = re.compile(
    r"的(?:同事|朋友|同学|家人|父亲|母亲|爸爸|妈妈|儿子|女儿|老师|学生|邻居|伴侣|妻子|丈夫|上司|下属|客户)"
)
_PERSON_CLAIM_FIELDS = STYLE | {"所属组织及职务", "性别", "出生时间", "真名", "姓名", "当前关注"}


def _quote_names_owner(quote: str) -> bool:
    text = quote or ""
    return any(label and label in text for label in _owner_labels())


def _owner_is_subject(quote: str) -> bool:
    """True when an owner label is in the quote and not the possessive of someone else."""
    text = quote or ""
    for label in _owner_labels():
        if not label or label not in text:
            continue
        if re.search(re.escape(label) + _OWNER_OTHER.pattern, text):
            continue
        return True
    return False


def _set_sheet_verdict(sheet: list, crystal: str, item: dict) -> bool:
    kind = "chapter" if item.get("op") == "chapter" else "field"
    key = item.get("chapter") if kind == "chapter" else item.get("field")
    for block in sheet:
        if not isinstance(block, dict) or block.get("crystal") != crystal:
            continue
        for entry in block.get("entries") or []:
            if isinstance(entry, dict) and entry.get("kind") == kind and entry.get("key") == key:
                entry["verdict"] = "待核实"
                entry["quote"] = item.get("quote") or entry.get("quote") or ""
                return True
    return False


def _rebind_claims(source: sqlite3.Row, items: list[dict], sheet):
    """Files never default a claim onto the memory owner.

    A named person can receive a written claim when their name is in the quote.
    A guess stays on that person and is marked unverified. An unnamed claim is dropped.
    """
    if source["kind"] != "file":
        return items, sheet
    kept: list[dict] = []
    downgraded = False
    opened_now = {
        str(item.get("name") or "")
        for item in items or []
        if isinstance(item, dict) and item.get("op") == "open" and item.get("name")
    }
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if item.get("op") == "open":
            kept.append(item)
            continue
        target = str(item.get("crystal") or "")
        quote = str(item.get("quote") or "")
        if target in {"", "记忆主人", "owner"}:
            if _owner_is_subject(quote):
                item["crystal"] = "记忆主人"
                kept.append(item)
            continue
        named_in_quote = target in quote
        in_batch = target in opened_now
        guessed = ((not named_in_quote) and not in_batch) or item.get("trust") not in {None, "", "written"} or _uncertain(
            str(item.get("value") or ""), str(item.get("trust") or "written")
        )
        if not named_in_quote and not in_batch and _active_named(target) is None:
            continue
        if guessed:
            item["trust"] = "unverified"
            downgraded = True
        kept.append(item)
    explicit_empty = isinstance(sheet, list) and not sheet
    if isinstance(sheet, list):
        sheet = json.loads(json.dumps(sheet, ensure_ascii=False))
        if not any(str(item.get("crystal") or "") in {"记忆主人", "owner"} for item in kept):
            sheet = [block for block in sheet if not (isinstance(block, dict) and block.get("crystal") in {"记忆主人", "owner"})]
        if downgraded:
            missing = False
            for item in kept:
                if item.get("trust") != "unverified":
                    continue
                if not _set_sheet_verdict(sheet, str(item.get("crystal") or ""), item):
                    missing = True
            if missing:
                sheet = _complete_sheet(kept)
    elif sheet is None:
        sheet = []
    kept = _open_missing_people(kept)
    if isinstance(sheet, list) and not explicit_empty:
        sheet = _cover_kept_crystals(sheet, kept)
    return kept, sheet


def _open_missing_people(items: list[dict]) -> list[dict]:
    """A file claim about a new person has to open that page or the sentence is discarded."""
    opens = {str(item.get("name") or "") for item in items if item.get("op") == "open"}
    extra: list[dict] = []
    for item in items:
        if item.get("op") not in {"field", "chapter"}:
            continue
        target = str(item.get("crystal") or "")
        if not target or target in {"记忆主人", "owner"} or target in opens:
            continue
        if _active_named(target) is not None:
            continue
        quote = str(item.get("quote") or "")
        if target not in quote:
            continue
        field = str(item.get("field") or "")
        chapter = str(item.get("chapter") or "")
        person = field in _PERSON_CLAIM_FIELDS or chapter in PERSON_CHAPTERS or item.get("type") == "人物"
        if not person:
            continue
        extra.append({"op": "open", "type": "人物", "name": target, "quote": quote})
        opens.add(target)
    return extra + items


def _cover_kept_crystals(sheet: list, items: list[dict]) -> list:
    needed = set()
    for item in items:
        if item.get("op") == "open" and item.get("name"):
            needed.add(item["name"])
        elif item.get("op") in {"field", "chapter"} and item.get("crystal"):
            needed.add(item["crystal"])
    have = {block.get("crystal"): block for block in sheet if isinstance(block, dict)}
    filled = {block.get("crystal"): block for block in _complete_sheet(items)}
    for name in needed:
        complete = filled.get(name)
        if name not in have:
            if complete:
                sheet.append(complete)
            continue
        if not complete:
            continue
        block = have[name]
        if complete.get("type"):
            block["type"] = complete["type"]
        got = {(entry.get("kind"), entry.get("key")) for entry in (block.get("entries") or [])}
        for entry in complete.get("entries") or []:
            pair = (entry.get("kind"), entry.get("key"))
            if pair not in got:
                block.setdefault("entries", []).append(entry)
                got.add(pair)
    return sheet


_HAN_NAME = re.compile(r"^[\u4e00-\u9fff]{2,4}$")
_CONTACT = re.compile(r"@|\d{8,}")
_ROLE_CHAPTER = {"产品": "功能与形态", "工程": "目的目标"}
_JUNK_LINE = re.compile(r"^(?:thumb\||Category:|Zh$|https?://|\{\{|\d+px\|)|thumb\|", re.I)
_CRUMB_LINE = re.compile(r"^(?:thumb|right|left|\d+px)(?:\|.*)?$", re.I)
_LEAD_LAW = re.compile(r"^《([^》]{2,40}(?:法|条例|规定|办法))》")
_LEAD_LAW_BARE = re.compile(r"^([\u4e00-\u9fff]{2,40}(?:法|条例|规定|办法))(?=是|为|為)")
_LEAD_TOPIC = re.compile(
    r"^([\u4e00-\u9fffA-Za-z]{2,12})於.{2,60}(?:均有|有著|有着|有不同|是指|意為|意为|意義|意义)"
)
_LEAD_NAME = re.compile(r"^([\u4e00-\u9fffA-Za-z0-9·]{2,24}?)(?=是|为|為|指|（|\(|，|,|。|$)")
_LEAD_ORG_SUFFIX = re.compile(r"(?:公司|集团|集團|协会|協會|联合会|聯合會|委员会|委員會|组织|組織|银行|銀行|商会|商會)$")
_LEAD_LAW_SUFFIX = re.compile(r"(?:法|条例|规定|办法)$")
_CLAUSE_NOISE = re.compile(r"[是的在与和於于與用]")
_LEAD_CHAPTER = {"人物": "事业经历", "组织": "发展沿革", "产品": "发展沿革", "概念": "定义", "工程": "目的目标"}
_BIO_PAREN = re.compile(r"^[，,]?[（(][\d年月日—\-，,\s]*[）)]")
_QUOTED_TITLE = re.compile(r"《([^》]{4,40})》|「([^」]{4,40})」|“([^”]{4,40})”")
_SECTION_HEAD = re.compile(r"^={2,6}\s*.+\s*={2,6}$")
_LEAD_SCAN = 12
_ALIAS_MARK = re.compile(r"亦称|又名|又称|又稱|简称|簡稱|英语：|英語：")


def _document_subject(full: str) -> str:
    """The person a file is about, when the file itself introduces them by name.

    A short name on its own line, plus career or education evidence later in the
    same file. Not the memory owner, and not a name that is already an org or a place.
    """
    from rogmem.publish_gate import evidence_chapters

    if not evidence_chapters(full or "", "人物"):
        return ""
    seen = 0
    for raw in (full or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        seen += 1
        if seen > 8:
            break
        if not _HAN_NAME.fullmatch(line):
            continue
        if line in COMMON or line in ORG_NAMES or line in PLACE_NAMES or line in PRODUCT_NAMES:
            continue
        return line
    return ""


def _is_crumb_line(line: str) -> bool:
    """Image crumbs and wiki chrome are not a document lead.

    Bare `thumb` has no pipe. That is still a picture shard, not a subject.
    """
    text = (line or "").strip()
    if not text:
        return True
    if _JUNK_LINE.search(text) or _CRUMB_LINE.fullmatch(text):
        return True
    return False


def _first_real_line(full: str) -> str:
    for raw in (full or "").splitlines():
        line = raw.strip()
        if _is_opening_chrome(line):
            continue
        return line
    return ""


def _is_opening_chrome(line: str) -> bool:
    """Image crumbs and infobox fields are not a document lead."""
    if _is_crumb_line(line):
        return True
    return line.startswith("|")


def _bio_paren_lead(name: str, line: str) -> bool:
    """A short name plus empty or birth parentheses is a person, not a firm.

    Wiki pages leave the birth slot blank. The next words may be 企业家 or a
    company the person founded. Those are a role, not the subject type.
    """
    if not re.match(r"^[\u4e00-\u9fff]{2,4}$", name):
        return False
    if name in COMMON or name in PLACE_NAMES or name in ORG_NAMES or name in PRODUCT_NAMES:
        return False
    rest = line[len(name):]
    match = _BIO_PAREN.match(rest)
    if not match:
        return False
    after = rest[match.end():]
    if re.match(r"是", after) or after[:8].startswith("全稱") or after[:8].startswith("全称"):
        return False
    return True


def _lead_from_line(line: str) -> tuple[str, str, str] | None:
    """One sentence is a lead only when it already names a subject."""
    if len(line) < 4:
        return None
    match = _LEAD_LAW.match(line)
    if match:
        return match.group(1), "概念", line
    match = _LEAD_LAW_BARE.match(line)
    if match:
        return match.group(1), "概念", line
    match = _LEAD_NAME.match(line)
    if not match:
        topic = _LEAD_TOPIC.match(line)
        if topic:
            name = topic.group(1)
            if name not in COMMON and name not in PLACE_NAMES:
                return name, "概念", line
        return None
    name = match.group(1)
    lawish = bool(_LEAD_LAW_SUFFIX.search(name))
    if name in COMMON or name in PLACE_NAMES:
        return None
    if not lawish and _CLAUSE_NOISE.search(name):
        return None
    head = line[:48]
    if _LEAD_LAW_SUFFIX.search(name) or re.search(r"是一部|该法案", head):
        return name, "概念", line
    if _bio_paren_lead(name, line):
        return name, "人物", line
    if _LEAD_ORG_SUFFIX.search(name):
        return name, "组织", line
    if re.search(r"公司|组织|組織|协会|協會|集团|集團|企业|企業|论坛|論壇|非政府", head):
        return name, "组织", line
    if re.search(r"是一[款种个]|用来|芯片|品牌|平台", head):
        return name, "产品", line
    if re.search(r"是.{0,8}(?:一家|一個|一个)", head):
        return name, "组织", line
    if re.match(r"^[\u4e00-\u9fff]{2,4}$", name) and name not in ORG_NAMES | PRODUCT_NAMES:
        if re.match(rf"^{re.escape(name)}[（(](?:\d|年)", line):
            return name, "人物", line
        if re.search(r"[（(][^）)]*[）)][，,].*(?:字|号|男|女|其名|原名)", line) or re.search(
            r"[，,].*(?:字|号|男|女|其名|原名)", line
        ):
            return name, "人物", line
    if "是" in line or "（" in line or "(" in line:
        return name, "概念", line
    return None


def _file_lead_subject(full: str) -> tuple[str, str, str] | None:
    """The named subject an encyclopedic file introduces in its opening prose.

    Resume-style files still go through _document_subject. This path is for running
    prose that opens with a proper name, a titled norm, or an organization.
    Opening chrome is not the subject; the first sentence that already looks like
    a lead still names it. The name stops at the copula or first pause.
    """
    seen = 0
    for raw in (full or "").splitlines():
        line = raw.strip()
        if _SECTION_HEAD.match(line):
            break
        if _is_opening_chrome(line):
            continue
        seen += 1
        if seen > _LEAD_SCAN:
            break
        parsed = _lead_from_line(line)
        if parsed:
            return parsed
    return None


def _quoted(items: list[dict]) -> str:
    return "\n".join(
        str(item.get("quote") or "")
        for item in items
        if item.get("op") in {"chapter", "field"}
    )


def _land_file_facts(source: sqlite3.Row, items: list[dict], unit: str) -> list[dict]:
    """Put a file's biographical lines on its subject, and a product's purpose on that product.

    The model may open only a name, or drop a passage because a side page has no sheet.
    Lines the file already states are still that subject's facts.
    """
    from rogmem.publish_gate import career_lines, education_lines

    if source["kind"] != "file":
        return items
    items = [dict(item) for item in items]
    full = source["body"] or ""
    subject = _document_subject(full)
    covered = _quoted(items)
    if subject and subject in full:
        career = [line for line in career_lines(unit) if len(line) >= 8 and line not in covered]
        education = [line for line in education_lines(unit) if len(line) >= 8 and line not in covered]
        taken = set(career) | set(education)
        other = []
        for line in _source_sentences(unit):
            if len(line) < 8 or line in covered or line in taken or line == subject:
                continue
            if _CONTACT.search(line):
                continue
            other.append(line)
        if career or education or other:
            opened = any(item.get("op") == "open" and item.get("name") == subject for item in items)
            if not opened and _active_named(subject) is None:
                items.insert(0, {"op": "open", "type": "人物", "name": subject, "quote": subject, "landed": True})
            for line in education:
                items.append(
                    {
                        "op": "chapter",
                        "chapter": "教育背景",
                        "value": line,
                        "quote": line,
                        "crystal": subject,
                        "type": "人物",
                        "trust": "written",
                        "landed": True,
                    }
                )
            for line in career + other:
                items.append(
                    {
                        "op": "chapter",
                        "chapter": "事业经历",
                        "value": line,
                        "quote": line,
                        "crystal": subject,
                        "type": "人物",
                        "trust": "written",
                        "landed": True,
                    }
                )
            covered = _quoted(items)
    targets = {}
    for item in items:
        if item.get("op") == "open" and item.get("name") and _page_type(item.get("type")) in _ROLE_CHAPTER:
            targets[str(item["name"])] = item["type"]
    if _DB is not None:
        for row in db().execute(
            "SELECT name, type FROM crystallizations WHERE status='active' AND type IN ('产品','工程')"
        ):
            name = str(row["name"] or "")
            if len(name) >= 2 and name in unit:
                targets.setdefault(name, row["type"])
    extra = []
    sentences = [line for line in _source_sentences(unit) if len(line) >= 8]
    for name, ctype in targets.items():
        chapter = _ROLE_CHAPTER[ctype]
        own = "\n".join(
            str(other.get("quote") or "")
            for other in items
            if other.get("op") in {"chapter", "field"} and other.get("crystal") == name
        )
        for line in sentences:
            if line.strip() == name or name not in line or line in own:
                continue
            extra.append(
                {
                    "op": "chapter",
                    "chapter": chapter,
                    "value": line,
                    "quote": line,
                    "crystal": name,
                    "type": ctype,
                    "trust": "written",
                    "landed": True,
                }
            )
            own += "\n" + line
    lead = _file_lead_subject(full)
    if lead:
        name, ctype, quote = lead
        hay = unit if quote in (unit or "") else full
        if name in hay and quote in (full or ""):
            opened = False
            for item in items:
                if item.get("op") == "open" and item.get("name") == name:
                    item["landed"] = True
                    item["type"] = ctype
                    opened = True
            if not opened and _active_named(name) is None:
                items.insert(
                    0,
                    {"op": "open", "type": ctype, "name": name, "quote": name, "landed": True},
                )
            if quote not in _quoted(items):
                extra.append(
                    {
                        "op": "chapter",
                        "chapter": _LEAD_CHAPTER.get(ctype, "发展沿革"),
                        "value": quote,
                        "quote": quote,
                        "crystal": name,
                        "type": ctype,
                        "trust": "written",
                        "landed": True,
                    }
                )
    host = ""
    host_type = ""
    if lead:
        host, host_type, _lead_quote = lead
    elif subject:
        host, host_type = subject, "人物"
    if host:
        covered = _quoted(items) + "\n" + _quoted(extra)
        for raw in (full or "").splitlines():
            line = raw.strip()
            if _is_crumb_line(line) or len(line) < 6:
                continue
            found = _QUOTED_TITLE.search(line)
            if not found:
                continue
            title = next(group for group in found.groups() if group)
            if title == host or title in COMMON or title in PLACE_NAMES:
                continue
            if title in covered or line in covered:
                continue
            extra.append(
                {
                    "op": "chapter",
                    "chapter": _LEAD_CHAPTER.get(host_type, "发展沿革"),
                    "value": line,
                    "quote": line,
                    "crystal": host,
                    "type": host_type,
                    "trust": "written",
                    "landed": True,
                }
            )
            covered += "\n" + line
        for raw in (full or "").splitlines():
            line = raw.strip()
            if _is_opening_chrome(line) or len(line) < 6:
                continue
            if host not in line or not _ALIAS_MARK.search(line):
                continue
            if line in covered:
                continue
            extra.append(
                {
                    "op": "chapter",
                    "chapter": _LEAD_CHAPTER.get(host_type, "发展沿革"),
                    "value": line,
                    "quote": line,
                    "crystal": host,
                    "type": host_type,
                    "trust": "written",
                    "landed": True,
                }
            )
            covered += "\n" + line
    return items + extra


def _landed_backs(items: list[dict], name: str, kind: str, key: str) -> bool:
    for item in items:
        if not item.get("landed"):
            continue
        if item.get("op") == "open" and item.get("name") == name:
            title = "姓名" if item.get("type") == "人物" else "名称"
            if kind == "field" and key == title:
                return True
        if item.get("op") == "chapter" and item.get("crystal") == name and kind == "field" and key in {"名称", "姓名"}:
            return True
        if item.get("op") == "chapter" and item.get("crystal") == name and kind == "chapter" and item.get("chapter") == key:
            return True
        if item.get("op") == "field" and item.get("crystal") == name and kind == "field" and item.get("field") == key:
            return True
    return False


def _copy_landed_quote(entry: dict, items: list[dict], name: str, kind: str, key: str) -> None:
    """A landed line can stand in when the sheet says 写入 but left the quote blank."""
    for item in items:
        if not item.get("landed"):
            continue
        kind_l, key_l, value_l, quote_l = _landed_write(item)
        crystal = item.get("name") if item.get("op") == "open" else item.get("crystal")
        if crystal == name and kind_l == kind and key_l == key:
            entry["value"] = value_l
            entry["quote"] = quote_l
            return


def _landed_write(item: dict) -> tuple[str, str, str, str]:
    if item.get("op") == "open":
        key = "姓名" if item.get("type") == "人物" else "名称"
        return "field", key, str(item.get("name") or ""), str(item.get("quote") or item.get("name") or "")
    if item.get("op") == "chapter":
        return (
            "chapter",
            str(item.get("chapter") or ""),
            str(item.get("value") or ""),
            str(item.get("quote") or ""),
        )
    return (
        "field",
        str(item.get("field") or ""),
        str(item.get("value") or ""),
        str(item.get("quote") or ""),
    )


def _apply_landed_to_sheet(sheet: list, items: list[dict]) -> list:
    """A fact the program landed overrides a checklist entry that still says 无."""
    by_name = {block.get("crystal"): block for block in sheet if isinstance(block, dict)}
    for item in items:
        if not item.get("landed") or item.get("op") not in {"open", "chapter", "field"}:
            continue
        name = item.get("name") if item.get("op") == "open" else item.get("crystal")
        block = by_name.get(name)
        if not isinstance(block, dict):
            continue
        if item.get("op") == "open" and item.get("type"):
            block["type"] = _page_type(item.get("type"))
        kind, key, value, quote = _landed_write(item)
        if not key:
            continue
        verdict = "写入" if not _uncertain(value, str(item.get("trust") or "written")) else "待核实"
        updated = False
        for entry in block.get("entries") or []:
            if entry.get("kind") == kind and entry.get("key") == key:
                entry["verdict"] = verdict
                entry["value"] = value
                entry["quote"] = quote
                updated = True
        if not updated:
            block.setdefault("entries", []).append(
                {"kind": kind, "key": key, "verdict": verdict, "value": value, "quote": quote}
            )
    return sheet


def _declared_payload_kind(data) -> str:
    """Declarative extract/integrate contract. Reads only payload_kind."""
    if not isinstance(data, dict):
        return ""
    declared = data.get("payload_kind")
    inner = data["result"] if isinstance(data.get("result"), dict) else None
    if not declared and isinstance(inner, dict):
        declared = inner.get("payload_kind")
    kind = str(declared or "").strip()
    if kind in {PAYLOAD_KIND_EXTRACT, PAYLOAD_KIND_INTEGRATE}:
        return kind
    return ""


def _integrate_matched_id(data) -> str:
    inner = data["result"] if isinstance(data.get("result"), dict) else data
    if not isinstance(inner, dict):
        return ""
    matched = inner.get("matched_id") or ""
    ident = inner.get("identity")
    if not matched and isinstance(ident, dict):
        matched = ident.get("matched_id") or ""
    return str(matched or "")


def _pack_library_candidate(crystal, extra_aliases: list[str] | None = None) -> dict:
    """Shape a library crystal for integrate v1.4. Read-only; no envelope mutation."""
    aliases = []
    for label in [crystal.name, *(crystal.aliases or []), *(extra_aliases or [])]:
        name = str(label or "").strip()
        if name and name not in aliases and name not in {"尚无来源", "暂无来源"}:
            aliases.append(name)
    facts = []
    for item in crystal.facts or []:
        if hasattr(item, "to_dict"):
            facts.append(item.to_dict())
        elif isinstance(item, dict):
            facts.append(item)
    return {
        "id": crystal.id,
        "type": crystal.type,
        "name": crystal.name,
        "aliases": [alias for alias in aliases if alias != crystal.name],
        "summary": crystal.summary,
        "body_md": crystal.body_md,
        "facts": facts,
        "key_fields": dict(getattr(crystal, "keys", None) or {}),
        "body_injection": "full",
    }


def _integrate_decision(data) -> str:
    inner = data["result"] if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    if not isinstance(inner, dict):
        return ""
    ident = inner.get("identity")
    if isinstance(ident, dict):
        return str(ident.get("decision") or ident.get("identity") or "")
    return str(ident or "")


def _full_library_pack(hit: dict) -> dict:
    """A-tier full pack after identity lock."""
    packed = dict(hit)
    injection = str(packed.get("body_injection") or "").strip()
    packed["body_injection"] = injection if injection in {"full", "sectioned"} else "full"
    packed.setdefault("aliases", list(hit.get("aliases") or []))
    packed.setdefault("summary", hit.get("summary") or "")
    packed.setdefault("body_md", hit.get("body_md") or "")
    packed.setdefault("facts", list(hit.get("facts") or []) if isinstance(hit.get("facts"), list) else [])
    keys = hit.get("key_fields") if isinstance(hit.get("key_fields"), dict) else hit.get("keys")
    if isinstance(keys, dict):
        packed["key_fields"] = dict(keys)
    elif "key_fields" not in packed:
        packed["key_fields"] = {}
    return packed


def _is_b_card(item) -> bool:
    if not isinstance(item, dict):
        return False
    injection = str(item.get("body_injection") or "").strip()
    if injection in {"full", "sectioned"}:
        return False
    if injection == _BA_CARD_INJECTION:
        return True
    return not str(item.get("body_md") or "").strip()


def _is_a_pack(item) -> bool:
    if not isinstance(item, dict):
        return False
    if str(item.get("body_injection") or "").strip() not in {"full", "sectioned"}:
        return False
    return all(key in item for key in ("id", "type", "name", "aliases", "summary", "body_md", "facts"))


def _complete_integrate_mid(
    text: str,
    kind: str = "",
    *,
    draft_crystal=None,
    library_candidates=None,
    owner_real_name: str = "",
    owner_aliases: str = "",
):
    """One mid-稿 Integrate. Every candidate goes in full, in one call.

    A name, a summary and a few facts are not the page. The model has to see
    the body before it says two things are the same.
    """
    hits = [
        item
        for item in (library_candidates or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    common = {
        "draft_crystal": draft_crystal,
        "owner_real_name": owner_real_name,
        "owner_aliases": owner_aliases,
    }
    packs = [_full_library_pack(item) for item in hits] if hits else library_candidates
    return _llm_integrate(text, kind, library_candidates=packs, **common)


def _library_for_match() -> list[dict]:
    """Active crystals as name/alias Match library. Owner 真名/别名 via _owner_labels."""
    handle = db()
    rows = handle.execute("SELECT * FROM crystallizations WHERE status='active'").fetchall()
    packed = []
    for row in rows:
        extra = _owner_labels() if row["id"] == "owner" else []
        packed.append(_pack_library_candidate(hydrate(row), extra))
    return packed


def _match_integrate_parts(text: str) -> dict:
    """Name and alias hits. A close vector is for the scheduled reconcile, not this pass."""
    return match_integrate_parts(text, _library_for_match())


def _project_library_hits(project_ref: str, text: str = "") -> list[dict]:
    """Existing 工程 pages that share a repo / project_ref. Does not invent pages."""
    refs: list[str] = []
    for raw in (project_ref, _repo_identity(project_ref), _repo_identity(text)):
        value = str(raw or "").strip()
        if value and value not in refs:
            refs.append(value)
    extras: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        row = _project_crystal(ref)
        if row is None:
            continue
        cid = str(row["id"] or "")
        if not cid or cid in seen:
            continue
        extras.append(_pack_library_candidate(hydrate(row)))
        seen.add(cid)
    return extras


def _align_project_candidates(text: str, integrate_kw: dict) -> dict:
    """Prepend existing project hits when source has project_ref / repo identity."""
    filled = dict(integrate_kw)
    extras = _project_library_hits(str(filled.get("project_ref") or ""), text)
    if not extras:
        return filled
    hits = [item for item in (filled.get("library_candidates") or []) if isinstance(item, dict)]
    seen = {str(item.get("id") or "") for item in hits}
    prepend = [item for item in extras if str(item.get("id") or "") not in seen]
    if prepend:
        filled["library_candidates"] = prepend + hits
    return filled


def _valid_library_candidates(library_candidates) -> bool:
    if not isinstance(library_candidates, list) or not library_candidates:
        return False
    return any(
        isinstance(item, dict) and str(item.get("id") or "").strip()
        for item in library_candidates
    )


def _draft_has_facts(draft_crystal) -> bool:
    if not isinstance(draft_crystal, dict):
        return False
    facts = draft_crystal.get("facts")
    return isinstance(facts, list) and any(isinstance(item, dict) and item for item in facts)


def _valid_integrate_user_parts(draft_crystal, library_candidates) -> bool:
    """Integrate v1.4 needs a draft crystal and at least one library id."""
    if not isinstance(draft_crystal, dict) or not draft_crystal:
        return False
    return _valid_library_candidates(library_candidates)


def _distill_known_pages(kind: str, project_ref: str) -> str:
    """A session about a repo sees that engineering page. Every call sees the owner page when it exists.

    The model reconciles the new material with those pages while writing. A file does not
    receive the engineering page. Other subjects are not preloaded.
    """
    blocks = []
    ref = (project_ref or "").strip()
    if kind in {"user", "agent", "summary"} and ref:
        try:
            row = _project_crystal(ref)
        except Exception:
            row = None
        body = (hydrate(row).body_md or "").strip() if row is not None else ""
        if body:
            blocks.append("已有工程结晶：\n" + _dated_page(row["id"], body))
            sketch = _owner_brief_for_distill()
            if sketch:
                blocks.append("记忆主人速写：\n" + sketch)
    owner_page = _owner_page_for_distill()
    if owner_page:
        blocks.append("已有记忆主人结晶：\n" + _dated_page("owner", owner_page))
    return "\n\n".join(blocks)


def _latest_event_at(crystal_id: str) -> str:
    """The latest time a statement already on this page was said. Empty if none was dated.

    A revision only counts when its clock is speech. A file's last write, and a
    stamp equal to the moment the row was stored, are not speech.
    """
    if not crystal_id:
        return ""
    try:
        con = db()
    except Exception:
        return ""
    found: list[tuple[datetime, str]] = []
    queries = (
        """SELECT st.event_at, src.event_basis, src.ingested_at
           FROM statements st LEFT JOIN sources src ON src.id = st.source_id
           WHERE st.crystal_id=? AND st.invalid_at IS NULL AND IFNULL(st.event_at,'')!=''""",
        """SELECT rev.event_at, src.event_basis, src.ingested_at
           FROM revisions rev LEFT JOIN sources src ON src.id = rev.source_id
           WHERE rev.crystal_id=? AND IFNULL(rev.event_at,'')!=''""",
    )
    for sql in queries:
        try:
            rows = con.execute(sql, (crystal_id,)).fetchall()
        except sqlite3.Error:
            continue
        for event_at, basis, ingested_at in rows:
            instant = _speech_instant(event_at, basis, ingested_at)
            if instant is None:
                continue
            found.append((instant, str(event_at or "").strip()))
    if not found:
        return ""
    found.sort(key=lambda item: item[0])
    return found[-1][1]


def _dated_page(crystal_id: str, body: str) -> str:
    from rogmem.prompts import page_event_line

    return page_event_line(_latest_event_at(crystal_id)) + "\n" + body


def _owner_page_for_distill() -> str:
    """The owner page itself, so the first writing pass can see what it already says."""
    try:
        row = db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    return (hydrate(row).body_md or "").strip()


def _owner_brief_for_distill() -> str:
    """Owner sketch for an engineering session. The full page is the fallback."""
    try:
        row = db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    crystal = hydrate(row)
    sketch = (getattr(crystal, "sketch_md", "") or "").strip()
    if sketch:
        return sketch
    return (crystal.body_md or "").strip()


@charging("初始蒸馏")
def _llm_draft(
    text: str,
    kind: str = "",
    *,
    filename: str = "",
    owner_real_name: str = "",
    owner_aliases: str = "",
    material_kind: str = "",
    project_ref: str = "",
    source_id: str = "",
    event_at: str = "",
    ingested_at: str = "",
    event_basis: str = "",
    owner_appellation: str | None = None,
) -> dict | None:
    """Call approved Draft when a distill target exists.

    No target raises AuthPause so the unit pauses instead of looking like
    an empty mid-稿. ``kind`` fills ``材料类型`` (会话/文件). ``project_ref``
    is injected into the assembled user payload; the approved prompt
    markdown is not edited.
    """
    target = llm_target("distill")
    if not target:
        raise AuthPause(MISSING_DISTILL_MODEL)
    from rogmem.pages_md import parse_pages
    from rogmem.prompts import distill_header, speaker_for

    system = _distill_system(kind)
    kind_label = material_kind or material_kind_label(kind)
    if owner_appellation is None:
        owner_appellation = _stored_owner_appellation()
    header = distill_header(
        material_kind=kind_label,
        speaker=speaker_for(kind),
        owner_name=owner_real_name,
        owner_aliases=owner_aliases,
        source_id=source_id,
        filename=filename,
        repo=project_ref,
        event_at=event_at,
        ingested_at=ingested_at,
        event_basis=event_basis,
        owner_appellation=owner_appellation,
    )
    context = _distill_known_pages(kind, project_ref)
    user = header + (("\n\n" + context) if context else "") + "\n\n" + redact_text(text)
    base, key, model = target
    if not str(key or "").strip():
        raise AuthPause(MISSING_PROVIDER_KEY)
    _touch_lease(source_id)
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    content, reason = _complete_chat(base, key, payload, role="distill")
    if not content and reason == "length":
        payload["max_tokens"] = max(int(payload["max_tokens"]), _chat_max_tokens(base, model))
        content, reason = _complete_chat(base, key, payload, role="distill")
    if not content:
        raise RuntimeError(f"模型没有给出正文（{reason or '空'}）")
    pages = parse_pages(content)
    if pages:
        return {"markdown_pages": pages}
    return _parse_model_json(content)


def _match0_new_raw() -> dict:
    """Programmatic identity=new. Match=0 does not call Integrate LLM."""
    return {"identity": "new", "ops": [], "conflicts": []}


def _is_match_zero(integrate_kw: dict) -> bool:
    return not _valid_library_candidates(integrate_kw.get("library_candidates"))


def _with_approved_draft(text: str, kind: str = "", **integrate_kw) -> dict:
    """Replace empty-facts stub with Draft v5.3 mid-稿s.

    Match=0 still drafts so cold-start can land identity=new. No mid-稿
    returns {} — caller fail-closes Match=0 as identity=new, Match≥1 as
    unwritten. Never extract.
    One Draft call. Keeps the full N ``library_candidates``.
    """
    inject = _prompt_injection(
        **{
            key: integrate_kw.get(key)
            for key in (
                "filename",
                "owner_real_name",
                "owner_aliases",
                "material_kind",
                "project_ref",
                "source_id",
                "event_at",
                "ingested_at",
                "event_basis",
            )
        },
        kind=kind,
    )
    filled = dict(integrate_kw)
    filled.update(inject)
    if _draft_has_facts(filled.get("draft_crystal")):
        filled["draft_crystals"] = [filled["draft_crystal"]]
        return filled
    produced = _llm_draft(
        text,
        kind,
        filename=filled["filename"],
        owner_real_name=filled["owner_real_name"],
        owner_aliases=filled["owner_aliases"],
        material_kind=filled.get("material_kind") or material_kind_label(kind),
        project_ref=str(filled.get("project_ref") or ""),
        source_id=str(filled.get("source_id") or ""),
        event_at=str(filled.get("event_at") or ""),
        ingested_at=str(filled.get("ingested_at") or ""),
        event_basis=str(filled.get("event_basis") or ""),
    )
    if isinstance(produced, dict) and produced.get("markdown_pages"):
        filled["markdown_pages"] = produced["markdown_pages"]
        return filled
    drafts = list_draft_crystals(produced)
    if not drafts:
        return {}
    filled["draft_crystals"] = drafts
    filled["draft_crystal"] = drafts[0]
    return filled


@charging("并页")
def _llm_integrate(
    text: str,
    kind: str = "",
    *,
    draft_crystal: dict | None = None,
    library_candidates: list | None = None,
    owner_real_name: str = "",
    owner_aliases: str = "",
) -> dict | None:
    """Call approved integrate v1.4 when a valid user payload can be assembled.

    No target, or no draft + library candidates, returns ``None`` so the
    caller fail-closes (unwritten / new). Stamp happens in
    ``_produce_integrate``.
    ``kind`` is kept for the extract-compatible call site; the approved
    prompt does not vary by source kind.
    """
    if not _valid_integrate_user_parts(draft_crystal, library_candidates):
        return None
    target = llm_target("distill")
    if not target:
        return None
    from rogmem.distill_resim import approved_integrate_prompts, fill_integrate_user
    from rogmem.prompts import owner_address

    system, user_tmpl = approved_integrate_prompts()
    system = with_kept_relations(system)
    name, aliases, extra = owner_address(owner_real_name, owner_aliases, _stored_owner_appellation())
    user = fill_integrate_user(
        user_tmpl,
        draft_crystal=draft_crystal,
        library_candidates=list(library_candidates),
        material=redact_text(text),
        owner_real_name=name,
        owner_aliases=aliases,
    )
    if extra:
        user = f"主人称呼：{extra}\n{user}"
    base, key, model = target
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    content, reason = _complete_chat(base, key, payload, role="distill")
    if not content and reason == "length":
        payload["max_tokens"] = max(int(payload["max_tokens"]), _chat_max_tokens(base, model))
        content, reason = _complete_chat(base, key, payload, role="distill")
    if not content:
        raise RuntimeError(f"模型没有给出正文（{reason or '空'}）")
    return _parse_model_json(content)


def _produce_integrate(raw) -> dict:
    """Production Distiller integrate produce boundary. Stamps before route."""
    payload, _issue = parse_integrate_payload(raw)
    if payload is None:
        payload = raw if isinstance(raw, dict) else {}
    return stamp_integrate_payload(payload)


_DRAFT_SIDECAR = "_draft_crystal"
_INTEGRATE_BATCH = "_integrate_batch"
_INTEGRATE_CALL_FAIL = "_integrate_call_failed"
_LIBRARY_SIDECAR = "_library_candidates"
_BA_PROMOTED = "_ba_promoted"
_BA_PROMOTE_FAIL = "_ba_promote_failed"
_BA_PROMOTE_CODE = "_ba_promote_code"
_BA_CARD_INJECTION = "card"
_DRAFT_RESERVED = frozenset(
    {
        "id",
        "type",
        "name",
        "aliases",
        "summary",
        "owner_relation",
        "body_md",
        "facts",
        "inferences",
        "status",
        "revision",
        "keys",
        "body_injection",
        "storage",
        "merged_into",
        "maybe_with",
        "possible_duplicate_of",
        "unresolved_conflicts",
        "deferred_retries",
        _DRAFT_SIDECAR,
        "payload_kind",
    }
)


def _peel_draft_sidecar(raw):
    """Pull produce→route Draft mid-稿 off the stamped payload. Not an identity sniff."""
    if not isinstance(raw, dict) or _DRAFT_SIDECAR not in raw:
        return raw, None
    cleaned = dict(raw)
    sidecar = cleaned.pop(_DRAFT_SIDECAR, None)
    if not isinstance(sidecar, dict) or not sidecar:
        return cleaned, None
    return cleaned, sidecar


def _peel_ba_sidecars(raw):
    """Pull B→A promote markers off the stamped payload before evaluate."""
    if not isinstance(raw, dict):
        return raw, False, None, None
    if not any(key in raw for key in (_BA_PROMOTED, _BA_PROMOTE_FAIL, _LIBRARY_SIDECAR, _BA_PROMOTE_CODE)):
        return raw, False, None, None
    cleaned = dict(raw)
    promoted = bool(cleaned.pop(_BA_PROMOTED, False))
    failed = cleaned.pop(_BA_PROMOTE_FAIL, None)
    code = cleaned.pop(_BA_PROMOTE_CODE, None)
    libs = cleaned.pop(_LIBRARY_SIDECAR, None)
    fail_code = None
    if failed:
        fail_code = str(code or "ba_promote_failed")
    hits = libs if isinstance(libs, list) else None
    return cleaned, promoted, hits, fail_code


def _unpromoted_same_on_b_cards(result, promoted: bool, lib_hits) -> bool:
    """True when same would land from B cards without an A-pack promote."""
    if promoted:
        return False
    if not isinstance(lib_hits, list) or not lib_hits:
        return False
    matched = str(getattr(result, "matched_id", None) or "")
    for item in lib_hits:
        if isinstance(item, dict) and str(item.get("id") or "") == matched and _is_a_pack(item):
            return False
    return any(_is_b_card(item) for item in lib_hits)


def _draft_thin_ops(draft: dict) -> list[dict]:
    """Map a Draft mid-稿 onto existing field / chapter / fact apply ops."""
    ops: list[dict] = []
    summary = str(draft.get("summary") or "").strip()
    if summary and summary not in {"暂无信息", "尚无来源", "暂无来源"}:
        ops.extend(field_update_ops("概述", summary))
    aliases = draft.get("aliases") or []
    if isinstance(aliases, list):
        alias_value = "，".join(str(item).strip() for item in aliases if str(item).strip())
    else:
        alias_value = str(aliases).strip()
    if alias_value:
        ops.extend(field_update_ops("别名", alias_value))
    relation = str(draft.get("owner_relation") or "").strip()
    if relation and relation not in {"暂无信息", "尚无来源", "暂无来源"}:
        ops.extend(field_update_ops("与记忆主人的关系", relation))
    body = str(draft.get("body_md") or "").strip()
    if body:
        ops.append({"op": "update", "path": "body_md", "value": body})
    for fact in draft.get("facts") or []:
        if isinstance(fact, dict) and str(fact.get("claim") or "").strip():
            ops.append({"op": "add", "path": "facts", "value": fact})
    for item in draft.get("inferences") or []:
        if isinstance(item, dict) and str(item.get("claim") or "").strip():
            ops.append({"op": "add", "path": "inferences", "value": item})
    keys = draft.get("keys") if isinstance(draft.get("keys"), dict) else {}
    extra = {**keys, **{key: draft[key] for key in draft if key not in _DRAFT_RESERVED}}
    for key, value in extra.items():
        if value in (None, "", [], {}, "暂无信息", "尚无来源", "暂无来源"):
            continue
        if isinstance(value, (dict, list)):
            continue
        ops.extend(field_update_ops(str(key), str(value)))
    return [op for op in ops if isinstance(op, dict) and apply_op_supported(op)]


_NEW_LAND_FAIL = frozenset(
    {"new_land_missing_draft", "new_land_write_switch_off", "new_land_failed"}
)
_BA_LAND_FAIL = frozenset(
    {
        "ba_promote_failed",
        "ba_promote_missing_full_pack",
        "ba_promote_write_switch_off",
        "ba_silent_stay_on_b",
    }
)
_CORE_MODULE_FAIL = frozenset({"core_module_ops_missing", "core_module_dropped"})
_FILE_NONFATAL = frozenset(
    {
        "all_facts_dropped",
        "unsupported_apply_path",
        "core_module_ops_missing",
        "core_module_dropped",
        "conflict_evidence_not_substring",
        "illegal_verdict",
    }
)


def _fail_integrate_new(result, code: str, message: str, *, decision: str = "rejected"):
    """Downgrade a gate accept so land failure cannot masquerade as success."""
    result.decision = decision
    result.issues = list(result.issues or [])
    from rogmem.distill_gate import GateIssue

    result.issues.append(GateIssue(code, message))
    return result


def _fail_integrate_call():
    """Integrate 调用失败/None：与 #79 land fail 同构，拒绝静默跳过中稿。"""
    from rogmem.distill_gate import GateIssue, GateResult

    return GateResult(
        decision="rejected",
        issues=[GateIssue("integrate_call_failed", "Integrate 调用失败，拒绝静默跳过中稿")],
    )


def _land_fail_hint(item) -> str:
    """Compact mid-稿 failure tags for last_error. Empty when nothing useful."""
    if item is None:
        return " [mid=None]"
    decision = getattr(item, "decision", None) or "?"
    identity = getattr(item, "identity", None) or "?"
    codes = sorted(
        {
            str(getattr(issue, "code", "") or "").strip()
            for issue in (getattr(item, "issues", None) or [])
            if str(getattr(issue, "code", "") or "").strip()
        }
    )
    name = ""
    payload = getattr(item, "payload", None)
    if isinstance(payload, dict):
        draft = payload.get("_draft_crystal") or payload.get("draft_crystal") or {}
        if isinstance(draft, dict):
            name = str(draft.get("name") or "").strip()
        if not name:
            name = str(payload.get("name") or "").strip()
    bits = [f"decision={decision}", f"identity={identity}"]
    if codes:
        bits.append("codes=" + ",".join(codes))
    if name:
        bits.append("name=" + name[:40])
    return " [" + "; ".join(bits) + "]"


def _batch_mid_skippable(item) -> bool:
    """Same-page mid with only all_facts_dropped: skip, do not sink the batch.

    Gate still rejects that mid (no unproven write). Sibling mids in the same
    multi-entity unit must still be allowed to finish.
    """
    if item is None:
        return False
    if getattr(item, "decision", None) != "rejected":
        return False
    codes = {
        str(getattr(issue, "code", "") or "").strip()
        for issue in (getattr(item, "issues", None) or [])
        if str(getattr(issue, "code", "") or "").strip()
    }
    return bool(codes) and codes <= _FILE_NONFATAL


def _raise_if_new_land_unwritten(result) -> None:
    """Surface unwritten integrate/land so pump_one cannot mark fake done.

    Single result: #79 new_land_* plus integrate_call_failed and core_module_*.
    Accept + identity=new still requires a non-empty matched_id (the new
    crystal id). Empty id after a successful-looking new is not done.
    Batch/list: any non-accept (rejected/deferred/retry) blocks done, except
    same-page mids rejected only for all_facts_dropped (nothing to write).
    identity=maybe is never accept: a maybe mid also blocks fake done.
    """
    if result is None:
        return
    if isinstance(result, (list, tuple)):
        for item in result:
            if item is None or getattr(item, "decision", None) != "accept":
                if _batch_mid_skippable(item):
                    continue
                raise RuntimeError(
                    "batch integrate/land not accepted; refuse silent done"
                    + _land_fail_hint(item)
                )
            if getattr(item, "identity", None) == "maybe":
                raise RuntimeError(
                    "identity=maybe deferred; refuse silent done" + _land_fail_hint(item)
                )
            if getattr(item, "identity", None) == "new" and not str(
                getattr(item, "matched_id", None) or ""
            ).strip():
                raise RuntimeError(
                    "identity=new land missing matched_id; refuse silent accept"
                    + _land_fail_hint(item)
                )
        return
    if getattr(result, "identity", None) == "maybe":
        raise RuntimeError("identity=maybe deferred; refuse silent done")
    decision = getattr(result, "decision", None)
    codes = {getattr(item, "code", "") for item in (getattr(result, "issues", None) or [])}
    fail_codes = _NEW_LAND_FAIL | _BA_LAND_FAIL | _CORE_MODULE_FAIL | {"integrate_call_failed"}
    if decision != "accept" and codes and codes <= _FILE_NONFATAL:
        return
    if decision != "accept" and codes & fail_codes:
        hit = ", ".join(sorted(code for code in codes & fail_codes if code))
        detail = f" [{hit}]" if hit else ""
        raise RuntimeError(f"integrate/land failed; refuse silent accept{detail}")
    if (
        decision == "accept"
        and getattr(result, "identity", None) == "new"
        and not str(getattr(result, "matched_id", None) or "").strip()
    ):
        raise RuntimeError("identity=new land missing matched_id; refuse silent accept")


def _existing_active_named(con, name: str, ctype: str = ""):
    """Reuse an already-active same-name page. Retry must not mint a second id."""
    if con is None or not name:
        return None
    if ctype:
        row = con.execute(
            "SELECT id FROM crystallizations WHERE name=? AND type=? AND status='active'",
            (name, ctype),
        ).fetchone()
        if row is not None:
            return str(row["id"] or "").strip() or None
    row = con.execute(
        "SELECT id FROM crystallizations WHERE name=? AND status='active'",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return str(row["id"] or "").strip() or None


def _draft_project_identity(draft, con=None, holder: str | None = None) -> str:
    """Repo URL or source project_ref on an 工程 mid-稿. Empty = no engineering identity."""
    if not isinstance(draft, dict):
        draft = {}
    keys = draft.get("keys") if isinstance(draft.get("keys"), dict) else {}
    fields = draft.get("key_fields") if isinstance(draft.get("key_fields"), dict) else {}
    parts = [
        draft.get("repo"),
        keys.get("仓库地址"),
        keys.get("repo"),
        fields.get("仓库地址"),
        fields.get("repo"),
    ]
    source = _holder_source(con, holder)
    if source is not None:
        parts.append(_source_project_ref(source))
    hay = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
    repo = _repo_identity(hay)
    if repo:
        return repo
    if source is not None:
        return _source_project_ref(source)
    return str(draft.get("repo") or keys.get("仓库地址") or "").strip()


def _land_integrate_new(draft, *, store, con, holder: str | None):
    """Open a thin active row from Draft mid-稿. Does not call _open_crystal.

    Same-name active already present (pump retry / re-apply) reuses that id.
    A second token_hex page would double-count the entity.
    工程 mid-稿s with a repo / project_ref reuse the existing engineering page.
    模型写成工程的，就按工程留下。旧名项目也按工程留下。
    """
    if not isinstance(draft, dict) or not draft or con is None:
        return None
    name = str(draft.get("name") or "").strip()
    ctype = _page_type(draft.get("type"))
    if not name:
        return None
    land_draft = draft
    if ctype == "工程":
        identity = _draft_project_identity(draft, con, holder)
        if identity:
            existing_proj = _project_crystal(identity, con)
            if existing_proj is not None:
                return str(existing_proj["id"]), _draft_thin_ops(draft)
        existing = _existing_active_named(con, name, ctype)
        if existing:
            return existing, _draft_thin_ops(draft)
        if not identity:
            existing = _existing_active_named(con, name, ctype)
            if existing:
                return existing, _draft_thin_ops(draft)
    else:
        if ctype == "人物" and _person_is_owner(name, draft.get("aliases")):
            return "owner", _draft_thin_ops(draft)
        existing = _existing_active_named(con, name, ctype)
        if existing:
            return existing, _draft_thin_ops(draft)
    cid = secrets.token_hex(8)
    _insert_crystal_page(con, cid, ctype, name, store=store, holder=holder)
    return cid, _draft_thin_ops(land_draft)


def _existing_deferred_named(con, name: str, ctype: str = ""):
    """Reuse an already-deferred same-name page. Cap must not mint a second id."""
    if con is None or not name:
        return None
    try:
        if ctype:
            row = con.execute(
                "SELECT id FROM crystallizations WHERE name=? AND type=? AND status='deferred'",
                (name, ctype),
            ).fetchone()
            if row is not None:
                return str(row["id"] or "").strip() or None
        row = con.execute(
            "SELECT id FROM crystallizations WHERE name=? AND status='deferred'",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row["id"] or "").strip() or None


def _draft_thin_crystal(
    cid: str,
    draft: dict,
    *,
    status: str,
    maybe_with: str | None,
    deferred_retries: int,
) -> ThinCrystal:
    """Mid-稿 → 薄信封。不经 apply_ops / sidecar。"""
    facts = []
    for item in draft.get("facts") or []:
        if isinstance(item, dict) and str(item.get("claim") or "").strip():
            facts.append(
                Fact(
                    claim=str(item.get("claim") or "").strip(),
                    evidence=str(item.get("evidence") or "").strip(),
                    anchor=str(item.get("anchor") or "").strip(),
                    valid_until=parse_valid_until(item.get("valid_until")),
                )
            )
    inferences = []
    for item in draft.get("inferences") or []:
        if isinstance(item, dict) and str(item.get("claim") or "").strip():
            inferences.append(
                Inference(
                    claim=str(item.get("claim") or "").strip(),
                    basis=str(item.get("basis") or "").strip(),
                    anchor=str(item.get("anchor") or "").strip(),
                )
            )
    aliases = draft.get("aliases") or []
    if isinstance(aliases, list):
        alias_list = [str(item).strip() for item in aliases if str(item).strip()]
    else:
        alias_list = [
            part.strip()
            for part in str(aliases).replace("，", ",").split(",")
            if part.strip()
        ]
    return ThinCrystal(
        id=cid,
        type=_page_type(draft.get("type")),
        name=str(draft.get("name") or "").strip(),
        status=status,
        revision=0,
        summary=str(draft.get("summary") or ""),
        aliases=alias_list,
        owner_relation=str(draft.get("owner_relation") or "暂无信息") or "暂无信息",
        body_md=str(draft.get("body_md") or ""),
        facts=facts,
        inferences=inferences,
        maybe_with=maybe_with or None,
        deferred_retries=int(deferred_retries or 0),
    )


def _lookup_deferred_id(store, con, draft, crystal_id: str) -> str:
    """Find the deferred page for this mid. Never treat an active candidate as the page."""
    draft_id = str((draft or {}).get("id") or "").strip() if isinstance(draft, dict) else ""
    getter = getattr(store, "get", None)
    for cid in (draft_id, str(crystal_id or "").strip()):
        if not cid or not callable(getter):
            continue
        row = getter(cid)
        if row is not None and getattr(row, "status", None) == "deferred":
            return cid
    name = str((draft or {}).get("name") or "").strip() if isinstance(draft, dict) else ""
    ctype = _page_type((draft or {}).get("type")) if isinstance(draft, dict) else ""
    return _existing_deferred_named(con, name, ctype) or ""


def _bump_or_land_deferred(
    draft,
    *,
    store,
    con,
    holder: str | None,
    crystal_id: str,
    maybe_with: str | None,
    commit: bool = True,
):
    """Land or increment a deferred thin page. Cap stays deferred; no second page."""
    if not isinstance(draft, dict) or not str(draft.get("name") or "").strip():
        return None
    put = getattr(store, "put", None)
    if not callable(put):
        return None
    existing = _lookup_deferred_id(store, con, draft, crystal_id)
    if existing:
        crystal = store.get(existing)
        retries = int(getattr(crystal, "deferred_retries", 0) or 0)
        if retries >= DEFERRED_RETRY_LIMIT:
            return crystal
        extra = {"deferred_retries": retries + 1}
        if maybe_with:
            extra["maybe_with"] = maybe_with
        transition = getattr(store, "transition", None)
        if not callable(transition):
            return crystal
        return transition(existing, "deferred", extra=extra, holder=holder, commit=commit)
    cid = secrets.token_hex(8)
    page = _draft_thin_crystal(
        cid, draft, status="draft", maybe_with=None, deferred_retries=0
    )
    put(page, holder=holder, commit=False)
    transition = getattr(store, "transition", None)
    extra = {"deferred_retries": 1}
    if maybe_with:
        extra["maybe_with"] = maybe_with
    if callable(transition):
        return transition(cid, "deferred", extra=extra, holder=holder, commit=commit)
    page.status = "deferred"
    page.maybe_with = maybe_with
    page.deferred_retries = 1
    return put(page, holder=holder, commit=commit)


def _apply_integrate_maybe(
    result,
    *,
    draft,
    store,
    crystal_id: str,
    holder: str | None,
    commit: bool = True,
):
    """identity=maybe → decision=deferred. Optional deferred page. Zero active / ops."""
    from rogmem import crystal_store as crystal_store_mod
    from rogmem.distill_gate import GateIssue

    result.decision = "deferred"
    result.issues = list(result.issues or [])
    if not any(getattr(item, "code", "") == "identity_maybe_deferred" for item in result.issues):
        result.issues.append(
            GateIssue(
                "identity_maybe_deferred",
                "identity=maybe 进 deferred 全自动消化，不改 active",
            )
        )
    if not isinstance(draft, dict) or not str(draft.get("name") or "").strip():
        return result
    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED:
        return result
    target = store
    con = None
    if target is not None and getattr(target, "con", None) is not None:
        con = target.con
    elif _DB is not None:
        con = db()
        if target is None:
            target = CrystalStore(con)
    if target is None or not callable(getattr(target, "put", None)):
        return result
    try:
        _bump_or_land_deferred(
            draft,
            store=target,
            con=con if con is not None else getattr(target, "con", None),
            holder=holder or "integrate:maybe",
            crystal_id=crystal_id or "",
            maybe_with=str(result.matched_id or "").strip() or None,
            commit=commit,
        )
    except IntegrateWriteNotLive:
        return result
    return result


def _approved_drafts(integrate_kw: dict) -> list[dict]:
    """Mid-稿s after one Draft call. Injected single draft_crystal still counts as one."""
    packed = integrate_kw.get("draft_crystals")
    if isinstance(packed, list) and packed:
        return [item for item in packed if isinstance(item, dict) and item]
    one = integrate_kw.get("draft_crystal")
    if isinstance(one, dict) and one:
        return [one]
    return []


def _unwritten_integrate_payload(draft) -> dict:
    """Keep a mid-稿 that failed Integrate. Not an extract fallback."""
    payload = {"payload_kind": PAYLOAD_KIND_INTEGRATE, _INTEGRATE_CALL_FAIL: True}
    if isinstance(draft, dict) and draft:
        payload[_DRAFT_SIDECAR] = draft
    return payload


def _produce_distill_payload(text: str, kind: str = "", **integrate_kw) -> dict:
    """Production Distiller produce. One Draft; each mid-稿 gets Integrate.

    Match=0 stamps identity=new without calling Integrate LLM, then lands
    via apply / ``_land_integrate_new``. A single mid-稿 returns the same
    stamped payload as #79. Two or more wrap already-stamped items under
    ``_integrate_batch``; route still only looks at ``payload_kind``. A
    real mid whose Integrate returns None is fail-closed (not silently
    dropped, not digested as extract). Match=0 without a mid also
    fail-closes as identity=new — not extract. Match≥1 without a
    non-empty mid is unwritten — not extract.
    """
    integrate_kw = _align_project_candidates(text, integrate_kw)
    match_zero = _is_match_zero(integrate_kw)
    integrate_kw = _with_approved_draft(text, kind, **integrate_kw)
    pages = integrate_kw.get("markdown_pages")
    if pages:
        return {"payload_kind": "markdown", "pages": pages}
    drafts = _approved_drafts(integrate_kw)
    had_mids = bool(drafts)
    common = {
        key: integrate_kw[key]
        for key in ("library_candidates", "owner_real_name", "owner_aliases")
        if key in integrate_kw
    }
    payloads = []
    if not drafts:
        if match_zero:
            return _produce_integrate(_match0_new_raw())
        return _unwritten_integrate_payload(None)
    for draft in drafts:
        if match_zero:
            raw = _match0_new_raw()
        elif draft is None:
            raw = _llm_integrate(
                text,
                kind,
                **{
                    key: integrate_kw[key]
                    for key in ("draft_crystal", "library_candidates", "owner_real_name", "owner_aliases")
                    if key in integrate_kw
                },
            )
            draft = integrate_kw.get("draft_crystal")
        else:
            raw = _complete_integrate_mid(text, kind, draft_crystal=draft, **common)
        if raw is None:
            if had_mids or match_zero:
                payloads.append(_unwritten_integrate_payload(draft))
            continue
        produced = _produce_integrate(raw)
        if isinstance(produced, dict):
            produced = dict(produced)
            if isinstance(raw, dict):
                for key in (_BA_PROMOTED, _BA_PROMOTE_FAIL, _BA_PROMOTE_CODE, _LIBRARY_SIDECAR):
                    if key in raw:
                        produced[key] = raw[key]
            if isinstance(draft, dict) and draft:
                produced[_DRAFT_SIDECAR] = draft
        payloads.append(produced)
    if len(payloads) == 1:
        return payloads[0]
    if len(payloads) > 1:
        return {"payload_kind": PAYLOAD_KIND_INTEGRATE, _INTEGRATE_BATCH: payloads}
    return _unwritten_integrate_payload(None)


def _fail_ba_promote_call(code: str = "ba_promote_failed"):
    """Promote/land miss: same shape as integrate_call_failed. No silent accept."""
    from rogmem.distill_gate import GateIssue, GateResult

    decision = "deferred" if code == "ba_promote_write_switch_off" else "rejected"
    return GateResult(
        decision=decision,
        issues=[GateIssue(code, "B→A 升格失败，拒绝伪装 accept")],
    )


def _enter_integrate(source: sqlite3.Row, data: dict, body: str, *, commit: bool = True):
    """Apply an already-stamped integrate payload. Flatten only; do not first-stamp."""
    if isinstance(data, dict) and data.get(_INTEGRATE_CALL_FAIL):
        return _fail_integrate_call()
    if isinstance(data, dict) and data.get(_BA_PROMOTE_FAIL):
        return _fail_ba_promote_call(str(data.get(_BA_PROMOTE_CODE) or "ba_promote_failed"))
    payload, _issue = parse_integrate_payload(data)
    if payload is None:
        payload = data
    payload, sidecar = _peel_draft_sidecar(payload)
    if sidecar is None and isinstance(data, dict):
        extra = data.get(_DRAFT_SIDECAR)
        if isinstance(extra, dict) and extra:
            sidecar = extra
    if isinstance(data, dict) and isinstance(payload, dict):
        payload = dict(payload)
        for key in (_BA_PROMOTED, _LIBRARY_SIDECAR):
            if key in data:
                payload[key] = data[key]
    matched = _integrate_matched_id(payload)
    return apply_integrate(
        payload,
        candidates=[matched] if matched else [],
        corpus=body,
        crystal_id=matched,
        holder=f"distill:{source['id']}",
        draft_crystal=sidecar,
        commit=commit,
    )


def _integrate_batch_items(data):
    """Unpack N stamped integrate payloads. Not an identity sniff."""
    if not isinstance(data, dict):
        return None
    raw = data.get(_INTEGRATE_BATCH)
    if not isinstance(raw, list) or not raw:
        return None
    return [item for item in raw if isinstance(item, dict)]


def _batch_rejected(results) -> bool:
    try:
        _raise_if_new_land_unwritten(results)
    except Exception:
        return True
    return False


def _land_integrate_batch(source: sqlite3.Row, items: list, body: str):
    """Land every mid of one source together.

    A later miss rolls back the earlier writes from this batch, so a retry
    does not land the successful ones a second time.
    """
    with LOCK:
        con = db()
        results = []
        try:
            for item in items:
                results.append(_enter_integrate(source, item, body, commit=False))
        except Exception:
            con.rollback()
            raise
        if _batch_rejected(results):
            con.rollback()
            return results
        con.commit()
    return results


def _route_distill_payload(source: sqlite3.Row, data: dict, body: str):
    """Route by stamped payload_kind. Does not stamp. No ops+identity sniff."""
    if _declared_payload_kind(data) == PAYLOAD_KIND_INTEGRATE:
        items = _integrate_batch_items(data)
        if items is not None:
            return _land_integrate_batch(source, items, body)
        return _enter_integrate(source, data, body)
    raise RuntimeError("逐字段提取已停用，材料要交成整页")


def _op_statement_slot(op: dict) -> tuple[str | None, str | None]:
    """Map apply op path to (field, chapter) — inverse of field/chapter_update_ops."""
    path = str(op.get("path") or "").strip()
    if path.startswith("body_md##"):
        heading = path[9:].strip()
        return None, heading or None
    envelope_field = {"summary": "概述", "aliases": "别名", "owner_relation": "与记忆主人的关系"}
    if path in envelope_field:
        return envelope_field[path], None
    if path.startswith("keys."):
        key = path[5:].strip()
        return key or None, None
    return None, None


def _holder_source(con, holder: str | None):
    if not holder or ":" not in str(holder) or con is None:
        return None
    source_id = str(holder).split(":", 1)[1]
    if not source_id:
        return None
    try:
        return con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    except Exception:
        return None


def _write_ops_sidecar(con, crystal_id: str, ops: list[dict], *, holder: str | None) -> None:
    """Accept sidecar: statements / edges / revision. Same primitives as _write_*."""
    source = _holder_source(con, holder)
    source_id = source["id"] if source is not None else (holder or "")
    rank = rank_of(source) if source is not None else "file"
    event_at = source["event_at"] if source is not None else utcnow()
    crystal = None
    try:
        crystal = con.execute(
            "SELECT id, type FROM crystallizations WHERE id=?",
            (crystal_id,),
        ).fetchone()
    except Exception:
        crystal = None
    crystal_type = crystal["type"] if crystal is not None else ""
    for op in ops:
        if not isinstance(op, dict):
            continue
        field, chapter = _op_statement_slot(op)
        if rank in {"agent", "summary"} and _restricted_agent_slot(field, chapter):
            continue
        fact = op.get("fact") if isinstance(op.get("fact"), dict) else {}
        value = op.get("value")
        value_evidence = value.get("evidence") if isinstance(value, dict) else ""
        if isinstance(value, dict):
            text = str(fact.get("claim") or value.get("claim") or "").strip()
        else:
            text = str(fact.get("claim") or value or "").strip()
        quote = str(
            fact.get("evidence")
            or value_evidence
            or op.get("evidence")
            or op.get("quote")
            or ""
        ).strip()
        if not text and not quote:
            continue
        written = text or quote
        cited = quote or text
        until = None if _is_agree_slot(field, chapter) else parse_valid_until(fact.get("valid_until"))
        sid = _add_statement(
            con,
            crystal_id,
            field,
            chapter,
            written,
            cited,
            source_id,
            rank,
            "written",
            event_at,
            valid_until=until,
        )
        _link_relations(con, sid)
        if field:
            _sidecar_field_edges(con, crystal_id, crystal_type, field, written)
        _record_revision(
            con,
            crystal_id,
            f"{field or chapter or 'op'}:{written}"[:80],
            source_id,
            cited,
            event_at,
            sid,
            gist=str(op.get("gist") or "") or str(op.get("reason") or ""),
        )
        if source is not None:
            _maybe_dual_land_engineering(
                con,
                source,
                crystal_id=crystal_id,
                crystal_type=crystal_type,
                field=field,
                chapter=chapter,
                text=written,
                quote=cited,
                event_at=event_at,
                holder=holder,
            )


def apply_integrate(
    raw,
    *,
    candidates,
    corpus: str = "",
    chunks=None,
    crystal_id: str = "",
    holder: str | None = None,
    store=None,
    commit: bool = True,
    draft_crystal=None,
):
    """Unified integrate pipeline: evaluate → persist unresolved → accept apply.

    ``evaluate_integrate`` stays in memory. This engine entry is the production
    Distiller / distill_resim / worker path. Accept persists unresolved (empty
    list clears) and then applies gate ops via ``_apply_then_sidecar``.
    persist / apply / sidecar share one try. reject / deferred / retry do not write.
    identity=new accept opens a thin active row from the Draft mid-稿; land
    failure is rejected/deferred (not a silent accept). On success the new
    crystal id is written back to ``result.matched_id``. identity=maybe is
    remapped to ``decision=deferred`` and may land a deferred thin page
    (``maybe_with`` / ``deferred_retries``); it never writes active or ops.
    same still only mutates matched_id.
    """
    from rogmem import crystal_store as crystal_store_mod
    from rogmem.distill_gate import evaluate_integrate, persist_unresolved

    eval_raw, sidecar_draft = _peel_draft_sidecar(raw)
    eval_raw, promoted, lib_hits, promote_fail = _peel_ba_sidecars(eval_raw)
    draft = draft_crystal if isinstance(draft_crystal, dict) and draft_crystal else sidecar_draft
    if promote_fail:
        from rogmem.distill_gate import GateResult

        matched = ""
        if isinstance(eval_raw, dict):
            matched = str(eval_raw.get("matched_id") or "")
        failed = GateResult(decision="rejected", identity="same", matched_id=matched or None)
        return _fail_integrate_new(
            failed,
            str(promote_fail),
            "B→A 升格失败，拒绝伪装 accept",
        )
    result = evaluate_integrate(eval_raw, candidates=candidates, corpus=corpus, chunks=chunks)
    if result.decision != "accept":
        return result
    if result.identity == "maybe":
        with LOCK:
            return _apply_integrate_maybe(
                result,
                draft=draft,
                store=store,
                crystal_id=crystal_id,
                holder=holder,
                commit=commit,
            )
    if result.identity == "same" and _unpromoted_same_on_b_cards(result, promoted, lib_hits):
        return _fail_integrate_new(
            result,
            "ba_silent_stay_on_b",
            "same 后仍用 B 档卡片出 ops，拒绝静默留 B",
        )
    with LOCK:
        con = None
        if store is not None and getattr(store, "con", None) is not None:
            con = store.con
        elif _DB is not None:
            con = db()
        target_store = store if store is not None else (CrystalStore(con) if con is not None else None)
        if target_store is None:
            if result.identity == "new":
                return _fail_integrate_new(
                    result, "new_land_failed", "identity=new 无 CrystalStore，拒绝伪装 accept"
                )
            return result
        apply_con = con if con is not None else getattr(target_store, "con", None)
        target_id = crystal_id or result.matched_id or ""
        ops = [
            op
            for op in ((result.payload or {}).get("ops") or [])
            if isinstance(op, dict) and apply_op_supported(op)
        ]
        use_holder = holder or (f"integrate:{target_id}" if target_id else "integrate:new")
        if result.identity == "same" and not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED:
            return _fail_integrate_new(
                result,
                "ba_promote_write_switch_off",
                "写开关关闭，不得落库也不得伪装 accept",
                decision="deferred",
            )
        if promoted and result.identity == "same":
            if target_store is None or apply_con is None:
                return _fail_integrate_new(
                    result, "ba_promote_failed", "B→A 升格无 CrystalStore，拒绝伪装 accept"
                )
        if result.identity == "new":
            if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED:
                return _fail_integrate_new(
                    result,
                    "new_land_write_switch_off",
                    "写开关关闭，identity=new 不得落库也不得伪装 accept",
                    decision="deferred",
                )
            if not isinstance(draft, dict) or not str(draft.get("name") or "").strip():
                return _fail_integrate_new(
                    result,
                    "new_land_missing_draft",
                    "identity=new 缺少 Draft 中稿，拒绝伪装 accept",
                )
            if apply_con is None:
                return _fail_integrate_new(
                    result, "new_land_failed", "identity=new 无共享连接，拒绝伪装 accept"
                )
            try:
                landed = _land_integrate_new(
                    draft,
                    store=target_store,
                    con=apply_con,
                    holder=use_holder,
                )
            except IntegrateWriteNotLive:
                return _fail_integrate_new(
                    result,
                    "new_land_write_switch_off",
                    "写开关关闭，identity=new 不得落库也不得伪装 accept",
                    decision="deferred",
                )
            if landed is None:
                return _fail_integrate_new(
                    result, "new_land_failed", "identity=new 薄建失败，拒绝伪装 accept"
                )
            target_id, ops = landed
            if not str(target_id or "").strip():
                return _fail_integrate_new(
                    result, "new_land_failed", "identity=new 薄建失败，拒绝伪装 accept"
                )
            result.matched_id = str(target_id)
            if isinstance(result.payload, dict):
                result.payload = dict(result.payload)
                result.payload["matched_id"] = result.matched_id
            use_holder = holder or f"integrate:{target_id}"
        elif not target_id:
            return result

        def persist() -> None:
            persist_unresolved(
                target_store,
                target_id,
                result,
                holder=use_holder,
                commit=False,
            )

        if not ops:
            persist_unresolved(
                target_store,
                target_id,
                result,
                holder=use_holder,
                commit=commit,
            )
            return result
        if apply_con is None:
            raise RuntimeError(
                "apply_integrate: ops present but no shared connection; "
                "refuse silent persist/apply skip"
            )
        expected_revision = 0
        getter = getattr(target_store, "get", None)
        existing_body = ""
        if callable(getter):
            crystal = getter(target_id)
            if crystal is not None:
                expected_revision = int(getattr(crystal, "revision", 0) or 0)
                existing_body = str(getattr(crystal, "body_md", "") or "")
        if result.identity == "same":
            core_issues = check_core_module_complete(
                result.payload or {"identity": "same", "ops": ops},
                draft=draft,
                existing_body_md=existing_body,
            )
            if core_issues:
                result.decision = "rejected"
                result.issues = list(result.issues or []) + core_issues
                return result
        ops = _filter_restricted_agent_ops(apply_con, ops, use_holder)
        injected = store is not None and getattr(store, "con", None) is not apply_con

        def sidecar() -> None:
            _write_ops_sidecar(apply_con, target_id, ops, holder=use_holder)

        _apply_then_sidecar(
            target_store,
            apply_con,
            crystal_id=target_id,
            expected_revision=expected_revision,
            ops=ops,
            holder=use_holder,
            sidecar=sidecar,
            injected=injected,
            before=persist,
            commit=commit,
        )
    return result


def _apply_items(source: sqlite3.Row, items: list[dict], critic: dict, sheet=None, unit_body: str | None = None) -> int:
    items, sheet = _rebind_claims(source, items, sheet)
    body = unit_body if unit_body is not None else (source["body"] or "")
    if source["kind"] == "file":
        items = _land_file_facts(source, items, body)
        if not isinstance(sheet, list):
            sheet = []
        sheet = _cover_kept_crystals(sheet, items)
        sheet = _apply_landed_to_sheet(sheet, items)
    report = _require_publish_sheet(source, items, critic, sheet, body, unit_body=body)
    rank = {"user": "user", "agent": "agent", "file": "file", "summary": "summary"}.get(source["kind"], "file")
    full = source["body"] or ""
    used = 0
    for item in items:
        quote = item.get("quote") or ""
        scope = _item_scope(item, body, full)
        if quote and not _quote_in_scope(quote, scope):
            continue
        op = item.get("op")
        if op == "open":
            used += 1
            _open_crystal(source, item, critic, scope)
        elif op == "field":
            used += 1
            _write_field(source, item, rank, scope)
        elif op == "chapter":
            used += 1
            _write_chapter(source, item, scope)
    if report and report.residual:
        with LOCK:
            db().execute("UPDATE sources SET residual=? WHERE id=?", (report.residual, source["id"]))
            db().commit()
    return used


def _source_sentences(body: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？\n]", body or "") if part.strip()]


def _uncovered_sentences(body: str, quotes: list[str]) -> list[str]:
    covered = [quote for quote in quotes if quote]
    kept = []
    for part in _source_sentences(body):
        if len(part) < 8 or any(part in quote for quote in covered):
            continue
        kept.append(part)
    return kept


def _copies_uncovered(body: str, quote: str, value: str) -> bool:
    return any(part in (value or "") for part in _uncovered_sentences(body, [quote]))


def _section_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？\n]", text or "") if part.strip()]


def _ordered_embed(prev: str, value: str) -> bool:
    """The new section keeps the old sentences in order and does not only add a suffix."""
    old = _section_sentences(prev)
    new = _section_sentences(value)
    if not old or len(new) <= len(old):
        return False
    start = 0
    for sentence in old:
        try:
            at = new.index(sentence, start)
        except ValueError:
            return False
        start = at + 1
    return new[: len(old)] != old


def _source_appended(prev: str, value: str, body: str) -> bool:
    """True when this version only pastes the source after the old section."""
    if not prev or prev in {"", "暂无来源"}:
        return False
    old = _section_sentences(prev)
    new = _section_sentences(value)
    if old and len(new) > len(old) and new[: len(old)] == old:
        return all(part in body for part in new[len(old) :])
    if value.startswith(prev):
        tail = value[len(prev) :].strip()
        return bool(tail) and tail in body
    return False


def _inline_marks(text: str) -> list[str]:
    found = []
    for line in (text or "").splitlines():
        if re.match(r"^\[\^([^\]]+)\]:\s*", line.strip()):
            continue
        found.extend(re.findall(r"\[\^([^\]]+)\]", line))
    return found


def _crystal_links(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"\[\[([^\[\]\n]+)\]\]", text or "")]


def _citation_cover(page: str, *sources: str) -> bool:
    """Every citation mark and its passage is still on the page."""
    marks = set(_inline_marks(page))
    from rogmem.pages_md import citation_passage, cite_notes

    kept = cite_notes(page)
    for source in sources:
        if not set(_inline_marks(source)) <= marks:
            return False
        for note in cite_notes(source).values():
            passage = citation_passage(note)
            if passage and passage not in page and not any(passage in (item or "") for item in kept.values()):
                return False
    return True


def _link_cover(page: str, *sources: str) -> bool:
    links = set(_crystal_links(page))
    return all(set(_crystal_links(source)) <= links for source in sources)


def _link_name(raw: str) -> str:
    """The page a crystal link points at. The part after ``|`` is only how it is shown."""
    text = (raw or "").strip()
    head, _, _shown = text.partition("|")
    return (head or text).strip()


def _relations_cover(page: str, *sources: str) -> bool:
    """The rewritten page still carries every citation and crystal link from the pages it came from."""
    return _citation_cover(page, *sources) and _link_cover(page, *sources)


def _relation_explanation(gist: str, conflicts: list) -> str:
    """Gist and basis are where a withdrawal is named. The conflict subject is not that list."""
    parts = [str(gist or "")]
    for item in conflicts or []:
        if not isinstance(item, dict):
            continue
        parts.append(str(item.get("basis") or ""))
    return "\n".join(part for part in parts if part.strip())


def _mentions_apart(name: str, explanation: str, siblings: set[str]) -> bool:
    """True when this name appears as itself, not only inside a longer one."""
    if not name or not explanation or name not in explanation:
        return False
    start = 0
    while True:
        at = explanation.find(name, start)
        if at < 0:
            return False
        inside = False
        for other in siblings:
            if len(other) <= len(name) or name not in other:
                continue
            scan = 0
            while True:
                found = explanation.find(other, scan)
                if found < 0:
                    break
                if found <= at and at + len(name) <= found + len(other):
                    inside = True
                    break
                scan = found + 1
            if inside:
                break
        if not inside:
            return True
        start = at + len(name)


def _free_mark(used: set[str]) -> str:
    number = 1
    while str(number) in used:
        number += 1
    return str(number)


def _attach_mark(text: str, passage: str, key: str) -> tuple[str, bool]:
    """Put the mark on the sentence that still says this, not on a footnote line."""
    lines = []
    attached = False
    for line in text.splitlines():
        if not attached and not re.match(r"^\[\^[^\]]+\]:", line.strip()) and passage in line:
            line = line.replace(passage, f"{passage}[^{key}]", 1)
            attached = True
        lines.append(line)
    return "\n".join(lines), attached


def _keep_unnamed_relations(page: str, explanation: str, *sources: str, hang_unmatched: bool = True) -> str:
    """Keep a citation on the sentence that still says it.

    A revision that does not name the dropped relation has not withdrawn it.
    When the passage is still in a sentence, the mark goes back on that sentence.
    A mark or link with no sentence is hung at the end only for a page that is not
    a revision of the stored one. A revision leaves it off rather than inventing a line.
    """
    from rogmem.pages_md import citation_passage, cite_notes

    text = (page or "").strip()
    if not text:
        return text
    named = explanation or ""
    link_names = {_link_name(link) for source in sources for link in _crystal_links(source)}
    passages = {
        citation_passage(note)
        for source in sources
        for note in cite_notes(source).values()
        if citation_passage(note)
    }
    seen_links = {_link_name(link) for link in _crystal_links(text)}
    seen_marks = set(_inline_marks(text))
    note_body = "\n".join(cite_notes(text).values())
    links: list[str] = []
    marks: list[str] = []
    notes: list[str] = []
    for source in sources:
        for link in _crystal_links(source):
            name = _link_name(link)
            if not name or name in seen_links or _mentions_apart(name, named, link_names):
                seen_links.add(name)
                continue
            seen_links.add(name)
            if hang_unmatched:
                links.append(link)
        for key, note in cite_notes(source).items():
            passage = citation_passage(note)
            if not passage or _mentions_apart(passage, named, passages):
                continue
            if passage in note_body:
                continue
            use = key if key not in seen_marks else _free_mark(seen_marks)
            text, attached = _attach_mark(text, passage, use)
            if not attached:
                if not hang_unmatched:
                    continue
                marks.append(f"[^{use}]")
            line = f"[^{use}]: {note}"
            if line not in notes:
                notes.append(line)
            note_body += "\n" + passage
            seen_marks.add(use)
    if not links and not marks and not notes:
        return text
    tail = []
    head = " ".join(marks + [f"[[{link}]]" for link in links]).strip()
    if head:
        tail.append(head)
    tail.extend(notes)
    return text + "\n\n" + "\n".join(tail)


_H2_LINE = re.compile(r"^## (?!#)(.*)$")
_NOTE_DEF_LINE = re.compile(r"^\[\^[^\]]+\][:：]")
_WORD_CHAR = re.compile(r"[^\W_]", re.UNICODE)


def _is_note_definition(line: str) -> bool:
    return bool(_NOTE_DEF_LINE.match((line or "").strip()))


def _is_notation_only(line: str) -> bool:
    """True when removing marks and crystal links leaves no words.

    A trailing period or other punctuation is not a paragraph. Neither is a note
    definition, including one written with a fullwidth colon.
    """
    stripped = (line or "").strip()
    if not stripped or _is_note_definition(stripped):
        return False
    leftover = re.sub(r"\[\[[^\[\]\n]+\]\]", "", re.sub(r"\[\^[^\]]+\]", "", stripped))
    return _WORD_CHAR.search(leftover) is None


def _fact_sections(text: str) -> list[tuple[str, list[str]]]:
    """Headings already on the page, each with its blank-line paragraphs.

    The lead before the first heading has an empty heading. Note definitions and
    lines that are only marks or crystal links are not paragraphs.
    """
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    paragraphs: list[str] = []
    current: list[str] = []
    seen = False

    def flush_paragraph() -> None:
        if current:
            paragraphs.append("\n".join(current).strip())
            current.clear()

    def flush_section() -> None:
        nonlocal heading, paragraphs, seen
        flush_paragraph()
        if seen or paragraphs or heading:
            sections.append((heading, paragraphs))
        heading = ""
        paragraphs = []
        seen = False

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        match = _H2_LINE.match(line.strip())
        if match:
            flush_section()
            heading = match.group(1).strip()
            seen = True
            continue
        if not line.strip():
            flush_paragraph()
            continue
        if _is_note_definition(line) or _is_notation_only(line):
            flush_paragraph()
            continue
        current.append(line.strip())
    flush_section()
    folded: list[tuple[str, list[str]]] = []
    index: dict[str, int] = {}
    for title, paras in sections:
        if title in index:
            at = index[title]
            folded[at] = (title, folded[at][1] + list(paras))
            continue
        if not title and not paras:
            continue
        index[title] = len(folded)
        folded.append((title, list(paras)))
    return folded


def _claim_text(paragraph: str) -> str:
    return re.sub(r"\[\^[^\]]+\]", "", paragraph or "").strip()


def _strip_named_relations(
    paragraph: str,
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> str:
    """Drop links and marks the explanation names. Other words stay where they were."""
    from rogmem.pages_md import citation_passage

    text = paragraph or ""

    def drop_mark(match: re.Match) -> str:
        passage = citation_passage(notes.get(match.group(1), ""))
        if passage and _mentions_apart(passage, explanation, passages):
            return ""
        return match.group(0)

    text = re.sub(r"\[\^([^\]]+)\]", drop_mark, text)
    for raw in _crystal_links(text):
        name = _link_name(raw)
        if name and _mentions_apart(name, explanation, link_names):
            text = text.replace(f"[[{raw}]]", "", 1)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _stable_words(
    paragraph: str,
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> str:
    """The unnamed words, with marks and named links taken out of the comparison."""
    return re.sub(r"\s+", "", _claim_text(_strip_named_relations(paragraph, explanation, link_names, passages, notes)))


def _graft_unnamed_marks(
    host: str,
    old: str,
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> str:
    """The model kept the old words and added more. Unnamed marks stay on those words."""
    from rogmem.pages_md import citation_passage

    literal = _claim_text(_strip_named_relations(old, explanation, link_names, passages, notes)).strip()
    text = host or ""
    if not literal:
        return text
    found = text.find(literal)
    if found < 0:
        return text
    insert_at = found + len(literal)
    missing = []
    for key in re.findall(r"\[\^([^\]]+)\]", old or ""):
        token = f"[^{key}]"
        if token in text:
            continue
        passage = citation_passage(notes.get(key, ""))
        if passage and _mentions_apart(passage, explanation, passages):
            continue
        missing.append(token)
    if not missing:
        return text
    return text[:insert_at] + "".join(missing) + text[insert_at:]


def _paragraph_named(paragraph: str, explanation: str, notes: dict, passages: set[str]) -> bool:
    """The explanation names this paragraph when it quotes the paragraph or its passage."""
    from rogmem.pages_md import citation_passage

    if not (explanation or "").strip() or not (paragraph or "").strip():
        return False
    for line in paragraph.splitlines():
        text = line.strip()
        if text and text in explanation:
            return True
    for key in re.findall(r"\[\^([^\]]+)\]", paragraph):
        passage = citation_passage(notes.get(key, ""))
        if passage and _mentions_apart(passage, explanation, passages):
            return True
    return False


def _heading_named(heading: str, explanation: str, longer: set[str]) -> bool:
    if not heading:
        return False
    return _mentions_apart(heading, explanation, longer)


def _restore_unnamed_paragraph(
    paragraph: str,
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> str:
    """Paste the unnamed words back. A relation the explanation names may leave."""
    return _strip_named_relations(paragraph, explanation, link_names, passages, notes)


def _merge_replace_gap(
    old_slice: list[str],
    new_slice: list[str],
    flags: list[bool],
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> list[str]:
    """Keep unnamed words. Do not guess which extra paragraph is the rewrite.

    A new paragraph that still contains the old words is that paragraph plus an
    insertion, so it stays and the old paragraph is not copied again. A one-to-one
    change that does not contain the old words is a rewrite: the old words stay and
    the rewrite does not. Any other count is not one paragraph replacing another,
    so the old words stay and every extra paragraph stays too.
    """
    if old_slice and all(flags):
        return [item for item in new_slice if item and item.strip()]
    slots: list[str | None] = []
    used: set[int] = set()
    pending = 0
    for index, paragraph in enumerate(old_slice):
        if flags[index]:
            if index < len(new_slice) and index not in used:
                slots.append(new_slice[index])
                used.add(index)
            else:
                slots.append(None)
            continue
        host = None
        old_words = _stable_words(paragraph, explanation, link_names, passages, notes)
        for new_index, new_paragraph in enumerate(new_slice):
            if new_index in used:
                continue
            new_words = _stable_words(new_paragraph, explanation, link_names, passages, notes)
            if old_words and old_words in new_words:
                host = new_index
                break
        if host is not None:
            used.add(host)
            slots.append(_graft_unnamed_marks(new_slice[host], paragraph, explanation, link_names, passages, notes))
            continue
        restored = _restore_unnamed_paragraph(paragraph, explanation, link_names, passages, notes)
        slots.append(restored or None)
        if restored:
            pending += 1
    unused = [new_slice[index] for index in range(len(new_slice)) if index not in used]
    out = [item for item in slots if item and item.strip()]
    if not pending:
        out.extend(item for item in unused if item and item.strip())
        return out
    if len(unused) == pending:
        return out
    out.extend(item for item in unused if item and item.strip())
    return out


def _merge_paragraphs(
    old: list[str],
    new: list[str],
    named: list[bool],
    explanation: str,
    link_names: set[str],
    passages: set[str],
    notes: dict,
) -> list[str]:
    """Unnamed paragraphs stay verbatim and in order. Insertions stay.

    Equality is the paragraph text. A blank-line paragraph the old page did not
    have is an insertion. A one-to-one rewrite of an unnamed paragraph is not.
    """
    if not old:
        return list(new)
    if not new:
        restored = []
        for paragraph, flag in zip(old, named):
            if flag:
                continue
            text = _restore_unnamed_paragraph(paragraph, explanation, link_names, passages, notes)
            if text:
                restored.append(text)
        return restored
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(old[i1:i2])
        elif tag == "insert":
            out.extend(new[j1:j2])
        elif tag == "delete":
            for index in range(i1, i2):
                if named[index]:
                    continue
                text = _restore_unnamed_paragraph(old[index], explanation, link_names, passages, notes)
                if text:
                    out.append(text)
        else:
            out.extend(
                _merge_replace_gap(
                    old[i1:i2],
                    new[j1:j2],
                    named[i1:i2],
                    explanation,
                    link_names,
                    passages,
                    notes,
                )
            )
    return out


def _page_has_paragraph(page: str, paragraph: str) -> bool:
    target = (paragraph or "").strip()
    if not target:
        return False
    return any(target in paras for _heading, paras in _fact_sections(page))


def _bind_paragraph_notes(sections: list[tuple[str, list[str]]], old: str, new: str) -> tuple[list[tuple[str, list[str]]], dict[str, str]]:
    """One mark key carries one note. A second note for the same key gets a free key."""
    from rogmem.pages_md import cite_notes

    old_notes = cite_notes(old)
    new_notes = cite_notes(new)
    used: dict[str, str] = {}
    used_keys: set[str] = set()
    bound: list[tuple[str, list[str]]] = []

    def desired(paragraph: str, key: str) -> str:
        in_old = _page_has_paragraph(old, paragraph)
        in_new = _page_has_paragraph(new, paragraph)
        if in_new and not in_old:
            return new_notes.get(key, "") or old_notes.get(key, "")
        if in_old and not in_new:
            return old_notes.get(key, "") or new_notes.get(key, "")
        return old_notes.get(key, "") or new_notes.get(key, "")

    for heading, paras in sections:
        fixed: list[str] = []
        for para in paras:
            def repl(match: re.Match, paragraph: str = para) -> str:
                key = match.group(1)
                note = desired(paragraph, key)
                if not note:
                    return match.group(0)
                current = used.get(key)
                if current is None:
                    used[key] = note
                    used_keys.add(key)
                    return match.group(0)
                if current == note:
                    return match.group(0)
                fresh = _free_mark(used_keys)
                used[fresh] = note
                used_keys.add(fresh)
                return f"[^{fresh}]"

            fixed.append(re.sub(r"\[\^([^\]]+)\]", repl, para))
        bound.append((heading, fixed))
    return bound, used


def _assemble_page(sections: list[tuple[str, list[str]]], notes: dict[str, str]) -> str:
    from rogmem.pages_md import citation_passage

    chunks: list[str] = []
    for heading, paras in sections:
        body = [para.strip() for para in paras if para and para.strip()]
        if heading:
            if not body:
                continue
            chunks.append("## " + heading + "\n\n" + "\n\n".join(body))
        elif body:
            chunks.append("\n\n".join(body))
    text = "\n\n".join(chunks).strip()
    defs: list[str] = []
    seen: set[str] = set()
    for key in _inline_marks(text):
        if key in seen:
            continue
        seen.add(key)
        note = notes.get(key, "")
        if note and citation_passage(note):
            defs.append(f"[^{key}]: {note}")
    if defs:
        text = text + "\n\n" + "\n".join(defs)
    return text


def _sections_claim(sections: list[tuple[str, list[str]]]) -> str:
    parts: list[str] = []
    for heading, paras in sections:
        if heading:
            parts.append(heading)
        parts.extend(paras)
    return _claim_text("\n".join(parts))


def _put_back_named_wording(
    ordered: list[tuple[str, list[str]]],
    old_sections: list[tuple[str, list[str]]],
    explanation: str,
    longer: set[str],
    notes: dict,
    passages: set[str],
) -> None:
    """更新 keeps the previous wording. If it is gone, paste that paragraph back."""
    by_heading: dict[str, list[str]] = {}
    for heading, paras in ordered:
        by_heading.setdefault(heading, paras)
    claims = _sections_claim(ordered)
    for heading, old_paras in old_sections:
        current = by_heading.get(heading)
        if current is None:
            continue
        heading_is_named = _heading_named(heading, explanation, longer)
        cursor = 0
        for para in old_paras:
            named = heading_is_named or _paragraph_named(para, explanation, notes, passages)
            found = next((index for index in range(cursor, len(current)) if current[index] == para), None)
            if not named:
                if found is not None:
                    cursor = found + 1
                continue
            claim = _claim_text(para)
            if not claim or claim in claims or found is not None:
                if found is not None:
                    cursor = found + 1
                continue
            current.insert(cursor, para)
            claims += "\n" + claim
            cursor += 1


def _restore_unnamed_blocks(old: str, new: str, explanation: str, conflicts: list) -> str:
    """Unnamed paragraphs stay verbatim and in their original order.

    The program checks that text only. It does not decide which sentence is current,
    which subject a heading names, or where a new fact belongs. A paragraph the
    explanation does not quote is pasted back. A paragraph it quotes may take the
    model's words. Paragraphs and headings the old page did not have are insertions
    and stay. Old headings keep their order. Note definitions, and lines that are
    only marks or crystal links, are not paragraphs: they are not pasted back and
    not written out as a new trailing line. When a conflict's verdict is 更新, a
    named paragraph's previous wording must still occur; otherwise that paragraph
    is pasted back beside the model's new one.
    """
    from rogmem.pages_md import citation_passage, cite_notes

    old_sections = _fact_sections(old)
    new_sections = _fact_sections(new)
    if not old_sections:
        bound, used = _bind_paragraph_notes(new_sections, old, new)
        return _assemble_page(bound, used) or (old or "").strip()
    notes = cite_notes(old)
    passages = {
        citation_passage(note)
        for note in list(notes.values()) + list(cite_notes(new).values())
        if citation_passage(note)
    }
    fact_lines: set[str] = set()
    headings: set[str] = set()
    for title, paras in old_sections + new_sections:
        if title:
            headings.add(title)
        for para in paras:
            for line in para.splitlines():
                text = line.strip()
                if text:
                    fact_lines.add(text)
    longer = set(headings) | fact_lines
    explanation = explanation or ""
    link_names = {_link_name(link) for link in _crystal_links(old) + _crystal_links(new)}
    link_names.discard("")
    old_keys: list[str] = []
    old_map: dict[str, list[str]] = {}
    for title, paras in old_sections:
        if title not in old_map:
            old_keys.append(title)
            old_map[title] = paras
    new_map: dict[str, list[str]] = {}
    for title, paras in new_sections:
        if title not in new_map:
            new_map[title] = paras
    old_key_set = set(old_keys)
    contents: dict[str, list[str]] = {}
    for title in old_keys:
        old_paras = old_map[title]
        heading_is_named = _heading_named(title, explanation, longer)
        flags = [
            heading_is_named or _paragraph_named(para, explanation, notes, passages) for para in old_paras
        ]
        contents[title] = _merge_paragraphs(
            old_paras,
            new_map.get(title, []),
            flags,
            explanation,
            link_names,
            passages,
            notes,
        )
    sentinel = "\0"
    insert_after: dict[str, list[tuple[str, list[str]]]] = {sentinel: []}
    for title in old_keys:
        insert_after[title] = []
    preceding = sentinel
    seen: set[str] = set()
    for title, paras in new_sections:
        if title in old_key_set:
            if title not in seen:
                preceding = title
                seen.add(title)
            continue
        insert_after.setdefault(preceding, []).append((title, list(paras)))
    ordered: list[tuple[str, list[str]]] = []
    ordered.extend(insert_after.get(sentinel, []))
    for title in old_keys:
        ordered.append((title, contents.get(title, [])))
        ordered.extend(insert_after.get(title, []))
    if any(isinstance(item, dict) and str(item.get("verdict") or "") == "更新" for item in conflicts or []):
        _put_back_named_wording(ordered, old_sections, explanation, longer, notes, passages)
    bound, used = _bind_paragraph_notes(ordered, old, new)
    return _assemble_page(bound, used)


def _keep_both_pages(old: str, new: str) -> str:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        return new
    if not new or new in old:
        return old
    if old in new and _relations_cover(new, old):
        return new
    return old + "\n\n" + new


def _parse_page_merge(raw: str) -> dict:
    """Read one merge reply. `same` says whether the new page joins the stored one."""
    empty = {"page": "", "conflicts": [], "gist": "", "same": "", "name": "", "type": ""}
    data = _parse_model_json(raw)
    if not isinstance(data, dict):
        return empty
    page = str(data.get("page") or "").strip()
    conflicts = []
    allowed = {"重复", "更新", "并存", "无法说定"}
    for item in data.get("conflicts") or []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip()
        if verdict not in allowed:
            continue
        conflicts.append(
            {
                "matter": str(item.get("matter") or "").strip(),
                "verdict": verdict,
                "basis": str(item.get("basis") or "").strip(),
            }
        )
    same = str(data.get("same") or "").strip()
    if same not in {"是", "否", "无法说定"}:
        text = " ".join(f"{item.get('matter') or ''} {item.get('basis') or ''}" for item in conflicts)
        same = "否" if "不是同一个" in text else ""
    from rogmem.pages_md import PAGE_TYPES

    ctype = str(data.get("type") or "").strip()
    if ctype not in PAGE_TYPES:
        ctype = ""
    return {
        "page": page,
        "conflicts": conflicts,
        "gist": clip_revision_gist(str(data.get("gist") or "")),
        "same": same,
        "name": str(data.get("name") or "").strip(),
        "type": ctype,
    }


@charging("并页")
def _merge_existing_page(
    old: str,
    new: str,
    material: str,
    source=None,
    page_event_at: str = "",
    page_name: str = "",
    page_type: str = "",
) -> tuple[str, list[dict], str, str, bool, str, str]:
    """Ask whether this is the same subject. A different subject comes back as its own page."""
    from rogmem.prompts import merge_user

    target = llm_target("distill")
    if not target or not (old or "").strip():
        return new, [], "", "", False, "", ""
    _base, key, _model = target
    if not str(key or "").strip():
        raise AuthPause(MISSING_PROVIDER_KEY)
    clock_at, clock_basis = _history_unit_clock(source)
    shown_event, shown_basis = _model_clock(
        clock_at or _source_column(source, "event_at"),
        _source_column(source, "ingested_at"),
        clock_basis or _source_column(source, "event_basis"),
    )
    base, key, model = target
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": INTEGRATE_PAGE_SYSTEM},
            {
                "role": "user",
                "content": merge_user(
                    old,
                    new,
                    material or "",
                    event_at=shown_event,
                    ingested_at=_source_column(source, "ingested_at"),
                    event_basis=shown_basis,
                    page_event_at=page_event_at,
                    page_name=page_name,
                    page_type=page_type,
                    owner_appellation=_stored_owner_appellation(),
                ),
            },
        ],
    }
    content, reason = _complete_chat(base, key, payload, role="distill")
    if not content and reason == "length":
        payload["max_tokens"] = max(int(payload["max_tokens"]), _chat_max_tokens(base, model))
        content, _reason = _complete_chat(base, key, payload, role="distill")
    parsed = _parse_page_merge(content or "")
    page = parsed["page"]
    conflicts = parsed["conflicts"]
    gist = parsed["gist"]
    explanation = _relation_explanation(gist, conflicts)
    if parsed["same"] == "否":
        candidate = page
        if not candidate or candidate.strip() == (old or "").strip():
            candidate = new
        else:
            candidate = _keep_unnamed_relations(candidate, explanation, new)
        return candidate, conflicts, "", gist, True, parsed["name"], parsed["type"]
    if parsed["same"] == "无法说定":
        return old, conflicts, "", gist, False, "", ""
    if not page:
        raise RuntimeError("并页没有交回修订")
    page = _restore_unnamed_blocks(old, page, explanation, conflicts)
    kept_old = page.strip() == (old or "").strip()
    sources = (old,) if kept_old else (old, new)
    proposed = parsed["name"] if parsed["same"] == "是" else ""
    return (
        _keep_unnamed_relations(page, explanation, *sources, hang_unmatched=False),
        conflicts,
        "",
        gist,
        False,
        proposed,
        "",
    )


def _conflict_note(conflicts: list[dict], fallback: str = "") -> str:
    lines = []
    for item in conflicts or []:
        matter = item.get("matter") or "同一件事"
        line = f"{item.get('verdict')}：{matter}"
        if item.get("basis"):
            line += f"。{item['basis']}"
        lines.append(line)
    text = "\n".join(lines)
    if fallback:
        text = (text + "\n" + fallback).strip() if text else fallback
    return text


def _drop_unbacked_marks(text: str) -> str:
    """A mark with no passage is not a citation. Leave the sentence, drop the mark."""
    from rogmem.pages_md import citation_passage, cite_notes

    notes = cite_notes(text)
    backed = {key for key, note in notes.items() if citation_passage(note)}

    def repl(match: re.Match) -> str:
        return match.group(0) if match.group(1) in backed else ""

    lines = []
    for line in (text or "").splitlines():
        if re.match(r"^\[\^([^\]]+)\]:\s*", line.strip()):
            key = re.match(r"^\[\^([^\]]+)\]:", line.strip()).group(1)
            if key in backed:
                lines.append(line)
            continue
        lines.append(re.sub(r"\[\^([^\]]+)\]", repl, line))
    return "\n".join(lines).strip()


SKETCH_CHAR_LIMIT_DEFAULT = 3500
GRAPH_SPIN_SECONDS_DEFAULT = 5
GRAPH_AUTO_LABELS_DEFAULT = False
GRAPH_LABEL_BUDGET_DEFAULT = 50
GRAPH_LABEL_BUDGET_MIN = 1
GRAPH_LABEL_BUDGET_MAX = 200

COST_SPAN_VALUES = ("all", "24h", "7d", "30d")
COST_DETAIL_VALUES = ("all", "24h", "30d")
_COST_FILTER_KEYS = ("span", "detail", "worker", "model", "dworker", "dmodel")
_COST_STRING_LIMITS = {"worker": 200, "model": 400, "dworker": 200, "dmodel": 400}


def graph_auto_labels() -> bool:
    """Whether the graph page draws names on points above the zoom threshold. Off by default."""
    raw = (get_setting("graph_auto_labels") or "").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def graph_label_budget() -> int:
    """Max on-screen label DOM budget (hover + focus + auto deduped). 1..200, default 50.

    Any stored value that is not a bare non-negative integer, or is out of range, degrades to the
    default rather than raising — the UI is still usable with sane behaviour.
    """
    raw = (get_setting("graph_label_budget") or "").strip()
    if not raw or len(raw) > 4 or not raw.isdigit():
        return GRAPH_LABEL_BUDGET_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return GRAPH_LABEL_BUDGET_DEFAULT
    if value < GRAPH_LABEL_BUDGET_MIN or value > GRAPH_LABEL_BUDGET_MAX:
        return GRAPH_LABEL_BUDGET_DEFAULT
    return value


def cost_filters() -> dict | None:
    """Return the stored cost page filter combo, or None when none is stored or stored value is corrupt."""
    raw = (get_setting("cost_filters") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    cleaned: dict = {}
    for key in _COST_FILTER_KEYS:
        value = data.get(key)
        if key == "span":
            if value not in COST_SPAN_VALUES:
                return None
            cleaned[key] = value
        elif key == "detail":
            if value not in COST_DETAIL_VALUES:
                return None
            cleaned[key] = value
        else:
            if value is None:
                cleaned[key] = ""
                continue
            if not isinstance(value, str) or len(value) > _COST_STRING_LIMITS[key]:
                return None
            cleaned[key] = value
    return cleaned


def save_cost_filters(values: dict | None) -> dict:
    """Validate and store the cost filter combo as a single JSON blob in `settings.cost_filters`."""
    if not isinstance(values, dict):
        return {"ok": False, "error": "成本筛选不是一份清单"}
    span = values.get("span", "30d")
    detail = values.get("detail", "30d")
    if span not in COST_SPAN_VALUES:
        return {"ok": False, "error": "时间区间不合法"}
    if detail not in COST_DETAIL_VALUES:
        return {"ok": False, "error": "明细区间不合法"}
    cleaned = {"span": span, "detail": detail}
    for key in ("worker", "model", "dworker", "dmodel"):
        raw = values.get(key, "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            return {"ok": False, "error": f"{key} 要是字符串"}
        if len(raw) > _COST_STRING_LIMITS[key]:
            return {"ok": False, "error": f"{key} 太长"}
        cleaned[key] = raw
    with LOCK:
        set_setting("cost_filters", json.dumps(cleaned, ensure_ascii=False, sort_keys=True))
        db().commit()
    return {"ok": True}


def graph_spin_seconds() -> int:
    """Kept setting. The starfield does not orbit; the number no longer turns the view."""
    raw = (get_setting("graph_spin_seconds") or "").strip()
    if raw.isdigit():
        value = int(raw)
        if value == 0 or 1 <= value <= 3600:
            return value
    return GRAPH_SPIN_SECONDS_DEFAULT


def sketch_char_limit() -> int:
    """How long a sketch may be. Existing sketches stay until they are written again."""
    raw = (get_setting("sketch_char_limit") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return SKETCH_CHAR_LIMIT_DEFAULT


def _fit_sketch(text: str, limit: int | None = None) -> str:
    """Keep a sketch inside the configured length, ending on a paragraph when one fits."""
    body = (text or "").strip()
    ceiling = limit if isinstance(limit, int) and limit > 0 else sketch_char_limit()
    if len(body) <= ceiling:
        return body
    clipped = body[:ceiling]
    for sep in ("\n\n", "\n", "。"):
        at = clipped.rfind(sep)
        if at >= int(ceiling * 0.6):
            return clipped[: at + (1 if sep == "。" else 0)].strip()
    return clipped.strip()


@charging("速写")
def _ask_sketch(body: str, ctype: str, *, owner: bool) -> str:
    """Model call only. Does not touch the database. Sketch uses the distill model."""
    target = llm_target("distill")
    if not target or not str(target[1] or "").strip() or not (body or "").strip():
        return ""
    from rogmem.prompts import sketch_system

    limit = sketch_char_limit()
    role = "distill"
    base, key, model = target
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": sketch_system(ctype or "", owner=owner, limit=limit)},
            {"role": "user", "content": body},
        ],
    }
    try:
        content, reason = _complete_chat(base, key, payload, role=role)
        if reason == "length":
            payload["max_tokens"] = max(int(payload["max_tokens"]), _chat_max_tokens(base, model))
            content, _reason = _complete_chat(base, key, payload, role=role)
    except ModelCallFailed:
        raise
    except Exception:
        return ""
    return _fit_sketch((content or "").strip())


def _drop_pending(con, source_id: str, crystal_id: str) -> None:
    """Remove one finished sketch from the checkpoint. The caller commits."""
    if not source_id:
        return
    row = con.execute("SELECT stage_json FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None or not row["stage_json"]:
        return
    try:
        data = json.loads(row["stage_json"])
    except json.JSONDecodeError:
        return
    pending = data.get("pending_sketches") if isinstance(data, dict) else None
    if not isinstance(pending, list) or crystal_id not in pending:
        return
    data["pending_sketches"] = [item for item in pending if item != crystal_id]
    data["phase"] = "landed"
    con.execute(
        "UPDATE sources SET stage_json=? WHERE id=?",
        (json.dumps(data, ensure_ascii=False), source_id),
    )


def _write_sketch(crystal_id: str, *, source_id: str = "") -> None:
    """Read the page, ask for a sketch, then write it with the checkpoint in one commit."""
    with LOCK:
        row = db().execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
        if row is None:
            if source_id:
                _drop_pending(db(), source_id, crystal_id)
                db().commit()
            return
        crystal = hydrate(row)
        body = crystal.body_md or ""
        ctype = crystal.type or ""
        owner = crystal.id == "owner"
    text = _ask_sketch(body, ctype, owner=owner)
    with LOCK:
        if not text:
            if source_id:
                _drop_pending(db(), source_id, crystal_id)
                db().commit()
            return
        fresh_row = db().execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
        if fresh_row is None:
            if source_id:
                _drop_pending(db(), source_id, crystal_id)
                db().commit()
            return
        fresh = hydrate(fresh_row)
        before_sketch = fresh.sketch_md or ""
        if text != before_sketch.strip():
            fresh.sketch_md = text
            CrystalStore(db()).put(fresh, holder=f"sketch:{crystal_id}", commit=False)
            _record_revision(
                db(),
                crystal_id,
                "速写",
                actor="系统",
                change=_describe_text_change(before_sketch, text, label="结晶速写"),
                basis="按这一页当时的正文写成速写。",
            )
        _drop_pending(db(), source_id, crystal_id)
        db().commit()


def _same_label(left: str, right: str) -> bool:
    """True when two writings are one label. The stored surface stays."""
    from rogmem.names import name_key

    first = (left or "").strip()
    second = (right or "").strip()
    if not first or not second:
        return False
    if first == second:
        return True
    folded = name_key(first)
    return bool(folded) and len(folded) >= 2 and folded == name_key(second)


def _label_index(con) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Exact writings and case-folded writings, each pointing at the pages that list them."""
    from rogmem.names import name_key

    exact: dict[str, set[str]] = {}
    folded: dict[str, set[str]] = {}
    for row in con.execute("SELECT * FROM crystallizations WHERE status='active'"):
        for label in _crystal_labels(row):
            exact.setdefault(label, set()).add(row["id"])
            key = name_key(label)
            if len(key) >= 2:
                folded.setdefault(key, set()).add(row["id"])
    return exact, folded


def _label_claim(index: tuple[dict[str, set[str]], dict[str, set[str]]], name: str, self_id: str) -> str:
    """Who owns this writing: free, self, taken, or invalid.

    A label is the writing itself. Case and separators do not make a second label.
    A near spelling is still a different label, so it can become its own title.
    """
    from rogmem.names import name_key

    text = (name or "").strip()
    if not text or any(mark in text for mark in "\r\n"):
        return "invalid"
    exact, folded = index
    owners = set(exact.get(text, ()))
    key = name_key(text)
    if len(key) >= 2:
        owners.update(folded.get(key, ()))
    if not owners:
        return "free"
    if owners == {self_id}:
        return "self"
    return "taken"


def _strip_taken_aliases(body: str, index: tuple[dict[str, set[str]], dict[str, set[str]]], self_id: str) -> str:
    """The alias line lists only labels this page may carry."""
    lines = (body or "").splitlines()
    out: list[str] = []
    changed = False
    for line in lines:
        probe = line.strip()
        while probe.startswith("#"):
            probe = probe[1:].strip()
        if not (probe.startswith("别名：") or probe.startswith("别名:")):
            out.append(line)
            continue
        names = _alias_names(line)
        kept = [name for name in names if _label_claim(index, name, self_id) != "taken"]
        if kept == names:
            out.append(line)
            continue
        changed = True
        if not kept:
            continue
        colon = "：" if "：" in probe else ":"
        out.append(f"别名{colon}{'、'.join(kept)}")
    if not changed:
        return body or ""
    rendered = "\n".join(out)
    if (body or "").endswith("\n"):
        rendered += "\n"
    return rendered


def _ensure_alias_line(body: str, name: str) -> str:
    """Keep a former title on the one line the program reads aliases from."""
    kept = (name or "").strip()
    if not kept or kept in _alias_names(body):
        return body or ""
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        text = line.strip()
        while text.startswith("#"):
            text = text[1:].strip()
        if not (text.startswith("别名：") or text.startswith("别名:")):
            continue
        stripped = line.rstrip()
        joiner = "" if stripped.endswith(("：", ":")) else "、"
        lines[index] = stripped + joiner + kept
        return "\n".join(lines)
    prefix = f"别名：{kept}"
    rest = (body or "").strip()
    if not rest:
        return prefix
    return prefix + "\n" + rest


def _alias_names(body: str) -> list[str]:
    """Names the page itself lists on a 别名 line. Not guessed from the prose."""
    found = []
    for line in (body or "").splitlines():
        text = line.strip()
        while text.startswith("#"):
            text = text[1:].strip()
        if not (text.startswith("别名：") or text.startswith("别名:")):
            continue
        rest = re.split(r"[:：]", text, maxsplit=1)[-1]
        for part in re.split(r"[、,，]", rest):
            name = part.strip()
            if name and name not in found:
                found.append(name)
    return found


def _active_titled(con, name: str, ctype: str):
    """Active pages of this type whose title is this written name. Not a near spelling."""
    text = (name or "").strip()
    if not text:
        return []
    rows = con.execute(
        "SELECT * FROM crystallizations WHERE status='active' AND type=? AND id!='owner'",
        (ctype,),
    ).fetchall()
    return [row for row in rows if _same_label(text, row["name"] or "")]


def _row_for_named_page(con, source, page: dict, owner, owner_labels: list[str]):
    """The page this write belongs on, and its repo identity when it is 工程."""
    from rogmem.names import owner_names_match

    bound = str(page.get("id") or "").strip()
    if bound:
        row = con.execute(
            "SELECT * FROM crystallizations WHERE id=? AND status='active'",
            (bound,),
        ).fetchone()
        if row is not None:
            repo = ""
            if (row["type"] or "") == "工程" or page.get("type") == "工程":
                repo = _engineering_identity(source, page)
                if not repo:
                    for address in _project_addresses(row, con):
                        repo = _repo_identity(address) or address
                        if repo:
                            break
            return row, repo
    if page["type"] == "工程":
        repo = _engineering_identity(source, page)
        if repo:
            found = _project_crystal(repo, con)
            if found is not None:
                return found, repo
        named = _active_titled(con, page["name"], "工程")
        if named:
            return _older_crystal(con, named), repo
        if not repo:
            return None, ""
        return None, repo
    row = _named_or_folded(page["name"], page["type"], con)
    if row is None and owner is not None and any(owner_names_match(page["name"], label) for label in owner_labels):
        row = owner
    if row is None:
        for alias in _alias_names(page.get("body") or ""):
            found = _named_or_folded(alias, page["type"], con)
            if found is not None:
                row = found
                break
    return row, ""


def _existing_page_body(con, source, page: dict, owner, owner_labels: list[str]) -> str:
    row, _repo = _row_for_named_page(con, source, page, owner, owner_labels)
    if row is None:
        return ""
    return (hydrate(row).body_md or "").strip()


def _page_priors(source, prepared: list[dict]) -> list[dict]:
    """What each page would merge into. Read outside the model call."""
    with LOCK:
        con = db()
        owner = con.execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
        owner_labels = _crystal_labels(owner) if owner is not None else []
        if owner is not None:
            real = str((hydrate(owner).keys or {}).get("真名") or "").strip()
            if real and real not in owner_labels:
                owner_labels.append(real)
        queued = []
        for page in prepared:
            row, _repo = _row_for_named_page(con, source, page, owner, owner_labels)
            queued.append(
                {
                    "old": (hydrate(row).body_md or "").strip() if row is not None else "",
                    "at": _latest_event_at(row["id"]) if row is not None else "",
                    "name": ((row["name"] if row is not None else "") or page.get("name") or "").strip(),
                    "type": ((row["type"] if row is not None else "") or page.get("type") or "").strip(),
                    "id": row["id"] if row is not None else "",
                }
            )
    return queued


def _merge_one_page(source, page: dict, prior: dict) -> None:
    """One merge call. The caller stores the page before the next call."""
    material = _cited_material(source)
    bound = _bind_source_cites(page["body"], source["id"], material or "")
    old = prior["old"]
    if not old or old == bound.strip():
        page["body"] = bound
        page["conflicts"] = []
        page["merge_note"] = ""
        page["gist"] = ""
        if prior["id"] and not page.get("separate"):
            page["id"] = prior["id"]
        return
    try:
        _touch_lease(source["id"])
        merged, conflicts, note, gist, separate, new_name, new_type = _merge_existing_page(
            old,
            bound,
            material or "",
            source,
            prior["at"],
            page_name=prior["name"],
            page_type=prior["type"],
        )
    except ModelCallFailed as exc:
        mark_pipeline(source["id"], "并页", "fail")
        _note_stage_error(source["id"], exc)
        raise
    page["body"] = merged
    page["conflicts"] = conflicts
    page["merge_note"] = note
    page["gist"] = gist
    if separate:
        page["separate"] = True
        page.pop("id", None)
        page.pop("title", None)
        chosen = (new_name or "").strip()
        if chosen and chosen != prior["name"]:
            page["name"] = chosen
        if new_type:
            page["type"] = new_type
    elif prior["id"]:
        page["id"] = prior["id"]
        proposed = (new_name or "").strip()
        if proposed and not _same_label(proposed, prior["name"]):
            page["title"] = proposed
        else:
            page.pop("title", None)


def _merge_prepared_pages(source, prepared: list[dict]) -> None:
    """Merge one page, deliver it, then the next. A failed call leaves earlier pages in the store.

    Priors are read before any of these pages are written, so two fragments of one
    subject are merged against the store as it was, then delivered onto the same page.
    """
    priors = _page_priors(source, prepared)
    for page, prior in zip(prepared, priors):
        if not page.get("settled"):
            _merge_one_page(source, page, prior)
            page["settled"] = True
            _save_stage(source["id"], prepared, _phase_for(prepared))
        if not page.get("landed"):
            _deliver_page(source, prepared, page)


def _deliver_page(source, catalog: list[dict], page: dict) -> None:
    """Write one merged page into the store and remember that in the same commit.

    A draft is not delivered. This runs only after that page's merge has settled.
    """
    cleaned = _drop_unbacked_marks(page.get("body") or "")
    if not str(page.get("name") or "").strip() or not cleaned:
        raise RuntimeError("模型没有交出页面")
    page["body"] = cleaned
    saved: list[str] = []
    with LOCK:
        con = db()
        owner = con.execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
        owner_labels = _crystal_labels(owner) if owner is not None else []
        if owner is not None:
            real = str((hydrate(owner).keys or {}).get("真名") or "").strip()
            if real and real not in owner_labels:
                owner_labels.append(real)
        holder = f"distill:{source['id']}"
        unplaced = ""
        repo = ""
        row = None
        if page.get("separate") and page["type"] == "工程":
            repo = _engineering_identity(source, page)
            if repo:
                row = _project_crystal(repo, con)
            named = _active_titled(con, page["name"], "工程")
            if row is None and named:
                row = _older_crystal(con, named)
            if row is not None:
                page["body"] = _keep_both_pages((hydrate(row).body_md or "").strip(), page["body"])
            elif not repo:
                unplaced = "工程页没有仓库地址，没有另存"
        elif page.get("separate"):
            taken = con.execute(
                "SELECT id FROM crystallizations WHERE name=? AND status='active'",
                (page["name"],),
            ).fetchone()
            if taken is not None:
                unplaced = "已有同名的一页，这一页没有另存"
        else:
            row, repo = _row_for_named_page(con, source, page, owner, owner_labels)
        if not unplaced and not (page["type"] == "工程" and not repo and row is None):
            if row is None:
                cid = secrets.token_hex(8)
                _insert_crystal_page(con, cid, page["type"], page["name"], holder=holder)
                row = con.execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()
            crystal = hydrate(row)
            renamed_from = ""
            refused = ""
            page_body = page.get("body") or ""
            proposed = str(page.get("title") or "").strip()
            labels = _label_index(con) if crystal.id != "owner" else ({}, {})
            if crystal.id != "owner" and proposed and not _same_label(proposed, crystal.name):
                claim = _label_claim(labels, proposed, crystal.id)
                if claim in {"free", "self"}:
                    renamed_from = crystal.name
                    page_body = _ensure_alias_line(page_body, renamed_from)
                    crystal.name = proposed
                elif claim == "taken":
                    refused = proposed
            if crystal.id != "owner":
                page_body = _strip_taken_aliases(page_body, labels, crystal.id)
            alias_added: list[str] = []
            if crystal.id != "owner":
                known = {label for label in _crystal_labels(row) if label}
                for alias in _alias_names(page_body):
                    if alias in known or alias == crystal.name:
                        continue
                    if _label_claim(labels, alias, crystal.id) == "taken":
                        continue
                    alias_added.append(alias)
                    known.add(alias)
                if renamed_from and renamed_from != crystal.name and renamed_from not in alias_added:
                    alias_added.append(renamed_from)
                if alias_added:
                    current = [alias for alias in crystal.aliases if alias and alias != crystal.name]
                    for alias in alias_added:
                        if alias not in current and alias != crystal.name:
                            current.append(alias)
                    crystal.aliases = current
            before = (crystal.body_md or "").strip()
            incoming = _bind_source_cites(page_body, source["id"], _cited_material(source))
            fragment = any(
                other is not page
                and other.get("landed")
                and str(other.get("crystal_id") or "") == crystal.id
                for other in catalog
                if isinstance(other, dict)
            )
            incoming = _stored_body(before, incoming, fragment=fragment)
            body_changed = incoming.strip() != before
            address_note = _settle_repository(con, crystal, repo)
            if body_changed or renamed_from or alias_added or address_note:
                who, why = _who_and_basis(con, source["id"], "")
                if body_changed:
                    crystal.body_md = incoming
                CrystalStore(con).put(crystal, holder=holder, commit=False)
                parts: list[str] = []
                judged = ""
                title_line = ""
                if renamed_from:
                    title_line = f"篇名改为{crystal.name}。原名写入别名：{renamed_from}。"
                    parts.append(title_line)
                elif refused:
                    title_line = f"篇名仍是{crystal.name}。{refused}已经是另一页的标签。"
                    parts.append(title_line)
                if body_changed:
                    parts.append(_describe_text_change(before, incoming))
                    judged = _conflict_note(page.get("conflicts") or [], str(page.get("merge_note") or ""))
                if address_note:
                    parts.append(address_note)
                elif alias_added and not title_line:
                    parts.append(f"别名加上{'、'.join(alias_added)}。")
                change = "\n\n".join(part for part in parts if part).strip()
                if judged:
                    change = (change + "\n\n冲突裁决：\n" + judged).strip()
                model_gist = str(page.get("gist") or "").strip()
                if title_line and (crystal.name not in model_gist or (renamed_from or refused) not in model_gist):
                    gist = title_line
                else:
                    gist = model_gist or judged
                if renamed_from or refused:
                    note = "篇名"
                    basis = _cited_basis(incoming, why, before) if body_changed else (why or "并页认定这个主体现在的名字已经说定。")
                elif body_changed:
                    note = "正文"
                    basis = _cited_basis(incoming, why, before)
                else:
                    note = "别名"
                    basis = why or "这是同一主体的另一种叫法。"
                clock_at, _clock_basis = _history_unit_clock(source)
                _record_revision(
                    con,
                    crystal.id,
                    note,
                    source["id"],
                    "",
                    clock_at or (source["event_at"] or ""),
                    actor=who or "蒸馏",
                    change=change,
                    basis=basis,
                    snapshot=incoming if body_changed else "",
                    gist=gist,
                )
            saved.append(crystal.id)
        if unplaced:
            con.rollback()
            raise RuntimeError("已有同名的一页，这一页没有另存")
        if not saved:
            con.rollback()
            raise RuntimeError("模型没有交出页面")
        page["landed"] = True
        page["crystal_id"] = saved[-1]
        if not page.get("separate"):
            page["id"] = saved[-1]
        phase = _phase_for(catalog)
        pending = _ids_from_pages(catalog) if phase == "landed" else None
        con.execute(
            "UPDATE sources SET stage_json=? WHERE id=?",
            (
                _checkpoint_body(catalog, phase, source_hash=_source_column(source, "content_hash"), pending=pending),
                source["id"],
            ),
        )
        con.commit()


def _normalize_stage_pages(pages: list) -> list[dict]:
    drafted = []
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        name = str(page.get("name") or "").strip()
        body = str(page.get("body") or "").strip()
        if not name or not body:
            continue
        drafted.append(
            _stage_page(
                {
                    "name": name,
                    "type": str(page.get("type") or "").strip(),
                    "body": body,
                    "gist": str(page.get("gist") or ""),
                    "id": page.get("id") or "",
                    "conflicts": page.get("conflicts") or [],
                    "merge_note": page.get("merge_note") or "",
                    "separate": page.get("separate") or False,
                    "title": page.get("title") or "",
                    "settled": page.get("settled") or False,
                    "landed": page.get("landed") or False,
                    "crystal_id": page.get("crystal_id") or "",
                }
            )
        )
    return drafted


def _land_markdown_pages(source: sqlite3.Row, pages: list, *, start: str = "并页") -> None:
    """Merge a drafted page into the stored one, deliver it, then write its sketch.

    The draft stays in the checkpoint until that page's merge succeeds. The merged
    page is written into the store before the next page is merged, and before any
    sketch. A retry reuses pages already delivered. 落地 means every page is already merged.
    """
    drafted = _normalize_stage_pages(pages)
    if not drafted:
        raise RuntimeError("模型没有交出页面")
    for page in drafted:
        page["type"] = _page_type(page.get("type")) or "概念"
    if start == "落地":
        for page in drafted:
            page["settled"] = True
            if not page.get("landed"):
                _deliver_page(source, drafted, page)
    else:
        _save_stage(source["id"], drafted, _phase_for(drafted))
        mark_pipeline(source["id"], "并页", "run")
        _merge_prepared_pages(source, drafted)
    if not any(page.get("landed") for page in drafted):
        raise RuntimeError("模型没有交出页面")
    pending = _pending_on_disk(source["id"])
    if pending is None:
        pending = _ids_from_pages(drafted)
        _save_stage(source["id"], drafted, "landed", pending=pending)
    with LOCK:
        db().execute(
            "UPDATE sources SET status=?, last_error=NULL, coverage='pages' WHERE id=?",
            (ST_CRYSTAL, source["id"]),
        )
        db().commit()
    _sketch_pending(source, pending)


def _pending_note(rank: str, value: str, quote: str) -> str:
    if rank == "file":
        return f"推测，待核实：{value}（{quote}）"
    return f"待核实 {value}（{quote}）"


def _write_chapter(source: sqlite3.Row, item: dict, body: str, *, store=None) -> None:
    """Chapter persist goes through CrystalStore.apply_ops. No chapters_json bypass."""
    chapter = (item.get("chapter") or "").strip()
    value = (item.get("value") or "").strip()
    quote = item.get("quote") or ""
    target = item.get("crystal") or "记忆主人"
    if not chapter or not value or not quote or quote not in body:
        return
    if _restricted_agent_slot(None, chapter) and rank_of(source) in {"agent", "summary"}:
        return
    if source["kind"] == "file" and target in {"", "记忆主人", "owner"} and not _owner_is_subject(quote):
        return
    if source["kind"] == "file" and target in {"", "记忆主人", "owner"}:
        target = "记忆主人"
    if _verbatim_missing(quote, value) or _copies_uncovered(body, quote, value):
        return
    with LOCK:
        con = db()
        crystal = _named_or_folded(target, item.get("type"), con)
        if crystal is None:
            return
        chapters = json.loads(crystal["chapters_json"] or "{}")
        prev = chapters.get(chapter) or "暂无来源"
        if value in prev:
            _refresh_live_quote(con, crystal["id"], None, chapter, value, quote, source)
            con.commit()
            return
        if prev in {"", "暂无来源"}:
            written = value
        elif _ordered_embed(prev, value):
            written = value
        elif _source_appended(prev, value, body):
            return
        else:
            written = prev + "\n" + value
        holder = f"distill:{source['id']}"
        target_store = store if store is not None else CrystalStore(con)

        def sidecar() -> None:
            statement_id = _add_statement(
                con, crystal["id"], None, chapter, value, quote, source["id"], rank_of(source), "written", source["event_at"]
            )
            _link_relations(con, statement_id)
            _record_revision(con, crystal["id"], f"{chapter}:{value}"[:80], source["id"], quote, source["event_at"], statement_id)
            _maybe_dual_land_engineering(
                con,
                source,
                crystal_id=crystal["id"],
                crystal_type=crystal["type"],
                field=None,
                chapter=chapter,
                text=value,
                quote=quote,
                event_at=source["event_at"],
                holder=holder,
            )

        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=crystal["id"],
            expected_revision=int(crystal["revision"] or 0),
            ops=chapter_update_ops(chapter, written),
            holder=holder,
            sidecar=sidecar,
            injected=store is not None,
        )
        saved_id = crystal["id"]
        con.commit()
    if saved_id:
        _write_sketch(saved_id)


def _crystal_labels(row: sqlite3.Row) -> list[str]:
    labels = [row["name"] or ""]
    for alias in hydrate(row).aliases:
        if alias and alias not in labels:
            labels.append(alias)
    return [label for label in labels if label]


def _named_or_folded(name: str, ctype: str | None = None, con=None):
    """The active page for this name, including another spelling already on that page.

    Open may fold 云岚紙 into 云岚纸. Later writes still name the surface form.
    Those writes belong on the folded page, not a missing title.
    """
    handle = con if con is not None else db()
    if name == "记忆主人":
        row = handle.execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
        if row is not None:
            return row
    row = handle.execute("SELECT * FROM crystallizations WHERE name=? AND status='active'", (name,)).fetchone()
    if row is not None:
        return row
    if not name:
        return None
    from rogmem.names import names_correspond

    query = "SELECT * FROM crystallizations WHERE status='active' AND id!='owner'"
    args: list = []
    if ctype:
        query += " AND type=?"
        args.append(ctype)
    matches = [
        row
        for row in handle.execute(query, args)
        if any(names_correspond(name, label) for label in _crystal_labels(row))
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _fold_spelling(con, name: str, ctype: str, repo: str, quote: str, source: sqlite3.Row, *, store=None) -> str:
    """Fold a spelling into the one corresponding page. Two matches are not opened.

    Persist goes through CrystalStore thin ``aliases``. No summary_json bypass.
    """
    from rogmem import crystal_store as crystal_store_mod
    from rogmem.names import names_correspond

    rows = con.execute(
        "SELECT * FROM crystallizations WHERE status='active' AND type=? AND id!='owner'",
        (ctype,),
    ).fetchall()
    matches = [row for row in rows if any(names_correspond(name, label) for label in _crystal_labels(row))]
    if any((row["name"] or "") == name for row in matches):
        return "open"
    if len(matches) > 1:
        con.execute(
            "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
            (utcnow(), "可能同一实体", f"{name} 对上不止一篇"),
        )
        return "blocked"
    if len(matches) != 1:
        return "open"
    target = matches[0]
    current = [alias for alias in hydrate(target).aliases if alias]
    aliases_value = name if not current else ("、".join(current) if name in current else "、".join([*current, name]))

    def sidecar() -> None:
        _add_statement(
            con,
            target["id"],
            "别名",
            None,
            name,
            quote,
            source["id"],
            rank_of(source),
            "written",
            source["event_at"],
        )
        if repo:
            _add_statement(
                con,
                target["id"],
                "仓库地址",
                None,
                repo,
                quote,
                source["id"],
                rank_of(source),
                "written",
                source["event_at"],
            )
        _record_revision(con, target["id"], f"并入写法：{name}"[:80], source["id"], quote, source["event_at"] or "")

    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
        return "folded"
    target_store = store if store is not None else CrystalStore(con)
    _apply_then_sidecar(
        target_store,
        con,
        crystal_id=target["id"],
        expected_revision=int(target["revision"] or 0),
        ops=field_update_ops("别名", aliases_value),
        holder=f"distill:{source['id']}",
        sidecar=sidecar,
        injected=store is not None,
        commit=False,
    )
    return "folded"


def _open_crystal(source: sqlite3.Row, item: dict, critic: dict, body: str) -> None:
    name = item.get("name") or ""
    ctype = _page_type(item.get("type"))
    quote = item.get("quote") or ""
    if name in COMMON or name in PLACE_NAMES:
        return
    if name in ORG_NAMES and ctype != "组织":
        return
    if name in PRODUCT_NAMES and ctype != "产品":
        return
    if ctype not in {"人物", "工程", "组织", "产品", "概念"}:
        return
    full = source["body"] or ""
    in_unit = name in body and quote in body
    in_file = bool(item.get("landed")) and name in full and quote in full
    if name not in quote or not (in_unit or in_file):
        return
    if _verbatim_missing(quote, name):
        return
    if critic.get(name) is False:
        return
    if ctype == "产品" and any(word in name for word in NOT_PRODUCT):
        return
    repo = ""
    if ctype == "工程":
        repo = _repo_identity(str(item.get("repo") or "")) or _repo_identity(quote)
        if not repo or repo not in body:
            return
    if not item.get("landed") and not _proper_name_ok(name, ctype, quote):
        return
    if ctype == "人物" and source["kind"] == "user" and re.search(r"我(叫|是)" + re.escape(name), body):
        _write_field(source, {"field": "真名", "value": name, "quote": quote, "crystal": "记忆主人", "trust": "written"}, "user", body)
        return
    with LOCK:
        con = db()
        folded = _fold_spelling(con, name, ctype, repo, quote, source)
        if folded != "open":
            con.commit()
            return
        existing = con.execute(
            "SELECT id FROM crystallizations WHERE name=? AND status='active'", (name,)
        ).fetchone()
        if existing:
            title = "姓名" if ctype == "人物" else "名称"
            _refresh_live_quote(con, existing["id"], title, None, name, quote, source)
            con.commit()
            return
        cid = secrets.token_hex(8)
        holder = f"distill:{source['id']}"
        thin = _insert_crystal_page(con, cid, ctype, name, holder=holder)
        title = "姓名" if ctype == "人物" else "名称"
        ops = field_update_ops(title, name)
        if repo:
            ops.extend(field_update_ops("仓库地址", repo))

        def sidecar() -> None:
            statement_id = _add_statement(
                con, cid, title, None, name, quote, source["id"], rank_of(source), "written", source["event_at"]
            )
            if repo:
                _add_statement(
                    con, cid, "仓库地址", None, repo, quote, source["id"], rank_of(source), "written", source["event_at"]
                )
            _record_revision(con, cid, "开篇", source["id"], quote, source["event_at"], statement_id)

        if thin:
            created = con.execute("SELECT revision FROM crystallizations WHERE id=?", (cid,)).fetchone()
            _apply_then_sidecar(
                CrystalStore(con),
                con,
                crystal_id=cid,
                expected_revision=int((created["revision"] if created else 0) or 0),
                ops=ops,
                holder=holder,
                sidecar=sidecar,
                injected=False,
            )
        else:
            sidecar()
            con.commit()
        _persist_overview(con, cid, ctype, name, holder=holder)
        if ctype == "工程" and repo:
            _maybe_dual_land_engineering(
                con,
                source,
                crystal_id=cid,
                crystal_type="工程",
                field="仓库地址",
                chapter=None,
                text=repo,
                quote=quote,
                event_at=source["event_at"],
                holder=holder,
            )
            con.commit()


def rank_of(source: sqlite3.Row) -> str:
    return {"user": "user", "summary": "summary", "file": "file"}.get(source["kind"], "agent")


def _chapters_for(ctype: str) -> list[str]:
    if ctype == "人物":
        return PERSON_CHAPTERS
    if ctype == "组织":
        return ["发展沿革", "业务与产品", "关联组织与人物", "与记忆主人的相关往来"]
    if ctype == "产品":
        return ["发展沿革", "功能与形态", "记忆主人的使用情况"]
    if ctype == "概念":
        return ["定义", "适用与边界", "条目与判据", "来源与演变", "关联"]
    return ["目的目标", "发展沿革", "发版日志", "开发中特性", "计划中特性", "架构说明", "依赖环境", "关联"]


def _judge_model(role: str) -> tuple[str, str, str] | None:
    """Identity merge uses the distill model when its own model is unset."""
    target = llm_target(role)
    if target:
        return target
    if role == "merge":
        return llm_target("distill")
    return None


@charging("身份合并")
def _ask_model(role: str, system: str, user: str) -> dict:
    target = _judge_model(role)
    if not target:
        raise RuntimeError("这一问没有模型")
    base, key, model = target
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    content, reason = _complete_chat(base, key, payload, role=role)
    if not content and reason == "length":
        payload["max_tokens"] = _chat_max_tokens(base, model)
        content, _reason = _complete_chat(base, key, payload, role=role)
    if not content:
        raise RuntimeError("这一问没有给出正文")
    return _parse_model_json(content)


def _ask_same_entity(name_a: str, name_b: str, anchors: list, extra: str) -> str:
    """One identity question. The caller decides whether a yes may merge."""
    same = _ask_model(
        "merge",
        MERGE_ENTITY_SYSTEM,
        "先判断是不是同一实体。这一问不合并页面。\n"
        f"甲：{name_a}\n乙：{name_b}\n锚点：{'、'.join(str(item) for item in anchors[:4])}\n"
        + (f"新的原始记忆：{extra[:400]}" if extra else ""),
    )
    answer = same.get("answer")
    if answer not in {"是", "否"}:
        raise RuntimeError("身份合并结论无效")
    return answer


def _verbatim_missing(quote: str, published: str) -> str:
    """Dates, amounts, commands, settings, and field text must be copied unchanged."""
    text = quote or ""
    out = published or ""
    if (STATUTE_RE.search(text) or INSTRUMENT_RE.search(text)) and text not in out:
        return text
    for pattern in (VERBATIM_RE, COMMAND_RE, CONFIG_RE, FIELD_RE):
        for match in pattern.finditer(text):
            span = match.group(0)
            if span not in out:
                return span
    return ""


def _field_apply_ops(key: str, value: str, summary: dict, chapters: dict, orig_summary: dict, orig_chapters: dict) -> list[dict]:
    """Ops for the field/chapter slots this write actually changed. Overview is persist-only."""
    ops: list[dict] = []
    if key and summary.get(key) != orig_summary.get(key):
        ops.extend(field_update_ops(key, summary.get(key) or value))
    for name, text in chapters.items():
        if orig_chapters.get(name) != text:
            ops.extend(chapter_update_ops(name, text))
    if not ops and key:
        ops.extend(field_update_ops(key, summary.get(key) or value))
    return ops


def _sidecar_field_edges(con, crystal_id: str, crystal_type: str, key: str, value: str) -> None:
    """Field-slot edges: 适用对象 / 所属组织. Shared by _write_field and accept sidecar."""
    if key == "适用对象" and crystal_type == "概念":
        other = con.execute(
            "SELECT id FROM crystallizations WHERE name=? AND status='active'",
            (value,),
        ).fetchone()
        fresh = con.execute(
            "SELECT id FROM statements WHERE crystal_id=? AND field='适用对象' ORDER BY created_at DESC LIMIT 1",
            (crystal_id,),
        ).fetchone()
        if other and fresh and other["id"] != crystal_id:
            con.execute(
                "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES(?,?,?,?,?,0)",
                (secrets.token_hex(8), crystal_id, other["id"], "适用", fresh["id"]),
            )
    if key in {"所属组织", "所属组织及职务"}:
        con.execute("UPDATE edges SET ended=1 WHERE src=? AND kind='所属' AND ended=0", (crystal_id,))
        other = _org_named_in(con, value)
        fresh = con.execute(
            "SELECT id FROM statements WHERE crystal_id=? AND field=? ORDER BY created_at DESC LIMIT 1",
            (crystal_id, key),
        ).fetchone()
        if other and fresh and other["id"] != crystal_id:
            con.execute(
                "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES(?,?,?,?,?,0)",
                (secrets.token_hex(8), crystal_id, other["id"], "所属", fresh["id"]),
            )


def _note_chapter(crystal, chapters: dict) -> str:
    """Chapter to park an unverified note. Schema list, not the thick blob, is the fallback."""
    names = _chapters_for(crystal["type"])
    if "个人生活" in chapters or "个人生活" in names:
        return "个人生活"
    if chapters:
        return next(iter(chapters))
    return names[0] if names else "个人生活"


def _field_on_page(crystal, key: str, summary: dict) -> bool:
    """Allow a field write from the page schema, not from a leftover thick skeleton."""
    if key in summary:
        return True
    if key in {"真名", "姓名", "名称", "出生时间", "所属组织及职务"}:
        return True
    return key in set(summary_fields(crystal["type"], owner=crystal["id"] == "owner"))


def _write_field(source: sqlite3.Row, item: dict, rank: str, body: str, *, store=None) -> None:
    """Field persist goes through CrystalStore.apply_ops. No summary_json / chapters_json bypass."""
    field = item.get("field") or ""
    if field == "现职":
        return
    value = item.get("value") or ""
    quote = item.get("quote") or ""
    trust = item.get("trust") or "written"
    target = item.get("crystal") or "记忆主人"
    if quote and quote not in body and "«令牌»" not in quote:
        return
    if find_secrets(value or ""):
        value = "有一则令牌"
    if field in {"出生日期", "出生时间"}:
        field = "出生时间"
    if _restricted_agent_slot(field, None) and rank in {"agent", "summary"}:
        return
    if field == "约定待办" and rank != "user":
        return
    if any(w in value for w in ("可能", "好像")):
        trust = "unverified"
    if _verbatim_missing(quote, value) or _copies_uncovered(body, quote, value):
        return
    with LOCK:
        con = db()
        crystal = _named_or_folded(target, item.get("type"), con)
        if crystal is None:
            return
        if rank == "file" and crystal["id"] == "owner" and not _owner_is_subject(quote):
            return
        if field in {"所在地"} and target == "记忆主人":
            pass
        orig_summary = json.loads(crystal["summary_json"])
        orig_chapters = json.loads(crystal["chapters_json"])
        summary = json.loads(crystal["summary_json"])
        chapters = json.loads(crystal["chapters_json"])
        key = "真名" if crystal["id"] == "owner" and field in {"姓名", "真名"} else field
        if crystal["type"] == "人物" and key == "所属组织":
            key = "所属组织及职务"
        current = con.execute(
            """SELECT * FROM statements WHERE crystal_id=? AND field=? AND invalid_at IS NULL AND trust='written'
               ORDER BY created_at DESC LIMIT 1""",
            (crystal["id"], key),
        ).fetchone()
        statement_id = current["id"] if current else ""
        planned_add = None
        planned_invalidate = None
        planned_link = None
        planned_decisions: list[tuple[str, str]] = []
        if trust != "written":
            chapter = _note_chapter(crystal, chapters)
            prev = chapters.get(chapter) or "暂无来源"
            note = _pending_note(rank, value, quote)
            chapters[chapter] = note if prev in {"", "暂无来源"} else prev + "\n" + note
            planned_add = (crystal["id"], None, chapter, note, quote or value, source["id"], rank, "unverified", source["event_at"])
        elif key in STYLE and current and current["text"] == value:
            planned_link = (current["id"], source["id"], quote or value)
            statement_id = current["id"]
        elif key in STYLE and current and current["text"] in value:
            planned_add = (crystal["id"], key, None, value, quote or value, source["id"], rank, "written", source["event_at"])
            kept = [
                row["text"]
                for row in con.execute(
                    """SELECT text FROM statements
                       WHERE crystal_id=? AND field=? AND invalid_at IS NULL AND trust='written'
                       ORDER BY COALESCE(event_at, created_at), created_at""",
                    (crystal["id"], key),
                )
            ]
            summary[key] = "；".join([*kept, value])
        elif current and current["text"] == value:
            pass
        elif current and current["text"] != value:
            if _event_cannot_reopen(source, current):
                return
            old_rank = current["rank"]
            engineering = _is_focus_slot(key, None) and bool(
                _repo_identity(value) or _repo_identity(quote) or _source_project_ref(source)
            )
            if (
                _outranks(rank, old_rank)
                or (rank == "user" and old_rank == "user")
                or (engineering and rank == "agent")
            ):
                planned_invalidate = (current["id"], False)
                summary[key] = value
                planned_add = (crystal["id"], key, None, value, quote or value, source["id"], rank, "written", source["event_at"])
            elif rank == old_rank == "summary" and key in SINGLE:
                detail = json.dumps({"field": key, "a": current["text"], "b": value}, ensure_ascii=False)
                planned_invalidate = (current["id"], True)
                summary[key] = "尚无来源"
                chapter = _note_chapter(crystal, chapters)
                chapters[chapter] = f"待核实 {current['text']}；待核实 {value}"
                planned_decisions.append(("无法说定", detail))
            elif rank == "file" and old_rank == "user":
                chapter = _note_chapter(crystal, chapters)
                note = _pending_note(rank, value, quote)
                prev = chapters.get(chapter) or "暂无来源"
                chapters[chapter] = note if prev in {"", "暂无来源"} else prev + "\n" + note
                planned_add = (crystal["id"], None, chapter, note, quote or value, source["id"], rank, "unverified", source["event_at"])
            else:
                return
        else:
            if not _field_on_page(crystal, key, summary):
                return
            summary[key] = value
            planned_add = (crystal["id"], key, None, value, quote or value, source["id"], rank, "written", source["event_at"])
        ops = _field_apply_ops(key, value, summary, chapters, orig_summary, orig_chapters)
        holder = f"distill:{source['id']}"
        target_store = store if store is not None else CrystalStore(con)

        def sidecar() -> None:
            sid = statement_id
            if planned_invalidate is not None:
                inv_id, unverified = planned_invalidate
                if unverified:
                    con.execute(
                        "UPDATE statements SET invalid_at=?, trust='unverified' WHERE id=?",
                        (utcnow(), inv_id),
                    )
                else:
                    con.execute("UPDATE statements SET invalid_at=? WHERE id=?", (utcnow(), inv_id))
            if planned_add is not None:
                sid = _add_statement(con, *planned_add)
            if planned_link is not None:
                _link_statement_source(con, *planned_link)
            for kind, detail in planned_decisions:
                con.execute(
                    "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
                    (utcnow(), kind, detail),
                )
            _sidecar_field_edges(con, crystal["id"], crystal["type"], key, value)
            _record_revision(
                con,
                crystal["id"],
                f"{key}:{value}"[:80],
                source["id"],
                quote or value,
                source["event_at"],
                sid,
            )
            _maybe_dual_land_engineering(
                con,
                source,
                crystal_id=crystal["id"],
                crystal_type=crystal["type"],
                field=key,
                chapter=None,
                text=value,
                quote=quote or value,
                event_at=source["event_at"],
                holder=holder,
            )

        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=crystal["id"],
            expected_revision=int(crystal["revision"] or 0),
            ops=ops,
            holder=holder,
            sidecar=sidecar,
            injected=store is not None,
        )


def _outranks(new: str, old: str) -> bool:
    order = {"user": 3, "file": 2, "summary": 1, "agent": 0}
    return order.get(new, 0) > order.get(old, 0)


def _overview_text(con, crystal_id: str, ctype: str, name: str) -> str:
    rows = con.execute(
        """SELECT field, chapter, text FROM statements
           WHERE crystal_id=? AND invalid_at IS NULL AND trust='written'
           ORDER BY created_at""",
        (crystal_id,),
    ).fetchall()
    bits = []
    for row in rows:
        label = row["field"] or row["chapter"]
        if not label or label in {"概述", "名称", "姓名"}:
            continue
        if not row["text"] or row["text"] in {"尚无来源", "暂无来源"}:
            continue
        bits.append(f"{label}是{row['text']}")
    return f"这是{ctype}「{name}」。" + "。".join(bits) + "。" if bits else "尚无来源"


def _overview_from_row(row) -> str:
    """Thin envelope summary only. Leftover thick 概述 is not a read source."""
    crystal = hydrate(row)
    shown = str(crystal.summary or "").strip()
    if shown and shown not in {"尚无来源", "暂无来源"}:
        return shown
    return "尚无来源"


def _aliases_from_row(row) -> str:
    """Thin envelope aliases only. Leftover thick 别名 is not a read source."""
    crystal = hydrate(row)
    if crystal.aliases:
        return "、".join(crystal.aliases)
    return "尚无来源"


def _put_overview(con, crystal_id: str, ctype: str, name: str, summary: dict) -> None:
    """In-memory slot only. Persist goes through ``_persist_overview`` (thin apply_ops)."""
    summary["概述"] = _overview_text(con, crystal_id, ctype, name)


def _persist_overview(
    con,
    crystal_id: str,
    ctype: str,
    name: str,
    *,
    store=None,
    holder: str = "sweep:overview",
    commit: bool = True,
) -> bool:
    """Write overview through CrystalStore thin ``summary``. No summary_json bypass."""
    from rogmem import crystal_store as crystal_store_mod

    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
        return False
    row = con.execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
    if row is None:
        return False
    text = _overview_text(con, crystal_id, ctype, name)
    if _overview_from_row(row) == text:
        return False
    target_store = store if store is not None else CrystalStore(con)
    before_rev = int(row["revision"] or 0)
    before_env = row["envelope_json"] if "envelope_json" in row.keys() else None

    def sidecar() -> None:
        _record_revision(
            con,
            crystal_id,
            "概述改按当前陈述重写",
            actor="系统",
            change=text,
            basis="概述只保留当前仍然有效的陈述。",
        )

    try:
        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=crystal_id,
            expected_revision=before_rev,
            ops=field_update_ops("概述", text),
            holder=holder,
            sidecar=sidecar,
            injected=store is not None,
            commit=commit,
        )
    except LeaseConflict:
        return False
    after = con.execute(
        "SELECT revision, envelope_json FROM crystallizations WHERE id=?",
        (crystal_id,),
    ).fetchone()
    if after is None:
        return False
    return int(after["revision"] or 0) != before_rev or after["envelope_json"] != before_env


def _link_statement_source(con, statement_id: str, source_id: str, quote: str) -> None:
    if not statement_id or not source_id:
        return
    con.execute(
        """INSERT INTO statement_sources(statement_id, source_id, quote, created_at)
           VALUES(?,?,?,?)
           ON CONFLICT(statement_id, source_id) DO NOTHING""",
        (statement_id, source_id, quote, utcnow()),
    )


def _style_delta(previous: str, text: str) -> str:
    return style_delta(previous, text)


def _refresh_live_quote(con, crystal_id, field, chapter, text, quote, source) -> None:
    """Keep a live citation when the page already contains the wording."""
    if not quote or source is None:
        return
    hit = con.execute(
        """SELECT 1 FROM statements
           WHERE crystal_id=? AND source_id=? AND quote=? AND invalid_at IS NULL""",
        (crystal_id, source["id"], quote),
    ).fetchone()
    if hit:
        return
    _add_statement(
        con,
        crystal_id,
        field,
        chapter,
        text,
        quote,
        source["id"],
        rank_of(source),
        "written",
        source["event_at"],
    )


def _statement_place(field, chapter) -> str:
    """The heading this wording belongs to. A different heading is a different place."""
    return str(chapter or "").strip() or str(field or "").strip()


_PLACE_MATCH = """(
    (? != '' AND COALESCE(chapter, '') = ?)
    OR (? != '' AND COALESCE(chapter, '') = '' AND COALESCE(field, '') = ?)
)"""


def _add_statement(con, crystal_id, field, chapter, text, quote, source_id, rank, trust, event_at, valid_until=None) -> str:
    until = _resolve_statement_valid_until(field, chapter, event_at, valid_until)
    place = _statement_place(field, chapter)
    if place and text:
        place_args = (place, place, place, place)
        same = con.execute(
            f"""SELECT id FROM statements
               WHERE crystal_id=? AND invalid_at IS NULL AND text=?
                 AND {_PLACE_MATCH}
               LIMIT 1""",
            (crystal_id, text, *place_args),
        ).fetchone()
        if same:
            con.execute("UPDATE statements SET valid_until=? WHERE id=?", (until, same["id"]))
            return same["id"]
        con.execute(
            f"""UPDATE statements SET invalid_at=?
               WHERE crystal_id=? AND invalid_at IS NULL AND text!=?
                 AND {_PLACE_MATCH}""",
            (utcnow(), crystal_id, text, *place_args),
        )
    sid = secrets.token_hex(8)
    con.execute(
        """INSERT INTO statements(id,crystal_id,field,chapter,text,quote,source_id,chunk_seq,trust,rank,event_at,created_at,valid_until)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, crystal_id, field, chapter, text, quote, source_id, _CHUNK_SEQ.get(), trust, rank, event_at, utcnow(), until),
    )
    _link_statement_source(con, sid, source_id, quote)
    if trust == "written":
        from rogmem.publish_gate import mark_chunks_cited, matching_chunk_seqs

        con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", (sid, _tokens(text + " " + quote)))
        rows = [
            dict(row)
            for row in con.execute(
                "SELECT seq, loc, text FROM chunks WHERE source_id=? ORDER BY seq",
                (source_id,),
            )
        ]
        seqs = matching_chunk_seqs(rows, quote)
        if not seqs:
            loc = _CHUNK_SEQ.get()
            if loc is not None:
                wanted = str(loc)
                seqs = [int(row["seq"]) for row in rows if str(row.get("loc") or "") == wanted]
        mark_chunks_cited(con, source_id, seqs)
    return sid


def _diff_blocks(text: str) -> list[str]:
    """Paragraphs in reading order. A changed paragraph is one block, not every line in the page."""
    blocks: list[str] = []
    for heading, paras in _fact_sections(text):
        if heading:
            blocks.append("## " + heading)
        blocks.extend(para.strip() for para in paras if para and para.strip())
    for line in (text or "").splitlines():
        if _is_note_definition(line):
            blocks.append(line.strip())
    return blocks


def _emit_diff_blocks(removed: list[str], added: list[str]) -> list[str]:
    """One deletion and the write that follows it, then the next paragraph.

    A replace of several paragraphs must not list every deletion before any write.
    """
    parts: list[str] = []
    if not removed and not added:
        return parts
    count = max(len(removed), len(added), 1)
    for index in range(count):
        if index < len(removed):
            parts.append("删去：\n" + removed[index])
        if index < len(added):
            parts.append("写入：\n" + added[index])
    return parts


def _describe_text_change(before: str, after: str, *, label: str = "正文") -> str:
    """The paragraphs that actually changed, in reading order. Empty when the text is the same."""
    old = (before or "").strip()
    new = (after or "").strip()
    if old == new:
        return ""
    if not old:
        text = f"新写了{label}：\n{new}"
    elif not new:
        text = f"删去了{label}。"
    else:
        old_blocks = _diff_blocks(old)
        new_blocks = _diff_blocks(new)
        if old_blocks == new_blocks:
            old_blocks = old.splitlines()
            new_blocks = new.splitlines()
        parts: list[str] = []
        matcher = difflib.SequenceMatcher(a=old_blocks, b=new_blocks, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            parts.extend(_emit_diff_blocks(old_blocks[i1:i2], new_blocks[j1:j2]))
        text = "\n".join(parts) or f"改写了{label}。"
    if len(text) > 4000:
        text = text[:4000].rstrip() + "\n其余改动在这一页正文里。"
    return text


def _who_and_basis(con, source_id: str, quote: str = "") -> tuple[str, str]:
    """蒸馏 is the writer. The basis is the material it was given."""
    if not source_id:
        return "", ""
    row = con.execute(
        "SELECT kind, filename, agent_name FROM sources WHERE id=?",
        (source_id,),
    ).fetchone()
    if row is None:
        return "蒸馏", "来源已不在库里。"
    kind = row["kind"] or ""
    if kind == "file":
        name = (row["filename"] or "").strip() or "未命名文件"
        basis = f"文件「{name}」"
    elif kind == "user":
        basis = "会话里记忆主人说的这段话"
    elif kind == "agent":
        name = (row["agent_name"] or "").strip()
        basis = "会话里智能体" + (f"「{name}」" if name else "") + "写的这段话"
    elif kind == "summary":
        basis = "会话摘要"
    else:
        basis = "这份材料"
    passage = (quote or "").strip()
    if passage:
        basis = basis + "：「" + passage + "」"
    return "蒸馏", basis


def _passage_texts(body: str) -> list[str]:
    """Citation notes on a page, in order, without repeating the same passage."""
    from rogmem.pages_md import cite_notes

    passages: list[str] = []
    for text in cite_notes(body or "").values():
        cleaned = re.sub(r"https?://\S+", "", text)
        cleaned = re.sub(r"/admin/sources/\S+", "", cleaned).strip()
        cleaned = re.sub(r"^(实|推)\s*", "", cleaned).strip(" 「」")
        if cleaned and cleaned not in passages:
            passages.append(cleaned)
    return passages


def _cited_basis(body: str, source_label: str, previous: str = "") -> str:
    """The material for this write, plus passages this write newly cited.

    Notes the page already carried are the page's evidence index, not this
    revision's claim. A claim that does not fit the log budget stays on the page.
    """
    label = source_label or "这份材料"
    carried = set(_passage_texts(previous))
    passages = [item for item in _passage_texts(body) if item not in carried]
    if not passages:
        if _passage_texts(body):
            return label
        return f"{label}。这一页没有逐句标出引文，依据是整份材料。"
    quoted = "\n".join(f"「{item}」" for item in passages)
    return clip_revision_gist(f"{label}。这一页标出的原文：\n{quoted}")


_PAGE_PARTS = (("name", "标题"), ("body", "正文"), ("sketch", "速写"))


def _page_view(con, crystal_id: str) -> dict:
    """The three parts a revision can restore: title, body, sketch."""
    row = con.execute(
        "SELECT name, body_md, sketch_md FROM crystallizations WHERE id=?",
        (crystal_id,),
    ).fetchone()
    if row is None:
        return {"name": "", "body": "", "sketch": ""}
    return {"name": row["name"] or "", "body": row["body_md"] or "", "sketch": row["sketch_md"] or ""}


def _parse_page_json(raw) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "body" not in data:
        return None
    return {"name": data.get("name") or "", "body": data.get("body") or "", "sketch": data.get("sketch") or ""}


def _body_touch_scope(before: str, after: str) -> str:
    """Headings whose paragraphs changed. A page with no heading stays 正文."""
    old = _fact_sections(before)
    new = _fact_sections(after)
    old_map = {heading: paras for heading, paras in old}
    new_map = {heading: paras for heading, paras in new}
    order: list[str] = []
    for heading, _paras in old + new:
        if heading not in order:
            order.append(heading)
    touched: list[str] = []
    lead = False
    for heading in order:
        if old_map.get(heading, []) != new_map.get(heading, []):
            if heading:
                touched.append(heading)
            else:
                lead = True
    if not any(heading for heading, _paras in old + new):
        return "正文"
    if touched and lead:
        return "、".join(["正文", *touched])
    if touched:
        return "、".join(touched)
    return "正文"


def _scope_between(before: dict, after: dict) -> str:
    labels = []
    for key, label in _PAGE_PARTS:
        if (before.get(key) or "") != (after.get(key) or ""):
            if key == "body":
                labels.append(_body_touch_scope(before.get("body") or "", after.get("body") or ""))
            else:
                labels.append(label)
    return "、".join(labels)


_GIST_LIMIT = 80


def clip_revision_gist(text: str) -> str:
    """The explanation that fits in the budget. A pause only avoids ending mid-clause."""
    compact = " ".join(str(text or "").split())
    if len(compact) <= _GIST_LIMIT:
        return compact
    window = compact[:_GIST_LIMIT]
    pause = max(window.rfind(mark) for mark in "。！？!?，、；;")
    if pause >= _GIST_LIMIT - 12:
        return window[: pause + 1]
    return window.rstrip("，,、；;：: ")


def revision_gist_for(item: dict) -> str:
    """The claim a later reader needs. A stored sentence wins unless it is only a short prefix of a fuller one."""
    stored = clip_revision_gist(item.get("gist") or "")
    fuller = clip_revision_gist(item.get("change") or "") or clip_revision_gist(item.get("note") or "")
    if stored and not (fuller.startswith(stored) and len(fuller) > len(stored)):
        return stored
    return fuller or stored


def _record_revision(
    con,
    crystal_id: str,
    note: str,
    source_id: str = "",
    quote: str = "",
    event_at: str = "",
    statement_id: str = "",
    *,
    actor: str = "",
    change: str = "",
    basis: str = "",
    snapshot: str = "",
    gist: str = "",
) -> None:
    """One log line: who changed what, and the material they used."""
    if source_id and not (actor and basis):
        who, why = _who_and_basis(con, source_id, quote)
        actor = actor or who
        basis = basis or why
    if not change:
        change = note
    page = _page_view(con, crystal_id)
    previous = con.execute(
        "SELECT page_json FROM revisions WHERE crystal_id=? ORDER BY id DESC LIMIT 1",
        (crystal_id,),
    ).fetchone()
    if previous is None:
        before = {"name": "", "body": "", "sketch": ""}
        scope = _scope_between(before, page) or "未改标题、正文或速写"
    else:
        before = _parse_page_json(previous["page_json"])
        scope = "" if before is None else (_scope_between(before, page) or "未改标题、正文或速写")
    stored_gist = clip_revision_gist(gist) or clip_revision_gist(change) or clip_revision_gist(note)
    con.execute(
        """INSERT INTO revisions(
             crystal_id,created_at,note,source_id,quote,event_at,statement_id,actor,change,basis,snapshot,page_json,scope,gist)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            crystal_id,
            utcnow(),
            note[:200],
            source_id or None,
            quote or None,
            event_at or None,
            statement_id or None,
            actor or None,
            change or None,
            basis or None,
            snapshot or page["body"] or None,
            json.dumps(page, ensure_ascii=False),
            scope,
            stored_gist or None,
        ),
    )


def _statement_marks(crystal_id: str) -> tuple[dict, dict, list]:
    """Number each distinct source passage so a fact can point at its quote."""
    rows = db().execute(
        """SELECT s.id, s.field, s.chapter, s.text, s.quote, s.source_id, s.chunk_seq, s.trust,
                  src.session_id, src.turn_id
           FROM statements s
           LEFT JOIN sources src ON src.id = s.source_id
           WHERE s.crystal_id=? AND s.invalid_at IS NULL AND s.source_id IS NOT NULL
             AND s.quote IS NOT NULL AND s.quote != ''
           ORDER BY s.created_at""",
        (crystal_id,),
    ).fetchall()
    numbers: dict[tuple, int] = {}
    fields: dict[str, list] = {}
    chapters: dict[str, list] = {}
    overview: list[dict] = []
    for row in rows:
        key = (row["source_id"], row["chunk_seq"] or 0, row["quote"])
        if key not in numbers:
            numbers[key] = len(numbers) + 1
        links = origin_links(
            row["session_id"] or "",
            row["turn_id"] or "",
            row["source_id"] or "",
            row["id"] or "",
        )
        item = {
            "n": numbers[key],
            "id": row["id"],
            "source_id": row["source_id"],
            "session_id": row["session_id"] or "",
            "turn_id": row["turn_id"] or "",
            "quote": row["quote"],
            "chunk_seq": row["chunk_seq"],
            "text": row["text"],
            **links,
        }
        if row["field"] and row["trust"] == "written":
            fields.setdefault(row["field"], []).append(item)
            if row["field"] not in {"概述", "名称", "姓名"} and row["text"] not in {"", "尚无来源", "暂无来源"} and "待核实" not in (row["text"] or ""):
                overview.append({"text": f"{row['field']}是{row['text']}。", "marks": [item]})
        elif row["chapter"]:
            chapters.setdefault(row["chapter"], []).append(item)
    return fields, chapters, overview


def _insert_crystal_page(
    con: sqlite3.Connection,
    cid: str,
    ctype: str,
    name: str,
    *,
    store=None,
    holder: str | None = None,
) -> bool:
    """Create a new page. Thin empty envelope when the write switch is on.

    Switch-off 新页策略：只建行，``summary_json`` / ``chapters_json`` 用 ``{}``
    占位，不写厚栏骨架，不 mutate 已有页。开关开（或注入 store）时
    ``CrystalStore.put`` 薄空信封。返回是否已 put 薄信封。
    """
    from rogmem import crystal_store as crystal_store_mod

    if store is None and not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED:
        con.execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
            "VALUES(?,?,?,'active','{}','{}')",
            (cid, ctype, name),
        )
        return False
    target = store if store is not None else CrystalStore(con)
    put = getattr(target, "put", None)
    if not callable(put):
        con.execute(
            "INSERT INTO crystallizations(id,type,name,status,summary_json,chapters_json) "
            "VALUES(?,?,?,'active','{}','{}')",
            (cid, ctype, name),
        )
        return False
    put(
        ThinCrystal(id=cid, type=ctype, name=name, status="active", revision=0),
        holder=holder,
        commit=False,
    )
    return True


def _ensure_owner(con: sqlite3.Connection) -> None:
    if not con.execute("SELECT 1 FROM crystallizations WHERE id='owner'").fetchone():
        _insert_crystal_page(con, "owner", "人物", "记忆主人", holder="connect:owner")
        con.commit()


def model_kind(provider: dict, name: str) -> str:
    """A model without a recorded kind is a chat model. That covers rows saved before kinds existed."""
    kinds = provider.get("model_kinds") if isinstance(provider.get("model_kinds"), dict) else {}
    kind = str(kinds.get(name) or "")
    return kind if kind in _MODEL_KINDS else MODEL_KIND_CHAT


def _ensure_model_catalog() -> None:
    """Chat models stay chat. The local embedding presets are always on the list."""
    items = _providers()
    changed = False
    for item in items:
        kinds = dict(item.get("model_kinds") or {}) if isinstance(item.get("model_kinds"), dict) else {}
        for name in item.get("models") or []:
            if kinds.get(name) not in _MODEL_KINDS:
                kinds[name] = MODEL_KIND_CHAT
                changed = True
        item["model_kinds"] = kinds
    known = {
        name
        for item in items
        for name in (item.get("models") or [])
        if model_kind(item, name) == MODEL_KIND_EMBED
    }
    local = next((item for item in items if item.get("id") == LOCAL_EMBED_ID), None)
    presets = [item["id"] for item in EMBED_PRESETS]
    if local is None:
        items.insert(
            0,
            {
                "id": LOCAL_EMBED_ID,
                "name": LOCAL_EMBED_NAME,
                "base_url": "",
                "api_key": "",
                "models": presets,
                "model_kinds": {name: MODEL_KIND_EMBED for name in presets},
                "health": "ok",
                "health_detail": LOCAL_EMBED_PLACE,
                "remark": "",
                "health_at": "",
                "health_unix": 0,
            },
        )
        changed = True
    else:
        models = list(local.get("models") or [])
        kinds = dict(local.get("model_kinds") or {})
        for name in presets:
            if name not in models:
                models.append(name)
                changed = True
            if kinds.get(name) != MODEL_KIND_EMBED:
                kinds[name] = MODEL_KIND_EMBED
                changed = True
        local["models"] = models
        local["model_kinds"] = kinds
        if local.get("name") != LOCAL_EMBED_NAME:
            local["name"] = LOCAL_EMBED_NAME
            changed = True
        if str(local.get("health_detail") or "") in {"", "本机"}:
            local["health_detail"] = LOCAL_EMBED_PLACE
            changed = True
        known.update(presets)
    flow = next((item for item in items if item.get("id") == SILICONFLOW_ID), None)
    if flow is None:
        items.append(
            {
                "id": SILICONFLOW_ID,
                "name": "硅基流动",
                "base_url": SILICONFLOW_BASE,
                "api_key": "",
                "models": list(SILICONFLOW_EMBEDS),
                "model_kinds": {name: MODEL_KIND_EMBED for name in SILICONFLOW_EMBEDS},
                "health": "",
                "health_detail": "待填写密钥",
                "health_at": "",
                "health_unix": 0,
            }
        )
        changed = True
    else:
        models = list(flow.get("models") or [])
        kinds = dict(flow.get("model_kinds") or {})
        for name in SILICONFLOW_EMBEDS:
            if name not in models:
                models.append(name)
                changed = True
            if kinds.get(name) != MODEL_KIND_EMBED:
                kinds[name] = MODEL_KIND_EMBED
                changed = True
        flow["models"] = models
        flow["model_kinds"] = kinds
        if not str(flow.get("base_url") or "").strip():
            flow["base_url"] = SILICONFLOW_BASE
            changed = True
    ordered = [item for item in items if item.get("id") == LOCAL_EMBED_ID]
    ordered.extend(item for item in items if item.get("id") != LOCAL_EMBED_ID)
    if [item.get("id") for item in ordered] != [item.get("id") for item in items]:
        items = ordered
        changed = True
    if changed:
        _store_providers(items)
    local_seed = f"{LOCAL_EMBED_ID}|{EMBED_PRESETS[0]['id']}"
    remote_seed = f"{SILICONFLOW_ID}|{SILICONFLOW_DEFAULT_EMBED}"
    current = get_setting("model_embed") or ""
    if current in {"", local_seed}:
        set_setting("model_embed", remote_seed)
    if not get_setting("embed_model"):
        set_setting("embed_model", EMBED_PRESETS[0]["id"])


def _ensure_default_minimax() -> None:
    """Seed the China MiniMax endpoint and preselect M3.1. The key stays empty."""
    if _providers() or get_setting("model_distill"):
        return
    _store_providers(
        [
            {
                "id": DEFAULT_MINIMAX_ID,
                "name": "MiniMax",
                "base_url": DEFAULT_MINIMAX_BASE,
                "api_key": "",
                "models": [DEFAULT_MINIMAX_MODEL],
                "model_kinds": {DEFAULT_MINIMAX_MODEL: MODEL_KIND_CHAT},
                "health": "",
                "health_detail": "待填写密钥",
                "health_at": "",
                "health_unix": 0,
            }
        ]
    )
    token = f"{DEFAULT_MINIMAX_ID}|{DEFAULT_MINIMAX_MODEL}"
    for key in ("model_distill", "model_merge"):
        if not get_setting(key):
            set_setting(key, token)
    db().execute(
        """UPDATE queue SET state='queued', next_at=NULL, attempts=0, last_error=NULL
           WHERE state='failed' AND last_error=?""",
        (_LAND_MISREAD,),
    )
    db().execute(
        "UPDATE sources SET status=?, last_error=NULL, attempt=0 WHERE last_error=?",
        (ST_VEC_READY, _LAND_MISREAD),
    )


def _is_forget_request(text: str) -> bool:
    return any(word in (text or "") for word in ("忘掉", "不要再记住", "请删除", "删除这段", "删除这句"))


def _forget_spans(text: str) -> list[str]:
    """Continuous wording after an explicit forget request. Short leftovers are not a target."""
    spans = []
    for phrase in ("不要再记住", "忘掉", "请删除", "删除这段", "删除这句"):
        start = 0
        while True:
            at = (text or "").find(phrase, start)
            if at < 0:
                break
            tail = text[at + len(phrase) :]
            piece = re.split(r"[。！？\n]", tail, maxsplit=1)[0]
            piece = piece.strip(" ：:，,。！？!?「」\"' ")
            piece = re.sub(r"^(请|把|将|这句|这段|这个|一下)", "", piece).strip(" ：:，,。！？!?「」\"' ")
            if len(piece) >= 4 and piece not in {"已忘掉"} and not _is_forget_request(piece):
                spans.append(piece)
            start = at + len(phrase)
    return spans


def _maybe_forget(source: sqlite3.Row) -> None:
    text = source["body"]
    if not _is_forget_request(text) or source["kind"] != "user":
        return
    targets = set(find_secrets(text))
    if "全部" in text and ("密钥" in text or "令牌" in text):
        for row in db().execute("SELECT body FROM sources"):
            targets.update(find_secrets(row["body"]))
    else:
        targets.update(_forget_spans(text))
    if not targets:
        return
    with LOCK:
        con = db()
        for token in targets:
            needle = _like_contains(token)
            for row in con.execute("SELECT id, body FROM sources WHERE body LIKE ? ESCAPE '\\'", (needle,)):
                new = row["body"].replace(token, "已忘掉")
                con.execute("UPDATE sources SET body=?, redacted=1 WHERE id=?", (new, row["id"]))
            for row in con.execute("SELECT id, text FROM chunks WHERE text LIKE ? ESCAPE '\\'", (needle,)):
                new = row["text"].replace(token, "已忘掉")
                if new.strip() == "已忘掉":
                    con.execute("UPDATE chunks SET text=?, vec_json=NULL WHERE id=?", (new, row["id"]))
                else:
                    con.execute("UPDATE chunks SET text=? WHERE id=?", (new, row["id"]))
                con.execute("DELETE FROM fts_chunk WHERE chunk_id=?", (str(row["id"]),))
                if new.strip() and new.strip() != "已忘掉":
                    con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(row["id"]), _tokens(new)))
            for row in con.execute(
                "SELECT id, text, quote FROM statements WHERE text LIKE ? ESCAPE '\\' OR quote LIKE ? ESCAPE '\\'",
                (needle, needle),
            ):
                new_text = (row["text"] or "").replace(token, "已忘掉")
                new_quote = (row["quote"] or "").replace(token, "已忘掉")
                con.execute("UPDATE statements SET text=?, quote=? WHERE id=?", (new_text, new_quote, row["id"]))
                con.execute("DELETE FROM fts_stmt WHERE stmt_id=?", (row["id"],))
                if new_text.strip() and new_text.strip() != "已忘掉":
                    con.execute("INSERT INTO fts_stmt(stmt_id,text) VALUES(?,?)", (row["id"], _tokens(new_text + " " + new_quote)))
        con.commit()
        _load_vecs(con)


def _like_contains(token: str) -> str:
    """LIKE pattern that treats `_` and `%` in a secret as literals."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _prefer_raw_query(query: str) -> bool:
    """Secrets, statutes, and field values are answered from the original text."""
    text = query or ""
    if find_secrets(text):
        return True
    return any(word in text for word in ("密钥", "令牌", "法条", "字段值"))


def _candidate_pool(limit: int) -> int:
    return max(int(limit) * 4, 20)


def _fts_top(table: str, id_col: str, match: str, limit: int):
    """按 FTS5 bm25/rank 取 top-k。

    rank 默认就是 bm25，数值越小越相关。只写 MATCH … LIMIT、不写 ORDER BY rank
    时按 rowid 出，早入库的弱命中会占满 top-k。QA-BENCH-001 / BUG-001。
    """
    if table not in {"fts_stmt", "fts_chunk", "fts_crystal"} or id_col not in {"stmt_id", "chunk_id", "crystal_id"}:
        raise ValueError("不支持的全文表")
    if (table == "fts_crystal") != (id_col == "crystal_id"):
        raise ValueError("不支持的全文表")
    return db().execute(
        f"SELECT {id_col} FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    )


def _search_chunk(ch: sqlite3.Row, *, semantic: bool, prefer_raw: bool) -> dict:
    item = {
        "source_id": ch["source_id"],
        "loc": ch["loc"],
        "text": ch["text"],
        "mark": "原文" if prefer_raw else "尚未写入结晶",
    }
    if semantic:
        item["match"] = "语义"
    return item


def _open_labels(name: str, envelope_json: str, extra: list[str]) -> tuple[str, list[str]]:
    """The title and the other names already used to open this page."""
    title = str(name or "").strip()
    aliases: list[str] = []

    def add(raw: str) -> None:
        text = str(raw or "").strip()
        if not text or text == title or text in aliases or text in {"尚无来源", "暂无来源"}:
            return
        aliases.append(text)

    try:
        envelope = json.loads(envelope_json or "{}")
    except json.JSONDecodeError:
        envelope = {}
    raw_aliases = envelope.get("aliases") if isinstance(envelope, dict) else None
    if isinstance(raw_aliases, str):
        raw_aliases = re.split(r"[、,，]", raw_aliases)
    if isinstance(raw_aliases, list):
        for item in raw_aliases:
            add(str(item))
    keys = envelope.get("keys") if isinstance(envelope, dict) and isinstance(envelope.get("keys"), dict) else {}
    for key in ("真名", "别名"):
        raw = str(keys.get(key) or "")
        if raw in {"", "尚无来源", "暂无来源"}:
            continue
        for part in re.split(r"[、,，]", raw):
            add(part)
    for item in extra:
        add(item)
    return title, aliases


def _pages_opened_by(query: str, limit: int) -> list[tuple[str, dict]]:
    """Pages whose published name is what the query is asking for.

    Full-text search scores a page as one long document. A shorter page that
    only mentions the name then outranks the page a reader would open by that
    name. This lane asks the same question the rest of the system asks when it
    opens a page: is this query one of the names the page already publishes?
    """
    from rogmem.crystal_index import excerpt, readable_text
    from rogmem.distill_match import page_identity

    text = (query or "").strip()
    if not text or limit <= 0:
        return []
    owner_extra = _owner_labels()
    ranked: list[tuple[int, int, str, str]] = []
    rows = db().execute(
        """SELECT id, name, IFNULL(envelope_json,'') AS envelope_json
           FROM crystallizations WHERE status='active'"""
    )
    for row in rows:
        extra = owner_extra if row["id"] == "owner" else []
        title, aliases = _open_labels(row["name"] or "", row["envelope_json"] or "", extra)
        identity = page_identity(text, title, aliases)
        if identity is None:
            continue
        tier, span = identity
        ranked.append((tier, -span, title, row["id"]))
    ranked.sort()
    hits: list[tuple[str, dict]] = []
    for _tier, _span, _title, crystal_id in ranked[:limit]:
        page = db().execute(
            """SELECT id, name, IFNULL(body_md,'') AS body_md, IFNULL(sketch_md,'') AS sketch_md
               FROM crystallizations WHERE id=?""",
            (crystal_id,),
        ).fetchone()
        if not page:
            continue
        shown = readable_text(page["name"] or "", page["body_md"] or "", page["sketch_md"] or "")
        hits.append(
            (
                "page:" + page["id"],
                {
                    "crystal": page["name"] or "",
                    "field": "正文",
                    "text": excerpt(shown, text) if shown else (page["name"] or ""),
                    "source_id": "",
                },
            )
        )
    return hits


def search(query: str, limit: int = 5) -> dict:
    from rogmem.retrieve import rrf_fuse

    limit = max(int(limit), 0)
    prefer_raw = _prefer_raw_query(query)
    if limit == 0:
        return {"crystals": [], "raw": [], "prefer_raw": prefer_raw, "embed": _EMBED}
    pool = _candidate_pool(limit)
    mq = _match_query(query)
    identity: list[tuple[str, dict]] = []
    if not prefer_raw:
        identity = _pages_opened_by(query, pool)
    statements: list[tuple[str, dict]] = []
    chunks: list[tuple[str, dict]] = []
    if mq:
        for row in _fts_top("fts_stmt", "stmt_id", mq, pool):
            st = db().execute(
                "SELECT * FROM statements WHERE id=? AND invalid_at IS NULL AND trust='written'",
                (row["stmt_id"],),
            ).fetchone()
            if not st:
                continue
            crystal = db().execute("SELECT name FROM crystallizations WHERE id=?", (st["crystal_id"],)).fetchone()
            statements.append(
                (
                    st["id"],
                    {
                        "crystal": crystal["name"] if crystal else "",
                        "field": st["field"],
                        "text": st["text"],
                        "source_id": st["source_id"],
                    },
                )
            )
        from rogmem.crystal_index import excerpt, readable_text

        for row in _fts_top("fts_crystal", "crystal_id", mq, pool):
            page = db().execute(
                "SELECT id, name, status, IFNULL(body_md,'') AS body_md, IFNULL(sketch_md,'') AS sketch_md FROM crystallizations WHERE id=?",
                (row["crystal_id"],),
            ).fetchone()
            if not page or page["status"] != "active":
                continue
            shown = readable_text(page["name"] or "", page["body_md"] or "", page["sketch_md"] or "")
            statements.append(
                (
                    "page:" + page["id"],
                    {
                        "crystal": page["name"] or "",
                        "field": "正文",
                        "text": excerpt(shown, query),
                        "source_id": "",
                    },
                )
            )
        for row in _fts_top("fts_chunk", "chunk_id", mq, pool):
            ch = db().execute("SELECT * FROM chunks WHERE id=?", (int(row["chunk_id"]),)).fetchone()
            if not ch or ch["role"] == "parent" or "已忘掉" in (ch["text"] or ""):
                continue
            if ch["cited"] and not prefer_raw:
                continue
            chunks.append((str(ch["id"]), _search_chunk(ch, semantic=False, prefer_raw=prefer_raw)))
    vectors: list[tuple[str, dict]] = []
    if _VECS and query:
        qv = _embed_one(query)
        if qv:
            scored = []
            for cid, vec in _VECS:
                if len(vec) != len(qv):
                    continue
                scored.append((sum(a * b for a, b in zip(qv, vec)), cid))
            scored.sort(reverse=True)
            for _score, cid in scored[:pool]:
                ch = db().execute("SELECT * FROM chunks WHERE id=?", (cid,)).fetchone()
                if not ch or ch["role"] == "parent" or "已忘掉" in (ch["text"] or ""):
                    continue
                if ch["cited"] and not prefer_raw:
                    continue
                vectors.append((str(ch["id"]), _search_chunk(ch, semantic=True, prefer_raw=prefer_raw)))
    crystals = [] if prefer_raw else rrf_fuse([identity, statements])[:limit]
    raw = []
    seen: set[str] = set()
    for item in rrf_fuse([chunks, vectors]):
        if item["text"] in seen:
            continue
        seen.add(item["text"])
        raw.append(item)
        if len(raw) >= limit:
            break
    return {
        "crystals": crystals,
        "raw": raw,
        "prefer_raw": prefer_raw,
        "embed": _EMBED,
    }


def _split_addresses(text: str) -> list[str]:
    found: list[str] = []
    for piece in re.split(r"[、\n]", text or ""):
        address = piece.strip()
        if address and address not in found:
            found.append(address)
    return found


def _known_repository_slugs(row) -> set[str]:
    from rogmem.hermes_memory import repository_slug

    slugs: set[str] = set()
    for address in _project_addresses(row):
        slug = repository_slug(address, declared=True)
        if slug:
            slugs.add(slug)
    return slugs


def _settle_repository(con, crystal, repo: str) -> str:
    """Keep the address already written. A different repository is noted beside it.

    A repeat mention must not open another page and must not replace the
    address a person or an earlier write already settled.
    """
    from rogmem.hermes_memory import repository_slug

    incoming = repository_slug(repo or "", declared=True)
    if not incoming:
        return ""
    keys = crystal.keys if isinstance(crystal.keys, dict) else {}
    crystal.keys = keys
    current = str(keys.get("仓库地址") or "").strip()
    current_slug = repository_slug(current, declared=True)
    known: set[str] = set()
    for key in ("仓库地址", "repo", "其他仓库"):
        for address in _split_addresses(str(keys.get(key) or "")):
            slug = repository_slug(address, declared=True)
            if slug:
                known.add(slug)
    if not current_slug:
        keys["仓库地址"] = "https://" + incoming
        return f"仓库地址写成https://{incoming}。"
    if incoming in known:
        return ""
    for row in con.execute(
        "SELECT * FROM crystallizations WHERE type='工程' AND status='active' AND id!=?",
        (crystal.id,),
    ):
        if incoming in _known_repository_slugs(row):
            return ""
    extras = _split_addresses(str(keys.get("其他仓库") or ""))
    extras.append("https://" + incoming)
    keys["其他仓库"] = "、".join(extras)
    return f"另外记下仓库https://{incoming}。当前地址仍是{current}。"


def _assign_project_address(crystal, address: str) -> dict:
    """Replace the current repository address. Leaving it blank does not delete it."""
    from rogmem.hermes_memory import repository_slug

    keys = crystal.keys if isinstance(crystal.keys, dict) else {}
    crystal.keys = keys
    current = str(keys.get("仓库地址") or "").strip()
    text = (address or "").strip()
    if not text:
        if current and current not in {"尚无来源", "暂无来源"}:
            return {"ok": False, "error": "仓库地址留空不会删掉已经写下的地址"}
        return {"ok": True, "unchanged": True}
    slug = repository_slug(text, declared=True)
    if not slug:
        return {"ok": False, "error": "这不是一个仓库地址"}
    canonical = "https://" + slug
    if repository_slug(current, declared=True) == slug:
        return {"ok": True, "unchanged": True}
    if current and current not in {"尚无来源", "暂无来源"} and repository_slug(current, declared=True) not in {"", slug}:
        extras = _split_addresses(str(keys.get("其他仓库") or ""))
        if current not in extras:
            extras.append(current)
        keys["其他仓库"] = "、".join(extras)
    keys["仓库地址"] = canonical
    change = f"仓库地址改为{canonical}。"
    if current and current not in {"尚无来源", "暂无来源"}:
        change = f"仓库地址从{current}改为{canonical}。原来的地址还留着。"
    return {"ok": True, "change": change}


def _project_addresses(row, con=None) -> list[str]:
    """仓库地址 from thin keys and live statements. Name is not an address."""
    handle = con if con is not None else db()
    found: list[str] = []
    summary = _summary_slots_from_row(row)
    for key in ("仓库地址", "repo", "其他仓库"):
        for address in _split_addresses(str(summary.get(key) or "")):
            if address not in {"尚无来源", "暂无来源"} and address not in found:
                found.append(address)
    try:
        for stmt in handle.execute(
            "SELECT text FROM statements WHERE crystal_id=? AND field IN ('仓库地址','repo') AND invalid_at IS NULL",
            (row["id"],),
        ):
            text = str(stmt["text"] or "").strip()
            if text and text not in found:
                found.append(text)
    except Exception:
        pass
    body_repo = _repo_identity(str(hydrate(row).body_md or ""))
    if body_repo and body_repo not in found:
        found.append(body_repo)
    return found


def _project_ref_hits(ref: str, address: str) -> bool:
    """Same repository, whatever spelling each side used. A lookalike path does not count.

    A release page, a .git suffix, and wrapping marks are the same repository.
    """
    if not ref or not address:
        return False
    from rogmem.hermes_memory import repository_slug

    left = repository_slug(ref, declared=True)
    right = repository_slug(address, declared=True)
    if left and right:
        return left == right
    return ref == address


def _project_crystal(ref: str, con=None):
    if not ref:
        return None
    handle = con if con is not None else db()
    rows = handle.execute("SELECT * FROM crystallizations WHERE type='工程' AND status='active'").fetchall()
    hits = []
    for row in rows:
        if row["name"] == ref or any(_project_ref_hits(ref, address) for address in _project_addresses(row, handle)):
            hits.append(row)
    if not hits:
        return None
    keyed = [
        row
        for row in hits
        if _project_ref_hits(ref, str(_summary_slots_from_row(row).get("仓库地址") or ""))
    ]
    if len(keyed) == 1:
        return keyed[0]
    pool = keyed or hits
    if len(pool) == 1:
        return pool[0]
    return _older_crystal(handle, pool)


_FOCUS_FIELDS = frozenset({"当前关注"})
_FOCUS_CHAPTERS = frozenset({"当前关注与计划", "当前关注"})
_AGREE_FIELDS = frozenset({"约定待办"})
_AGREE_CHAPTERS = frozenset({"约定与待办", "约定待办"})
_STYLE_CHAPTERS = frozenset(
    {"沟通与决策风格", "能力与风格", "兴趣与偏好", "沟通风格", "决策风格", "兴趣爱好"}
)


_ENGINEERING_PROGRESS = (
    "正在推进",
    "在推进",
    "最近在搞",
    "在搞",
    "正在做",
    "进行中",
    "修了",
    "卡在",
)


def _is_focus_slot(field, chapter) -> bool:
    return (field or "") in _FOCUS_FIELDS or (chapter or "") in _FOCUS_CHAPTERS


def _has_engineering_progress(*parts: str) -> bool:
    hay = " ".join(str(part) for part in parts if part)
    return any(marker in hay for marker in _ENGINEERING_PROGRESS)


def _is_agree_slot(field, chapter) -> bool:
    return (field or "") in _AGREE_FIELDS or (chapter or "") in _AGREE_CHAPTERS


def _restricted_agent_slot(field, chapter) -> bool:
    if (field or "") in STYLE or (field or "") in _AGREE_FIELDS:
        return True
    return (chapter or "") in _STYLE_CHAPTERS or (chapter or "") in _AGREE_CHAPTERS


def _soft_ttl_days() -> int:
    raw = os.environ.get("ROGMEM_SOFT_TTL_DAYS") or ""
    if not raw:
        try:
            raw = get_setting("soft_ttl_days") or "30"
        except Exception:
            raw = "30"
    try:
        days = int(str(raw).strip())
    except ValueError:
        return 30
    return days if days > 0 else 30


def _event_date(event_at) -> datetime:
    text = str(event_at or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed
        except ValueError:
            try:
                return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return datetime.now(timezone.utc)


def _resolve_statement_valid_until(field, chapter, event_at, valid_until) -> str | None:
    if _is_agree_slot(field, chapter):
        return None
    parsed = parse_valid_until(valid_until)
    if parsed:
        return parsed
    if not _is_focus_slot(field, chapter):
        return None
    return (_event_date(event_at).date() + timedelta(days=_soft_ttl_days())).isoformat()


def _as_clock(now) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if now:
        try:
            parsed = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _valid_until_expired(valid_until, now: datetime) -> bool:
    parsed = parse_valid_until(valid_until)
    if not parsed:
        return False
    return datetime.strptime(parsed, "%Y-%m-%d").date() < now.date()


def _row_event_at(row) -> str | None:
    if "event_at" in row.keys() and row["event_at"]:
        return row["event_at"]
    if "created_at" in row.keys() and row["created_at"]:
        return row["created_at"]
    return None


def _effective_valid_until(row, *, allow_undated: bool):
    until = row["valid_until"] if "valid_until" in row.keys() else None
    if until or allow_undated:
        return until
    field = row["field"] if "field" in row.keys() else None
    chapter = row["chapter"] if "chapter" in row.keys() else None
    return _resolve_statement_valid_until(field, chapter, _row_event_at(row), None)


def _backfill_undated_focus_statements(con) -> int:
    """Write soft-TTL valid_until onto stock undated 当前关注. History stays; no delete."""
    try:
        rows = con.execute(
            """SELECT id, field, chapter, event_at, created_at, valid_until
               FROM statements
               WHERE invalid_at IS NULL
                 AND (valid_until IS NULL OR TRIM(valid_until)='')
                 AND (COALESCE(field,'') IN ('当前关注')
                      OR COALESCE(chapter,'') IN ('当前关注与计划','当前关注'))"""
        ).fetchall()
    except sqlite3.Error:
        return 0
    changed = 0
    for row in rows:
        until = _resolve_statement_valid_until(
            row["field"], row["chapter"], _row_event_at(row), None
        )
        if not until:
            continue
        con.execute("UPDATE statements SET valid_until=? WHERE id=?", (until, row["id"]))
        changed += 1
    return changed


def _source_project_ref(source) -> str:
    if source is None:
        return ""
    try:
        return str(source["project_ref"] or "").strip()
    except Exception:
        return ""


def _project_short_name(repo: str, fallback: str = "") -> str:
    text = (repo or "").rstrip("/").split("/")[-1]
    if text.endswith(".git"):
        text = text[:-4]
    return text or fallback or repo


def _filter_restricted_agent_ops(con, ops: list[dict], holder: str | None) -> list[dict]:
    source = _holder_source(con, holder)
    if source is None or rank_of(source) not in {"agent", "summary"}:
        return list(ops)
    kept = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        field, chapter = _op_statement_slot(op)
        if _restricted_agent_slot(field, chapter):
            continue
        kept.append(op)
    return kept


def _ensure_project_for_repo(con, source, *, name: str, repo: str, quote: str, event_at) -> str:
    existing = _project_crystal(repo, con)
    if existing is not None:
        return existing["id"]
    named = con.execute(
        "SELECT id FROM crystallizations WHERE type='工程' AND name=? AND status='active'",
        (name,),
    ).fetchone()
    if named:
        return named["id"]
    cid = secrets.token_hex(8)
    holder = f"distill:{source['id']}"
    _insert_crystal_page(con, cid, "工程", name or _project_short_name(repo), holder=holder)
    _add_statement(
        con, cid, "名称", None, name or _project_short_name(repo), quote or name, source["id"], rank_of(source), "written", event_at
    )
    _add_statement(con, cid, "仓库地址", None, repo, quote or repo, source["id"], rank_of(source), "written", event_at)
    return cid


def _ensure_owner_engineering_focus(con, source, *, name: str, repo: str, quote: str, event_at, status: str = "") -> None:
    focus = f"正在推进：{name}（{repo}）"
    extra = (status or "").strip()
    if extra and extra not in focus:
        focus = f"{focus}——{extra[:80]}"
    _add_statement(
        con,
        "owner",
        "当前关注",
        "当前关注与计划",
        focus,
        quote or focus,
        source["id"],
        rank_of(source),
        "written",
        event_at,
    )


def _maybe_dual_land_engineering(
    con,
    source,
    *,
    crystal_id: str,
    crystal_type: str,
    field,
    chapter,
    text: str,
    quote: str,
    event_at,
    holder: str | None,
) -> None:
    if source is None:
        return
    hay = " ".join(part for part in (text, quote, _source_project_ref(source)) if part)
    repo = _repo_identity(hay) or _source_project_ref(source)
    if not repo:
        return
    if crystal_type == "工程" or (field or "") == "仓库地址":
        body = ""
        try:
            body = str(source["body"] or "")
        except Exception:
            body = ""
        if not _has_engineering_progress(text, quote, body):
            return
        row = con.execute("SELECT name FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
        name = (row["name"] if row else "") or _project_short_name(repo)
        _ensure_owner_engineering_focus(
            con, source, name=name, repo=repo, quote=quote or text, event_at=event_at, status=text
        )
        return
    if _is_focus_slot(field, chapter) or crystal_id in {"owner", ""}:
        _ensure_project_for_repo(
            con,
            source,
            name=_project_short_name(repo),
            repo=repo,
            quote=quote or text,
            event_at=event_at,
        )


def _slice_live_rows(rows, *, field_names, chapter_names, now: datetime, allow_undated: bool) -> tuple[list[str], list[str]]:
    live: list[str] = []
    expired: list[str] = []
    for row in rows:
        field = row["field"] or ""
        chapter = row["chapter"] or ""
        if field not in field_names and chapter not in chapter_names:
            continue
        text = (row["text"] or "").strip()
        if not text:
            continue
        until = _effective_valid_until(row, allow_undated=allow_undated)
        if _valid_until_expired(until, now):
            expired.append(text)
            continue
        if not allow_undated and not until:
            expired.append(text)
            continue
        live.append(text)
    return live, expired


CORE_BUDGET_DEFAULT = 15000
CORE_BUDGET_MIN = 1000
CORE_BUDGET_MAX = 200000
FILE_TYPES = ("txt", "md", "markdown", "pdf", "docx", "pptx", "xlsx", "xls", "csv")
# One document is read in this process. A larger one is refused on its own,
# before it can reset the connection the rest of the batch is still using.
DOCUMENT_MAX_BYTES = 1024 * 1024 * 1024
REPLY_CLIP = 200
_CLIP_MARK = "（此条已截断）"


def _parse_before(before: str) -> tuple[str, str]:
    text = (before or "").strip()
    if not text:
        return "", ""
    if "\t" in text:
        at, _, sid = text.partition("\t")
        return at, sid
    return "", text


def _turn_in_project(project_ref: str, project: str) -> bool:
    ref = (project_ref or "").strip()
    if not ref or not project:
        return False
    if ref == project or _project_ref_hits(project, ref) or _project_ref_hits(ref, project):
        return True
    row = _project_crystal(project)
    if row is None:
        return False
    return any(_project_ref_hits(ref, address) for address in _project_addresses(row))


def _older_than(event_at: str, source_id: str, before_at: str, before_id: str) -> bool:
    if not before_at and not before_id:
        return True
    ev = event_at or ""
    if before_at:
        if ev < before_at:
            return True
        if ev > before_at:
            return False
        return bool(before_id) and source_id < before_id
    return bool(before_id) and source_id < before_id


_RECENT_PAGE = 32
_RECENT_SCAN = 800
_PREFIX_SLACK = 64


def _recent_identity(before_at: str, before_id: str, limit: int, offset: int):
    """Newest turn identities. The index carries this order, so the body stays unread."""
    sql = """SELECT id, kind, IFNULL(event_at,'') AS event_at, IFNULL(project_ref,'') AS project_ref
             FROM sources
             WHERE redacted=0 AND kind IN ('user','agent')"""
    params: list = []
    if before_at:
        sql += " AND (IFNULL(event_at,'') < ? OR (IFNULL(event_at,'') = ? AND id < ?))"
        params.extend([before_at, before_at, before_id])
    sql += " ORDER BY IFNULL(event_at,'') DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return db().execute(sql, params).fetchall()


def _spoken_prefix(source_id: str, kind: str, room: int) -> str:
    """Return the prefix the packer can still show. Rows the budget never reaches are not opened."""
    need = REPLY_CLIP + _PREFIX_SLACK if kind == "agent" else max(room, 0) + _PREFIX_SLACK
    row = db().execute(
        "SELECT substr(trim(body), 1, ?) AS body FROM sources WHERE id=?",
        (need, source_id),
    ).fetchone()
    body = redact_text(((row["body"] if row else "") or "").strip())
    if kind == "agent" and len(body) > REPLY_CLIP:
        body = body[:REPLY_CLIP].rstrip() + _CLIP_MARK
    return body


def _has_spoken(before_at: str, before_id: str) -> bool:
    scanned = 0
    offset = 0
    while scanned < _RECENT_SCAN:
        rows = _recent_identity(before_at, before_id, _RECENT_PAGE, offset)
        if not rows:
            return False
        offset += len(rows)
        for row in rows:
            scanned += 1
            if not _older_than(row["event_at"], row["id"], before_at, before_id):
                continue
            if _spoken_prefix(row["id"], row["kind"], 1):
                return True
            if scanned >= _RECENT_SCAN:
                return False
    return False


def _pack_recent(budget: int, project: str, before: str) -> tuple[list[dict], bool, dict | None]:
    """Newest user lines and clipped replies, across repositories. Files stay out.

    The budget is a character budget. Turn identities come off the recent index;
    a body is read only until that budget is full, and only the prefix that can fit.
    """
    del project
    before_at, before_id = _parse_before(before)
    if budget <= 0:
        return [], _has_spoken(before_at, before_id), None
    loaded: list[dict] = []
    used = 0
    truncated = False
    oldest = None
    first_spoken = None
    scanned = 0
    offset = 0
    while scanned < _RECENT_SCAN and not truncated:
        rows = _recent_identity(before_at, before_id, min(_RECENT_PAGE, _RECENT_SCAN - scanned), offset)
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            scanned += 1
            if not _older_than(row["event_at"], row["id"], before_at, before_id):
                continue
            room = budget - used
            body = _spoken_prefix(row["id"], row["kind"], room)
            if not body:
                continue
            if first_spoken is None:
                first_spoken = row
            if room <= 0:
                truncated = True
                break
            prefix = "用户：" if row["kind"] == "user" else "回复："
            text = prefix + body
            if len(text) > room:
                if room <= len(prefix) + len(_CLIP_MARK):
                    truncated = True
                    break
                text = text[: room - len(_CLIP_MARK)].rstrip() + _CLIP_MARK
                truncated = True
                loaded.append(_excerpt_item(row, text))
                oldest = row
                break
            loaded.append(_excerpt_item(row, text))
            used += len(text)
            oldest = row
    continuation = None
    if truncated and oldest is not None:
        continuation = {"before": f"{oldest['event_at']}\t{oldest['id']}"}
    elif truncated and first_spoken is not None:
        continuation = {"before": f"{first_spoken['event_at']}\t\uffff"}
    return loaded, truncated, continuation


def _pack_sketches(sections: list[dict], budget: int) -> tuple[list[dict], list[str], int, bool]:
    """Sketches first. A block that crosses the budget is cut and marked; later blocks wait."""
    loaded: list[dict] = []
    omitted: list[str] = []
    used = 0
    stopped = False
    cut = False
    for sec in sections:
        if stopped or budget - used <= 0:
            omitted.append(sec["name"])
            stopped = True
            continue
        text = sec["text"]
        room = budget - used
        if len(text) <= room:
            loaded.append(sec)
            used += len(text)
            continue
        if room <= len(_CLIP_MARK):
            omitted.append(sec["name"])
            stopped = True
            cut = True
            continue
        loaded.append({"name": sec["name"], "text": text[: room - len(_CLIP_MARK)].rstrip() + _CLIP_MARK})
        used = budget
        stopped = True
        cut = True
    return loaded, omitted, used, cut


def _excerpt_item(row, text: str) -> dict:
    return {
        "name": "最近摘录",
        "text": text,
        "source_id": row["id"],
        "at": row["event_at"],
        "project": (row["project_ref"] or "").strip(),
    }


def _core_convention(*, continued: bool, truncated: bool, continuation: dict | None, project: str) -> str:
    if continued:
        text = "这是更早的摘录，不是历史包。速写不在这次返回里。绝对禁止把这次当成新的历史包。"
    else:
        text = (
            "这是历史包。顺序是人物速写、工程速写，然后才是最近摘录。"
            "身份、偏好、工程规范以速写为准。近况若摘录里的原句更新，以摘录为准。用户本场原话高于这包。"
            "最近摘录按时间排，不按仓库切开。有仓库标记的带着，没有就空着。"
        )
        if not (project or "").strip():
            text += "没有传入仓库远程地址或根提交，这次没有工程速写。"
    if truncated and continuation and continuation.get("before"):
        text += (
            f" 最近摘录被截断。要更早的原文，必须再调用 core，传入 before={continuation['before']}。"
            "这次只返回更早摘录。绝对禁止用 search 代替这次续读。"
        )
    elif truncated:
        text += " 有内容没装进这次预算。绝对禁止把没装进来的当成已知。"
    text += " 按一个具体问题查找用 search。绝对禁止用 search 顶替历史包。"
    return text


def _finish_core(
    loaded: list[dict],
    omitted: list[str],
    truncated: bool,
    continuation: dict | None,
    *,
    continued: bool,
    project: str,
) -> dict:
    from rogmem.host_rules import RULES_REV

    return {
        "loaded": loaded,
        "omitted": omitted,
        "truncated": truncated,
        "continuation": continuation,
        "rules_rev": RULES_REV,
        "rules_block": None,
        "convention": _core_convention(
            continued=continued, truncated=truncated, continuation=continuation, project=project
        ),
        "llm": False,
    }


def core_memory(
    budget: int = CORE_BUDGET_DEFAULT,
    project: str = "",
    *,
    before: str = "",
    now: str | datetime | None = None,
) -> dict:
    row = db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
    summary = _summary_slots_from_row(row) if row else {}
    birth = _birth_text(summary)
    if birth:
        summary["出生时间"] = birth
        summary["年龄"] = _age(birth)
    org = summary.get("所属组织及职务") or summary.get("所属组织")
    if org and org != "尚无来源":
        summary["所属组织及职务"] = org
    crystal = hydrate(row) if row else None
    owner_sketch = (crystal.sketch_md or "").strip() if crystal is not None else ""
    project_row = _project_crystal(project)
    project_sketch_item = None
    if project_row is not None:
        project_sketch = (hydrate(project_row).sketch_md or "").strip()
        if project_sketch:
            project_sketch_item = {"name": project_row["name"] or "工程", "text": project_sketch}
    before_text = (before or "").strip()
    if before_text:
        loaded, truncated, continuation = _pack_recent(budget, project, before_text)
        omitted = ["最近摘录"] if truncated else []
        return _finish_core(loaded, omitted, truncated, continuation, continued=True, project=project)
    if owner_sketch:
        sketch_sections = [{"name": "记忆主人", "text": owner_sketch}]
        if project_sketch_item is not None:
            sketch_sections.append(project_sketch_item)
        loaded, omitted, used, sketch_cut = _pack_sketches(sketch_sections, budget)
        excerpts, cut, continuation = _pack_recent(max(0, budget - used), project, "")
        if cut and not excerpts:
            omitted.append("最近摘录")
        truncated = sketch_cut or cut or bool(omitted)
        loaded.extend(excerpts)
        return _finish_core(loaded, omitted, truncated, continuation, continued=False, project=project)
    parsed = _parse_body_sections(crystal.body_md if crystal else "")
    sections: list[dict] = []
    if crystal is not None:
        alias_text = "、".join(crystal.aliases) if crystal.aliases else ""
        bits = []
        if crystal.name:
            bits.append(f"真名：{crystal.name}" if crystal.id == "owner" else f"名称：{crystal.name}")
        if alias_text:
            bits.append(f"别名：{alias_text}")
        if bits:
            sections.append({"name": "信封", "text": "\n".join(bits)})
    covered: set[str] = set()
    for heading, text in parsed.items():
        if heading and text:
            sections.append({"name": heading, "text": text})
            covered.add(heading)
    for key in OWNER_FIELDS:
        if key in covered or key == "概述":
            continue
        value = summary.get(key)
        if value in {None, "", "尚无来源", "暂无来源"}:
            continue
        sections.append({"name": key, "text": f"{key}：{value}"})
        if key == "出生时间" and summary.get("年龄") not in {None, "", "尚无来源"}:
            sections.append({"name": "年龄", "text": f"年龄：{summary['年龄']}"})
    leftover = []
    if project_sketch_item is not None:
        leftover.append(project_sketch_item)
    elif project_row is not None:
        project_summary = _summary_slots_from_row(project_row)
        for key, value in project_summary.items():
            leftover.append({"name": f"工程·{project_row['name']}·{key}", "text": f"{key}：{value}"})
    sections.extend(leftover)
    loaded = []
    omitted = []
    used = 0
    for sec in sections:
        if used and used + len(sec["text"]) > budget:
            omitted.append(sec["name"])
            continue
        if not loaded and len(sec["text"]) > budget:
            omitted.append(sec["name"])
            continue
        loaded.append(sec)
        used += len(sec["text"])
    excerpts, cut, continuation = _pack_recent(max(0, budget - used), project, "")
    if cut and not excerpts:
        omitted.append("最近摘录")
    loaded.extend(excerpts)
    truncated = bool(omitted) or cut
    return _finish_core(loaded, omitted, truncated, continuation, continued=False, project=project)


_REPO_URL = re.compile(
    r"(?:(?:https?://|git@)[^\s]+|git://[^\s]+|(?:github|gitlab)\.com[/:][A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.I,
)
_REPO_SHA = re.compile(r"\b[0-9a-f]{40}\b", re.I)


def _repo_identity(text: str) -> str:
    """A git repository, not the first URL in the paragraph.

    A release page and a .git suffix are that same repository. A health check
    or a console link is not a repository.
    """
    from rogmem.hermes_memory import repository_slug

    slug = repository_slug(text or "")
    if slug:
        return "https://" + slug
    found = _REPO_SHA.search(text or "")
    return found.group(0) if found else ""


def _bind_source_cites(body: str, source_id: str, source_text: str = "") -> str:
    """Attach this material's address to notes whose passage is in this material."""
    from rogmem.pages_md import citation_passage

    sid = (source_id or "").strip()
    if not sid:
        return body or ""
    link = f"/admin/sources/{sid}"

    def repl(match: re.Match) -> str:
        note = match.group(2)
        if link in note or not note.startswith("实"):
            return match.group(0)
        passage = citation_passage(note)
        if source_text and passage and passage not in source_text:
            return match.group(0)
        return f"[^{match.group(1)}]: 实 {link} {note[1:].lstrip()}"

    return re.sub(r"^\[\^([^\]]+)\]:\s*(.*)$", repl, body or "", flags=re.M)


def _engineering_identity(source, page: dict) -> str:
    """The repository this mention is about. Empty means it is not an 工程.

    The session's own project parameter wins. Otherwise the first real
    repository in the page, not the first link.
    """
    from rogmem.hermes_memory import repository_slug, repository_slugs

    ref = _source_project_ref(source)
    declared = repository_slug(ref, declared=True)
    if declared:
        return "https://" + declared
    for raw in (page.get("body") or "", page.get("name") or ""):
        found = repository_slugs(raw)
        if found:
            return "https://" + found[0]
    return ""


@charging("专名核对")
def _proper_name_ok(name: str, ctype: str, quote: str) -> bool:
    target = llm_target("distill")
    if not target:
        return False
    base, key, model = target
    payload = {
        "model": model,
        "max_tokens": _chat_max_tokens(base, model),
        "messages": [
            {"role": "system", "content": PROPER_NAME_SYSTEM},
            {"role": "user", "content": proper_name_user(name, ctype, quote)},
        ],
    }
    try:
        content, _reason = _complete_chat(base, key, payload, role="distill")
        data = _parse_model_json(content)
    except Exception:
        return False
    return data.get("answer") == "是"


def withdraw_projects_without_repo() -> int:
    """A project page without a remote or root commit is not a project."""
    changed = 0
    with LOCK:
        con = db()
        rows = con.execute(
            "SELECT * FROM crystallizations WHERE type='工程' AND status='active'"
        ).fetchall()
        now = utcnow()
        for row in rows:
            summary = _summary_slots_from_row(row)
            statements = con.execute(
                "SELECT text, quote FROM statements WHERE crystal_id=? AND invalid_at IS NULL",
                (row["id"],),
            ).fetchall()
            hay = "\n".join(
                [summary.get("仓库地址") or ""]
                + [(item["text"] or "") + "\n" + (item["quote"] or "") for item in statements]
            )
            if _repo_identity(hay):
                continue
            if (row["body_md"] or "").strip():
                continue
            con.execute("UPDATE crystallizations SET status='已撤回' WHERE id=?", (row["id"],))
            con.execute(
                "UPDATE statements SET invalid_at=? WHERE crystal_id=? AND invalid_at IS NULL",
                (now, row["id"]),
            )
            con.execute("UPDATE edges SET ended=1 WHERE (src=? OR dst=?) AND ended=0", (row["id"], row["id"]))
            _record_revision(
                con,
                row["id"],
                "撤回：工程没有仓库身份",
                actor="系统",
                change=f"撤回「{row['name']}」。",
                basis="工程没有仓库身份，不能作为工程发布。",
            )
            changed += 1
        if changed:
            con.commit()
    return changed


def _crystals_named(con: sqlite3.Connection, text: str, self_id: str) -> list:
    """Exact active names only. A shorter name covered by a longer one does not count."""
    raw = text or ""
    rows = con.execute(
        "SELECT id, name FROM crystallizations WHERE status='active' AND id!=? ORDER BY length(name) DESC",
        (self_id,),
    ).fetchall()
    occupied: list[tuple[int, int]] = []
    found = []
    for row in rows:
        name = row["name"] or ""
        if len(name) < 2:
            continue
        start = 0
        bare = False
        while True:
            at = raw.find(name, start)
            if at < 0:
                break
            end = at + len(name)
            if not any(left <= at and end <= right for left, right in occupied):
                bare = True
                occupied.append((at, end))
            start = at + len(name)
        if bare:
            found.append(row)
    return found


def _relation_kinds(chapter: str, text: str, name: str) -> list[str]:
    gap = r".{0,12}"
    social = rf"(?:同事|家人|朋友|配偶|同学|社交){gap}{re.escape(name)}|{re.escape(name)}{gap}(?:同事|家人|朋友|配偶|同学|社交)"
    linked = rf"关联{gap}{re.escape(name)}|{re.escape(name)}{gap}关联"
    kinds = []
    if chapter == "社交关系" or re.search(social, text or ""):
        kinds.append("社交")
    if chapter == "关联" or re.search(linked, text or ""):
        kinds.append("关联")
    return kinds


def _link_relations(con: sqlite3.Connection, statement_id: str = "") -> None:
    con.execute(
        """UPDATE edges SET ended=1
           WHERE kind IN ('社交','关联') AND ended=0
             AND statement_id NOT IN (
               SELECT id FROM statements WHERE invalid_at IS NULL AND trust='written'
             )"""
    )
    sql = """SELECT id, crystal_id, chapter, text FROM statements
             WHERE invalid_at IS NULL AND trust='written'"""
    args: tuple = ()
    if statement_id:
        sql += " AND id=?"
        args = (statement_id,)
    for row in con.execute(sql, args):
        for other in _crystals_named(con, row["text"] or "", row["crystal_id"]):
            for kind in _relation_kinds(row["chapter"] or "", row["text"] or "", other["name"]):
                exists = con.execute(
                    """SELECT 1 FROM edges
                       WHERE src=? AND dst=? AND kind=? AND statement_id=? AND ended=0""",
                    (row["crystal_id"], other["id"], kind, row["id"]),
                ).fetchone()
                if exists:
                    continue
                con.execute(
                    "INSERT INTO edges(id,src,dst,kind,statement_id,ended) VALUES(?,?,?,?,?,0)",
                    (secrets.token_hex(8), row["crystal_id"], other["id"], kind, row["id"]),
                )


def _org_named_in(con: sqlite3.Connection, text: str):
    exact = con.execute(
        "SELECT id, name FROM crystallizations WHERE type='组织' AND status='active' AND name=?",
        (text,),
    ).fetchone()
    if exact:
        return exact
    rows = con.execute(
        "SELECT id, name FROM crystallizations WHERE type='组织' AND status='active'"
    ).fetchall()
    hits = [row for row in rows if row["name"] and row["name"] in (text or "")]
    if not hits:
        return None
    hits.sort(key=lambda row: len(row["name"]), reverse=True)
    return hits[0]


def _birth_text(summary: dict) -> str:
    for key in ("出生时间", "出生日期"):
        value = summary.get(key)
        if value and value != "尚无来源":
            return value
    return ""


def _age(date_text: str) -> str:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", date_text)
    if not m:
        return "尚无来源"
    born = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    years = now.year - born.year - ((now.month, now.day) < (born.month, born.day))
    return str(years)


def backfill_revision_basis(con) -> None:
    """A stored basis that no longer fits a log line is the evidence index. Keep the claim."""
    rows = con.execute(
        "SELECT id, basis FROM revisions WHERE basis IS NOT NULL AND length(basis) > ?",
        (_GIST_LIMIT,),
    ).fetchall()
    for row in rows:
        fitted = clip_revision_gist(row["basis"])
        if fitted != (row["basis"] or ""):
            con.execute("UPDATE revisions SET basis=? WHERE id=?", (fitted, row["id"]))


def backfill_revision_scopes(con) -> None:
    """Fill scope once from stored snapshots. Later reads do not open the snapshots."""
    crystals = con.execute("SELECT DISTINCT crystal_id FROM revisions WHERE scope IS NULL").fetchall()
    blank = {"name": "", "body": "", "sketch": ""}
    for crystal in crystals:
        rows = con.execute(
            "SELECT id, page_json, scope FROM revisions WHERE crystal_id=? ORDER BY id",
            (crystal["crystal_id"],),
        ).fetchall()
        before = None
        for row in rows:
            state = _parse_page_json(row["page_json"])
            if row["scope"] is None:
                if before is None and state is not None:
                    scope = _scope_between(blank, state) or "未改标题、正文或速写"
                elif isinstance(before, dict) and state is not None:
                    scope = _scope_between(before, state) or "未改标题、正文或速写"
                else:
                    scope = ""
                con.execute("UPDATE revisions SET scope=? WHERE id=?", (scope, row["id"]))
            if state is not None:
                before = state
            elif before is not None:
                before = False


def settle_file_statuses() -> None:
    """Correct a file's status from whether its id has a revision. Do not read revision text."""
    with LOCK:
        con = db()
        migrate_source_statuses(con)
        demote = con.execute(
            """SELECT id FROM sources
               WHERE kind='file' AND status=?
                 AND NOT EXISTS (SELECT 1 FROM statements WHERE statements.source_id = sources.id)
                 AND NOT EXISTS (SELECT 1 FROM revisions WHERE revisions.source_id = sources.id)""",
            (ST_CRYSTAL,),
        ).fetchall()
        for row in demote:
            _mark_no_crystal(con, row["id"])
        promote = con.execute(
            """SELECT id, IFNULL(pipeline,'') AS pipeline, IFNULL(status,'') AS status FROM sources
               WHERE kind='file' AND status=?
                 AND EXISTS (SELECT 1 FROM revisions WHERE revisions.source_id = sources.id)
                 AND NOT EXISTS (SELECT 1 FROM statements WHERE statements.source_id = sources.id)""",
            (ST_NO_CRYSTAL,),
        ).fetchall()
        for row in promote:
            con.execute(
                "UPDATE sources SET status=?, coverage='pages' WHERE id=?",
                (ST_CRYSTAL, row["id"]),
            )
            if mark_is_no_crystal(row["pipeline"], row["status"]):
                _set_pipeline(con, row["id"], "完成", "ok")
        con.commit()


def rebuild_overviews() -> None:
    with LOCK:
        con = db()
        for row in con.execute("SELECT id, type, name FROM crystallizations"):
            _persist_overview(
                con,
                row["id"],
                row["type"],
                row["name"],
                holder="rebuild:overview",
            )


def display_status(kind: str, status: str) -> str:
    del kind
    return canonical_status(status)


def summary_fields(ctype: str, owner: bool = False) -> list[str]:
    if owner:
        return OWNER_FIELDS
    if ctype == "工程":
        return PROJECT_FIELDS
    if ctype == "组织":
        return ORG_FIELDS
    if ctype == "产品":
        return PRODUCT_FIELDS
    if ctype == "概念":
        return CONCEPT_FIELDS
    return PERSON_FIELDS


PAGE_SIZE = 50


def _page_bounds(page: int, total: int, page_size: int = PAGE_SIZE) -> tuple[int, int, int]:
    size = page_size if page_size > 0 else PAGE_SIZE
    pages = max(1, (total + size - 1) // size) if total else 1
    current = page if page > 0 else 1
    if current > pages:
        current = pages
    return current, pages, (current - 1) * size


_SHOWN_DEVICE = "COALESCE(NULLIF(a.device_name, ''), s.device_name)"
_SHOWN_AGENT = "COALESCE(NULLIF(a.display_name, ''), s.agent_name)"
_NAMED_SOURCES = "sources s LEFT JOIN agents a ON a.id = s.agent_id"


def _source_page(columns: str, ids: list[str]) -> list:
    """The body is read for this page only. Ordering must not touch it."""

    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    fetched = db().execute(
        f"SELECT {columns} FROM {_NAMED_SOURCES} WHERE s.id IN ({marks})",
        ids,
    ).fetchall()
    by_id = {row["id"]: row for row in fetched}
    return [by_id[item] for item in ids if item in by_id]


def list_sources(
    tab: str = "session",
    query: str = "",
    kind: str = "",
    device: str = "",
    agent: str = "",
    project: str = "",
    status: str = "",
    session: str = "",
    turn: str = "",
    page: int = 1,
) -> dict:
    where = ["s.kind='file'" if tab == "file" else "s.kind!='file'"]
    args: list[str] = []
    if kind:
        where.append("s.kind=?")
        args.append(kind)
    if device:
        where.append(f"IFNULL({_SHOWN_DEVICE},'')=?")
        args.append(device)
    if agent:
        where.append(f"IFNULL({_SHOWN_AGENT},'')=?")
        args.append(agent)
    if project:
        where.append("IFNULL(s.project_ref,'')=?")
        args.append(project)
    if session:
        where.append("IFNULL(s.session_id,'')=?")
        args.append(session)
    if turn:
        where.append("IFNULL(s.turn_id,'')=?")
        args.append(turn)
    if query:
        where.append("(s.body LIKE ? OR IFNULL(s.filename,'') LIKE ? OR s.id LIKE ?)")
        args.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
    joined = f"FROM {_NAMED_SOURCES} WHERE {' AND '.join(where)}"
    # Session rows hover the whole body. File rows show a filename, so a missing name only needs a short stand-in.
    held = "s.body" if tab != "file" else "substr(s.body,1,80)"
    columns = (
        f"s.id,s.kind,{held} AS preview,{_SHOWN_DEVICE} AS device_name,"
        f"{_SHOWN_AGENT} AS agent_name,s.status,s.coverage,s.ingested_at,"
        "s.project_ref,s.filename,s.last_error,s.redacted,s.pipeline"
    )
    if status:
        narrow = db().execute(
            f"SELECT s.id AS id, IFNULL(s.pipeline,'') AS pipeline, IFNULL(s.status,'') AS status {joined} ORDER BY s.ingested_at DESC",
            args,
        ).fetchall()
        ids = [row["id"] for row in narrow if source_progress_matches(row["pipeline"], row["status"], status)]
        total = len(ids)
        current, pages, offset = _page_bounds(page, total)
        chosen = ids[offset : offset + PAGE_SIZE]
        rows = _source_page(columns, chosen)
    else:
        total = int(db().execute(f"SELECT COUNT(*) AS n {joined}", args).fetchone()["n"])
        current, pages, offset = _page_bounds(page, total)
        chosen = [
            row["id"]
            for row in db().execute(
                f"SELECT s.id AS id {joined} ORDER BY s.ingested_at DESC LIMIT ? OFFSET ?",
                [*args, PAGE_SIZE, offset],
            ).fetchall()
        ]
        rows = _source_page(columns, chosen)
    out = []
    for row in rows:
        item = dict(row)
        item["status_label"] = display_status(item["kind"], item["status"])
        item["note"] = list_note(item)
        out.append(item)
    return {"rows": out, "total": total, "page": current, "pages": pages, "page_size": PAGE_SIZE}


def list_note(row: dict) -> str:
    """The remark on a source row. The list and an import receipt read it the same way."""
    if row.get("redacted"):
        return "已忘掉"
    if row.get("status") == ST_CRYSTAL and row.get("coverage") == "partial" and not row.get("last_error"):
        return "覆盖未齐"
    return row.get("last_error") or ""


def source_facets(tab: str) -> dict:
    where = "s.kind='file'" if tab == "file" else "s.kind!='file'"

    def uniq(expression: str) -> list[str]:
        found = db().execute(
            f"""SELECT DISTINCT {expression} AS value FROM {_NAMED_SOURCES}
                WHERE {where} AND IFNULL({expression}, '') != ''"""
        ).fetchall()
        return sorted(row["value"] for row in found)

    pairs = db().execute(
        f"SELECT DISTINCT s.kind AS kind, s.status AS status FROM {_NAMED_SOURCES} WHERE {where} AND IFNULL(s.status, '') != ''"
    ).fetchall()
    statuses = sorted({display_status(row["kind"], row["status"]) for row in pairs})
    return {
        "kinds": uniq("kind"),
        "devices": uniq(_SHOWN_DEVICE),
        "agents": uniq(_SHOWN_AGENT),
        "projects": uniq("s.project_ref"),
        "statuses": statuses,
    }


def resolve_source_origin(source_id: str) -> dict:
    """source_id → session/turn. Fail-soft: missing or unbound never raises."""
    sid = str(source_id or "").strip()
    empty = {
        "ok": False,
        "source_id": sid,
        "kind": "",
        "session_id": "",
        "turn_id": "",
        "session_href": "",
        "source_href": "",
    }
    if not sid:
        return empty
    try:
        row = db().execute(
            "SELECT id, kind, session_id, turn_id, filename FROM sources WHERE id=?",
            (sid,),
        ).fetchone()
    except Exception:
        return empty
    if not row:
        return empty
    session_id = str(row["session_id"] or "")
    turn_id = str(row["turn_id"] or "")
    links = origin_links(session_id, turn_id, row["id"], "")
    return {
        "ok": True,
        "source_id": row["id"],
        "kind": str(row["kind"] or ""),
        "session_id": session_id,
        "turn_id": turn_id,
        "filename": str(row["filename"] or ""),
        **links,
    }


def source_snapshot(source_id: str) -> dict | None:
    """JSON view of one source: status plus quotes, including invalidated rows."""
    row = db().execute(
        "SELECT id, filename, status, last_error, coverage, session_id, turn_id, kind FROM sources WHERE id=?",
        (source_id,),
    ).fetchone()
    if not row:
        return None
    statements = [
        dict(item)
        for item in db().execute(
            """SELECT id, field, text, quote, invalid_at
               FROM statements WHERE source_id=? ORDER BY created_at""",
            (source_id,),
        )
    ]
    origin = resolve_source_origin(source_id)
    return {**dict(row), "statements": statements, "origin": origin}


def cited_crystals(source_id: str) -> list[dict]:
    """Crystals that used this material. A new page and an edit of an old page both count.

    A sentence is included only when a statement or a citation note already ties it to this source.
    """
    if not source_id:
        return []
    from rogmem.pages_md import citation_passage, cite_notes

    link = f"/admin/sources/{source_id}"
    rows = db().execute(
        """SELECT id, name, type, body_md FROM crystallizations
           WHERE id IN (
             SELECT crystal_id FROM revisions WHERE source_id=? AND crystal_id IS NOT NULL
             UNION
             SELECT crystal_id FROM statements WHERE source_id=? AND crystal_id IS NOT NULL
           )
           OR instr(COALESCE(body_md, ''), ?) > 0
           ORDER BY CASE id WHEN 'owner' THEN 0 ELSE 1 END, name""",
        (source_id, source_id, link),
    ).fetchall()
    found = []
    for row in rows:
        passages: list[str] = []
        for stmt in db().execute(
            """SELECT text FROM statements
               WHERE source_id=? AND crystal_id=? AND text IS NOT NULL AND text != ''
               ORDER BY created_at""",
            (source_id, row["id"]),
        ):
            sentence = str(stmt["text"] or "").strip()
            if sentence in {"尚无来源", "暂无来源", "已忘掉"} or sentence in passages:
                continue
            passages.append(sentence)
        for note in cite_notes(row["body_md"] or "").values():
            if link not in note:
                continue
            passage = citation_passage(note)
            if passage and passage not in passages:
                passages.append(passage)
        found.append(
            {
                "id": row["id"],
                "name": row["name"] or row["id"],
                "type": row["type"] or "",
                "passages": passages,
            }
        )
    return found


def _with_host_names(row: sqlite3.Row) -> dict:
    """The names on a turn are the host's names when the turn belongs to one."""
    item = dict(row)
    agent_id = str(item.get("agent_id") or "")
    if not agent_id:
        return item
    host = db().execute(
        "SELECT device_name, display_name FROM agents WHERE id=?",
        (agent_id,),
    ).fetchone()
    if host is None:
        return item
    if host["device_name"]:
        item["device_name"] = host["device_name"]
    if host["display_name"]:
        item["agent_name"] = host["display_name"]
    return item


def source_detail(source_id: str) -> dict | None:
    row = db().execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if not row:
        return None
    statements = [
        dict(item)
        for item in db().execute(
            """SELECT s.id, s.field, s.chapter, s.text, s.quote, s.trust, s.chunk_seq, s.crystal_id, c.name AS crystal_name
               FROM statements s LEFT JOIN crystallizations c ON c.id=s.crystal_id
               WHERE s.source_id=? ORDER BY s.created_at""",
            (source_id,),
        )
    ]
    mate = None
    if row["turn_id"]:
        other = db().execute(
            "SELECT id,kind FROM sources WHERE turn_id=? AND id!=? LIMIT 1",
            (row["turn_id"], source_id),
        ).fetchone()
        mate = dict(other) if other else None
    source = _with_host_names(row)
    source["_cited"] = cited_crystals(source_id)
    source["_retained"] = _uncovered_sentences(row["body"] or "", [item.get("quote") or "" for item in statements])
    chunks = [
        dict(item)
        for item in db().execute(
            "SELECT seq, loc, text, cited FROM chunks WHERE source_id=? ORDER BY seq",
            (source_id,),
        )
    ]
    source["_uncovered_chunks"] = [item for item in chunks if not item["cited"]]
    source["_fate"] = _source_fate(
        row["body"] or "",
        statements,
        row["status"] in {ST_CRYSTAL, ST_NO_CRYSTAL, "已蒸馏", "覆盖完成"},
    )
    source["_origin"] = resolve_source_origin(source_id)
    return {"source": source, "statements": statements, "mate": mate}


def _source_sentences(body: str) -> list[str]:
    parts = []
    for piece in re.split(r"[。！？\n]", body or ""):
        text = piece.strip()
        if text:
            parts.append(text)
    return parts


def _source_fate(body: str, links: list[dict], settled: bool) -> list[dict]:
    usable = []
    for item in links:
        quote = item.get("quote") or ""
        text = (item.get("text") or "").strip()
        if not quote or "已忘掉" in quote or text in {"", "已忘掉"}:
            continue
        usable.append(item)
    out = []
    for part in _source_sentences(body):
        if "已忘掉" in part:
            out.append({"sentence": part, "fate": "丢掉", "crystal_id": "", "crystal_name": ""})
            continue
        hit = next((item for item in usable if part in (item.get("quote") or "") or (item.get("quote") or "") in part), None)
        if hit:
            out.append(
                {
                    "sentence": part,
                    "fate": "写入",
                    "crystal_id": hit.get("crystal_id") or "",
                    "crystal_name": hit.get("crystal_name") or "",
                }
            )
        elif settled and len(part) >= 8:
            out.append({"sentence": part, "fate": "丢掉", "crystal_id": "", "crystal_name": ""})
    return out


def revise_chunk(chunk_id: int, text: str) -> dict:
    """Replace one chunk. Rebuild only that chunk's index and vector. Do not re-distill the file."""
    new_text = (text or "").strip()
    if not new_text:
        return {"ok": False, "error": "修订不能是空的"}
    with LOCK:
        con = db()
        row = con.execute("SELECT id, role, source_id FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "没有这块"}
        queued = con.execute("SELECT id, state FROM queue WHERE source_id=?", (row["source_id"],)).fetchall()
        before = [(item["id"], item["state"]) for item in queued]
        previous = con.execute("SELECT text FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        con.execute(
            "INSERT INTO chunk_revisions(chunk_id, previous, created_at) VALUES(?,?,?)",
            (chunk_id, previous["text"] or "", utcnow()),
        )
        vec = None if row["role"] == "parent" else _embed_one(new_text)
        con.execute(
            "UPDATE chunks SET text=?, vec_json=?, embed_text=NULL WHERE id=?",
            (new_text, json.dumps(vec) if vec else None, chunk_id),
        )
        con.execute("DELETE FROM fts_chunk WHERE chunk_id=?", (str(chunk_id),))
        if row["role"] != "parent":
            con.execute("INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)", (str(chunk_id), _tokens(new_text)))
        after = con.execute("SELECT id, state FROM queue WHERE source_id=?", (row["source_id"],)).fetchall()
        con.commit()
        _load_vecs(con)
    return {"ok": True, "queue": before == [(item["id"], item["state"]) for item in after]}


def record_questions(chunk_id: int, questions: list[str]) -> dict:
    """Optional question lines go to the chunk index only, and only when explicitly enabled."""
    if (get_setting("index_questions") or "") != "on":
        return {"enabled": False, "indexed": 0}
    clean = [item.strip() for item in questions if item and item.strip()]
    if not clean:
        return {"enabled": True, "indexed": 0}
    with LOCK:
        con = db()
        row = con.execute("SELECT id, text FROM chunks WHERE id=? AND role!='parent'", (chunk_id,)).fetchone()
        if row is None:
            return {"enabled": True, "indexed": 0, "error": "没有这块"}
        statements = con.execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
        con.execute("DELETE FROM fts_chunk WHERE chunk_id=?", (str(chunk_id),))
        con.execute(
            "INSERT INTO fts_chunk(chunk_id,text) VALUES(?,?)",
            (str(chunk_id), _tokens((row["text"] or "") + "\n" + "\n".join(clean))),
        )
        con.commit()
        after = con.execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    return {"enabled": True, "indexed": len(clean), "statements_unchanged": statements == after}


# How long one user write waits for a connection that is not in this process.
# In-process writers already take LOCK and commit before they release it.
_WRITE_WAIT_MS = 20000


def _discard_connection(con) -> None:
    """Drop a connection whose rollback did not finish.

    Close and reopen stay inside this function. The caller still raises the
    original failure, and this connection is not handed out again. A close that
    does not return cannot be forced to drop the driver's lock.
    """
    global _DB, _DB_PATH
    try:
        con.close()
    except Exception:
        pass
    if getattr(_LOCAL, "con", None) is con:
        _LOCAL.con = None
        _LOCAL.path = None
    if _DB is not con:
        return
    _DB = None
    _DB_PATH = None
    if _DATA is None:
        return
    try:
        fresh = _open_connection(_DATA / "rogmem.sqlite")
    except Exception:
        return
    _DB = fresh
    _DB_PATH = (_DATA / "rogmem.sqlite").resolve()
    if threading.current_thread() is threading.main_thread() and getattr(_LOCAL, "con", None) is None:
        _LOCAL.con = fresh
        _LOCAL.path = _DB_PATH


def _rollback_write(con) -> None:
    try:
        con.rollback()
    except sqlite3.Error:
        _discard_connection(con)


def _finish_write(con, write):
    """Commit one write. On failure, undo it and keep the original error."""
    try:
        result = write()
        con.commit()
        return result
    except sqlite3.OperationalError as exc:
        _rollback_write(con)
        if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return {"ok": False, "error": "数据库正忙，请再试一次"}
        raise
    except BaseException:
        _rollback_write(con)
        raise


def _requeue_source(con, source_id: str) -> dict:
    """Ask for the failed stage again. A running attempt is marked, not leased twice."""
    row = con.execute(
        "SELECT status, kind, lane, pipeline FROM sources WHERE id=?",
        (source_id,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "没有这条"}
    if row["status"] == "解析失败":
        return {"ok": False, "error": "解析失败不会靠重试变成正文"}
    con.execute(
        "UPDATE sources SET status=?, last_error=NULL, pipeline=? WHERE id=?",
        (ST_DISTILLING, _begin_attempt(row["pipeline"] or ""), source_id),
    )
    queued = con.execute(
        "SELECT id, state FROM queue WHERE source_id=? AND state!='done' ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if queued and queued["state"] == "leased":
        con.execute(
            "UPDATE queue SET superseded=1, last_error=NULL, next_at=NULL WHERE id=? AND state='leased'",
            (queued["id"],),
        )
    elif queued:
        con.execute(
            """UPDATE queue SET state='queued', superseded=0, next_at=NULL, attempts=0, last_error=NULL,
               cursor=0, leased_until=NULL WHERE id=?""",
            (queued["id"],),
        )
    else:
        retry_lane = lane_for(kind=row["kind"], origin=ORIGIN_RETRY, legacy_lane=row["lane"])
        con.execute(
            "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES(?, ?, 'queued', ?)",
            (source_id, retry_lane, utcnow()),
        )
    return {"ok": True}


def _reread_file(source_id: str, filename: str, lane: str) -> dict:
    """Read the file again. A failed read, or a body that is only picture embeds, is not words yet."""
    from rogmem.sections import join_sections, read_sections

    name = Path(filename or "").name
    root = active_scan()
    path = (root / name) if root is not None and name else None
    if path is None or not path.is_file():
        return {"ok": False, "error": "文件已经不在了"}
    try:
        sections = read_sections(path)
        text = join_sections(sections)
        error = ""
    except Exception as exc:
        sections = []
        text = ""
        error = str(exc)[:200] or "没有抽出正文"
    if error or _too_thin(text):
        message = error or "去掉标记后没有文字，不调用模型"
        with LOCK:
            con = db()

            def note() -> None:
                current = con.execute(
                    "SELECT status, body FROM sources WHERE id=?",
                    (source_id,),
                ).fetchone()
                if current is None:
                    return
                if current["status"] == ST_PARSE_FAIL:
                    con.execute(
                        "UPDATE sources SET last_error=? WHERE id=? AND status=?",
                        (message, source_id, ST_PARSE_FAIL),
                    )
                    return
                if _too_thin(current["body"] or ""):
                    con.execute(
                        "UPDATE sources SET status=?, last_error=? WHERE id=?",
                        (ST_NO_BODY, message, source_id),
                    )
                    _set_pipeline(con, source_id, "解析", "fail")

            outcome = _finish_write(con, note)
        if isinstance(outcome, dict) and outcome.get("ok") is False:
            return outcome
        return {"ok": False, "error": message}
    planned = _plan_document(path, sections)
    file_lane = lane or lane_for(kind="file", origin=ORIGIN_BATCH)
    with LOCK:
        con = db()
        current = con.execute(
            "SELECT status, body FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        if current is None or (
            current["status"] != ST_PARSE_FAIL and not _too_thin(current["body"] or "")
        ):
            return {"ok": False, "error": "没有这条"}
        picture_only = current["status"] != ST_PARSE_FAIL

        def write() -> None:
            if picture_only:
                con.execute(
                    """UPDATE sources
                       SET body=?, status=?, last_error=NULL, pipeline='', coverage=NULL
                       WHERE id=?""",
                    (text, ST_PARSED, source_id),
                )
                _drop_source_chunks(con, source_id)
                _retire_quotes_outside(con, source_id, text)
                _write_planned(con, source_id, planned)
                _queue_source(con, source_id, file_lane)
                _finish_vector_gate(con, source_id, file_lane)
                return
            con.execute(
                "UPDATE sources SET body=?, status=?, last_error=NULL WHERE id=?",
                (text, ST_PARSED, source_id),
            )
            _write_planned(con, source_id, planned)
            _finish_vector_gate(con, source_id, file_lane)

        outcome = _finish_write(con, write)
    if isinstance(outcome, dict) and outcome.get("ok") is False:
        return outcome
    return {"ok": True}


def _drop_source_chunks(con: sqlite3.Connection, source_id: str) -> None:
    """Remove slices of an earlier reading so a new reading does not sit beside them."""
    global _VECS
    rows = con.execute("SELECT id FROM chunks WHERE source_id=?", (source_id,)).fetchall()
    gone = {int(row["id"]) for row in rows}
    if not gone:
        return
    _VECS = [(cid, vec) for cid, vec in _VECS if cid not in gone]
    for cid in gone:
        con.execute("DELETE FROM fts_chunk WHERE chunk_id=?", (str(cid),))
    con.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))


def _retire_quotes_outside(con: sqlite3.Connection, source_id: str, text: str) -> None:
    """A quote that is not in the new reading is no longer this file's words."""
    now = utcnow()
    rows = con.execute(
        "SELECT id, quote FROM statements WHERE source_id=? AND invalid_at IS NULL",
        (source_id,),
    ).fetchall()
    for row in rows:
        quote = row["quote"] or ""
        if quote and quote not in (text or ""):
            con.execute("UPDATE statements SET invalid_at=? WHERE id=?", (now, row["id"]))


def retry_source(source_id: str) -> dict:
    """Put one source back on the queue.

    A file that failed while being read is read again from disk. A stored body
    with no words once picture embeds are set aside was never read, so it is
    read again too.
    Waiting for a writer outside this process is the database timeout. It comes
    back as a result, not as an unhandled server error.
    """
    with LOCK:
        con = db()
        row = con.execute(
            "SELECT status, kind, lane, filename, body FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
    unread = row is not None and (row["status"] == ST_PARSE_FAIL or _too_thin(row["body"] or ""))
    if row and row["kind"] == "file" and unread:
        return _reread_file(source_id, row["filename"] or "", row["lane"] or "")
    with LOCK:
        con = db()
        con.execute(f"PRAGMA busy_timeout={_WRITE_WAIT_MS}")
        try:
            return _finish_write(con, lambda: _requeue_source(con, source_id))
        finally:
            try:
                con.execute("PRAGMA busy_timeout=5000")
            except sqlite3.Error:
                pass


def rebench_source(source_id: str) -> dict:
    """Invalidate this bench file's statements and distill it again.

    Only filenames that start with wiki-bench- are accepted. Other memories stay.
    """
    with LOCK:
        con = db()
        row = con.execute("SELECT filename FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "没有这条"}
        filename = str(row["filename"] or "")
        if not filename.startswith("wiki-bench-"):
            return {"ok": False, "error": "只接受评测样本"}
        try:
            closed = con.execute(
                "UPDATE statements SET invalid_at=? WHERE source_id=? AND invalid_at IS NULL",
                (utcnow(), source_id),
            ).rowcount
            con.execute("UPDATE sources SET status=?, last_error=NULL WHERE id=?", (ST_VEC_READY, source_id))
            queued = con.execute(
                "SELECT id FROM queue WHERE source_id=? ORDER BY id DESC LIMIT 1", (source_id,)
            ).fetchone()
            if queued:
                con.execute(
                    "UPDATE queue SET state='queued', next_at=NULL, attempts=0, last_error=NULL, cursor=0 WHERE id=?",
                    (queued["id"],),
                )
            else:
                con.execute(
                    "INSERT INTO queue(source_id,lane,state,enqueued_at) VALUES(?, ?, 'queued', ?)",
                    (source_id, lane_for(kind="file", origin=ORIGIN_REBENCH), utcnow()),
                )
            con.commit()
        except BaseException:
            _rollback_write(con)
            raise
    return {"ok": True, "invalidated": closed}


def _crystal_where(type_name: str, status: str) -> tuple[list[str], list[str]]:
    where = ["1=1"]
    args: list[str] = []
    if status and status != "全部":
        where.append("status=?")
        args.append(status)
    if type_name:
        where.append("type=?")
        args.append(type_name)
    return where, args


def list_crystals(type_name: str = "", status: str = "active") -> list[dict]:
    where, args = _crystal_where(type_name, status)
    rows = db().execute(
        f"""SELECT id,type,name,status FROM crystallizations
            WHERE {' AND '.join(where)}
            ORDER BY CASE id WHEN 'owner' THEN 0 ELSE 1 END, name""",
        args,
    ).fetchall()
    return [dict(r) for r in rows]


def crystal_nav_slice(type_name: str = "", status: str = "active", offset: int = 0) -> dict:
    """One window of the crystal nav. The sidebar asks for the next window when scrolled."""
    where, args = _crystal_where(type_name, status)
    total = int(db().execute(f"SELECT COUNT(*) AS n FROM crystallizations WHERE {' AND '.join(where)}", args).fetchone()["n"])
    start = offset if offset > 0 else 0
    if start > total:
        start = total
    rows = db().execute(
        f"""SELECT id,type,name,status FROM crystallizations
            WHERE {' AND '.join(where)}
            ORDER BY CASE id WHEN 'owner' THEN 0 ELSE 1 END, name
            LIMIT ? OFFSET ?""",
        [*args, PAGE_SIZE, start],
    ).fetchall()
    items = [dict(row) for row in rows]
    return {"items": items, "total": total, "next": start + len(items)}


def _parse_body_sections(body_md: str) -> dict[str, str]:
    """Split thin ``body_md`` ``## heading`` blocks. Not a thick-blob migrator."""
    sections: dict[str, str] = {}
    heading = None
    buf: list[str] = []
    for line in (body_md or "").splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections[heading] = "\n".join(buf).strip()
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if heading is not None:
        sections[heading] = "\n".join(buf).strip()
    return sections


def _written_chapters(mapping: dict) -> dict:
    """Headings that were actually written. Empty placeholders are not chapters."""
    return {
        key: value
        for key, value in (mapping or {}).items()
        if str(value or "").strip() not in {"", "暂无来源", "尚无来源"}
    }


def _thin_chapters_map(row) -> dict:
    """Production read: headings present in body_md. No preset chapter is invented."""
    crystal = hydrate(row)
    return _written_chapters(_parse_body_sections(crystal.body_md))


def _chapters_map(row) -> dict:
    """Deprecated for display. Write-seam leftover current-value map.

    Strike / move / merge still read leftover chapters_json when body_md is
    empty. Crystal_detail / read_section must use ``_thin_chapters_map``.
    This is not a production page read and does not replace wipe.
    """
    crystal = hydrate(row)
    if crystal.body_md:
        return _written_chapters(_parse_body_sections(crystal.body_md))
    try:
        chapters = json.loads(row["chapters_json"] or "{}")
    except (TypeError, json.JSONDecodeError, KeyError):
        chapters = {}
    if not isinstance(chapters, dict):
        return {}
    return _written_chapters(chapters)


def _summary_slots_from_row(row) -> dict:
    """Thin envelope keys only. Leftover summary_json is not a read source."""
    crystal = hydrate(row)
    stored: dict = {}
    if crystal.summary:
        stored["概述"] = crystal.summary
    if crystal.aliases:
        stored["别名"] = "、".join(crystal.aliases)
    if crystal.owner_relation and crystal.owner_relation not in {"", "暂无信息", "尚无来源", "暂无来源"}:
        stored["与记忆主人的关系"] = crystal.owner_relation
    for key, value in crystal.keys.items():
        if value not in {None, ""}:
            stored[key] = value
    return stored


def _crystal_row_by_name(name: str):
    wanted = (name or "").strip()
    if not wanted:
        return None
    if wanted in {"记忆主人", "owner"}:
        row = db().execute("SELECT * FROM crystallizations WHERE id='owner'").fetchone()
        if row is not None:
            return row
    return db().execute(
        "SELECT * FROM crystallizations WHERE name=? AND status='active'",
        (wanted,),
    ).fetchone()


def read_section(name: str, section: str) -> dict:
    """Read one heading the page actually wrote. Parent file chunks for that title are included."""
    chapter = (section or "").strip()
    if not (name or "").strip() or not chapter:
        return {"error": "要写明结晶和章节"}
    row = _crystal_row_by_name(name)
    if row is None:
        return {"error": "没有这篇结晶"}
    parsed = _parse_body_sections(hydrate(row).body_md)
    headings = list(parsed.keys())
    text = parsed.get(chapter)
    if chapter == "概述" and text in {None, ""}:
        text = _overview_from_row(row)
    missing = text in {None, "", "暂无来源", "尚无来源"}
    parents = db().execute(
        "SELECT text, embed_text FROM chunks WHERE role='parent' AND embed_text LIKE ? ORDER BY seq LIMIT 3",
        (f"%标题：{chapter}%",),
    ).fetchall()
    return {
        "crystal": row["name"],
        "section": chapter,
        "text": "" if missing else text,
        "missing": bool(missing),
        "sections": headings,
        "parents": [piece for piece in (_parent_section(item["text"] or "", chapter) for item in parents) if piece],
    }


def _statement_basis(crystal_id: str, statement_id: str) -> str:
    """The reason recorded when this wording was written. Empty when none was kept."""
    found = db().execute(
        """SELECT basis FROM revisions
           WHERE crystal_id=? AND statement_id=? AND basis IS NOT NULL AND TRIM(basis)!=''
           ORDER BY id DESC LIMIT 1""",
        (crystal_id, statement_id),
    ).fetchone()
    return (found["basis"] or "").strip() if found else ""


def read_chain(name: str, section: str) -> dict:
    """Time order of what was written under one heading, with the reason kept for each wording."""
    chapter = (section or "").strip()
    if not (name or "").strip() or not chapter:
        return {"ok": False, "error": "要写明结晶和章节"}
    row = _crystal_row_by_name(name)
    if row is None:
        return {"ok": False, "error": "没有这篇结晶"}
    headings = list(_parse_body_sections(hydrate(row).body_md).keys())
    rows = db().execute(
        """SELECT s.id, s.text, s.quote, s.source_id, s.event_at, s.created_at, s.invalid_at
           FROM statements s
           WHERE s.crystal_id=? AND s.trust='written'
             AND (s.field=? OR s.chapter=?)
           ORDER BY COALESCE(s.event_at, s.created_at), s.created_at""",
        (row["id"], chapter, chapter),
    ).fetchall()
    rings = []
    previous = ""
    for item in rows:
        text = item["text"] or ""
        source_id = item["source_id"] or ""
        origin = db().execute(
            "SELECT session_id, turn_id FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        links = origin_links(
            origin["session_id"] if origin else "",
            origin["turn_id"] if origin else "",
            source_id,
            item["id"],
        )
        rings.append(
            {
                "id": item["id"],
                "text": text,
                "quote": item["quote"] or "",
                "basis": _statement_basis(row["id"], item["id"]),
                "source_id": source_id,
                "session_id": (origin["session_id"] if origin else "") or "",
                "turn_id": (origin["turn_id"] if origin else "") or "",
                "event_at": item["event_at"] or "",
                "current": item["invalid_at"] is None,
                "delta": style_delta(previous, text),
                **links,
            }
        )
        previous = text
    return {
        "ok": True,
        "crystal": row["name"],
        "section": chapter,
        "sections": headings,
        "rings": rings,
    }


def _parent_section(text: str, section: str) -> str:
    """Keep only the headed slice for this section. Other chapters stay out of the read."""
    marker = f"标题：{section}"
    if marker not in text:
        return ""
    start = text.find(marker)
    rest = text[start:]
    nxt = rest.find("\n标题：", len(marker))
    if nxt >= 0:
        rest = rest[:nxt]
    return rest.strip()


def crystal_detail(cid: str) -> dict | None:
    row = db().execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    stored = _summary_slots_from_row(row)
    birth = _birth_text(stored)
    if birth:
        stored["出生时间"] = birth
    if row["type"] == "人物":
        org = stored.get("所属组织及职务") or stored.get("所属组织")
        if org and org != "尚无来源":
            stored["所属组织及职务"] = org
    stored["概述"] = _overview_from_row(row)
    stored["别名"] = _aliases_from_row(row)
    fields = summary_fields(row["type"], owner=row["id"] == "owner")
    summary = {key: stored.get(key) or "尚无来源" for key in fields}
    if stored.get("其他仓库"):
        summary["其他仓库"] = stored["其他仓库"]
    chapters = _thin_chapters_map(row)
    field_marks, chapter_marks, bits = _statement_marks(row["id"])
    lede = [{"text": f"这是{row['type']}「{row['name']}」。", "marks": []}] + bits
    if not bits:
        lede = [{"text": "尚无来源", "marks": []}]
    else:
        joined = "".join(part["text"] for part in lede)
        if len(joined) > 180:
            lede = lede[:2]
    from rogmem.wikilink import linked_pages, marked_titles, title_index

    out_links, in_links = linked_pages(db(), row["id"])
    index = title_index(db())
    body = hydrate(row).body_md
    link_targets = {title: index.get(title) for title in marked_titles(body) if index.get(title)}
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "status": row["status"],
        "status_label": {"active": "活跃"}.get(row["status"], row["status"]),
        "summary": summary,
        "chapters": chapters,
        "field_marks": field_marks,
        "chapter_marks": chapter_marks,
        "lede": lede,
        "out_links": out_links,
        "in_links": in_links,
        "link_targets": link_targets,
        "body_md": body,
        "sketch_md": str(getattr(hydrate(row), "sketch_md", "") or ""),
        "body_updated_at": str(row["body_updated_at"] or "") if "body_updated_at" in row.keys() else "",
        "sketch_updated_at": str(row["sketch_updated_at"] or "") if "sketch_updated_at" in row.keys() else "",
    }


def format_crystal_article(detail: dict) -> str:
    """One editable text: summary lines, then ## chapters."""
    lines = [f"{key}：{value}" for key, value in (detail.get("summary") or {}).items()]
    lines.append("")
    for key, value in (detail.get("chapters") or {}).items():
        lines.append(f"## {key}")
        lines.append("" if value is None else str(value))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _split_article(text: str) -> tuple[list[tuple[str, str]], str]:
    fields: list[tuple[str, str]] = []
    body: list[str] = []
    in_body = False
    for line in (text or "").replace("\r\n", "\n").splitlines():
        if line.startswith("## "):
            in_body = True
        if in_body:
            body.append(line)
            continue
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        key = key.strip()
        if key:
            fields.append((key, value.strip()))
    return fields, "\n".join(body).strip()


def save_crystal_article(cid: str, text: str) -> dict:
    """Store a hand edit and leave one revision-log line. No new statement."""
    detail = crystal_detail(cid)
    if detail is None:
        return {"ok": False, "error": "没有这篇"}
    incoming = (text or "").replace("\r\n", "\n").strip()
    if incoming == format_crystal_article(detail).strip():
        return {"ok": True, "unchanged": True}
    fields, body = _split_article(incoming)
    present = {key for key, _ in fields}
    for key in detail.get("summary") or {}:
        if key not in present:
            fields.append((key, ""))
    row = db().execute("SELECT revision FROM crystallizations WHERE id=?", (cid,)).fetchone()
    if row is None:
        return {"ok": False, "error": "没有这篇"}
    ops = []
    for key, value in fields:
        ops.extend(field_update_ops(key, value))
    ops.append({"op": "update", "path": "body_md", "value": body})
    from rogmem.crystal_store import CrystalStore

    change = _describe_text_change(format_crystal_article(detail), incoming, label="词条")

    def sidecar() -> None:
        _record_revision(
            db(),
            cid,
            "人工修订正文",
            actor="人工",
            change=change,
            basis="在词条里直接改的，没有另一份材料。",
        )

    _apply_then_sidecar(
        CrystalStore(db()),
        db(),
        crystal_id=cid,
        expected_revision=int(row["revision"] or 0),
        ops=ops,
        holder=f"manual:{cid}",
        sidecar=sidecar,
        injected=False,
    )
    return {"ok": True}


def _blank_summary(value: str) -> str:
    text = (value or "").strip()
    if text in {"", "尚无来源", "暂无来源"}:
        return "尚无来源"
    return text


def _blank_chapter(value: str) -> str:
    text = (value or "").strip()
    if text in {"", "尚无来源", "暂无来源"}:
        return "暂无来源"
    return text


def save_crystal_markdown(cid: str, body: str, sketch: str | None = None, address: str | None = None) -> dict:
    """Replace the page markdown. Do not rebuild it from a fixed field list.

    For an 工程 page, address replaces the current repository. A blank address
    does not delete the one already written.
    """
    row = db().execute("SELECT * FROM crystallizations WHERE id=?", (cid,)).fetchone()
    if row is None:
        return {"ok": False, "error": "没有这篇"}
    crystal = hydrate(row)
    address_change = ""
    if address is not None and crystal.type == "工程":
        applied = _assign_project_address(crystal, address)
        if not applied.get("ok"):
            return applied
        address_change = str(applied.get("change") or "")
    before_body = crystal.body_md or ""
    before_sketch = crystal.sketch_md or ""
    incoming = (body or "").replace("\r\n", "\n").strip()
    raw_sketch = None if sketch is None else str(sketch).replace("\r\n", "\n").strip()
    if raw_sketch is None or raw_sketch == before_sketch.strip():
        incoming_sketch = raw_sketch
    else:
        incoming_sketch = _fit_sketch(raw_sketch)
    if (
        not address_change
        and incoming == before_body.strip()
        and (incoming_sketch is None or incoming_sketch == before_sketch.strip())
    ):
        return {"ok": True, "unchanged": True}
    parts = []
    if address_change:
        parts.append(address_change)
    body_change = _describe_text_change(before_body, incoming)
    if body_change:
        parts.append(body_change)
    if incoming_sketch is not None:
        sketch_change = _describe_text_change(before_sketch, incoming_sketch, label="结晶速写")
        if sketch_change:
            parts.append(sketch_change)
    crystal.body_md = incoming
    if incoming_sketch is not None:
        crystal.sketch_md = incoming_sketch
    CrystalStore(db()).put(crystal, holder=f"manual:{cid}", commit=False)
    _record_revision(
        db(),
        cid,
        "人工修订正文",
        actor="人工",
        change="\n\n".join(parts),
        basis="在词条里直接改的，没有另一份材料。",
        snapshot=incoming,
    )
    db().commit()
    return {"ok": True}


def save_crystal_fields(cid: str, fields: dict, chapters: dict) -> dict:
    """Save summary fields and chapter text from the edit form. One revision."""
    detail = crystal_detail(cid)
    if detail is None:
        return {"ok": False, "error": "没有这篇"}
    incoming_fields = {str(key): "" if value is None else str(value) for key, value in (fields or {}).items()}
    incoming_chapters = {str(key): "" if value is None else str(value) for key, value in (chapters or {}).items()}
    lines = []
    for key, current in (detail.get("summary") or {}).items():
        value = incoming_fields[key] if key in incoming_fields else current
        lines.append(f"{key}：{_blank_summary(value)}")
    lines.append("")
    seen: set[str] = set()
    for key, current in (detail.get("chapters") or {}).items():
        seen.add(key)
        value = incoming_chapters[key] if key in incoming_chapters else current
        written = _blank_chapter(value)
        if written == "暂无来源":
            continue
        lines.append(f"## {key}")
        lines.append(written)
        lines.append("")
    for key, value in incoming_chapters.items():
        if key in seen:
            continue
        written = _blank_chapter(value)
        if written == "暂无来源":
            continue
        lines.append(f"## {key}")
        lines.append(written)
        lines.append("")
    return save_crystal_article(cid, "\n".join(lines))


HOST_PROGRAMS = ("Cursor", "Hermes", "Codex", "WorkBuddy", "CodeBuddy", "DSH")


def report_hosts(items: list[dict]) -> dict:
    now = utcnow()
    with LOCK:
        con = db()
        for item in items:
            program = str(item.get("program") or "")
            if program not in HOST_PROGRAMS:
                continue
            access = str(item.get("access") or "未安装")
            health = str(item.get("health") or "通道未开")
            total = item.get("importable")
            try:
                importable = None if total is None or total == "" else int(total)
            except (TypeError, ValueError):
                importable = None
            con.execute(
                """INSERT INTO hosts(program, found, access, health, detail, reported_at, importable)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(program) DO UPDATE SET
                     found=excluded.found, access=excluded.access, health=excluded.health,
                     detail=excluded.detail, reported_at=excluded.reported_at,
                     importable=excluded.importable""",
                (program, 1 if item.get("found") else 0, access, health, str(item.get("detail") or ""), now, importable),
            )
        con.commit()
    return {"ok": True}


def confirm_host(program: str, confirm: bool) -> dict:
    if program not in HOST_PROGRAMS:
        return {"ok": False, "refused": "unknown", "programs": []}
    if confirm is not True:
        return {"ok": False, "refused": "unconfirmed", "programs": confirmed_programs()}
    now = utcnow()
    with LOCK:
        con = db()
        con.execute(
            """INSERT INTO host_confirms(program, confirmed_at) VALUES(?,?)
               ON CONFLICT(program) DO UPDATE SET confirmed_at=excluded.confirmed_at""",
            (program, now),
        )
        con.commit()
    return {"ok": True, "refused": "", "programs": confirmed_programs()}


def confirm_import(program: str, confirm: bool) -> dict:
    """Admin-side 「program 级同意」—— 只做审计。

    评审 PR #116 §1：不得为所有 v1 agent 自动建作业（广播会跨设备错配、
    对同 program 两台 v1 还会双建）。从新路径起点，import 的实际执行必须显式
    绑定 agent_id：调用方请用 /admin/api/hosts/jobs 为具体 agent 下达。
    """
    if program not in HOST_PROGRAMS:
        return {"ok": False, "refused": "unknown", "programs": []}
    if confirm is not True:
        return {"ok": False, "refused": "unconfirmed", "programs": confirmed_imports()}
    now = utcnow()
    with LOCK:
        con = db()
        con.execute(
            """INSERT INTO host_imports(program, confirmed_at) VALUES(?,?)
               ON CONFLICT(program) DO UPDATE SET confirmed_at=excluded.confirmed_at""",
            (program, now),
        )
        con.commit()
    return {
        "ok": True,
        "refused": "",
        "programs": confirmed_imports(),
        "jobs": [],
        "note": "audit-only",
    }


def confirmed_imports() -> list[str]:
    return [row["program"] for row in db().execute("SELECT program FROM host_imports ORDER BY program")]


# ---------------------------------------------------------------------------
# host_jobs — 目标设备绑定的历史导入作业
# 设计稿：docs/reports/host-history-import-audit/design.md
# 评审 PR #116：加入 lease_token fencing、allowlist error、maintenance sweep、
# capability 精确集合解析、unknown/not-your-job 统一响应。
# ---------------------------------------------------------------------------

_HOST_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled", "expired"})
_HOST_JOB_ACTIVE = frozenset({"claimed", "running", "partial"})


def history_hold(job_id: str) -> str:
    """A wait that is not a clock. The session stays out of the distill queue until the job ends."""
    return "hold:" + str(job_id or "").strip()


def _hold_job_sessions(con, session_ids: set[str], job_id: str) -> None:
    """Keep every queued line of these sessions until this import job is finished.

    A later batch settles the session as ready. Putting the hold back on the
    whole session stops the first batch from leaving before the rest arrives.
    """
    hold = history_hold(job_id)
    if not hold or hold == "hold:":
        return
    for session_id in session_ids:
        if not str(session_id or "").strip():
            continue
        con.execute(
            """UPDATE sources SET pair_ready=0, wait_until=?
               WHERE session_id=? AND kind IN ('user','agent') AND lane IN ('p1','slow')
                 AND id IN (SELECT source_id FROM queue WHERE state='queued')""",
            (hold, session_id),
        )


def _release_terminal_history(con) -> None:
    """A finished import job may distill. A job that is still uploading may not."""
    rows = con.execute(
        "SELECT id FROM host_jobs WHERE status IN ('completed','failed','cancelled','expired')"
    ).fetchall()
    for row in rows:
        con.execute(
            "UPDATE sources SET pair_ready=1, wait_until=NULL WHERE wait_until=?",
            (history_hold(row["id"]),),
        )

# error class allowlist：客户端自报的 `last_error_class` 只接受这组标签；
# 其余一律归 `unknown`，避免把 token / URL / 路径等 payload 片段落库。
_ERROR_CLASS_ALLOWED = frozenset({
    "channel-closed",
    "post-failed",
    "http-400", "http-401", "http-403", "http-404",
    "http-408", "http-409", "http-410", "http-413", "http-415",
    "http-429",
    "http-500", "http-502", "http-503", "http-504",
    "timeout",
    "not-active",
    "stale-lease",
    "cancelled",
})


def _sanitize_error_class(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text if text in _ERROR_CLASS_ALLOWED else "unknown"


def _parse_capabilities(raw: str) -> set[str]:
    """Agent 的能力表以逗号分隔，精确集合解析，不做 LIKE 子串匹配。"""
    if not raw:
        return set()
    return {part.strip() for part in str(raw).split(",") if part and part.strip()}


def agent_has_capability(agent_id: str, cap: str) -> bool:
    row = db().execute(
        "SELECT capabilities FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    if row is None:
        return False
    return cap in _parse_capabilities(row["capabilities"])


_UNKNOWN_JOB_RESPONSE = {"ok": False, "refused": "unknown-job"}


def _job_row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _parse_iso(value: str):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso_plus(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _create_host_import_job(
    con,
    *,
    agent_id: str,
    program: str,
    request_id: str,
    now: str,
    ttl_s: int = 86400,
    max_attempts: int = 3,
    requested_by: str = "admin",
) -> dict:
    if not agent_id:
        return {"ok": False, "refused": "missing-agent"}
    agent = con.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if agent is None:
        return {"ok": False, "refused": "unknown-agent"}
    if agent["program"] != program:
        return {"ok": False, "refused": "program-mismatch"}
    if agent["status"] != "启用":
        return {"ok": False, "refused": "agent-not-enabled"}
    caps = _parse_capabilities(agent["capabilities"])
    if "host_import_jobs:v1" not in caps:
        return {"ok": False, "refused": "unsupported-client"}
    existing = con.execute(
        "SELECT * FROM host_jobs WHERE agent_id=? AND request_id=?",
        (agent_id, request_id),
    ).fetchone()
    if existing is not None:
        return {"ok": True, "refused": "", "already": True, "job": _job_row_to_dict(existing)}
    # 评审 R2 P2-7：同 (agent, program) 只许一条在途（pending/claimed/running/partial）。
    # 不同 request_id 的新请求命中在途作业 → 幂等返回该作业，不新建。
    inflight = con.execute(
        """SELECT * FROM host_jobs
             WHERE agent_id=? AND program=?
               AND status IN ('pending','claimed','running','partial')
             ORDER BY created_at
             LIMIT 1""",
        (agent_id, program),
    ).fetchone()
    if inflight is not None:
        return {"ok": True, "refused": "", "already": True, "job": _job_row_to_dict(inflight)}
    import secrets as _secrets

    job_id = "hj_" + _secrets.token_hex(8)
    expires_at = _iso_plus(ttl_s)
    # R5 A3：跨连接/跨进程竞争下，前面 SELECT inflight 可能漏；partial UNIQUE 索引
    # 会在真正 INSERT 时抛 IntegrityError。捕获 → rollback 本次 INSERT → re-read
    # winner（先按 scope active，再按 request_id）→ 返回同 job。
    # 其他完整性错误（例如 request_id UNIQUE 命中）走同一路径返回 winner；
    # 若两边都读不到，说明是真的非预期 IntegrityError，重新抛。
    try:
        con.execute(
            """INSERT INTO host_jobs(
                 id, request_id, agent_id, device_id, program, kind, status,
                 created_at, expires_at, attempts, max_attempts, requested_by
               ) VALUES(?,?,?,?,?,'history_import','pending',?,?,0,?,?)""",
            (job_id, request_id, agent_id, agent["device_id"], program,
             now, expires_at, int(max_attempts), requested_by),
        )
    except sqlite3.IntegrityError:
        try:
            con.rollback()
        except sqlite3.Error:
            pass
        # 1) 先看是不是同 request_id 命中（显式幂等）
        again = con.execute(
            "SELECT * FROM host_jobs WHERE agent_id=? AND request_id=?",
            (agent_id, request_id),
        ).fetchone()
        if again is not None:
            return {"ok": True, "refused": "", "already": True, "job": _job_row_to_dict(again)}
        # 2) 再看是不是 in-flight 唯一索引命中：返回当前 active winner
        winner = con.execute(
            """SELECT * FROM host_jobs
                 WHERE agent_id=? AND program=?
                   AND status IN ('pending','claimed','running','partial')
                 ORDER BY created_at
                 LIMIT 1""",
            (agent_id, program),
        ).fetchone()
        if winner is not None:
            return {"ok": True, "refused": "", "already": True, "job": _job_row_to_dict(winner)}
        raise
    row = con.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
    return {"ok": True, "refused": "", "already": False, "job": _job_row_to_dict(row)}


def create_host_import_job(
    *,
    agent_id: str,
    program: str,
    request_id: str,
    ttl_s: int = 86400,
    max_attempts: int = 3,
    requested_by: str = "admin",
) -> dict:
    """新建一条作业；遵守设备/程序/能力/幂等约束。详见 design.md §3."""
    if program not in HOST_PROGRAMS:
        return {"ok": False, "refused": "unknown-program"}
    with LOCK:
        con = db()
        now = utcnow()
        result = _create_host_import_job(
            con,
            agent_id=agent_id,
            program=program,
            request_id=request_id,
            now=now,
            ttl_s=ttl_s,
            max_attempts=max_attempts,
            requested_by=requested_by,
        )
        con.commit()
    return result


def cancel_host_import_job(*, job_id: str) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "refused": "unknown-job"}
        if row["status"] in _HOST_JOB_TERMINAL:
            return {"ok": False, "refused": "terminal"}
        now = utcnow()
        con.execute(
            "UPDATE host_jobs SET status='cancelled', cancelled_at=? WHERE id=?",
            (now, job_id),
        )
        _release_terminal_history(con)
        con.commit()
    return {"ok": True}


def list_host_import_jobs(
    *,
    agent_id: str | None = None,
    device_id: str | None = None,
    program: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if agent_id:
        clauses.append("agent_id=?")
        params.append(agent_id)
    if device_id:
        clauses.append("device_id=?")
        params.append(device_id)
    if program:
        clauses.append("program=?")
        params.append(program)
    if status:
        clauses.append("status=?")
        params.append(status)
    sql = "SELECT * FROM host_jobs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    return [_job_row_to_dict(row) for row in db().execute(sql, tuple(params))]


def claim_host_import_jobs(*, agent_id: str, lease_s: int = 600) -> list[dict]:
    """原子认领（CAS + 必须是本 agent + 签发 opaque lease_token）。

    评审 R2 P2-3：
    - 过期 pending → expired；过期 lease → 回 pending 清 token。
    - **CAS 自身**守住 `attempts < max_attempts`；若等于/超过直接标 failed，不依赖 sweep。
    - 候选 SELECT 也加同条件，避免 SELECT→UPDATE 之间种族。
    """
    if not agent_id:
        return []
    import secrets as _secrets

    with LOCK:
        con = db()
        now = utcnow()
        # 过期 pending → expired
        con.execute(
            """UPDATE host_jobs SET status='expired'
               WHERE status='pending' AND expires_at <= ? AND agent_id=?""",
            (now, agent_id),
        )
        # 过期 lease → 回 pending，清旧 token
        con.execute(
            """UPDATE host_jobs SET status='pending', lease_until=NULL,
                  claimed_at=NULL, lease_token=NULL
               WHERE status IN ('claimed','running','partial')
                 AND lease_until IS NOT NULL AND lease_until <= ?
                 AND agent_id=?""",
            (now, agent_id),
        )
        # 已达 max_attempts 的 pending 直接 failed（不依赖 sweep）
        con.execute(
            """UPDATE host_jobs SET status='failed', completed_at=?
               WHERE status='pending' AND agent_id=? AND attempts >= max_attempts""",
            (now, agent_id),
        )
        # 候选：attempts < max_attempts 的 pending
        rows = con.execute(
            """SELECT id FROM host_jobs
               WHERE agent_id=? AND status='pending' AND expires_at > ?
                 AND attempts < max_attempts
               ORDER BY created_at""",
            (agent_id, now),
        ).fetchall()
        lease_until = _iso_plus(lease_s)
        claimed: list[dict] = []
        for row in rows:
            new_token = "lt_" + _secrets.token_urlsafe(16)
            cursor = con.execute(
                """UPDATE host_jobs
                      SET status='claimed', claimed_at=?, lease_until=?,
                          lease_token=?, attempts=attempts+1
                    WHERE id=? AND agent_id=? AND status='pending'
                      AND attempts < max_attempts""",
                (now, lease_until, new_token, row["id"], agent_id),
            )
            if cursor.rowcount != 1:
                continue
            refreshed = con.execute(
                "SELECT * FROM host_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            claimed.append(_job_row_to_dict(refreshed))
        _release_terminal_history(con)
        con.commit()
    return claimed


def _lease_match(row, lease_token: str) -> bool:
    return bool(row["lease_token"]) and row["lease_token"] == (lease_token or "")


def renew_host_import_lease(
    *, agent_id: str, job_id: str, lease_token: str, lease_s: int = 600
) -> dict:
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return dict(_UNKNOWN_JOB_RESPONSE)
        if row["status"] not in {"claimed", "running", "partial"}:
            return {"ok": False, "refused": "not-active"}
        if not _lease_match(row, lease_token):
            return {"ok": False, "refused": "stale-lease"}
        now_iso = utcnow()
        if row["lease_until"] and str(row["lease_until"]) <= now_iso:
            return {"ok": False, "refused": "stale-lease"}
        new_lease = _iso_plus(lease_s)
        con.execute(
            "UPDATE host_jobs SET lease_until=? WHERE id=? AND lease_token=?",
            (new_lease, job_id, lease_token),
        )
        con.commit()
    return {"ok": True, "lease_until": new_lease}


def ack_host_import_job(
    *,
    agent_id: str,
    job_id: str,
    lease_token: str,
    status: str,
    posted: int = 0,
    duplicates: int = 0,
    refused: int = 0,
    failed: int = 0,
    last_error_class: str = "",
) -> dict:
    """评审 R2 P2-1：ack 只报告终止/错误类别，**不**累加 posted/duplicates/refused。
    计数仅由 /host/jobs/{id}/upload 的真实入库写入。`posted` / `duplicates` / `refused`
    参数在签名里保留以兼容旧 MCP 调用，但服务端一律丢弃。`failed` 作为 client 侧
    批次失败计数仍写。

    terminal 后重放 ack 幂等（不改状态、不改计数）。
    """
    allowed = {"completed", "failed", "partial", "cancelled_ack"}
    if status not in allowed:
        return {"ok": False, "refused": "bad-status"}
    del posted, duplicates, refused  # 服务端忽略；上传回执才是唯一真值
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return dict(_UNKNOWN_JOB_RESPONSE)
        now = utcnow()
        bounded_err = _sanitize_error_class(last_error_class)
        new_failed = int(row["failed"]) + max(0, int(failed))
        if row["status"] == "cancelled":
            # 评审 R4-3：cancelled 态的 ack 必须要求 lease_token 匹配；
            # 重放幂等——client 的 `failed` 只接受为 MAX（不累加），
            # last_error_class 只写一次。
            if not _lease_match(row, lease_token):
                return {"ok": False, "refused": "stale-lease"}
            final_failed = max(int(row["failed"]), max(0, int(failed)))
            con.execute(
                """UPDATE host_jobs SET failed=?,
                      last_error_class=COALESCE(NULLIF(last_error_class, ''), NULLIF(?, ''))
                   WHERE id=? AND lease_token=?""",
                (final_failed, bounded_err, job_id, lease_token),
            )
            _release_terminal_history(con)
            con.commit()
            return {"ok": True, "refused": "", "status": "cancelled"}
        if row["status"] in _HOST_JOB_TERMINAL:
            # terminal 幂等：不改状态、不改计数
            return {"ok": True, "refused": "", "status": row["status"], "idempotent": True}
        if not _lease_match(row, lease_token):
            return {"ok": False, "refused": "stale-lease"}
        if row["lease_until"] and str(row["lease_until"]) <= now:
            return {"ok": False, "refused": "stale-lease"}
        new_status = status
        if status == "partial":
            new_status = "failed" if int(row["attempts"]) >= int(row["max_attempts"]) else "pending"
        elif status == "cancelled_ack":
            new_status = "cancelled"
        completed_at = now if new_status in {"completed", "failed", "cancelled"} else None
        clear_lease = new_status == "pending"
        con.execute(
            """UPDATE host_jobs SET status=?, failed=?,
                  last_error_class=COALESCE(NULLIF(?, ''), last_error_class),
                  completed_at=COALESCE(?, completed_at),
                  lease_until=CASE WHEN ? THEN NULL ELSE lease_until END,
                  lease_token=CASE WHEN ? THEN NULL ELSE lease_token END
               WHERE id=? AND lease_token=?""",
            (new_status, new_failed,
             bounded_err, completed_at,
             1 if clear_lease else 0, 1 if clear_lease else 0,
             job_id, lease_token),
        )
        _release_terminal_history(con)
        con.commit()
    return {"ok": True, "refused": "", "status": new_status}


def ingest_history_for_job(
    *,
    agent_id: str,
    job_id: str,
    lease_token: str,
    program: str,
    turns: list[dict],
    device_name: str | None = None,
) -> dict:
    """受作业与 lease 约束的服务端摄入入口（评审 R2 P1 重写）。

    线性化点：**单短事务** `BEGIN ... COMMIT`，顺序：
      校验 (agent/program/status/lease_token/lease_until)
      → `_write_history_jobs_inline(sources)`
      → UPDATE host_jobs (posted/duplicates/refused + lease_until + status=running)
      → commit。

    这样 cancel 和 batch 可串行化：
      - 若 cancel 先 commit → 本调用看到 status='cancelled'，整批拒绝，**不**写 sources。
      - 若本 batch 先 commit → sources 入库 + 计数更新一起 commit；cancel 只阻止下一批。
    不可撤销已 commit 的 sources；这是既定语义，由 `host_jobs` 的 `cancelled_at` + 状态
    + 计数共同记录审计。
    """
    history_lane = lane_for(kind="user", origin=ORIGIN_HISTORY)
    jobs_prepared, prep_refused = _prepare_history_jobs(turns, program, device_name)
    with LOCK:
        con = db()
        row = con.execute("SELECT * FROM host_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["agent_id"] != agent_id:
            return {"ok": False, "refused": "unknown-job", "posted": 0, "duplicates": 0, "refused_count": 0}
        if row["program"] != str(program or ""):
            return {"ok": False, "refused": "program-mismatch", "posted": 0, "duplicates": 0, "refused_count": 0}
        if row["status"] == "cancelled":
            return {"ok": False, "refused": "cancelled", "posted": 0, "duplicates": 0, "refused_count": 0}
        if row["status"] in {"completed", "failed", "expired"}:
            return {"ok": False, "refused": "terminal", "posted": 0, "duplicates": 0, "refused_count": 0}
        if row["status"] not in {"claimed", "running", "partial"}:
            return {"ok": False, "refused": "not-active", "posted": 0, "duplicates": 0, "refused_count": 0}
        if not _lease_match(row, lease_token):
            return {"ok": False, "refused": "stale-lease", "posted": 0, "duplicates": 0, "refused_count": 0}
        now_iso = utcnow()
        if row["lease_until"] and str(row["lease_until"]) <= now_iso:
            return {"ok": False, "refused": "stale-lease", "posted": 0, "duplicates": 0, "refused_count": 0}
        # R5 A1：真·事务回滚。inline 可能部分 INSERT 后抛（例如 chunker/planner 异常），
        # sqlite3 的 autocommit 模式下 INSERT 已经进隐式事务但未 commit；必须显式 rollback
        # 清掉同线程连接的 pending writes，不污染后续 commit。LOCK 退出后不 close 共享连接。
        try:
            bound = con.execute(
                """SELECT id, device_id, device_name, program, display_name, status
                   FROM agents WHERE id=?""",
                (agent_id,),
            ).fetchone()
            imported, _dup_count = _ingest_history_inline(
                con,
                jobs_prepared,
                program=program,
                history_lane=history_lane,
                now=now_iso,
                bound_host=bound,
                settle=False,
            )
            new_posted = sum(1 for item in imported if not item.get("duplicate"))
            new_dups = sum(1 for item in imported if item.get("duplicate"))
            new_refused = int(prep_refused)
            new_lease = _iso_plus(600)
            cursor = con.execute(
                """UPDATE host_jobs
                     SET status='running', lease_until=?,
                         posted=posted+?, duplicates=duplicates+?, refused=refused+?
                   WHERE id=? AND lease_token=?
                     AND status IN ('claimed','running','partial')""",
                (new_lease, new_posted, new_dups, new_refused, job_id, lease_token),
            )
            if cursor.rowcount != 1:
                # 极罕见：事务中被别路径改动。回滚整个事务。
                con.rollback()
                return {"ok": False, "refused": "stale-lease", "posted": 0, "duplicates": 0, "refused_count": 0}
            _hold_job_sessions(con, {str(job.get("session_id") or "") for job in jobs_prepared}, job_id)
            con.commit()
        except BaseException:
            try:
                con.rollback()
            except sqlite3.Error:
                pass
            raise
    return {
        "ok": True,
        "refused": "",
        "posted": new_posted,
        "duplicates": new_dups,
        "refused_count": new_refused,
        "lease_until": new_lease,
    }


def sweep_host_import_jobs(now: str | None = None) -> dict:
    """维护任务。由 engine.worker 周期性调用；GET 路径禁止触发。
    - pending 作业到期 → expired。
    - claimed/running/partial 的 lease 过期 → 回 pending，清 token。
    - 回到 pending 后若 attempts >= max_attempts → 直接 failed。
    """
    stamp = now or utcnow()
    with LOCK:
        con = db()
        cur_expired = con.execute(
            """UPDATE host_jobs SET status='expired'
               WHERE status='pending' AND expires_at <= ?""",
            (stamp,),
        )
        expired = cur_expired.rowcount or 0
        cur_release = con.execute(
            """UPDATE host_jobs SET status='pending', lease_until=NULL,
                  lease_token=NULL, claimed_at=NULL
               WHERE status IN ('claimed','running','partial')
                 AND lease_until IS NOT NULL AND lease_until <= ?""",
            (stamp,),
        )
        released = cur_release.rowcount or 0
        cur_failed = con.execute(
            """UPDATE host_jobs SET status='failed', completed_at=?
               WHERE status='pending' AND attempts >= max_attempts""",
            (stamp,),
        )
        failed = cur_failed.rowcount or 0
        _release_terminal_history(con)
        con.commit()
    return {"ok": True, "expired": int(expired), "released": int(released), "failed": int(failed)}


def set_agent_capabilities(*, agent_id: str, capabilities: str) -> dict:
    caps = (capabilities or "").strip()[:200]
    with LOCK:
        con = db()
        row = con.execute("SELECT id FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            return {"ok": False, "refused": "unknown-agent"}
        con.execute("UPDATE agents SET capabilities=? WHERE id=?", (caps, agent_id))
        con.commit()
    return {"ok": True, "capabilities": caps}


def confirmed_programs() -> list[str]:
    return [row["program"] for row in db().execute("SELECT program FROM host_confirms ORDER BY program")]


def _history_imported() -> dict[str, int]:
    counts: dict[str, int] = {}
    names = ", ".join("'" + name.replace("'", "") + "'" for name in HOST_PROGRAMS)
    for row in db().execute(
        f"""SELECT agent_name, COUNT(*) AS n FROM sources
           WHERE lane IN ('slow', 'p1') AND kind IN ('user','agent')
             AND agent_name IN ({names})
           GROUP BY agent_name"""
    ):
        counts[str(row["agent_name"])] = int(row["n"])
    return counts


def list_hosts() -> list[dict]:
    rows = {row["program"]: dict(row) for row in db().execute("SELECT * FROM hosts")}
    imported = _history_imported()
    out = []
    for program in HOST_PROGRAMS:
        row = rows.get(program)
        if row is None:
            row = {"program": program, "found": 0, "access": "未安装", "health": "通道未开", "detail": "", "reported_at": "", "importable": None}
        else:
            row["importable"] = row.get("importable")
        row["imported"] = imported.get(program, 0)
        out.append(row)
    return out


_VIEWER_NAME_SKIP = BANNED_LABELS | {UNNAMED_LABEL}


def _viewer_name(name: str) -> str:
    text = (name or "").strip()
    if not text or text.lower() in _VIEWER_NAME_SKIP or text in _VIEWER_NAME_SKIP:
        return ""
    shown = display_device_name(text)
    if not shown or shown in _VIEWER_NAME_SKIP:
        return ""
    return shown


def _local_device_ids(rows: list[dict], viewer_id: str, viewer_name: str) -> tuple[set[str], bool]:
    """Which device ids are the computer viewing the page. Unknown means mark nobody.

    An id counts only when a listed host actually has it. A stale id falls through
    to a device name that belongs to exactly one computer. Anything else marks nobody.
    """
    viewer_id = (viewer_id or "").strip()
    known = {row["device_id"] for row in rows}
    if viewer_id and viewer_id in known:
        return {viewer_id}, True
    name = _viewer_name(viewer_name)
    if not name:
        return set(), False
    matched = {row["device_id"] for row in rows if display_device_name(row["device_name"]) == name}
    if len(matched) == 1:
        return matched, True
    return set(), False


# How long a proved LAN address still means that computer. The channel refreshes it.
_VIEWER_PEER_TTL = 600
_VIEWER_PEERS: dict[str, dict[str, float]] = {}
_VIEWER_PEER_LOCK = threading.Lock()


def _machine_peer(peer: str) -> str:
    """A private address that is one computer. A tunnel, loopback, or shared bridge is not."""
    try:
        ip = ipaddress.ip_address((peer or "").strip())
    except ValueError:
        return ""
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
        return ""
    # 172.16.0.0/12 is where bridges and tunnels fold many computers into one hop.
    if ip in ipaddress.ip_network("172.16.0.0/12"):
        return ""
    return str(ip)


def note_viewer_peer(device_id: str, peer: str) -> None:
    """Remember that this computer just proved its identity from this address."""
    device_id = (device_id or "").strip()
    peer = _machine_peer(peer)
    if not device_id or not peer:
        return
    now = time.monotonic()
    with _VIEWER_PEER_LOCK:
        bucket = _VIEWER_PEERS.setdefault(peer, {})
        bucket[device_id] = now
        stale = [key for key, seen in bucket.items() if now - seen > _VIEWER_PEER_TTL]
        for key in stale:
            bucket.pop(key, None)
        if not bucket:
            _VIEWER_PEERS.pop(peer, None)


def viewer_for_peer(peer: str) -> str:
    """The one computer that recently proved itself from this address. Shared or unknown is nobody."""
    peer = _machine_peer(peer)
    if not peer:
        return ""
    now = time.monotonic()
    with _VIEWER_PEER_LOCK:
        bucket = _VIEWER_PEERS.get(peer) or {}
        fresh = {device: seen for device, seen in bucket.items() if now - seen <= _VIEWER_PEER_TTL}
        if len(fresh) != len(bucket):
            if fresh:
                _VIEWER_PEERS[peer] = fresh
            else:
                _VIEWER_PEERS.pop(peer, None)
    if len(fresh) != 1:
        return ""
    return next(iter(fresh))


def device_for_key(token: str, device_id: str | None) -> str:
    """The device this key belongs to. Does not refresh last-used."""
    text = (token or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode()).hexdigest()
    row = db().execute(
        "SELECT device_id FROM agents WHERE key_hash=? AND status='启用'",
        (digest,),
    ).fetchone()
    if row is None:
        return ""
    device = (device_id or "").strip()
    if not device or device != (row["device_id"] or ""):
        return ""
    return device


def _label(value) -> str:
    """Whitespace around a name is not part of the name."""
    return " ".join(str(value or "").split())


def _hosts_with_label(hosts: list, text: str) -> list:
    """Hosts whose own label is this whole string. A piece of a longer label is not a second host."""
    if not text:
        return []
    found = []
    seen: set[str] = set()
    for host in hosts:
        labels = {_label(host.get("display_name")), _label(host.get("program"))}
        labels.discard("")
        if text not in labels:
            continue
        host_id = str(host.get("id") or "")
        if host_id and host_id not in seen:
            seen.add(host_id)
            found.append(host)
    return found


def _hosts_named(hosts: list, stored: str) -> list:
    """The string names a host when it is that host's label, or quotes exactly one label."""
    text = _label(stored)
    if not text:
        return []
    exact = _hosts_with_label(hosts, text)
    if exact:
        return exact
    spans = [_label(piece) for piece in re.findall(r"「([^」]*)」", text)]
    spans = [piece for piece in spans if piece]
    if len(spans) != 1:
        return []
    return _hosts_with_label(hosts, spans[0])


def _shown_host_name(agent) -> str:
    if not agent:
        return ""
    return _label(agent["display_name"] or "") or _label(agent["program"] or "")


def _host_binding(agent) -> tuple[str | None, str | None, str | None, str]:
    """A submitting host is its id and its label. A bare string is only a label."""
    if isinstance(agent, str) or agent is None:
        return None, None, None, _label(agent) or "智能体"
    try:
        name = _shown_host_name(agent) or "智能体"
        return agent["id"], agent["device_id"], _label(agent["device_name"] or ""), name
    except (KeyError, IndexError, TypeError):
        return None, None, None, "智能体"


def _tidy_host_labels(con: sqlite3.Connection) -> None:
    """One host has one label. A trailing tab is the same host, not another."""
    rows = con.execute("SELECT id, device_name, program, display_name FROM agents").fetchall()
    for row in rows:
        device = _label(row["device_name"])
        program = _label(row["program"])
        display = _label(row["display_name"]) or (f"{device} · {program}" if device and program else program or device)
        if (device, program, display) == (
            row["device_name"] or "",
            row["program"] or "",
            row["display_name"] or "",
        ):
            continue
        con.execute(
            "UPDATE agents SET device_name=?, program=?, display_name=? WHERE id=?",
            (device, program, display, row["id"]),
        )


def _same_device(left: str, right: str) -> bool:
    a = display_device_name(left)
    b = display_device_name(right)
    if not a or not b or a == "未命名设备" or b == "未命名设备":
        return False
    return a == b


def _hosts_for_program(con: sqlite3.Connection, program: str) -> list[sqlite3.Row]:
    program = _label(program)
    if program not in HOST_PROGRAMS:
        return []
    return list(
        con.execute(
            """SELECT id, device_id, device_name, program, display_name, status FROM agents
               WHERE program=? AND status IN ('启用', '停用')""",
            (program,),
        )
    )


def _pick_host(hosts: list, device_name: str):
    """A program's only host keeps turns that never named another of its hosts."""
    if not hosts:
        return None
    named = [row for row in hosts if device_name and _same_device(row["device_name"], device_name)]
    if len(named) == 1:
        return named[0]
    if named:
        return None
    if len(hosts) == 1:
        return hosts[0]
    return None


def _pick_loose_host(hosts: list, agent_name: str, device_name: str):
    return _pick_host(_hosts_named(hosts, agent_name), device_name)


def _attribute_loose_turns() -> None:
    """Stamp a host onto turns that never named one. Runs beside the page, not inside it."""
    if _DB is None:
        return
    with LOCK:
        con = db()
        _tidy_host_labels(con)
        agents = [
            dict(row)
            for row in con.execute(
                """SELECT id, device_id, device_name, program, display_name, status FROM agents
                   WHERE status IN ('启用', '停用')"""
            )
        ]
        _claim_unassigned_turns(con, agents)
        _align_host_names(con)
        con.commit()


def _claim_unassigned_turns(con: sqlite3.Connection, agents: list[dict]) -> None:
    """Give already stored turns the host they belong to. Does not move a turn that named one."""
    hosts = [row for row in agents if row.get("status") in {"启用", "停用"}]
    if not hosts:
        return
    groups = con.execute(
        """SELECT IFNULL(device_name, '') AS device_name, IFNULL(agent_name, '') AS agent_name
           FROM sources
           WHERE kind IN ('user', 'agent', 'draft') AND (agent_id IS NULL OR agent_id = '')
           GROUP BY IFNULL(device_name, ''), IFNULL(agent_name, '')"""
    ).fetchall()
    changed = False
    for group in groups:
        host = _pick_loose_host(hosts, group["agent_name"], group["device_name"])
        if host is None:
            continue
        con.execute(
            """UPDATE sources SET agent_id=?, device_id=?, device_name=?, agent_name=?
               WHERE kind IN ('user', 'agent', 'draft') AND (agent_id IS NULL OR agent_id = '')
                 AND IFNULL(agent_name, '')=? AND IFNULL(device_name, '')=?""",
            (
                host["id"],
                host["device_id"],
                host["device_name"],
                host["display_name"],
                group["agent_name"],
                group["device_name"],
            ),
        )
        changed = True
    if changed:
        con.commit()


def _align_host_names(con: sqlite3.Connection) -> None:
    """A turn that already names a host uses that host's device and agent names."""
    con.execute(
        """UPDATE sources
           SET device_name = (
                 SELECT device_name FROM agents WHERE agents.id = sources.agent_id
               ),
               agent_name = (
                 SELECT display_name FROM agents WHERE agents.id = sources.agent_id
               )
           WHERE kind IN ('user', 'agent', 'draft')
             AND IFNULL(agent_id, '') != ''
             AND EXISTS (
               SELECT 1 FROM agents
               WHERE agents.id = sources.agent_id
                 AND (
                   IFNULL(agents.device_name, '') != IFNULL(sources.device_name, '')
                   OR IFNULL(agents.display_name, '') != IFNULL(sources.agent_name, '')
                 )
             )"""
    )


def _count_turns(bucket: dict[str, int], found: int, lane: str) -> None:
    bucket["sessions"] += found
    if (lane or "") in {"p1", "slow"}:
        bucket["history"] += found


def _session_counts(con: sqlite3.Connection, agents: list[dict]) -> dict[str, dict[str, int]]:
    """Read the turns a host already has. Opening the page does not write."""
    counts = {agent["id"]: {"sessions": 0, "history": 0} for agent in agents}
    for row in con.execute(
        """SELECT agent_id, lane, COUNT(*) AS n FROM sources
           WHERE kind IN ('user', 'agent') AND agent_id IS NOT NULL AND agent_id != ''
           GROUP BY agent_id, lane"""
    ):
        bucket = counts.get(row["agent_id"])
        if bucket is None:
            continue
        _count_turns(bucket, int(row["n"]), row["lane"] or "")
    hosts = [agent for agent in agents if agent.get("status") in {"启用", "停用"}]
    loose = con.execute(
        """SELECT IFNULL(device_name, '') AS device_name, IFNULL(agent_name, '') AS agent_name,
                  lane, COUNT(*) AS n
           FROM sources
           WHERE kind IN ('user', 'agent') AND (agent_id IS NULL OR agent_id = '')
           GROUP BY IFNULL(device_name, ''), IFNULL(agent_name, ''), lane"""
    )
    for row in loose:
        host = _pick_loose_host(hosts, row["agent_name"], row["device_name"])
        if host is None:
            continue
        bucket = counts.get(host["id"])
        if bucket is None:
            continue
        _count_turns(bucket, int(row["n"]), row["lane"] or "")
    return counts


def list_connected_hosts(
    *,
    viewer_device_id: str = "",
    viewer_device_name: str = "",
    device: str = "",
    agent: str = "",
) -> dict:
    """Every device's hosts. （本机） is chosen here for display and is not written back."""
    con = db()
    stored = []
    for row in con.execute(
        """SELECT id, device_id, device_name, program, display_name, status, pubkey,
                  created_at, connected_at, last_used, COALESCE(call_count, 0) AS call_count
           FROM agents
           WHERE status IN ('待放行', '待领取', '启用', '停用')
           ORDER BY device_name, program, id"""
    ):
        item = dict(row)
        try:
            item["code"] = crypto.code_for(crypto.b64decode(item.pop("pubkey")))
        except Exception:
            item.pop("pubkey", None)
            item["code"] = ""
        stored.append(item)
    sessions = _session_counts(con, stored)
    local_ids, viewer_known = _local_device_ids(stored, viewer_device_id, viewer_device_name)
    prepared = []
    for item in stored:
        local = item["device_id"] in local_ids
        base = display_device_name(item["device_name"])
        raw_connected = (item.get("connected_at") or "").strip()
        if raw_connected:
            connected_at, estimated = raw_connected, False
        elif item["status"] in {"启用", "停用"}:
            connected_at, estimated = item.get("created_at") or "", True
        else:
            connected_at, estimated = "", False
        prepared.append(
            {
                "id": item["id"],
                "device_id": item["device_id"],
                "device_name": item["device_name"],
                "device_base": base,
                "device_label": device_label(item["device_name"], local),
                "local": local,
                "agent_name": item["display_name"],
                "program": item["program"],
                "status": item["status"],
                "code": item["code"],
                "connected_at": connected_at,
                "connected_estimated": estimated and bool(connected_at),
                "last_used": item.get("last_used") or "",
                "session_count": sessions.get(item["id"], {}).get("sessions", 0),
                "history_count": sessions.get(item["id"], {}).get("history", 0),
                "call_count": int(item.get("call_count") or 0),
            }
        )
    facets = {
        "devices": [],
        "agents": [],
    }
    seen_devices: set[str] = set()
    seen_agents: set[str] = set()
    for item in prepared:
        if item["device_id"] not in seen_devices:
            seen_devices.add(item["device_id"])
            facets["devices"].append(
                {"id": item["device_id"], "label": item["device_base"], "local": item["local"]}
            )
        if item["agent_name"] and item["agent_name"] not in seen_agents:
            seen_agents.add(item["agent_name"])
            facets["agents"].append(item["agent_name"])
    device_filter = (device or "").strip()
    agent_filter = (agent or "").strip()
    rows = [
        item
        for item in prepared
        if (not device_filter or item["device_id"] == device_filter)
        and (not agent_filter or item["agent_name"] == agent_filter)
    ]
    return {"rows": rows, "facets": facets, "viewer_known": viewer_known}


def graph() -> dict:
    """Active pages and the links a reader can still follow.

    Ended history stays in ``edges``. This read uses the live slices instead of
    joining every past link to its statement.
    """

    nodes = [dict(r) for r in db().execute("SELECT id,name,type FROM crystallizations WHERE status='active'")]
    alive = """
        AND EXISTS (SELECT 1 FROM crystallizations a WHERE a.id=e.src AND a.status='active')
        AND EXISTS (SELECT 1 FROM crystallizations b WHERE b.id=e.dst AND b.status='active')
    """
    mentions = db().execute(
        f"""SELECT e.src, e.dst, e.kind FROM edges e
            WHERE e.ended=0 AND e.kind='提及' {alive}"""
    ).fetchall()
    others = db().execute(
        f"""SELECT e.src, e.dst, e.kind FROM edges e
            WHERE e.ended=0 AND e.kind!='提及'
              AND EXISTS (
                SELECT 1 FROM statements s
                WHERE s.id=e.statement_id AND s.invalid_at IS NULL AND s.trust='written'
              )
              {alive}"""
    ).fetchall()
    return {"nodes": nodes, "edges": [dict(row) for row in list(mentions) + list(others)]}


def _crystal_by_ref(ref: str):
    text = (ref or "").strip()
    if not text:
        return None
    row = db().execute("SELECT * FROM crystallizations WHERE id=?", (text,)).fetchone()
    if row is not None:
        return row
    return db().execute(
        "SELECT * FROM crystallizations WHERE name=? AND status='active' ORDER BY id LIMIT 1",
        (text,),
    ).fetchone()


def revisions_for_agent(ref: str) -> dict:
    """The revision chain an agent can read. Snapshots stay in the library."""
    row = _crystal_by_ref(ref)
    if row is None:
        return {"ok": False, "error": "没有这篇"}
    items = []
    for item in revisions(row["id"]):
        items.append(
            {
                "id": item.get("id"),
                "created_at": item.get("created_at") or "",
                "note": item.get("note") or "",
                "actor": item.get("actor") or "",
                "change": item.get("change") or "",
                "basis": item.get("basis") or "",
                "quote": item.get("quote") or "",
                "source_id": item.get("source_id") or "",
                "gist": item.get("gist") or "",
            }
        )
    return {"ok": True, "id": row["id"], "name": row["name"], "type": row["type"], "revisions": items}


def _rejoin_draft(con: sqlite3.Connection, source_id: str) -> dict:
    """The same draft is one source. A finished one stays finished; a dropped one goes back to 并页."""
    landed = con.execute("SELECT 1 FROM revisions WHERE source_id=? LIMIT 1", (source_id,)).fetchone()
    queued = con.execute(
        "SELECT id, state FROM queue WHERE source_id=? ORDER BY id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    state = queued["state"] if queued else ""
    if landed and state in {"", "done"}:
        con.commit()
        return {"ok": True, "id": source_id, "duplicate": True, "queued": ""}
    if state in {"queued", "leased"}:
        return {"ok": True, "id": source_id, "duplicate": True, "queued": "并页"}
    if queued:
        con.execute(
            """UPDATE queue SET state='queued', lane='jump', next_at=NULL, attempts=0, last_error=NULL, cursor=0
               WHERE id=?""",
            (queued["id"],),
        )
    else:
        _queue_source(con, source_id, "jump")
    con.execute("UPDATE sources SET lane='jump' WHERE id=?", (source_id,))
    con.commit()
    return {"ok": True, "id": source_id, "duplicate": True, "queued": "并页"}


def submit_draft(markdown: str, agent=None) -> dict:
    """Queue a same-named page written in the first-distill shape.

    The live body and sketch stay as they are. The draft enters at 并页,
    ahead of the ordinary backlog, then polish and sketch.
    """
    from rogmem.pages_md import PAGE_TYPES, parse_pages

    text = (markdown or "").strip()
    pages = parse_pages(text)
    if not pages:
        return {"ok": False, "error": "这不是首蒸交来的一页"}
    prepared = []
    with LOCK:
        con = db()
        for page in pages:
            name = str(page.get("name") or "").strip()
            ctype = str(page.get("type") or "").strip()
            body = str(page.get("body") or "").strip()
            if not name or not body or ctype not in PAGE_TYPES:
                return {"ok": False, "error": "这不是首蒸交来的一页"}
            row = _named_or_folded(name, ctype, con)
            if row is None or (row["type"] or "") != ctype:
                return {"ok": False, "error": "没有这篇"}
            prepared.append({"id": row["id"], "name": row["name"] or name, "type": row["type"], "body": body})
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        idem = f"draft:{content_hash}"
        old = con.execute("SELECT id FROM sources WHERE idempotency_key=?", (idem,)).fetchone()
        if old:
            return _rejoin_draft(con, old["id"])
        source_id = secrets.token_hex(8)
        now = utcnow()
        agent_id, device_id, device_name, who = _host_binding(agent)
        payload = json.dumps({"phase": "draft", "pages": prepared}, ensure_ascii=False)
        con.execute(
            """INSERT INTO sources(
                 id,kind,device_id,device_name,agent_id,agent_name,event_at,ingested_at,idempotency_key,content_hash,body,status,lane,pair_ready,stage_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'已解析待向量化','jump',1,?)""",
            (source_id, "draft", device_id, device_name, agent_id, who, now, now, idem, content_hash, text, payload),
        )
        _queue_source(con, source_id, "jump")
        con.commit()
    return {"ok": True, "id": source_id, "duplicate": False, "queued": "并页"}


def _shown_gist(item: dict) -> str:
    return revision_gist_for(item)


def revisions(crystal_id: str, revision_id: int | None = None) -> list[dict]:
    sql = """SELECT id, created_at, note, source_id, quote, event_at, statement_id, actor, change, basis, scope, gist
             FROM revisions WHERE crystal_id=?"""
    params: list = [crystal_id]
    if revision_id is not None:
        sql += " AND id=?"
        params.append(revision_id)
    sql += " ORDER BY id"
    rows = db().execute(sql, params).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["scope"] = item.get("scope") or ""
        if item.get("quote") or not item.get("note"):
            item["gist"] = _shown_gist(item)
            out.append(item)
            continue
        found = None
        if item["note"] == "开篇":
            found = db().execute(
                """SELECT id, quote, source_id, event_at FROM statements
                   WHERE crystal_id=? AND field IN ('名称','姓名') AND quote IS NOT NULL AND quote != ''
                   ORDER BY created_at LIMIT 1""",
                (crystal_id,),
            ).fetchone()
        elif ":" in item["note"] and not item["note"].startswith(("回滚", "撤回")):
            label, _, value = item["note"].partition(":")
            found = db().execute(
                """SELECT id, quote, source_id, event_at FROM statements
                   WHERE crystal_id=? AND (field=? OR chapter=?) AND quote IS NOT NULL AND quote != ''
                     AND (text=? OR text LIKE ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (crystal_id, label, label, value, value + "%"),
            ).fetchone()
        if found:
            item["quote"] = item.get("quote") or found["quote"]
            item["source_id"] = item.get("source_id") or found["source_id"]
            item["statement_id"] = item.get("statement_id") or found["id"]
            item["event_at"] = item.get("event_at") or found["event_at"]
        if not (item.get("actor") or item.get("basis")) and item.get("source_id"):
            who, why = _who_and_basis(db(), item["source_id"], item.get("quote") or "")
            item["actor"] = item.get("actor") or who
            item["basis"] = item.get("basis") or why
        if item.get("note") == "正文" and not (item.get("change") or "").strip():
            item["actor"] = item.get("actor") or "蒸馏"
            item["change"] = "这一笔只记下写过正文，没有留下改了哪些字。"
            item["basis"] = item.get("basis") or "没有记下是哪一份材料。"
        origin = resolve_source_origin(item.get("source_id") or "")
        if origin.get("ok"):
            item["session_id"] = origin["session_id"]
            item["turn_id"] = origin["turn_id"]
            item["session_href"] = origin["session_href"]
            item["source_href"] = origin_links(
                origin["session_id"],
                origin["turn_id"],
                item.get("source_id") or "",
                item.get("statement_id") or "",
            )["source_href"]
        item["scope"] = item.get("scope") or ""
        item["gist"] = _shown_gist(item)
        out.append(item)
    return out


def rollback(crystal_id: str, revision_id: int | None = None) -> dict:
    with LOCK:
        con = db()
        crystal = con.execute("SELECT id FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
        if not crystal:
            return {"ok": False, "error": "没有这篇"}
        closed = 0
        if revision_id is not None:
            rev = con.execute(
                "SELECT created_at, page_json, snapshot FROM revisions WHERE id=? AND crystal_id=?",
                (revision_id, crystal_id),
            ).fetchone()
            if not rev:
                return {"ok": False, "error": "没有这一版"}
            rows = con.execute(
                "SELECT id FROM statements WHERE crystal_id=? AND invalid_at IS NULL AND created_at>?",
                (crystal_id, rev["created_at"]),
            ).fetchall()
            fresh = con.execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
            restored = hydrate(fresh)
            state = _parse_page_json(rev["page_json"])
            restored_page = False
            if state is not None:
                if (
                    restored.name != state["name"]
                    or (restored.body_md or "") != state["body"]
                    or (restored.sketch_md or "") != state["sketch"]
                ):
                    restored.name = state["name"]
                    restored.body_md = state["body"]
                    restored.sketch_md = state["sketch"]
                    restored_page = True
            elif (rev["snapshot"] or "").strip() and (restored.body_md or "") != rev["snapshot"]:
                restored.body_md = rev["snapshot"]
                restored_page = True
            if not restored_page and not rows:
                return {"ok": True, "unchanged": True, "closed": 0}
            now = utcnow()
            for row in rows:
                con.execute("UPDATE statements SET invalid_at=? WHERE id=?", (now, row["id"]))
                con.execute("UPDATE edges SET ended=1 WHERE statement_id=?", (row["id"],))
                closed += 1
            if rows:
                _rebuild_projection(con, crystal_id)
            if restored_page:
                CrystalStore(con).put(restored, holder=f"rollback:{crystal_id}", commit=False)
            restored_body = restored_page
        else:
            restored_body = False
        change = "撤销这一版之后的改动。这一版之后写入的陈述不再有效。已经抹掉的原文不写回来。"
        if restored_body:
            change = "撤销这一版之后的改动。这一页退回到所选的那一版。已经抹掉的原文不写回来。"
        _record_revision(
            con,
            crystal_id,
            "回滚。已忘掉的原文不恢复",
            actor="人工",
            change=change,
            basis="按修订日志退回。",
        )
        con.commit()
    leaked = db().execute("SELECT body FROM sources WHERE redacted=1 AND body LIKE '%sk-live-%'").fetchone()
    return {"ok": True, "restored_secret": bool(leaked), "closed": closed}


def alerts() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT id,created_at,message,open FROM alerts ORDER BY id DESC LIMIT 80")]


def _apply_rejudge_update(con, field: str, old_text: str, new_text: str, quote: str, source, *, store=None, commit: bool = True) -> None:
    """An 更新 verdict replaces the open value. Two summaries never reach this path.

    Persist goes through CrystalStore thin field ops. No summary_json bypass.
    """
    from rogmem import crystal_store as crystal_store_mod

    if not field or not new_text:
        return
    published_quote = quote or new_text
    if published_quote not in new_text or _verbatim_missing(published_quote, new_text):
        return
    now = utcnow()
    for crystal in con.execute("SELECT * FROM crystallizations WHERE status='active'"):
        summary = json.loads(crystal["summary_json"] or "{}")
        if field not in summary:
            continue
        current = con.execute(
            """SELECT id, text FROM statements
               WHERE crystal_id=? AND field=? AND invalid_at IS NULL AND trust='written'
               ORDER BY created_at DESC LIMIT 1""",
            (crystal["id"], field),
        ).fetchone()
        shown = current["text"] if current else summary.get(field)
        if shown == new_text:
            continue
        if shown not in {old_text, "尚无来源", "", None}:
            continue
        had_old = summary.get(field) == old_text or con.execute(
            "SELECT 1 FROM statements WHERE crystal_id=? AND field=? AND text=?",
            (crystal["id"], field, old_text),
        ).fetchone()
        if not had_old:
            continue
        rank = {"user": "user", "summary": "summary", "file": "file"}.get(source["kind"], "agent")

        def sidecar(crystal_id=crystal["id"], current_row=current) -> None:
            if current_row:
                con.execute("UPDATE statements SET invalid_at=? WHERE id=?", (now, current_row["id"]))
                con.execute("UPDATE edges SET ended=1 WHERE statement_id=?", (current_row["id"],))
            statement_id = _add_statement(
                con,
                crystal_id,
                field,
                None,
                new_text,
                published_quote,
                source["id"],
                rank,
                "written",
                source["event_at"] or source["ingested_at"],
            )
            _link_relations(con, statement_id)
            _record_revision(
                con,
                crystal_id,
                f"{field}:{new_text}"[:80],
                source["id"],
                published_quote,
                source["event_at"] or "",
                statement_id,
                change=f"更新：{field}。{new_text}",
                basis=published_quote,
            )

        if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
            continue
        target_store = store if store is not None else CrystalStore(con)
        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=crystal["id"],
            expected_revision=int(crystal["revision"] or 0),
            ops=field_update_ops(field, new_text),
            holder=f"distill:{source['id']}",
            sidecar=sidecar,
            injected=store is not None,
            commit=commit,
        )


def _identity_denied(con, left_id: str, right_id: str, name_a: str, name_b: str) -> bool:
    judged = con.execute(
        """SELECT 1 FROM decisions WHERE kind='不同实体' AND detail LIKE ? AND detail LIKE ?""",
        (f"%{left_id}%", f"%{right_id}%"),
    ).fetchone()
    if judged:
        return True
    for source in con.execute("SELECT body FROM sources"):
        body = source["body"] or ""
        if any(word in body for word in ("不是同一人", "不是同一个人", "并非同一人")) and (
            (name_a and name_a in body) or (name_b and name_b in body)
        ):
            return True
    return False


def _same_basis(body: str, name_a: str, name_b: str, anchors: list) -> bool:
    if any(phrase in body for phrase in ("就是", "是同一人", "是同一个人")) and name_a in body and name_b in body:
        return True
    found = [item for item in anchors if item and str(item) in body]
    return len(found) >= 2


def _merge_pair(con, left_id: str, right_id: str, source_id: str) -> int:
    rows = []
    for cid in (left_id, right_id):
        row = con.execute("SELECT * FROM crystallizations WHERE id=? AND status='active'", (cid,)).fetchone()
        if row is not None and row["id"] != "owner":
            rows.append(row)
    if len(rows) < 2:
        return 0
    keep = _older_crystal(con, rows)
    merged = 0
    for other in rows:
        if other["id"] == keep["id"]:
            continue
        _merge_into(con, keep["id"], other["id"], source_id)
        merged += 1
    return merged


def _mark_possible_same(con, focus_ids: set[str] | None = None) -> int:
    """Mark same-type pages that share two rare anchors. Do not merge them."""
    crystals = con.execute(
        "SELECT id, type, name FROM crystallizations WHERE status='active'"
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in crystals:
        grouped.setdefault(row["type"], []).append(row)
    marked = 0
    for group in grouped.values():
        if len(group) < 2:
            continue
        owned: dict[tuple[str, str], set[str]] = {}
        for row in group:
            statements = con.execute(
                """SELECT field, text FROM statements
                   WHERE crystal_id=? AND invalid_at IS NULL AND trust='written' AND field IS NOT NULL""",
                (row["id"],),
            )
            for item in statements:
                text = (item["text"] or "").strip()
                if item["field"] in {"名称", "姓名", "概述", "别名"}:
                    continue
                if len(text) < 6 or text in {"尚无来源", "暂无来源"}:
                    continue
                owned.setdefault((item["field"], text), set()).add(row["id"])
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if focus_ids is not None and left["id"] not in focus_ids and right["id"] not in focus_ids:
                    continue
                if left["name"].strip() == right["name"].strip():
                    continue
                shared = [
                    text
                    for (field, text), owners in owned.items()
                    if owners == {left["id"], right["id"]}
                ]
                if len(shared) < 2:
                    continue
                already = con.execute(
                    """SELECT 1 FROM decisions
                       WHERE kind IN ('可能同一实体','不同实体','同一实体')
                         AND detail LIKE ? AND detail LIKE ?""",
                    (f"%{left['id']}%", f"%{right['id']}%"),
                ).fetchone()
                if already:
                    continue
                con.execute(
                    "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
                    (
                        utcnow(),
                        "可能同一实体",
                        json.dumps(
                            {
                                "a": left["id"],
                                "b": right["id"],
                                "name_a": left["name"],
                                "name_b": right["name"],
                                "anchors": shared[:4],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                marked += 1
                if not _identity_denied(con, left["id"], right["id"], left["name"], right["name"]):
                    _merge_pair(con, left["id"], right["id"], "")
    return marked


def _rejudge_possible_same(con) -> int:
    """Ask once when a newer source names either page. Never merge or delete here."""
    rows = con.execute(
        "SELECT id, created_at, detail FROM decisions WHERE kind='可能同一实体' ORDER BY id DESC"
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    asked = 0
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            continue
        left = str(detail.get("a") or "")
        right = str(detail.get("b") or "")
        if not left or not right or left == right:
            continue
        key = tuple(sorted((left, right)))
        if key in seen:
            continue
        seen.add(key)
        settled = con.execute(
            """SELECT 1 FROM decisions
               WHERE id>? AND kind IN ('不同实体','同一实体')
                 AND detail LIKE ? AND detail LIKE ?""",
            (row["id"], f"%{left}%", f"%{right}%"),
        ).fetchone()
        if settled:
            continue
        name_a = str(detail.get("name_a") or "")
        name_b = str(detail.get("name_b") or "")
        newer = None
        for source in con.execute(
            "SELECT id, body FROM sources WHERE ingested_at>? ORDER BY ingested_at",
            (row["created_at"],),
        ):
            body = source["body"] or ""
            if (name_a and name_a in body) or (name_b and name_b in body):
                newer = source
        if newer is None:
            continue
        try:
            answer = _ask_same_entity(name_a, name_b, detail.get("anchors") or [], newer["body"] or "")
        except Exception:
            continue
        body = newer["body"] or ""
        anchors = detail.get("anchors") or []
        denied_now = any(word in body for word in ("不是同一人", "不是同一个人", "并非同一人"))
        if answer == "是" and not denied_now and _same_basis(body, name_a, name_b, anchors):
            _merge_pair(con, left, right, newer["id"])
        con.execute(
            "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
            (
                utcnow(),
                "同一实体" if answer == "是" else "不同实体",
                json.dumps(
                    {
                        "a": left,
                        "b": right,
                        "name_a": name_a,
                        "name_b": name_b,
                        "source_id": newer["id"],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        asked += 1
    return asked


def _release_quote(body: str, feature: str) -> str:
    parts = [part.strip() for part in re.split(r"[。！？\n]", body or "") if part.strip()]
    delivered = ("已经交付", "已交付", "交付了")
    for part in parts:
        if feature in part and any(word in part for word in delivered):
            return part
    for part in parts:
        if feature in part:
            return part
    return feature


def _release_stamp(text: str) -> str:
    found = []
    for pattern in (r"v\d+(?:\.\d+){1,3}", r"\d{4}-\d{2}-\d{2}", r"\d{4}年\d{1,2}月\d{1,2}日"):
        match = re.search(pattern, text or "")
        if match and match.group(0) not in found:
            found.append(match.group(0))
    return " ".join(found)


def _strike_features(con, *, store=None) -> int:
    """Strike a planned or in-progress line only when a later source names it and says delivered or abandoned.

    Persist goes through chapter ops. No chapters_json bypass. Switch-off fail-closed.
    """
    from rogmem import crystal_store as crystal_store_mod

    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
        return 0
    delivered = ("已经交付", "已交付", "交付了")
    abandoned = ("不做了", "已放弃", "放弃了")
    changed = 0
    rows = con.execute("SELECT * FROM crystallizations WHERE type='工程' AND status='active'").fetchall()
    for row in rows:
        chapters = _chapters_map(row)
        orig = dict(chapters)
        dirty = False
        planned_add: list[tuple] = []
        for chapter in ("计划中特性", "开发中特性"):
            text = chapters.get(chapter) or ""
            if text in {"", "暂无来源"}:
                continue
            lines = []
            for line in text.split("\n"):
                stripped = line.strip()
                if (
                    not stripped
                    or stripped.startswith("~~")
                    or "已交付" in stripped
                    or "已放弃" in stripped
                    or len(stripped) < 2
                ):
                    lines.append(line)
                    continue
                mark = ""
                hit = None
                for source in con.execute("SELECT id, body, kind, event_at, ingested_at FROM sources"):
                    body = source["body"] or ""
                    if stripped not in body:
                        continue
                    if any(word in body for word in abandoned):
                        mark = "已放弃"
                        hit = source
                        break
                    if any(word in body for word in delivered):
                        mark = "已交付"
                        hit = source
                        break
                if mark == "已放弃":
                    lines.append(f"~~{stripped}~~ 已放弃")
                    dirty = True
                elif mark == "已交付" and hit is not None:
                    quote = _release_quote(hit["body"] or "", stripped)
                    stamp = _release_stamp(quote)
                    entry = f"{stamp + ' ' if stamp else ''}交付了{stripped}。{quote}"
                    pointer = stamp or stripped
                    lines.append(f"~~{stripped}~~ 已交付 → 发版日志：{pointer}")
                    log = chapters.get("发版日志") or ""
                    entries = [] if log in {"", "暂无来源"} else [item for item in log.split("\n") if item.strip()]
                    if entry not in entries:
                        entries.insert(0, entry)
                        rank = {"user": "user", "summary": "summary", "file": "file"}.get(hit["kind"], "agent")
                        planned_add.append(
                            (
                                row["id"],
                                None,
                                "发版日志",
                                entry,
                                quote,
                                hit["id"],
                                rank,
                                "written",
                                hit["event_at"] or hit["ingested_at"],
                            )
                        )
                    chapters["发版日志"] = "\n".join(entries)
                    dirty = True
                else:
                    lines.append(line)
            chapters[chapter] = "\n".join(lines)
        if not dirty:
            continue
        ops = []
        for chapter, text in chapters.items():
            if orig.get(chapter) != text:
                ops.extend(chapter_update_ops(chapter, text))
        if not ops:
            continue
        target_store = store if store is not None else CrystalStore(con)

        def sidecar(planned=planned_add, cid=row["id"]) -> None:
            for args in planned:
                _add_statement(con, *args)
            _record_revision(
                con,
                cid,
                "清扫：特性已划掉",
                actor="系统",
                change="划掉已经交付或放弃的特性，并写进发版日志。",
                basis="后文写明这项已经交付或放弃。划掉的仍留在原处。",
            )

        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=row["id"],
            expected_revision=int(row["revision"] or 0),
            ops=ops,
            holder="sweep:strike",
            sidecar=sidecar,
            injected=store is not None,
            commit=False,
        )
        changed += 1
    return changed


def _move_started_features(con, *, store=None) -> int:
    """Move a planned line into 开发中 only when a source names that line and says work started.

    Persist goes through chapter ops. No chapters_json bypass. Switch-off fail-closed.
    """
    from rogmem import crystal_store as crystal_store_mod

    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
        return 0
    started = ("已经开工", "开工了", "开始做")
    finished = ("已经交付", "已交付", "交付了", "不做了", "已放弃", "放弃了")
    changed = 0
    rows = con.execute("SELECT * FROM crystallizations WHERE type='工程' AND status='active'").fetchall()
    for row in rows:
        chapters = _chapters_map(row)
        orig = dict(chapters)
        plan = chapters.get("计划中特性") or ""
        if plan in {"", "暂无来源"}:
            continue
        keep = []
        moved = []
        for line in plan.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("~~") or len(stripped) < 2:
                keep.append(line)
                continue
            go = False
            for source in con.execute("SELECT body FROM sources"):
                body = source["body"] or ""
                if stripped not in body or any(word in body for word in finished):
                    continue
                if any(word in body for word in started):
                    go = True
                    break
            if go:
                moved.append(stripped)
            else:
                keep.append(line)
        if not moved:
            continue
        chapters["计划中特性"] = "\n".join(keep) if any(item.strip() for item in keep) else "暂无来源"
        doing = chapters.get("开发中特性") or ""
        lines = [] if doing in {"", "暂无来源"} else [item for item in doing.split("\n") if item.strip()]
        for item in moved:
            if item not in lines:
                lines.append(item)
        chapters["开发中特性"] = "\n".join(lines)
        ops = []
        for chapter, text in chapters.items():
            if orig.get(chapter) != text:
                ops.extend(chapter_update_ops(chapter, text))
        if not ops:
            continue
        target_store = store if store is not None else CrystalStore(con)

        def sidecar(cid=row["id"]) -> None:
            _record_revision(
                con,
                cid,
                "清扫：计划中的特性已开工",
                actor="系统",
                change="把已经开工的项从计划挪到开发中。",
                basis="后文点名这项并写明已经开工。",
            )

        _apply_then_sidecar(
            target_store,
            con,
            crystal_id=row["id"],
            expected_revision=int(row["revision"] or 0),
            ops=ops,
            holder="sweep:move",
            sidecar=sidecar,
            injected=store is not None,
            commit=False,
        )
        changed += 1
    return changed


_SAME_PHRASE = ("就是", "是同一人", "是同一个人")


def _bare_name_set(text: str, names: list[str]) -> set[str]:
    occupied: list[tuple[int, int]] = []
    found: set[str] = set()
    for name in sorted({item for item in names if item and len(item) >= 2}, key=len, reverse=True):
        start = 0
        while True:
            at = text.find(name, start)
            if at < 0:
                break
            end = at + len(name)
            if not any(left <= at and end <= right for left, right in occupied):
                found.add(name)
                occupied.append((at, end))
            start = end
    return found


def _older_crystal(con, crystals: list) -> sqlite3.Row:
    def key(row: sqlite3.Row) -> tuple[str, str]:
        if row["id"] == "owner":
            return ("", "")
        first = con.execute("SELECT MIN(created_at) FROM statements WHERE crystal_id=?", (row["id"],)).fetchone()[0]
        return (first or "9999", row["id"])

    return min(crystals, key=key)


def _merge_into(con, keep_id: str, other_id: str, source_id: str, *, store=None) -> None:
    """Move statements/edges, then merge chapter text through thin chapter ops.

    Identity merge always runs. Chapter persist is fail-closed while the write
    switch is off. No chapters_json bypass.
    """
    from rogmem import crystal_store as crystal_store_mod

    keep = con.execute("SELECT * FROM crystallizations WHERE id=? AND status='active'", (keep_id,)).fetchone()
    other = con.execute("SELECT * FROM crystallizations WHERE id=? AND status='active'", (other_id,)).fetchone()
    if keep is None or other is None or keep["id"] == other["id"]:
        return
    con.execute(
        "UPDATE statements SET home_crystal=crystal_id WHERE crystal_id=? AND home_crystal IS NULL",
        (other["id"],),
    )
    con.execute("UPDATE edges SET home_src=src WHERE src=? AND home_src IS NULL", (other["id"],))
    con.execute("UPDATE edges SET home_dst=dst WHERE dst=? AND home_dst IS NULL", (other["id"],))
    con.execute("UPDATE statements SET crystal_id=? WHERE crystal_id=?", (keep["id"], other["id"]))
    con.execute("UPDATE edges SET src=? WHERE src=?", (keep["id"], other["id"]))
    con.execute("UPDATE edges SET dst=? WHERE dst=?", (keep["id"], other["id"]))
    con.execute("UPDATE edges SET ended=1 WHERE src=? AND dst=? AND ended=0", (keep["id"], keep["id"]))
    chapters = _chapters_map(keep)
    orig = dict(chapters)
    for key, value in _chapters_map(other).items():
        text = (value or "").strip()
        if text in {"", "暂无来源"} or text in (chapters.get(key) or ""):
            continue
        prev = chapters.get(key) or "暂无来源"
        chapters[key] = text if prev in {"", "暂无来源"} else prev + "\n" + text
    ops = []
    for key, text in chapters.items():
        if orig.get(key) != text:
            ops.extend(chapter_update_ops(key, text))
    con.execute("UPDATE crystallizations SET status='已合并', revision=revision+1 WHERE id=?", (other["id"],))
    _record_revision(con, keep["id"], f"合并：收进{other['name']}"[:80], source_id)
    _record_revision(con, other["id"], f"已合并到{keep['name']}"[:80], source_id)
    will_apply = (crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED or store is not None) and ops
    if not will_apply:
        con.execute("UPDATE crystallizations SET revision=revision+1 WHERE id=?", (keep["id"],))
        return
    target_store = store if store is not None else CrystalStore(con)
    _apply_then_sidecar(
        target_store,
        con,
        crystal_id=keep["id"],
        expected_revision=int(keep["revision"] or 0),
        ops=ops,
        holder=f"distill:{source_id}",
        sidecar=lambda: None,
        injected=store is not None,
        commit=False,
    )


_DENIAL = ("不是同一人", "不是同一个人", "并非同一人")


def _denial_phrase(body: str) -> bool:
    return any(word in (body or "") for word in _DENIAL)


def _unmerge_pair(con, merged_id: str, active_id: str, source_id: str) -> int:
    drop = con.execute("SELECT * FROM crystallizations WHERE id=? AND status='已合并'", (merged_id,)).fetchone()
    keep = con.execute("SELECT * FROM crystallizations WHERE id=? AND status='active'", (active_id,)).fetchone()
    if drop is None or keep is None or drop["id"] == "owner" or keep["id"] == "owner":
        return 0
    con.execute(
        "UPDATE statements SET crystal_id=home_crystal, home_crystal=NULL WHERE home_crystal=? AND crystal_id=?",
        (drop["id"], keep["id"]),
    )
    con.execute(
        "UPDATE edges SET src=home_src, home_src=NULL WHERE home_src=? AND src=?",
        (drop["id"], keep["id"]),
    )
    con.execute(
        "UPDATE edges SET dst=home_dst, home_dst=NULL WHERE home_dst=? AND dst=?",
        (drop["id"], keep["id"]),
    )
    con.execute("UPDATE crystallizations SET status='active', revision=revision+1 WHERE id=?", (drop["id"],))
    _rebuild_projection(con, keep["id"])
    _rebuild_projection(con, drop["id"])
    _record_revision(con, keep["id"], f"拆开：{drop['name']}不是同一人"[:80], source_id)
    _record_revision(con, drop["id"], f"拆开：与{keep['name']}不是同一人"[:80], source_id)
    return 1


def _split_denied(con, body: str, source_id: str) -> int:
    if not _denial_phrase(body):
        return 0
    rows = list(
        con.execute(
            "SELECT id, name, type, status FROM crystallizations WHERE status IN ('active','已合并') AND id!='owner'"
        )
    )
    done = 0
    seen: set[tuple[str, str]] = set()
    for sentence in re.split(r"[。！？\n]", body or ""):
        if not _denial_phrase(sentence):
            continue
        mentioned = [row for row in rows if row["name"] and row["name"] in sentence]
        for merged in mentioned:
            if merged["status"] != "已合并":
                continue
            for active in mentioned:
                if active["status"] != "active" or active["type"] != merged["type"]:
                    continue
                key = (merged["id"], active["id"])
                if key in seen:
                    continue
                seen.add(key)
                done += _unmerge_pair(con, merged["id"], active["id"], source_id)
    return done


def _explicit_same_merge(con) -> int:
    """Merge only when one sentence names both pages and says they are the same."""
    rows = con.execute("SELECT id, type, name FROM crystallizations WHERE status='active'").fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["type"], []).append(row)
    merged = 0
    for group in grouped.values():
        if len(group) < 2:
            continue
        names = [row["name"] for row in group]
        by_name: dict[str, list] = {}
        for row in group:
            by_name.setdefault(row["name"], []).append(row)
        for source in con.execute("SELECT id, body FROM sources"):
            body = source["body"] or ""
            if not any(phrase in body for phrase in _SAME_PHRASE):
                continue
            for sentence in re.split(r"[。！？\n]", body):
                if not any(phrase in sentence for phrase in _SAME_PHRASE):
                    continue
                found = _bare_name_set(sentence, names)
                if len(found) < 2:
                    continue
                live = []
                seen: set[str] = set()
                for name in found:
                    for row in by_name.get(name, []):
                        if row["id"] in seen:
                            continue
                        current = con.execute(
                            "SELECT id FROM crystallizations WHERE id=? AND status='active'",
                            (row["id"],),
                        ).fetchone()
                        if current is None:
                            continue
                        seen.add(row["id"])
                        live.append(current)
                if len(live) < 2:
                    continue
                keep = _older_crystal(con, live)
                for other in live:
                    if other["id"] == keep["id"]:
                        continue
                    before = con.execute(
                        "SELECT status FROM crystallizations WHERE id=?",
                        (other["id"],),
                    ).fetchone()
                    _merge_into(con, keep["id"], other["id"], source["id"])
                    after = con.execute(
                        "SELECT status FROM crystallizations WHERE id=?",
                        (other["id"],),
                    ).fetchone()
                    if before and after and before["status"] == "active" and after["status"] == "已合并":
                        merged += 1
    return merged


def _page_labels(row) -> list[str]:
    crystal = hydrate(row)
    labels = [str(row["name"] or "").strip()]
    labels.extend(str(alias).strip() for alias in crystal.aliases)
    return [label for label in labels if label]


def _crystal_born_at(con, crystal_id: str) -> str:
    event = con.execute(
        """SELECT MIN(at) FROM crystal_events
           WHERE crystal_id=? AND kind IN ('promote_new','draft_created')""",
        (crystal_id,),
    ).fetchone()
    if event and event[0]:
        return str(event[0])
    revision = con.execute(
        "SELECT MIN(created_at) FROM revisions WHERE crystal_id=?",
        (crystal_id,),
    ).fetchone()
    return str(revision[0] or "") if revision else ""


def _page_digest(con, crystal_id: str) -> str:
    bits = []
    rows = con.execute(
        """SELECT text FROM statements
           WHERE crystal_id=? AND invalid_at IS NULL AND trust='written' LIMIT 4""",
        (crystal_id,),
    )
    for row in rows:
        text = " ".join(str(row["text"] or "").split())
        if text:
            bits.append(text[:80])
    return "；".join(bits)[:300]


def _identity_payload(item: dict) -> str:
    return json.dumps(
        {
            "a": item["a"],
            "b": item["b"],
            "name_a": item["name_a"],
            "name_b": item["name_b"],
            "via": "reconcile",
        },
        ensure_ascii=False,
    )


def _record_identity(con, kind: str, item: dict) -> None:
    con.execute(
        "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
        (utcnow(), kind, _identity_payload(item)),
    )


def _record_possible_once(con, item: dict) -> None:
    existing = con.execute(
        """SELECT 1 FROM decisions
           WHERE kind='可能同一实体' AND detail LIKE ? AND detail LIKE ?""",
        (f"%{item['a']}%", f"%{item['b']}%"),
    ).fetchone()
    if existing:
        return
    _record_identity(con, "可能同一实体", item)


def _reconcile_hours() -> int:
    raw = get_setting("reconcile_hours") or "3"
    try:
        hours = int(raw)
    except ValueError:
        return 3
    return hours if 1 <= hours <= 168 else 3


def _reconcile_since() -> str:
    done = get_setting("reconcile_done_at")
    if done:
        return done
    return (datetime.now(timezone.utc) - timedelta(hours=_reconcile_hours())).isoformat()


def _mark_reconciled() -> None:
    set_setting("reconcile_done_at", utcnow())
    set_setting("reconcile_done_unix", str(time.time()))


def reconcile_due() -> bool:
    raw = get_setting("reconcile_done_unix")
    if not raw:
        return True
    try:
        last = float(raw)
    except ValueError:
        return True
    return time.time() - last >= _reconcile_hours() * 3600


def _remember_other_name(con, keep_id: str, other_name: str) -> None:
    """Keep the other language's spelling on the page that remains."""
    from rogmem.names import name_key

    other_name = (other_name or "").strip()
    if len(other_name) < 2:
        return
    row = con.execute(
        "SELECT * FROM crystallizations WHERE id=? AND status='active'",
        (keep_id,),
    ).fetchone()
    if row is None:
        return
    labels = _page_labels(row)
    wanted = name_key(other_name)
    if any(name_key(label) == wanted for label in labels):
        return
    aliases = [label for label in labels if label != (row["name"] or "")]
    aliases.append(other_name)
    _apply_then_sidecar(
        CrystalStore(con),
        con,
        crystal_id=keep_id,
        expected_revision=int(row["revision"] or 0),
        ops=field_update_ops("别名", "、".join(aliases)),
        holder=f"reconcile:{keep_id}",
        sidecar=lambda: _record_revision(
            con,
            keep_id,
            f"别名：{other_name}"[:80],
            actor="系统",
            change=f"别名加上{other_name}。",
            basis="另一处写法指的是同一页，把这个名字留在这一页上。",
        ),
        injected=False,
        commit=False,
    )


def _same_written_label(left: list[str], right: list[str]) -> bool:
    """The two pages already carry the same written label. Not a near-spelling guess."""
    from rogmem.names import name_key

    keys = {name_key(label) for label in left if len(name_key(label)) >= 2}
    return any(name_key(label) in keys for label in right)


def _fold_label_overlaps(con, recent: set[str]) -> int:
    """Merge same-type pages that already use the same written name. Keep the older page."""
    rows = list(con.execute("SELECT * FROM crystallizations WHERE status='active' AND id!='owner'"))
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["type"], []).append(row)
    merged = 0
    for group in grouped.values():
        labels = {row["id"]: _page_labels(row) for row in group}
        parent = {row["id"]: row["id"] for row in group}

        def find(cid: str) -> str:
            while parent[cid] != cid:
                parent[cid] = parent[parent[cid]]
                cid = parent[cid]
            return cid

        ids = list(labels)
        for index, left in enumerate(ids):
            for right in ids[index + 1 :]:
                if not _same_written_label(labels[left], labels[right]):
                    continue
                if _identity_denied(con, left, right, "", ""):
                    continue
                parent[find(left)] = find(right)
        clusters: dict[str, list[str]] = {}
        for cid in ids:
            clusters.setdefault(find(cid), []).append(cid)
        for members in clusters.values():
            if len(members) < 2:
                continue
            live = []
            for cid in members:
                row = con.execute(
                    "SELECT * FROM crystallizations WHERE id=? AND status='active'",
                    (cid,),
                ).fetchone()
                if row is not None:
                    live.append(row)
            if len(live) < 2:
                continue
            keep = _older_crystal(con, live)
            for other in live:
                if other["id"] == keep["id"]:
                    continue
                if _identity_denied(con, keep["id"], other["id"], keep["name"], other["name"]):
                    continue
                try:
                    _merge_into(con, keep["id"], other["id"], "")
                    _remember_other_name(con, keep["id"], other["name"])
                    _record_identity(
                        con,
                        "同一实体",
                        {
                            "a": keep["id"],
                            "b": other["id"],
                            "name_a": keep["name"],
                            "name_b": other["name"],
                        },
                    )
                    con.commit()
                    merged += 1
                except Exception:
                    con.rollback()
                    try:
                        _record_possible_once(
                            con,
                            {
                                "a": keep["id"],
                                "b": other["id"],
                                "name_a": keep["name"],
                                "name_b": other["name"],
                            },
                        )
                        con.commit()
                    except Exception:
                        con.rollback()
    return merged


# Neighbors below this cosine are not worth a model call on bge-small-zh.
# Live pages put 简历主人 next to 林知远 around 0.77, and unrelated pages much lower.
MERGE_NEIGHBOR_FLOOR = 0.68
# One nearest neighbor per page. A blob of poorly separated pages must not
# spend the whole model budget before a real nearest pair is asked.
MERGE_NEIGHBOR_K = 1
MERGE_ASK_CAP = 40


def _cosine(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _embed_many(texts: list[str]) -> list[list[float] | None]:
    if _EMBED == "off":
        return [None for _ in texts]
    if _EMBED == "fastembed" and (_MODEL is not None or _embed_is_remote()):
        return _run_model(texts)
    return [_embed_one(text) if text else None for text in texts]


def _crystal_embed_text(row) -> str:
    """Name, aliases, and the page text. The vector is how merge retrieval finds a page."""
    crystal = hydrate(row)
    bits = [str(row["name"] or ""), str(row["name"] or "")]
    for alias in crystal.aliases or []:
        label = str(alias or "").strip()
        if label and label not in {"尚无来源", "暂无来源"}:
            bits.append(label)
    summary = str(crystal.summary or "").strip()
    if summary and summary not in {"尚无来源", "暂无来源"}:
        bits.append(summary[:400])
    body = str(crystal.body_md or "").strip()
    if body and body not in {"尚无来源", "暂无来源"}:
        bits.append(body[:800])
    try:
        for stmt in db().execute(
            """SELECT text FROM statements
               WHERE crystal_id=? AND invalid_at IS NULL AND trust='written' LIMIT 6""",
            (row["id"],),
        ):
            text = " ".join(str(stmt["text"] or "").split())
            if text and text not in {"尚无来源", "暂无来源"}:
                bits.append(text[:180])
    except Exception:
        pass
    return "\n".join(bit for bit in bits if bit)[:1500]


_CRYSTAL_VEC_CACHE: dict[str, tuple[str, list[float]]] = {}


def _crystal_vector_rows() -> list[dict]:
    """Embed each active page once per text. Distill must not re-embed the library every passage."""
    if _EMBED == "off":
        return []
    rows = list(db().execute("SELECT * FROM crystallizations WHERE status='active'"))
    model = (get_setting("model_embed") or "") if _embed_is_remote() else embed_model_id()
    pending: list[tuple[object, str, str, str]] = []
    packed: list[dict] = []
    for row in rows:
        text = _crystal_embed_text(row)
        digest = hashlib.sha256(text.encode()).hexdigest()
        cache_key = f"{model}\n{row['id']}"
        cached = _CRYSTAL_VEC_CACHE.get(cache_key)
        if cached and cached[0] == digest:
            packed.append({"id": row["id"], "type": row["type"], "name": row["name"] or "", "vec": cached[1]})
            continue
        pending.append((row, cache_key, digest, text))
    if not pending:
        return packed
    vectors = _embed_many([item[3] for item in pending])
    if _EMBED == "fastembed" and all(vec is None for vec in vectors):
        raise RuntimeError("词条向量没有算出来")
    for (row, cache_key, digest, _text), vec in zip(pending, vectors):
        if not vec:
            continue
        _CRYSTAL_VEC_CACHE[cache_key] = (digest, vec)
        packed.append({"id": row["id"], "type": row["type"], "name": row["name"] or "", "vec": vec})
    return packed


def vector_merge_candidates(limit: int = MERGE_ASK_CAP) -> list[dict]:
    """Same-type neighbors close in the embedding space. The model decides, not the score."""
    packed = _crystal_vector_rows()
    grouped: dict[str, list[dict]] = {}
    for item in packed:
        grouped.setdefault(item["type"], []).append(item)
    chosen: list[tuple[float, dict, dict]] = []
    seen: set[tuple[str, str]] = set()
    for group in grouped.values():
        nearest: dict[str, list[tuple[float, dict]]] = {item["id"]: [] for item in group}
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                score = _cosine(left["vec"], right["vec"])
                if score < MERGE_NEIGHBOR_FLOOR:
                    continue
                nearest[left["id"]].append((score, right))
                nearest[right["id"]].append((score, left))
        for item in group:
            nearest[item["id"]].sort(key=lambda pair: pair[0], reverse=True)
            for score, other in nearest[item["id"]][:MERGE_NEIGHBOR_K]:
                key = tuple(sorted((item["id"], other["id"])))
                if key in seen:
                    continue
                seen.add(key)
                chosen.append((score, item, other))
    chosen.sort(key=lambda pair: pair[0], reverse=True)
    out = []
    handle = db()
    for score, left, right in chosen:
        if _identity_denied(handle, left["id"], right["id"], left["name"], right["name"]):
            continue
        extra = "\n".join(
            bit for bit in (_page_digest(handle, left["id"]), _page_digest(handle, right["id"])) if bit
        )
        out.append(
            {
                "a": left["id"],
                "b": right["id"],
                "name_a": left["name"],
                "name_b": right["name"],
                "anchors": [],
                "extra": f"向量接近 {score:.2f}\n{extra}"[:400],
            }
        )
        if len(out) >= limit:
            break
    return out


def _reconcile_ask_queue(con, recent: set[str]) -> list[dict]:
    """Pairs already marked and not yet answered. New pairs come from vector neighbors."""
    queued: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def push(left_id: str, right_id: str, name_a: str, name_b: str) -> None:
        if not left_id or not right_id or left_id == right_id:
            return
        key = tuple(sorted((left_id, right_id)))
        if key in seen:
            return
        if _identity_denied(con, left_id, right_id, name_a, name_b):
            return
        left = con.execute(
            "SELECT id FROM crystallizations WHERE id=? AND status='active'",
            (left_id,),
        ).fetchone()
        right = con.execute(
            "SELECT id FROM crystallizations WHERE id=? AND status='active'",
            (right_id,),
        ).fetchone()
        if left is None or right is None:
            return
        seen.add(key)
        queued.append(
            {
                "a": left_id,
                "b": right_id,
                "name_a": name_a,
                "name_b": name_b,
                "anchors": [],
                "extra": "\n".join(
                    bit for bit in (_page_digest(con, left_id), _page_digest(con, right_id)) if bit
                )[:400],
            }
        )

    retries = con.execute(
        """SELECT id, detail FROM decisions
           WHERE kind='可能同一实体' AND detail LIKE ?
           ORDER BY id""",
        ('%"via": "reconcile"%',),
    ).fetchall()
    for row in retries:
        try:
            detail = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            continue
        later = con.execute(
            """SELECT 1 FROM decisions
               WHERE id>? AND kind IN ('不同实体','同一实体')
                 AND detail LIKE ? AND detail LIKE ?""",
            (row["id"], f"%{detail.get('a') or ''}%", f"%{detail.get('b') or ''}%"),
        ).fetchone()
        if later:
            continue
        push(str(detail.get("a") or ""), str(detail.get("b") or ""), str(detail.get("name_a") or ""), str(detail.get("name_b") or ""))
        if len(queued) >= MERGE_ASK_CAP:
            return queued
    return queued


def _merge_named(con, left_id: str, right_id: str) -> int:
    """Merge two live pages. The owner page stays. The other name is kept as an alias."""
    rows = []
    for cid in (left_id, right_id):
        row = con.execute(
            "SELECT * FROM crystallizations WHERE id=? AND status='active'",
            (cid,),
        ).fetchone()
        if row is not None:
            rows.append(row)
    if len(rows) < 2 or rows[0]["id"] == rows[1]["id"]:
        return 0
    owner = next((row for row in rows if row["id"] == "owner"), None)
    if owner is not None:
        keep = owner
        other = next(row for row in rows if row["id"] != "owner")
    else:
        keep = _older_crystal(con, rows)
        other = next(row for row in rows if row["id"] != keep["id"])
    _merge_into(con, keep["id"], other["id"], "")
    _remember_other_name(con, keep["id"], other["name"])
    return 1


def _patrol_wikilinks(con) -> int:
    """Rewrite active bodies so wiki marks match titles and aliases still on file.

    已合并 and 已撤回 still resolve. A name with no row does not. Does not
    delete a row, does not change status, and does not open a page for a name
    that does not already resolve. A leased page is left for the next pass.
    """
    from rogmem.wikilink import align_wikilinks, sync_wikilinks, title_index

    index = title_index(con)
    rows = list(
        con.execute("SELECT id, body_md, revision FROM crystallizations WHERE status='active'")
    )
    changed = 0
    for row in rows:
        cid = row["id"]
        body = row["body_md"] or ""
        revised = align_wikilinks(body, index, cid)
        if revised == body:
            # One page, one commit. Holding edges for the whole patrol kept the
            # write lock up and made every MCP door and admin read wait.
            sync_wikilinks(con, cid, body, active=True)
            con.commit()
            continue
        before = int(row["revision"] or 0)

        def sidecar(crystal_id: str = cid) -> None:
            _record_revision(
                con,
                crystal_id,
                "链接",
                actor="系统",
                change="正文里的链接按活跃篇名和别名对齐。",
                basis="对得上的提及补上链接记号，对不上的只去掉记号。",
            )

        try:
            _apply_then_sidecar(
                CrystalStore(con),
                con,
                crystal_id=cid,
                expected_revision=before,
                ops=[{"op": "update", "path": "body_md", "value": revised}],
                holder=f"reconcile:{cid}",
                sidecar=sidecar,
                injected=False,
                commit=True,
            )
        except LeaseConflict:
            continue
        stored = con.execute("SELECT body_md FROM crystallizations WHERE id=?", (cid,)).fetchone()
        if stored and (stored["body_md"] or "") == revised:
            changed += 1
    return changed


def patrol_wikilinks() -> int:
    """Align wiki marks on active pages. Returns how many bodies changed."""
    with LOCK:
        con = db()
        changed = _patrol_wikilinks(con)
        con.commit()
    return changed


def reconcile_new_crystals() -> dict:
    """Fold identical names, ask about vector neighbors, then align wiki links."""
    since = _reconcile_since()
    with LOCK:
        con = db()
        rows = list(con.execute("SELECT * FROM crystallizations WHERE status='active' AND id!='owner'"))
        recent = {row["id"] for row in rows if (_crystal_born_at(con, row["id"]) or "") >= since}
        folded = _fold_label_overlaps(con, recent)
        pending = _reconcile_ask_queue(con, recent)
        for item in pending:
            _record_possible_once(con, item)
        con.commit()
    seen = {tuple(sorted((item["a"], item["b"]))) for item in pending}
    for item in vector_merge_candidates(MERGE_ASK_CAP):
        key = tuple(sorted((item["a"], item["b"])))
        if key in seen:
            continue
        seen.add(key)
        if len(pending) >= MERGE_ASK_CAP:
            break
        pending.append(item)
    asked = 0
    model_merged = 0
    judge = _judge_model("merge")
    can_ask = bool(judge and str(judge[1] or "").strip())
    for item in pending:
        answer = ""
        if can_ask:
            try:
                answer = _ask_same_entity(
                    item["name_a"],
                    item["name_b"],
                    item.get("anchors") or [],
                    item.get("extra") or "",
                )
            except Exception:
                answer = ""
        with LOCK:
            con = db()
            try:
                denied = _identity_denied(con, item["a"], item["b"], item["name_a"], item["name_b"])
                if answer == "是" and not denied:
                    model_merged += _merge_named(con, item["a"], item["b"])
                    _record_identity(con, "同一实体", item)
                    asked += 1
                elif answer == "否" or (answer == "是" and denied):
                    _record_identity(con, "不同实体", item)
                    asked += 1
                con.commit()
            except Exception:
                con.rollback()
    with LOCK:
        con = db()
        linked = _patrol_wikilinks(con)
        if folded or asked or model_merged or linked:
            note = (
                f"收起同名 {folded} 篇，向量送评 {asked} 对，模型合并 {model_merged} 篇，"
                f"对齐链接 {linked} 篇"
            )
            con.execute(
                "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
                (utcnow(), "词条整合", note),
            )
        _mark_reconciled()
        con.commit()
    return {"folded": folded, "asked": asked, "merged": model_merged, "links": linked}


def _fold_same_names() -> int:
    """Merge pages that already use the same written name. Does not ask the model."""
    with LOCK:
        con = db()
        folded = _fold_label_overlaps(con, set())
        if folded:
            con.execute(
                "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
                (
                    utcnow(),
                    "词条整合",
                    f"收起同名 {folded} 篇，向量送评 0 对，模型合并 0 篇，对齐链接 0 篇",
                ),
            )
        _mark_reconciled()
        con.commit()
    return folded


def run_scheduled_reconcile() -> dict | None:
    """Every few hours, and once after each pack, fold same-named pages and align wiki links.

    Identical written names do not wait on the embedder. Vector neighbors do.
    The build stamp stays empty until that vector pass actually runs.
    """
    from rogmem.version import product_version

    fresh = get_setting("reconcile_build") != product_version()
    due = reconcile_due()
    if not fresh and not due:
        return None
    if _EMBED != "fastembed" or not _embedder_ready():
        if not due:
            return None
        folded = _fold_same_names()
        return {"folded": folded, "asked": 0, "merged": 0, "links": 0}
    try:
        result = reconcile_new_crystals()
    except Exception as exc:
        if "词条向量没有算出来" in str(exc):
            return None
        with LOCK:
            con = db()
            con.execute(
                "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
                (utcnow(), "词条整合", f"这次没有跑完：{type(exc).__name__}"[:200]),
            )
            _mark_reconciled()
            set_setting("reconcile_build", product_version())
            con.commit()
        return None
    with LOCK:
        set_setting("reconcile_build", product_version())
        db().commit()
    return result


def sweep(scheduled: bool = False) -> dict:
    """Requeue failures and repair stale overviews. A scheduled run does not summarize the day."""
    with LOCK:
        con = db()
        cur = con.execute("UPDATE queue SET state='queued', next_at=NULL, attempts=0 WHERE state='failed'")
        rejudged = 0
        possible = _mark_possible_same(con)
        identified = _rejudge_possible_same(con)
        merged = _explicit_same_merge(con)
        struck = _strike_features(con)
        started = _move_started_features(con)
        refreshed = 0
        for row in con.execute("SELECT id, type, name FROM crystallizations"):
            if _persist_overview(
                con,
                row["id"],
                row["type"],
                row["name"],
                holder="sweep:overview",
            ):
                refreshed += 1
        note = f"重开失败项 {cur.rowcount} 条，重写概述 {refreshed} 篇，再裁决 {rejudged} 条，可能同一实体 {possible} 对，辨认 {identified} 对，合并 {merged} 篇，划掉特性 {struck} 篇，开工 {started} 篇"
        if scheduled:
            note = "到点触发。" + note
        con.execute(
            "INSERT INTO decisions(created_at,kind,detail) VALUES(?,?,?)",
            (utcnow(), "清扫", note),
        )
        con.commit()
        return {
            "scheduled": scheduled,
            "requeued": cur.rowcount,
            "overviews": refreshed,
            "rejudged": rejudged,
            "possible": possible,
            "identified": identified,
            "merged": merged,
            "struck": struck,
            "started": started,
        }


def run_scheduled_sweep() -> dict | None:
    """Run the daily sweep once at the configured UTC minute. Do not summarize that day."""
    if not sweep_due():
        return None
    result = sweep(scheduled=True)
    mark_swept()
    return result


def sweep_due() -> bool:
    stamp = get_setting("sweep_at") or "03:00"
    try:
        hour, minute = (int(part) for part in stamp.split(":", 1))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    if now.hour != hour or now.minute != minute:
        return False
    return get_setting("sweep_done") != now.date().isoformat()


def mark_swept() -> None:
    with LOCK:
        set_setting("sweep_done", datetime.now(timezone.utc).date().isoformat())
        db().commit()


def queue_next_lane() -> str | None:
    now = utcnow()
    candidates, _units = _schedule(db(), now)
    picked, _streak = choose_next(candidates, now=datetime.fromisoformat(now), high_streak=read_high_streak(db()))
    return picked.lane if picked else None


def _stored_owner_appellation() -> str:
    """The user's own record of how they are addressed. Not parsed into fields."""
    try:
        return (get_setting("owner_appellation") or "").strip()
    except Exception:
        return ""


def get_setting(key: str, default: str = "") -> str:
    row = db().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(key: str, value: str) -> None:
    db().execute(
        "INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


TURN_WAIT_DEFAULT = 600


def turn_wait_seconds() -> int:
    raw = get_setting("turn_wait") or os.environ.get("ROGMEM_TURN_WAIT", str(TURN_WAIT_DEFAULT))
    try:
        return max(0, int(raw))
    except ValueError:
        return TURN_WAIT_DEFAULT


def accepted_suffixes() -> set[str]:
    raw = get_setting("file_types") or ",".join(FILE_TYPES)
    return {part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip()}


def hash_dedup_enabled() -> bool:
    return True


def core_budget() -> int:
    """Characters packed into a new core call. Unset means the long-standing default."""
    raw = (get_setting("core_budget") or "").strip()
    try:
        value = int(raw) if raw else CORE_BUDGET_DEFAULT
    except ValueError:
        return CORE_BUDGET_DEFAULT
    if value < CORE_BUDGET_MIN:
        return CORE_BUDGET_MIN
    if value > CORE_BUDGET_MAX:
        return CORE_BUDGET_MAX
    return value


def core_budget_for(requested: int = 0) -> int:
    """A caller that names a size wins. Zero means the configured budget."""
    try:
        asked = int(requested or 0)
    except (TypeError, ValueError):
        asked = 0
    if asked > 0:
        return asked
    return core_budget()


def memory_store() -> dict:
    """Where the memory file is, and how many bytes it occupies. Not a setting."""
    path = _data_dir() / "rogmem.sqlite"
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return {"path": str(path), "bytes": total}


def active_scan() -> Path | None:
    custom = get_setting("scan_path")
    if custom:
        path = Path(custom)
        if path.is_dir():
            return path
    return _SCAN


def read_document(path: Path) -> str:
    """The same read as import. One document, one way of turning it into text."""
    from rogmem.sections import join_sections, read_sections

    return join_sections(read_sections(path))


def _provider_fernet():
    from cryptography.fernet import Fernet

    path = _data_dir() / "provider.key"
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o600)
    return Fernet(path.read_bytes().strip())


def _seal_secret(secret: str) -> str:
    if not secret or secret.startswith("enc:v1:"):
        return secret
    token = _provider_fernet().encrypt(secret.encode()).decode()
    return "enc:v1:" + token


def _open_secret(secret: str) -> str:
    if not secret.startswith("enc:v1:"):
        return secret
    return _provider_fernet().decrypt(secret[len("enc:v1:") :].encode()).decode()


def _store_providers(items: list[dict]) -> None:
    sealed = []
    for item in items:
        copy = dict(item)
        copy["api_key"] = _seal_secret(str(copy.get("api_key") or ""))
        sealed.append(copy)
    set_setting("providers", json.dumps(sealed, ensure_ascii=False))


def _providers() -> list[dict]:
    raw = get_setting("providers")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    opened = []
    for item in data:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        copy["api_key"] = _open_secret(str(copy.get("api_key") or ""))
        opened.append(copy)
    return opened


def llm_target(role: str) -> tuple[str, str, str] | None:
    key = {"distill": "model_distill", "merge": "model_merge"}.get(role)
    assigned = get_setting(key or "")
    found: tuple[str, str, str] | None = None
    if assigned and "|" in assigned:
        ident, model = assigned.split("|", 1)
        for provider in _providers():
            if provider.get("id") == ident or provider.get("name") == ident:
                base = str(provider.get("base_url") or "").rstrip("/")
                if base and model:
                    found = (base, str(provider.get("api_key") or ""), model)
                break
    if found and str(found[1]).strip():
        return found
    if role == "distill":
        env_base = os.environ.get("ROGMEM_LLM_BASE", "").rstrip("/")
        env_key = os.environ.get("ROGMEM_LLM_KEY", "")
        env_model = os.environ.get("ROGMEM_LLM_MODEL", "stub")
        if env_base and str(env_key).strip() and env_model:
            return env_base, env_key, env_model
    if found:
        return found
    if role != "distill":
        return None
    base = os.environ.get("ROGMEM_LLM_BASE", "").rstrip("/")
    if not base:
        return None
    return base, os.environ.get("ROGMEM_LLM_KEY", "test"), os.environ.get("ROGMEM_LLM_MODEL", "stub")


def llm_public() -> dict:
    assigned = get_setting("model_distill")
    target = llm_target("distill")
    host = ""
    model = ""
    if target:
        base, _key, model = target
        host = base.split("://", 1)[-1].split("/", 1)[0]
    return {"source": "settings" if assigned else ("env" if target else "none"), "host": host, "model": model}


_PROBE_BODY_LIMIT = 4096
_OK_VENDOR_CODES = {"0", "200", "ok", "success", "succeed", "succeeded"}
_DOWN_HTTP = {401, 403, 407}


def _ok_vendor_code(value) -> bool:
    return str(value).strip().lower() in _OK_VENDOR_CODES


def _vendor_code_from_mapping(obj: dict, *, allow_code: bool = True) -> str:
    keys = ("error_code", "status_code", "statusCode", "code") if allow_code else ("error_code", "status_code", "statusCode")
    for key in keys:
        if key not in obj:
            continue
        value = obj[key]
        if isinstance(value, bool) or value is None or not str(value).strip() or _ok_vendor_code(value):
            continue
        return str(value).strip()[:80]
    return ""


def _vendor_code(payload) -> str:
    """An error code the provider named in the body. Empty when the body is a normal success."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()[:80]
    if isinstance(error, dict):
        found = _vendor_code_from_mapping(error)
        if found:
            return found
        message = str(error.get("message") or error.get("msg") or "").strip()
        if message:
            return message[:80]
    found = _vendor_code_from_mapping(payload)
    if found:
        return found
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        found = _vendor_code_from_mapping(value, allow_code=False)
        if found:
            return found
    return ""


def _vendor_code_in_body(raw: bytes) -> str:
    text = (raw or b"")[:_PROBE_BODY_LIMIT].decode("utf-8", "replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    return _vendor_code(payload)


def _probe_remark(status: int, code: str) -> str:
    head = f"HTTP {status}"
    extra = (code or "").strip()
    if not extra or extra == str(status):
        return head
    return f"{head} · {extra}"


def _judge_probe(status: int, body: bytes) -> dict:
    """Red when this call cannot be served. Yellow when the server answered with a softer fault.

    401, 403, 407 and any 5xx are down: credentials were rejected or the provider is failing.
    Any other HTTP error, and an error code carried inside a 2xx body, is warn.
    """
    code = _vendor_code_in_body(body)
    if 200 <= status < 300 and not code:
        return {"health": "ok", "health_detail": "正常", "remark": ""}
    remark = _probe_remark(status, code)
    health = "down" if status in _DOWN_HTTP or status >= 500 else "warn"
    return {"health": health, "health_detail": remark, "remark": remark}


def _unreachable(exc: BaseException) -> dict:
    reason = exc
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        reason = exc.reason
    text = " ".join(str(reason).split())[:80]
    remark = "连不上" if not text else f"连不上：{text}"
    return {"health": "down", "health_detail": remark, "remark": remark}


def _probe_request(url: str, api_key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return _judge_probe(int(resp.status), resp.read(_PROBE_BODY_LIMIT))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_PROBE_BODY_LIMIT)
        except Exception:
            body = b""
        return _judge_probe(int(exc.code), body)
    except Exception as exc:
        return _unreachable(exc)


def probe_chat(base_url: str, api_key: str, model: str) -> dict:
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    return _probe_request(base_url.rstrip("/") + "/chat/completions", api_key, payload)


def _remember_probe(item: dict, result: dict) -> None:
    health = str(result.get("health") or "")
    detail = str(result.get("health_detail") or "")
    remark = result.get("remark")
    if remark is None:
        remark = "" if health == "ok" else detail
    item["health"] = health
    item["health_detail"] = detail
    item["remark"] = str(remark or "")
    item["health_at"] = utcnow()
    item["health_unix"] = time.time()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _probe_mark(provider_id: str) -> str:
    return f"探活异常（{provider_id}）"


def _probe_message(provider_id: str, name: str, detail: str) -> str:
    return f"供应商「{name}」{_probe_mark(provider_id)}：{detail}"


def _probe_id_like(provider_id: str) -> str:
    return "%" + _escape_like(_probe_mark(provider_id)) + "%"


def _close_probe_alerts_locked(provider_id: str) -> list[str]:
    pattern = _probe_id_like(provider_id)
    rows = db().execute(
        "SELECT message FROM alerts WHERE open=1 AND message LIKE ? ESCAPE '\\'",
        (pattern,),
    ).fetchall()
    if not rows:
        return []
    db().execute(
        "UPDATE alerts SET open=0 WHERE open=1 AND message LIKE ? ESCAPE '\\'",
        (pattern,),
    )
    return [row["message"] for row in rows]


def _sync_probe_alert(provider_id: str, name: str, result: dict) -> None:
    """One open alert per provider. A new error code replaces it. Recovery closes it."""
    health = str(result.get("health") or "")
    if health not in {"ok", "warn", "down"}:
        return
    detail = str(result.get("remark") or result.get("health_detail") or "").strip() or "异常"
    message = _probe_message(provider_id, name or provider_id, detail)
    notify: list[str] = []
    with LOCK:
        open_messages = _close_probe_alerts_locked(provider_id) if health == "ok" else []
        if health == "ok":
            if open_messages:
                db().commit()
                notify = ["恢复：" + item for item in open_messages]
        else:
            pattern = _probe_id_like(provider_id)
            current = db().execute(
                "SELECT message FROM alerts WHERE open=1 AND message LIKE ? ESCAPE '\\'",
                (pattern,),
            ).fetchall()
            if any(row["message"] == message for row in current):
                return
            if current:
                db().execute(
                    "UPDATE alerts SET open=0 WHERE open=1 AND message LIKE ? ESCAPE '\\'",
                    (pattern,),
                )
            db().execute("INSERT INTO alerts(created_at,message) VALUES(?,?)", (utcnow(), message))
            db().commit()
            notify = [message]
    for item in notify:
        notify_alert(item)


def _note_probe(provider_id: str, item: dict) -> None:
    from rogmem.availability import note

    note(db(), provider_id, str(item.get("health") or ""), str(item.get("health_at") or ""))


def _stamp_health(provider_id: str, result: dict) -> None:
    label = ""
    found = False
    with LOCK:
        items = _providers()
        touched = None
        for item in items:
            if item.get("id") == provider_id:
                _remember_probe(item, result)
                label = str(item.get("name") or provider_id)
                found = True
                touched = item
        _store_providers(items)
        if touched is not None:
            _note_probe(provider_id, touched)
        db().commit()
    if found:
        _sync_probe_alert(provider_id, label, result)


def probe_interval_seconds() -> float:
    raw = get_setting("probe_interval")
    if raw.strip().isdigit():
        return float(raw)
    try:
        return float(os.environ.get("ROGMEM_PROVIDER_PROBE", "300"))
    except ValueError:
        return 300.0


def probe_providers_if_due() -> int:
    interval = probe_interval_seconds()
    now = time.time()
    due = []
    for item in _providers():
        last = float(item.get("health_unix") or 0)
        if now - last >= interval:
            due.append(dict(item))
    checked = 0
    for item in due:
        if not str(item.get("api_key") or "").strip():
            continue
        result = _probe_provider(item)
        _stamp_health(str(item.get("id")), result)
        if result.get("health") == "ok":
            recover_auth_alerts()
        checked += 1
    return checked


def mask_api_key(secret: str) -> str:
    """Show the first and last two characters. The middle is only stars, not the secret."""
    text = str(secret or "")
    if len(text) < 4:
        return ""
    return text[:2] + "****" + text[-2:]


def _backfill_key_masks() -> None:
    """Write a display mask once. Later pages read that record and do not open the secret."""
    items = _providers()
    changed = False
    for item in items:
        key = str(item.get("api_key") or "")
        recorded = str(item.get("key_mask") or "")
        if key and not recorded:
            item["key_mask"] = mask_api_key(key)
            changed = True
        elif not key and recorded:
            item["key_mask"] = ""
            changed = True
    if not changed:
        return
    with LOCK:
        _store_providers(items)
        db().commit()


def public_providers() -> list[dict]:
    _backfill_key_masks()
    raw = get_setting("providers")
    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        data = []
    if not isinstance(data, list):
        data = []
    out = []
    for provider in data:
        if not isinstance(provider, dict):
            continue
        hint = "" if provider.get("id") == LOCAL_EMBED_ID else str(provider.get("key_mask") or "")
        name = LOCAL_EMBED_NAME if provider.get("id") == LOCAL_EMBED_ID else provider.get("name")
        out.append(
            {
                "id": provider.get("id"),
                "name": name,
                "base_url": provider.get("base_url"),
                "models": provider.get("models") or [],
                "model_kinds": {
                    model_name: model_kind(provider, model_name)
                    for model_name in (provider.get("models") or [])
                },
                "builtin": provider.get("id") == LOCAL_EMBED_ID,
                "key_hint": hint,
                "health": provider.get("health") or "",
                "health_detail": provider.get("health_detail") or "未检测",
                "remark": str(provider.get("remark") or ""),
                "health_at": provider.get("health_at") or "",
            }
        )
    out.sort(key=lambda item: 0 if item.get("id") == LOCAL_EMBED_ID else 1)
    return out


def normalize_model_kind(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if text in {"embed", "向量嵌入", "向量"}:
        return MODEL_KIND_EMBED
    return MODEL_KIND_CHAT


def probe_embed(base_url: str, api_key: str, model: str) -> dict:
    payload = {"model": model, "input": "hi"}
    return _probe_request(base_url.rstrip("/") + "/embeddings", api_key, payload)


def _probe_provider(item: dict) -> dict:
    models = [str(name) for name in (item.get("models") or []) if str(name).strip()]
    chat = [name for name in models if model_kind(item, name) != MODEL_KIND_EMBED]
    embed = [name for name in models if model_kind(item, name) == MODEL_KIND_EMBED]
    base = str(item.get("base_url") or "")
    key = str(item.get("api_key") or "")
    if chat:
        return probe_chat(base, key, chat[0])
    if embed:
        return probe_embed(base, key, embed[0])
    return {"health": "down", "health_detail": "没有模型", "remark": "没有模型"}


def save_provider(name: str, base_url: str, api_key: str, models: str, kind: str = MODEL_KIND_CHAT) -> dict:
    base = base_url.strip().rstrip("/")
    if not name.strip() or not base.startswith("http"):
        return {"ok": False, "error": "名称和 http 接口都要填"}
    names = [part.strip() for part in models.replace("，", ",").split(",") if part.strip()]
    if not names:
        return {"ok": False, "error": "至少填写一个模型名"}
    recorded = normalize_model_kind(kind)
    draft = {"models": names, "model_kinds": {item: recorded for item in names}, "base_url": base, "api_key": api_key}
    result = _probe_provider(draft)
    if result.get("health") == "ok":
        recover_auth_alerts()
    items = _providers()
    provider_id = secrets.token_hex(4)
    row = {
        "id": provider_id,
        "name": name.strip(),
        "base_url": base,
        "api_key": api_key,
        "key_mask": mask_api_key(api_key),
        "models": names,
        "model_kinds": {item: recorded for item in names},
    }
    _remember_probe(row, result)
    items.append(row)
    with LOCK:
        _store_providers(items)
        _note_probe(provider_id, row)
        db().commit()
    _sync_probe_alert(provider_id, name.strip(), result)
    return {"ok": True, "id": provider_id, "health": result["health"], "health_detail": result["health_detail"]}


def set_provider_key(provider_id: str, api_key: str) -> dict:
    """Store a token on an existing provider. A failed probe does not drop the row."""
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "error": "请填写密钥"}
    items = _providers()
    found = next((item for item in items if item.get("id") == provider_id), None)
    if found is None:
        return {"ok": False, "error": "没有这个供应商"}
    if provider_id == LOCAL_EMBED_ID:
        return {"ok": False, "error": "中枢服务器本地不需要密钥"}
    current = str(found.get("api_key") or "")
    recorded = str(found.get("key_mask") or "")
    shown = recorded or mask_api_key(current)
    if current and shown and key == shown:
        if not recorded:
            found["key_mask"] = shown
            with LOCK:
                _store_providers(items)
                db().commit()
        return {
            "ok": True,
            "id": provider_id,
            "health": found.get("health") or "",
            "health_detail": found.get("health_detail") or "",
        }
    had_key = bool(current.strip())
    found["api_key"] = key
    found["key_mask"] = mask_api_key(key)
    result = _probe_provider(found)
    found["api_key"] = key
    _remember_probe(found, result)
    with LOCK:
        _store_providers(items)
        _note_probe(provider_id, found)
        db().commit()
    _sync_probe_alert(provider_id, str(found.get("name") or provider_id), result)
    if result.get("health") == "ok":
        recover_auth_alerts()
        _release_pause_messages(MISSING_PROVIDER_KEY, MISSING_DISTILL_MODEL, "外部模型鉴权失败")
        _activate_embed_provider(found, key_was_empty=not had_key)
    return {"ok": True, "id": provider_id, "health": result["health"], "health_detail": result["health_detail"]}


def _activate_embed_provider(provider: dict, *, key_was_empty: bool = False) -> None:
    """A working embedding key uses that provider. The first key replaces vectors from another model."""
    names = [name for name in (provider.get("models") or []) if model_kind(provider, name) == MODEL_KIND_EMBED]
    if not names:
        return
    previous = get_setting("model_embed") or ""
    ident = str(provider.get("id") or "")
    current_model = previous.split("|", 1)[1] if previous.startswith(ident + "|") else ""
    chosen = current_model if current_model in names else names[0]
    token = f"{ident}|{chosen}"
    set_setting("model_embed", token)
    _apply_embed_choice(token)
    _release_local_weights()
    if previous == token and not key_was_empty:
        return
    db().execute("UPDATE chunks SET vec_json=NULL WHERE role!='parent'")
    db().commit()


def delete_provider(provider_id: str) -> dict:
    if provider_id in {LOCAL_EMBED_ID, DEFAULT_MINIMAX_ID, SILICONFLOW_ID}:
        return {"ok": False, "error": "这项是常驻模型，不能删除"}
    items = [item for item in _providers() if item.get("id") != provider_id]
    with LOCK:
        _store_providers(items)
        _close_probe_alerts_locked(provider_id)
        db().commit()
    return {"ok": True}


def _model_token_kind(token: str) -> str:
    if "|" not in token:
        return ""
    ident, model = token.split("|", 1)
    for provider in _providers():
        if provider.get("id") == ident and model in (provider.get("models") or []):
            return model_kind(provider, model)
    return ""


def assign_models(values: dict) -> dict:
    keys = [key for key in _ASSIGN_KEYS if key in (values or {})]
    incoming = {key: str(values.get(key) or "") for key in keys}
    # Empty POST / mistyped Content-Type yields {}. Must not wipe a working assignment
    # and reopen「没有配置蒸馏模型」to stall the distill queue.
    if not keys and any(str(get_setting(key) or "") for key in _ASSIGN_KEYS):
        return {"ok": False, "error": "不能用空分配清掉已选模型"}
    for key, value in incoming.items():
        if not value:
            continue
        kind = _model_token_kind(value)
        if not kind:
            return {"ok": False, "error": "分配的模型不在已保存的供应商里"}
        if key == "model_embed" and kind != MODEL_KIND_EMBED:
            return {"ok": False, "error": "向量嵌入工人只能选向量嵌入模型"}
        if key in _CHAT_ASSIGN_KEYS and kind != MODEL_KIND_CHAT:
            return {"ok": False, "error": "对话工人只能选文本对话模型"}
    with LOCK:
        for key, value in incoming.items():
            set_setting(key, value)
        for role in ("distill", "merge"):
            flag = f"thinking_{role}"
            if flag not in values:
                continue
            set_setting(flag, normalize_reasoning_level(values.get(flag)))
        db().commit()
    if "model_embed" in incoming and incoming["model_embed"]:
        _apply_embed_choice(incoming["model_embed"])
    release_model_pause()
    return {"ok": True, "effective": llm_public()}


def _apply_embed_choice(token: str) -> None:
    """A local preset switches the on-box model. Any other embedding model is called remotely."""
    _ident, model = token.split("|", 1)
    presets = {item["id"] for item in EMBED_PRESETS}
    if model in presets:
        if get_setting("embed_model") != model:
            os.environ["ROGMEM_EMBED_MODEL"] = model
            try:
                set_embed_mode("fastembed")
            except Exception:
                set_setting("embed_model", model)
        return
    set_setting("embed_model", token)


def collect_config() -> dict:
    return {
        "scan_path": get_setting("scan_path") or str(_SCAN or ""),
        "active_scan": str(active_scan() or ""),
        "scan_interval": get_setting("scan_interval") or "60",
        "file_types": get_setting("file_types") or ",".join(FILE_TYPES),
        "turn_wait": str(turn_wait_seconds()),
        "sweep_at": get_setting("sweep_at") or "03:00",
    }


def save_collect(values: dict) -> dict:
    interval = str(values.get("scan_interval") or "").strip()
    wait = str(values.get("turn_wait") or TURN_WAIT_DEFAULT)
    if "scan_interval" in values and (not interval.isdigit()):
        return {"ok": False, "error": "间隔和等待时间要是整数"}
    if not wait.isdigit():
        return {"ok": False, "error": "间隔和等待时间要是整数"}
    types = ",".join(part.strip().lower().lstrip(".") for part in str(values.get("file_types") or "").replace("，", ",").split(",") if part.strip())
    if not types:
        return {"ok": False, "error": "至少接受一种文件类型"}
    sweep_at = str(values.get("sweep_at") or "").strip()
    if "sweep_at" in values and (len(sweep_at) != 5 or sweep_at[2] != ":" or not sweep_at.replace(":", "").isdigit()):
        return {"ok": False, "error": "清扫时刻写成 03:00 这样"}
    with LOCK:
        set_setting("scan_path", str(values.get("scan_path") or "").strip())
        if "scan_interval" in values:
            set_setting("scan_interval", interval)
        set_setting("file_types", types)
        set_setting("turn_wait", wait)
        if "sweep_at" in values:
            set_setting("sweep_at", sweep_at)
        db().commit()
    return {"ok": True, "config": collect_config()}


def assignments() -> dict:
    return {key: get_setting(key) for key in _ASSIGN_KEYS}


def basic_config() -> dict:
    store = memory_store()
    return {
        "sketch_char_limit": str(sketch_char_limit()),
        "graph_spin_seconds": str(graph_spin_seconds()),
        "core_budget": str(core_budget()),
        "owner_appellation": _stored_owner_appellation(),
        "nas_path": nas_configured_path(),
        "graph_auto_labels": "1" if graph_auto_labels() else "0",
        "graph_label_budget": str(graph_label_budget()),
        "passwordless": "1" if passwordless_enabled() else "0",
        "store_path": store["path"],
        "store_bytes": store["bytes"],
    }


def save_basic(values: dict) -> dict:
    incoming = values or {}
    sketch = None
    if "sketch_char_limit" in incoming:
        raw = str(incoming.get("sketch_char_limit") or "").strip()
        if not raw.isdigit() or not 200 <= int(raw) <= 20000:
            return {"ok": False, "error": "速写字数上限要是 200 到 20000 的整数"}
        sketch = str(int(raw))
    budget = None
    if "core_budget" in incoming:
        budget_raw = str(incoming.get("core_budget") or "").strip()
        if not budget_raw.isdigit() or not CORE_BUDGET_MIN <= int(budget_raw) <= CORE_BUDGET_MAX:
            return {"ok": False, "error": "新会话核心记忆预算要是 1000 到 200000 的整数"}
        budget = str(int(budget_raw))
    spin = None
    if "graph_spin_seconds" in incoming:
        spin_raw = str(incoming.get("graph_spin_seconds") or "").strip()
        if not spin_raw.isdigit() or not (int(spin_raw) == 0 or 1 <= int(spin_raw) <= 3600):
            return {"ok": False, "error": "图谱转一圈的秒数要是 0，或 1 到 3600"}
        spin = str(int(spin_raw))
    appellation = None
    if "owner_appellation" in incoming:
        appellation = str(incoming.get("owner_appellation") or "").strip()
    auto_labels: str | None = None
    if "graph_auto_labels" in incoming:
        raw_auto = str(incoming.get("graph_auto_labels") or "").strip().lower()
        if raw_auto not in {"", "0", "1", "on", "off", "true", "false", "yes", "no"}:
            return {"ok": False, "error": "图谱自动名称开关要是 0 或 1"}
        auto_labels = "1" if raw_auto in {"1", "on", "true", "yes"} else "0"
    label_budget: str | None = None
    if "graph_label_budget" in incoming:
        raw_budget = str(incoming.get("graph_label_budget") or "").strip()
        if not raw_budget or len(raw_budget) > 4 or not raw_budget.isdigit():
            return {"ok": False, "error": f"图谱同屏名称数要是 {GRAPH_LABEL_BUDGET_MIN} 到 {GRAPH_LABEL_BUDGET_MAX} 的整数"}
        try:
            budget_n = int(raw_budget)
        except ValueError:
            return {"ok": False, "error": f"图谱同屏名称数要是 {GRAPH_LABEL_BUDGET_MIN} 到 {GRAPH_LABEL_BUDGET_MAX} 的整数"}
        if budget_n < GRAPH_LABEL_BUDGET_MIN or budget_n > GRAPH_LABEL_BUDGET_MAX:
            return {"ok": False, "error": f"图谱同屏名称数要是 {GRAPH_LABEL_BUDGET_MIN} 到 {GRAPH_LABEL_BUDGET_MAX} 的整数"}
        label_budget = str(budget_n)
    nas_path: str | None = None
    if "nas_path" in incoming:
        raw_nas = str(incoming.get("nas_path") or "").strip()
        if len(raw_nas) > 1:
            raw_nas = raw_nas.rstrip("/")
        if raw_nas and (
            len(raw_nas) > 4096
            or any(char in raw_nas for char in "\x00\n\r")
            or not Path(raw_nas).is_absolute()
        ):
            return {"ok": False, "error": "挂载路径要是绝对路径"}
        nas_path = raw_nas
    passwordless: str | None = None
    if "passwordless" in incoming:
        raw_open = str(incoming.get("passwordless") or "").strip().lower()
        if raw_open not in {"", "0", "1", "on", "off", "true", "false", "yes", "no"}:
            return {"ok": False, "error": "免密登录要是 0 或 1"}
        passwordless = "1" if raw_open in {"1", "on", "true", "yes"} else "0"
    if all(item is None for item in (sketch, budget, spin, appellation, auto_labels, label_budget, nas_path, passwordless)):
        return {"ok": True, "config": basic_config()}
    sessions_reset = passwordless == "0" and passwordless_enabled()
    with LOCK:
        if sketch is not None:
            set_setting("sketch_char_limit", sketch)
        if spin is not None:
            set_setting("graph_spin_seconds", spin)
        if budget is not None:
            set_setting("core_budget", budget)
        if appellation is not None:
            set_setting("owner_appellation", appellation)
        if auto_labels is not None:
            set_setting("graph_auto_labels", auto_labels)
        if label_budget is not None:
            set_setting("graph_label_budget", label_budget)
        if nas_path is not None:
            set_setting("nas_path", nas_path)
        if passwordless is not None:
            if sessions_reset:
                generation = int(get_setting("admin_password_generation") or "0") + 1
                set_setting("admin_password_generation", str(generation))
            set_setting("passwordless", passwordless)
        db().commit()
    saved = {"ok": True, "config": basic_config()}
    if sessions_reset:
        saved["sessions_reset"] = True
    return saved


def pipeline_config() -> dict:
    return {
        "scan_interval": get_setting("scan_interval") or "60",
        "probe_interval": get_setting("probe_interval") or "300",
        "reconcile_hours": get_setting("reconcile_hours") or "3",
        "sweep_at": get_setting("sweep_at") or "03:00",
        "thinking_distill": reasoning_level("distill"),
        "thinking_merge": reasoning_level("merge"),
    }


def save_pipeline(values: dict) -> dict:
    scan = str(values.get("scan_interval") or "").strip()
    probe = str(values.get("probe_interval") or "").strip()
    hours = str(values.get("reconcile_hours") or "").strip()
    sweep_at = str(values.get("sweep_at") or "").strip()
    if not scan.isdigit() or not 1 <= int(scan) <= 86400:
        return {"ok": False, "error": "扫描间隔要是 1 到 86400 秒"}
    if not probe.isdigit() or not 30 <= int(probe) <= 86400:
        return {"ok": False, "error": "探活间隔要是 30 到 86400 秒"}
    if not hours.isdigit() or not 1 <= int(hours) <= 168:
        return {"ok": False, "error": "词条整合间隔要是 1 到 168 小时"}
    if len(sweep_at) != 5 or sweep_at[2] != ":" or not sweep_at.replace(":", "").isdigit():
        return {"ok": False, "error": "清扫时刻写成 03:00 这样"}
    hour, minute = int(sweep_at[:2]), int(sweep_at[3:])
    if hour > 23 or minute > 59:
        return {"ok": False, "error": "清扫时刻写成 03:00 这样"}
    with LOCK:
        set_setting("scan_interval", scan)
        set_setting("probe_interval", probe)
        set_setting("reconcile_hours", hours)
        set_setting("sweep_at", sweep_at)
        db().commit()
    return {"ok": True, "config": pipeline_config()}


RUN_KINDS = ("告警", "蒸馏", "修订", "裁决", "词条整合", "清扫")
_LOG_LIMIT = 200


def _row_field(row, name: str) -> str:
    if row is None:
        return ""
    try:
        names = row.keys()
    except AttributeError:
        if isinstance(row, dict):
            value = row.get(name)
            return "" if value is None else str(value).strip()
        return ""
    if name not in names:
        return ""
    value = row[name]
    return "" if value is None else str(value).strip()


def _source_subject(source) -> str:
    filename = _row_field(source, "filename")
    if filename:
        return filename
    session = _row_field(source, "session_id")
    if session:
        return session
    source_id = _row_field(source, "id")
    return source_id or "材料"


def _log_href(ref_kind: str, ref_id: str) -> str:
    if not ref_id:
        return ""
    if ref_kind == "source":
        return f"/admin/sources/{ref_id}"
    if ref_kind == "crystal":
        return f"/admin/crystals/{ref_id}"
    return ""


def record_run(
    con,
    kind: str,
    outcome: str,
    subject: str,
    summary: str,
    *,
    step: str = "",
    detail: str = "",
    ref_kind: str = "",
    ref_id: str = "",
) -> None:
    """One operational line. kind is the job. step is set only when that job has steps."""
    con.execute(
        """INSERT INTO run_log(created_at,kind,step,outcome,subject,summary,detail,ref_kind,ref_id)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            utcnow(),
            (kind or "蒸馏")[:20],
            (step or "")[:20],
            (outcome or "")[:40],
            (subject or "系统")[:160],
            (summary or "")[:500],
            (detail or "")[:800],
            ref_kind or "",
            ref_id or "",
        ),
    )


def _settle_run_log(con) -> None:
    """A kind that is not a job was a distill step stored in the wrong column."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(run_log)")}
    if "step" not in columns:
        return
    marks = ",".join("?" * len(RUN_KINDS))
    con.execute(
        f"UPDATE run_log SET step=kind, kind='蒸馏' WHERE IFNULL(step,'')='' AND kind NOT IN ({marks})",
        RUN_KINDS,
    )


def _plain_detail(detail: str) -> tuple[str, str, str]:
    """Turn a stored decision into subject, summary, and an optional second line."""
    text = (detail or "").strip()
    if not text:
        return "记忆", "", ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "记忆", text, ""
    if not isinstance(data, dict):
        return "记忆", text, ""
    field = str(data.get("field") or "").strip()
    name_a = str(data.get("name_a") or "").strip()
    name_b = str(data.get("name_b") or "").strip()
    left = str(data.get("a") or "").strip()
    right = str(data.get("b") or "").strip()
    names = "、".join(item for item in (name_a, name_b) if item)
    claims = " → ".join(item for item in (left, right) if item)
    extra = ""
    anchors = data.get("anchors")
    if isinstance(anchors, list) and anchors:
        shown = [str(item).strip()[:40] for item in anchors[:4] if str(item).strip()]
        if shown:
            extra = "依据：" + "；".join(shown)
    if field and names:
        return field, names, extra
    if field and claims:
        return field, claims, extra
    if names:
        return names, "", extra
    if claims:
        return "记忆", claims, extra
    readable = [
        str(value).strip()
        for value in data.values()
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 80
    ]
    return "记忆", (" · ".join(readable[:4]) or "有一条结构化记录"), extra


def _log_entry(at, seq, kind, outcome, subject, summary, *, href="", detail="", step="") -> dict:
    return {
        "at": at or "",
        "seq": int(seq or 0),
        "kind": kind,
        "step": step or "",
        "outcome": outcome or "",
        "subject": subject or "系统",
        "href": href or "",
        "summary": summary or "",
        "detail": detail or "",
    }


def _entries_from_alerts() -> list[dict]:
    rows = db().execute(
        "SELECT id, created_at, message, open FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?",
        (_LOG_LIMIT,),
    )
    return [
        _log_entry(
            row["created_at"],
            row["id"],
            "告警",
            "未结案" if row["open"] else "已结案",
            "系统",
            row["message"] or "",
        )
        for row in rows
    ]


def _entries_from_decisions() -> list[dict]:
    rows = db().execute(
        """SELECT id, created_at, kind, substr(IFNULL(detail, ''), 1, 400) AS detail
           FROM decisions ORDER BY created_at DESC, id DESC LIMIT ?""",
        (_LOG_LIMIT,),
    )
    entries = []
    for row in rows:
        raw = (row["kind"] or "").strip()
        detail = row["detail"] or ""
        if raw == "词条整合":
            kind, outcome, subject = "词条整合", ("未完成" if "没有跑完" in detail else "完成"), "词条"
            summary, extra = detail.strip(), ""
        elif raw == "清扫":
            kind, outcome, subject = "清扫", "完成", "清扫"
            summary, extra = detail.strip(), ""
        else:
            kind, outcome = "裁决", raw or "已记下"
            subject, summary, extra = _plain_detail(detail)
            if not summary and extra:
                summary, extra = extra, ""
        entries.append(
            _log_entry(row["created_at"], row["id"], kind, outcome, subject, summary, detail=extra)
        )
    return entries


def _entries_from_revisions() -> list[dict]:
    rows = db().execute(
        """SELECT r.id, r.created_at, r.note, r.crystal_id, r.actor, r.change,
                  substr(IFNULL(r.basis, ''), 1, 400) AS basis, r.scope, c.name
           FROM revisions r
           LEFT JOIN crystallizations c ON c.id=r.crystal_id
           ORDER BY r.created_at DESC, r.id DESC LIMIT ?""",
        (_LOG_LIMIT,),
    )
    entries = []
    for row in rows:
        note = (row["note"] or "").strip()
        scope = (row["scope"] or "").strip()
        change = (row["change"] or "").strip()
        basis = (row["basis"] or "").strip()
        kind, step = "修订", ""
        summary = change or note or scope or "改了一页"
        detail = basis if basis and basis not in summary else ""
        name = (row["name"] or "").strip() or (row["crystal_id"] or "")
        href = f"/admin/crystals/{row['crystal_id']}" if row["crystal_id"] else ""
        entries.append(
            _log_entry(
                row["created_at"],
                row["id"],
                kind,
                (row["actor"] or "").strip() or "已写入",
                name or "结晶",
                summary,
                href=href,
                detail=detail,
                step=step,
            )
        )
    return entries


def _entries_from_runs() -> list[dict]:
    rows = db().execute(
        """SELECT id, created_at, kind, step, outcome, subject, summary,
                  substr(IFNULL(detail, ''), 1, 400) AS detail, ref_kind, ref_id
           FROM run_log ORDER BY created_at DESC, id DESC LIMIT ?""",
        (_LOG_LIMIT,),
    )
    return [
        _log_entry(
            row["created_at"],
            row["id"],
            row["kind"] or "蒸馏",
            row["outcome"] or "",
            row["subject"] or "系统",
            row["summary"] or "",
            href=_log_href(row["ref_kind"] or "", row["ref_id"] or ""),
            detail=row["detail"] or "",
            step=row["step"] or "",
        )
        for row in rows
    ]


_LOG_SNAP_LOCK = threading.Lock()
_LOG_SNAP: dict = {"at": 0.0, "entries": [], "kinds": list(RUN_KINDS)}
_LOG_SNAP_THREAD: threading.Thread | None = None


def _build_operation_log() -> list[dict]:
    """Gather the merged list. List views clip long fields so a busy volume can finish."""
    entries: list[dict] = []
    entries.extend(_entries_from_alerts())
    entries.extend(_entries_from_decisions())
    entries.extend(_entries_from_revisions())
    entries.extend(_entries_from_runs())
    entries.sort(key=lambda item: item["at"] or "", reverse=True)
    return entries[:_LOG_LIMIT]


def _refresh_operation_log_snap() -> None:
    try:
        entries = _build_operation_log()
    except Exception:
        return
    with _LOG_SNAP_LOCK:
        _LOG_SNAP["entries"] = entries
        _LOG_SNAP["kinds"] = list(RUN_KINDS)
        _LOG_SNAP["at"] = time.time()


def _operation_log_refresher() -> None:
    while True:
        _refresh_operation_log_snap()
        time.sleep(2)


def _start_operation_log_refresher() -> None:
    global _LOG_SNAP_THREAD
    if _LOG_SNAP_THREAD is not None and _LOG_SNAP_THREAD.is_alive():
        return
    _LOG_SNAP_THREAD = threading.Thread(target=_operation_log_refresher, name="rogmem-log-snap", daemon=True)
    _LOG_SNAP_THREAD.start()


def operation_log(kind: str = "") -> dict:
    """One list, newest first. An empty kind is every type."""
    selected = kind if kind in RUN_KINDS else ""
    entries = _build_operation_log()
    if selected:
        entries = [item for item in entries if item["kind"] == selected]
    return {"entries": entries, "kinds": list(RUN_KINDS), "selected": selected or "全部"}


def operation_log_view(kind: str = "", page: int = 1) -> dict:
    """Admin page view. Serve the background snapshot; never wait for a fresh rebuild on the request path.

    Distill writers can make a live gather miss the switch budget. Once any snapshot exists, the page
    returns it immediately and leaves freshness to the refresher thread. The page shows PAGE_SIZE rows.
    """
    selected = kind if kind in RUN_KINDS else ""
    with _LOG_SNAP_LOCK:
        ready = bool(_LOG_SNAP.get("at"))
        entries = list(_LOG_SNAP.get("entries") or [])
        kinds = list(_LOG_SNAP.get("kinds") or RUN_KINDS)
    if not ready:
        _refresh_operation_log_snap()
        with _LOG_SNAP_LOCK:
            entries = list(_LOG_SNAP.get("entries") or [])
            kinds = list(_LOG_SNAP.get("kinds") or RUN_KINDS)
    if selected:
        entries = [item for item in entries if item["kind"] == selected]
    try:
        page_no = int(page)
    except (TypeError, ValueError):
        page_no = 1
    total = len(entries)
    current, pages, offset = _page_bounds(page_no, total)
    return {
        "entries": entries[offset : offset + PAGE_SIZE],
        "total": total,
        "page": current,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "kinds": kinds,
        "selected": selected or "全部",
    }


def _rebuild_projection(
    con: sqlite3.Connection,
    crystal_id: str,
    *,
    store=None,
    holder: str = "rebuild:projection",
    commit: bool = False,
) -> None:
    """Refresh thin envelope + chapter ops from live statements. No thick blob write.

    Switch-off fail-closed: no summary_json / chapters_json persist, no thin persist.
    """
    from rogmem import crystal_store as crystal_store_mod

    crystal = con.execute("SELECT * FROM crystallizations WHERE id=?", (crystal_id,)).fetchone()
    if not crystal:
        return
    ops: list[dict] = []
    for key in summary_fields(crystal["type"], owner=crystal["id"] == "owner"):
        if key == "概述":
            continue
        row = con.execute(
            """SELECT text FROM statements WHERE crystal_id=? AND field=? AND invalid_at IS NULL AND trust='written'
               ORDER BY created_at DESC LIMIT 1""",
            (crystal_id, key),
        ).fetchone()
        if row:
            value = row["text"]
        elif key == "名称":
            value = crystal["name"]
        else:
            value = "尚无来源"
        ops.extend(field_update_ops(key, value))
    for chapter in _chapters_for(crystal["type"]):
        rows = con.execute(
            """SELECT text FROM statements WHERE crystal_id=? AND chapter=? AND invalid_at IS NULL ORDER BY created_at""",
            (crystal_id, chapter),
        ).fetchall()
        if not rows:
            continue
        text = "\n".join(item["text"] for item in rows)
        ops.extend(chapter_update_ops(chapter, text))
    if not ops:
        return
    if not crystal_store_mod.DISTILLER_STORE_WRITE_ENABLED and store is None:
        return
    target_store = store if store is not None else CrystalStore(con)
    _apply_then_sidecar(
        target_store,
        con,
        crystal_id=crystal_id,
        expected_revision=int(crystal["revision"] or 0),
        ops=ops,
        holder=holder,
        sidecar=lambda: None,
        injected=store is not None,
        commit=commit,
    )


_RELEASE_LOCK = threading.Lock()
_READER_GUARD = threading.Lock()
_READER: threading.Thread | None = None
_MANUAL_SUCCESS_COOLDOWN = 60
_MANUAL_FAIL_STEP = 10
_MANUAL_FAIL_CAP = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_fingerprint(url: str) -> str:
    """Stable 16-hex fingerprint of a source URL. Empty for empty input.

    All caches that embed a tag or an ETag must be stamped with this
    fingerprint; any read that sees a stale fingerprint must discard
    the cache rather than carry it across a source switch.
    """
    import hashlib

    text = (url or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def release_source_url() -> str:
    """Env override wins over the DB setting."""
    from rogmem.release_source import ENV_SOURCE_URL

    env = os.environ.get(ENV_SOURCE_URL, "").strip()
    if env:
        return env
    return (get_setting("release_source_url") or "").strip()


def _current_fingerprint() -> str:
    return _source_fingerprint(release_source_url())


def release_view() -> dict:
    """Pure read: no lock, no network, no DB writes.

    When the stored cache belongs to a *different* source than the
    currently configured URL, the view downgrades to "never checked
    against this source" rather than showing a stale tag. This never
    mutates state — the next successful probe repopulates the row.
    """
    from rogmem.release_parser import present
    from rogmem.release_source import SourceError, validate_source_url
    from rogmem.version import product_version

    url = release_source_url()
    stored_kind = get_setting("release_error_kind") or ""
    stored_message = get_setting("release_error") or ""
    stored_fingerprint = get_setting("release_source_fingerprint") or ""
    current_fingerprint = _source_fingerprint(url)
    if not url:
        error_kind = "config_missing"
        error_message = ""
        latest = ""
        last_success_at = ""
        last_fail_at = ""
    else:
        try:
            validate_source_url(url)
        except SourceError as error:
            error_kind = "config_invalid"
            error_message = str(error)
            latest = ""
            last_success_at = ""
            last_fail_at = ""
        else:
            if stored_fingerprint and stored_fingerprint == current_fingerprint:
                error_kind = stored_kind
                error_message = stored_message
                latest = get_setting("release_latest") or ""
                last_success_at = get_setting("release_checked_at") or ""
                last_fail_at = get_setting("release_last_fail_at") or ""
            else:
                error_kind = "never_checked"
                error_message = ""
                latest = ""
                last_success_at = ""
                last_fail_at = ""
    return present(
        product_version(),
        latest=latest,
        last_success_at=last_success_at,
        last_fail_at=last_fail_at,
        error_kind=error_kind,
        error_message=error_message,
        source_url=url,
    )


def _remember_release_success(tag: str, etag: str, fingerprint: str) -> None:
    now = _now_iso()
    set_setting("release_source_fingerprint", fingerprint)
    set_setting("release_latest", tag)
    set_setting("release_etag", etag or "")
    set_setting("release_error", "")
    set_setting("release_error_kind", "ok")
    set_setting("release_checked_at", now)
    set_setting("release_probe_unix", str(time.time()))
    set_setting("release_fail_streak", "0")
    db().commit()


def _remember_release_not_modified(fingerprint: str) -> None:
    now = _now_iso()
    set_setting("release_source_fingerprint", fingerprint)
    set_setting("release_error", "")
    set_setting("release_error_kind", "ok")
    set_setting("release_checked_at", now)
    set_setting("release_probe_unix", str(time.time()))
    set_setting("release_fail_streak", "0")
    db().commit()


def _remember_release_failure(kind: str, message: str, fingerprint: str) -> None:
    from rogmem.release_parser import error_text

    streak = 0
    try:
        streak = int(get_setting("release_fail_streak") or "0")
    except ValueError:
        streak = 0
    streak = min(streak + 1, 20)
    if fingerprint:
        set_setting("release_source_fingerprint", fingerprint)
    set_setting("release_error_kind", kind or "unknown")
    set_setting("release_error", message or error_text(kind) or "核对没有完成")
    set_setting("release_last_fail_at", _now_iso())
    set_setting("release_probe_unix", str(time.time()))
    set_setting("release_fail_streak", str(streak))
    db().commit()


def _next_probe_due_seconds() -> float:
    """How long to wait before the next background probe, in seconds."""
    from rogmem.release_schedule import (
        BACKOFF_MAX_SECONDS,
        BACKOFF_MIN_SECONDS,
        CONFIG_PROBE_INTERVAL,
        PROBE_INTERVAL,
    )

    kind = get_setting("release_error_kind") or ""
    if kind in ("config_missing", "config_invalid"):
        return float(CONFIG_PROBE_INTERVAL)
    try:
        streak = int(get_setting("release_fail_streak") or "0")
    except ValueError:
        streak = 0
    if streak <= 0:
        return float(PROBE_INTERVAL)
    doubled = BACKOFF_MIN_SECONDS * (2 ** (streak - 1))
    return float(min(doubled, BACKOFF_MAX_SECONDS))


def _probe_due() -> bool:
    try:
        last = float(get_setting("release_probe_unix") or "0")
    except ValueError:
        last = 0.0
    return time.time() - last >= _next_probe_due_seconds()


def _manual_cooldown_seconds() -> float:
    """Manual retries are bounded on BOTH success and failure.

    - Success: a fixed 60 s window so an impatient click doesn't hammer.
    - Failure: the first retry is free (lets the user confirm what they
      just saw); subsequent rapid retries grow with the fail streak so a
      user cannot pin the source open. The grow step is small (10 s *
      streak, capped at 60 s) so diagnosing a real outage stays snappy.
    """
    kind = get_setting("release_error_kind") or ""
    try:
        streak = int(get_setting("release_fail_streak") or "0")
    except ValueError:
        streak = 0
    if kind in ("", "ok"):
        return float(_MANUAL_SUCCESS_COOLDOWN)
    if streak <= 1:
        return 0.0
    return float(min(_MANUAL_FAIL_STEP * (streak - 1), _MANUAL_FAIL_CAP))


def _manual_cooled() -> bool:
    try:
        last = float(get_setting("release_manual_unix") or "0")
    except ValueError:
        last = 0.0
    if not last:
        return False
    return time.time() - last < _manual_cooldown_seconds()


def _apply_outcome_for_source(outcome, fingerprint: str) -> bool:
    """Write an outcome, but only if the source hasn't changed since the
    probe was launched. An in-flight response for a prior source is
    silently discarded.
    """
    from rogmem.release_manifest import ManifestError, parse_manifest
    from rogmem.release_parser import error_text

    if fingerprint and fingerprint != _current_fingerprint():
        return False
    kind = outcome.kind
    if kind == "ok":
        try:
            manifest = parse_manifest(outcome.body or b"")
        except ManifestError as error:
            _remember_release_failure(error.kind, str(error), fingerprint)
            return False
        _remember_release_success(manifest.stable_latest, outcome.etag, fingerprint)
        return True
    if kind == "not_modified":
        _remember_release_not_modified(fingerprint)
        return True
    _remember_release_failure(kind, error_text(kind), fingerprint)
    return False


def _apply_outcome(outcome) -> bool:  # back-compat for in-tree callers
    return _apply_outcome_for_source(outcome, _current_fingerprint())


def _read_holding() -> bool:
    """Perform one probe. The caller holds _RELEASE_LOCK."""
    from rogmem.release_source import (
        DEFAULT_CONNECT_TIMEOUT,
        DEFAULT_READ_TIMEOUT,
        SourceError,
        fetch_manifest,
        validate_source_url,
    )

    url = release_source_url()
    fingerprint = _source_fingerprint(url)
    if not url:
        _remember_release_failure("config_missing", "", fingerprint)
        return False
    try:
        validate_source_url(url)
    except SourceError as error:
        _remember_release_failure("config_invalid", str(error), fingerprint)
        return False
    stored_fingerprint = get_setting("release_source_fingerprint") or ""
    etag_setting = get_setting("release_etag") or ""
    etag = etag_setting if (etag_setting and stored_fingerprint == fingerprint) else ""
    outcome = fetch_manifest(
        url,
        etag=etag or None,
        timeout_connect=DEFAULT_CONNECT_TIMEOUT,
        timeout_read=DEFAULT_READ_TIMEOUT,
    )
    return _apply_outcome_for_source(outcome, fingerprint)


def _read_release() -> None:
    with _RELEASE_LOCK:
        _read_holding()


def _spawn_release_read() -> None:
    global _READER

    def run() -> None:
        _read_release()

    thread = threading.Thread(target=run, daemon=True, name="rogmem-release")
    with _READER_GUARD:
        _READER = thread
    thread.start()


def finish_release_probe(timeout: float = 5) -> None:
    with _READER_GUARD:
        thread = _READER
    if thread is not None:
        thread.join(timeout)


def probe_release_if_due() -> None:
    if not _probe_due():
        return
    if not _RELEASE_LOCK.acquire(blocking=False):
        return
    try:
        if not _probe_due():
            return
    finally:
        _RELEASE_LOCK.release()
    _spawn_release_read()


def refresh_release() -> dict:
    """Manual button. Bounded on both success and failure (see
    ``_manual_cooldown_seconds``)."""
    if _manual_cooled():
        return {"ok": True, "cooled": True, "view": release_view()}
    if not _RELEASE_LOCK.acquire(blocking=False):
        return {"ok": True, "inflight": True, "view": release_view()}
    try:
        if _manual_cooled():
            return {"ok": True, "cooled": True, "view": release_view()}
        _read_holding()
        set_setting("release_manual_unix", str(time.time()))
        db().commit()
    finally:
        _RELEASE_LOCK.release()
    return {"ok": True, "view": release_view()}


def _clear_release_cache() -> None:
    """Wipe all per-source cache rows. The fingerprint is set by the
    caller after it decides the new source's value.
    """
    for key in (
        "release_latest",
        "release_etag",
        "release_error",
        "release_error_kind",
        "release_checked_at",
        "release_last_fail_at",
        "release_fail_streak",
        "release_probe_unix",
        "release_manual_unix",
    ):
        set_setting(key, "")


def set_release_source_url(url: str) -> dict:
    """Persist the configured manifest URL; wipe any cache bound to a
    different source.
    """
    from rogmem.release_source import SourceError, validate_source_url

    cleaned = (url or "").strip()
    previous = (get_setting("release_source_url") or "").strip()
    if not cleaned:
        _clear_release_cache()
        set_setting("release_source_url", "")
        set_setting("release_source_fingerprint", "")
        set_setting("release_error_kind", "config_missing")
        db().commit()
        return {"ok": True, "view": release_view()}
    try:
        validate_source_url(cleaned)
    except SourceError as error:
        return {"ok": False, "error": str(error), "error_kind": error.kind}
    if cleaned != previous:
        _clear_release_cache()
    set_setting("release_source_url", cleaned)
    set_setting("release_source_fingerprint", _source_fingerprint(cleaned))
    set_setting("release_error_kind", "")
    set_setting("release_error", "")
    set_setting("release_probe_unix", "0")
    db().commit()
    return {"ok": True, "view": release_view()}
