"""SQLite 存储层：API Key 与用量记录。

设计要点：
- 密钥只存 SHA-256 哈希，数据库泄露也无法反推出明文 key。
- 日用量单独聚合到 daily_usage 表，配额校验是 O(1) 查询，不会随明细增长变慢。
- usage_log 保留请求级明细，便于排查和后续计费，可定期清理。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

KEY_PREFIX = "sk-qw-"
TS_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"


def hash_key(raw: str) -> str:
    """对明文 key 做哈希，返回十六进制摘要。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """生成一个新的明文 key。只在创建时返回一次，之后不再可见。"""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def _today_str() -> str:
    return datetime.now().strftime(DATE_FMT)


def parse_expires(spec: Optional[str]) -> Optional[str]:
    """把 '30d' / '12h' / '2026-12-31' 解析成时间字符串。None 表示永不过期。"""
    if not spec:
        return None
    spec = spec.strip()
    if spec.lower() in ("never", "none", "0"):
        return None
    if spec.endswith(("d", "D")):
        return (datetime.now() + timedelta(days=int(spec[:-1]))).strftime(TS_FMT)
    if spec.endswith(("h", "H")):
        return (datetime.now() + timedelta(hours=int(spec[:-1]))).strftime(TS_FMT)
    # 当作日期处理
    dt = datetime.strptime(spec, "%Y-%m-%d")
    return dt.strftime(TS_FMT)


_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gateway.db")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("GATEWAY_DB", _DEFAULT_DB)
        self._lock = threading.Lock()
        self._event_count = 0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # 不设 busy_timeout 时，SQLite 遇到锁会立刻抛 "database is locked"。
        # 网关与 CLI 同时写入是常见场景，给它 5 秒重试窗口。
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        self.harden_permissions()

    def harden_permissions(self) -> None:
        """把数据库及其 WAL 附属文件收紧到 600。

        库内虽然只存密钥哈希（不可逆），但用量数据包含使用行为，
        不应让同机其他账号读到。
        """
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path + suffix
            if os.path.exists(candidate):
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash       TEXT    NOT NULL UNIQUE,
                    key_prefix     TEXT    NOT NULL,
                    name           TEXT    NOT NULL,
                    rpm_limit      INTEGER NOT NULL DEFAULT 60,
                    daily_tokens   INTEGER NOT NULL DEFAULT 2000000,
                    max_concurrent INTEGER NOT NULL DEFAULT 4,
                    created_at     TEXT    NOT NULL,
                    expires_at     TEXT,
                    revoked        INTEGER NOT NULL DEFAULT 0,
                    note           TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_usage (
                    key_id            INTEGER NOT NULL,
                    date              TEXT    NOT NULL,
                    requests          INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (key_id, date)
                );

                CREATE TABLE IF NOT EXISTS usage_log (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id            INTEGER NOT NULL,
                    ts                TEXT    NOT NULL,
                    model             TEXT,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    status            INTEGER,
                    latency_ms        INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_log(key_id, ts);

                -- 限流事件。放在数据库里而不是内存，是为了让重启不清零计数：
                -- 否则攻击者只要想办法触发一次重启，限流窗口就被重置了。
                CREATE TABLE IF NOT EXISTS rate_events (
                    scope TEXT NOT NULL,
                    ts    REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events(scope, ts);

                -- 请求审计日志。记录每一次请求的去向与结果，包含被拒绝的请求，
                -- 这些恰恰是安全审计最需要的信息。
                CREATE TABLE IF NOT EXISTS audit_log (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                TEXT    NOT NULL,
                    ip                TEXT,
                    key_id            INTEGER,
                    key_prefix        TEXT,
                    method            TEXT,
                    path              TEXT,
                    model             TEXT,
                    status            INTEGER,
                    outcome           TEXT,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms        INTEGER,
                    user_agent        TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_ts  ON audit_log(ts);
                CREATE INDEX IF NOT EXISTS idx_audit_key ON audit_log(key_id, ts);
                CREATE INDEX IF NOT EXISTS idx_audit_ip  ON audit_log(ip, ts);
                """
            )
            self._conn.commit()

    # ---------- Key 管理 ----------

    def create_key(
        self,
        name: str,
        rpm_limit: int = 60,
        daily_tokens: int = 2_000_000,
        max_concurrent: int = 4,
        expires: Optional[str] = None,
        note: Optional[str] = None,
    ) -> tuple[str, dict[str, Any]]:
        """创建 key，返回 (明文 key, 记录字典)。明文只在此刻存在。"""
        raw = generate_key()
        expires_at = parse_expires(expires)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO api_keys
                    (key_hash, key_prefix, name, rpm_limit, daily_tokens,
                     max_concurrent, created_at, expires_at, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hash_key(raw),
                    raw[:12],
                    name,
                    rpm_limit,
                    daily_tokens,
                    max_concurrent,
                    _now_str(),
                    expires_at,
                    note,
                ),
            )
            self._conn.commit()
            key_id = cur.lastrowid
        return raw, self.get_by_id(key_id)

    def get_by_hash(self, key_hash: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, key_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE id = ?", (key_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_keys(self, include_revoked: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM api_keys"
        if not include_revoked:
            sql += " WHERE revoked = 0"
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def revoke_key(self, identifier: str) -> int:
        """按 id 或 key 前缀吊销，返回受影响行数。"""
        with self._lock:
            if identifier.isdigit():
                cur = self._conn.execute(
                    "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0",
                    (int(identifier),),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE api_keys SET revoked = 1 WHERE key_prefix = ? AND revoked = 0",
                    (identifier,),
                )
            self._conn.commit()
        return cur.rowcount

    # ---------- 用量 ----------

    def used_tokens_today(self, key_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT prompt_tokens + completion_tokens AS total "
                "FROM daily_usage WHERE key_id = ? AND date = ?",
                (key_id, _today_str()),
            ).fetchone()
        return int(row["total"]) if row and row["total"] is not None else 0

    def record_usage(
        self,
        key_id: int,
        model: Optional[str],
        prompt_tokens: int,
        completion_tokens: int,
        status: int,
        latency_ms: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_usage (key_id, date, requests, prompt_tokens, completion_tokens)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(key_id, date) DO UPDATE SET
                    requests          = requests + 1,
                    prompt_tokens     = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens
                """,
                (key_id, _today_str(), prompt_tokens, completion_tokens),
            )
            self._conn.execute(
                """
                INSERT INTO usage_log
                    (key_id, ts, model, prompt_tokens, completion_tokens, status, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key_id,
                    _now_str(),
                    model,
                    prompt_tokens,
                    completion_tokens,
                    status,
                    latency_ms,
                ),
            )
            self._conn.commit()

    def usage_summary(self, days: int = 7) -> list[dict[str, Any]]:
        since = (datetime.now() - timedelta(days=days)).strftime(DATE_FMT)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT k.id, k.name, k.key_prefix, u.date, u.requests,
                       u.prompt_tokens, u.completion_tokens
                FROM daily_usage u
                JOIN api_keys k ON k.id = u.key_id
                WHERE u.date >= ?
                ORDER BY u.date DESC, u.requests DESC
                """,
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 限流状态（持久化，重启不清零） ----------

    def check_rate(self, scope: str, limit: int, window: float) -> tuple[bool, int]:
        """滑动窗口限流检查，返回 (是否放行, 建议重试秒数)。

        状态放在 SQLite 而不是内存，是为了让网关重启不清零计数——
        否则攻击者只要想办法触发一次重启，限流窗口就被重置了。
        放行后调用方需自行调用 record_event 记录本次事件。
        """
        now = time.time()
        cutoff = now - window
        with self._lock:
            self._conn.execute(
                "DELETE FROM rate_events WHERE scope = ? AND ts < ?", (scope, cutoff)
            )
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest "
                "FROM rate_events WHERE scope = ? AND ts >= ?",
                (scope, cutoff),
            ).fetchone()
            self._conn.commit()

        count = int(row["n"] or 0)
        if count >= limit:
            oldest = row["oldest"]
            retry = int(window - (now - float(oldest))) + 1 if oldest is not None else int(window)
            return False, max(retry, 1)
        return True, 0

    def record_event(self, scope: str) -> None:
        """记录一次限流事件（一次放行的请求，或一次鉴权失败）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_events (scope, ts) VALUES (?, ?)", (scope, time.time())
            )
            self._event_count += 1
            # 顺带做一次全局清理，避免长期不再访问的 scope 留下垃圾记录
            if self._event_count % 500 == 0:
                self._conn.execute(
                    "DELETE FROM rate_events WHERE ts < ?", (time.time() - 3600.0,)
                )
            self._conn.commit()

    def purge_rate_events(self, max_age: float = 3600.0) -> int:
        cutoff = time.time() - max_age
        with self._lock:
            cur = self._conn.execute("DELETE FROM rate_events WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    # ---------- 审计日志 ----------

    def record_audit(
        self,
        ip: Optional[str],
        key_id: Optional[int],
        key_prefix: Optional[str],
        method: Optional[str],
        path: Optional[str],
        model: Optional[str],
        status: int,
        outcome: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        user_agent: Optional[str] = None,
    ) -> None:
        """写一条审计记录。包含被拒绝的请求——那些恰恰是安全审计的重点。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (ts, ip, key_id, key_prefix, method, path, model, status,
                     outcome, prompt_tokens, completion_tokens, latency_ms, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_str(), ip, key_id, key_prefix, method, path, model,
                    status, outcome, prompt_tokens, completion_tokens,
                    latency_ms, user_agent,
                ),
            )
            self._conn.commit()

    def audit_query(
        self,
        days: int = 1,
        limit: int = 100,
        outcome: Optional[str] = None,
        key_id: Optional[int] = None,
        ip: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        since = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
        sql = "SELECT * FROM audit_log WHERE ts >= ?"
        params: list[Any] = [since]
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if key_id is not None:
            sql += " AND key_id = ?"
            params.append(key_id)
        if ip:
            sql += " AND ip = ?"
            params.append(ip)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def audit_summary(self, days: int = 1) -> list[dict[str, Any]]:
        """按结果类型汇总。异常时（比如 auth_invalid 暴增）一眼能看出来。"""
        since = (datetime.now() - timedelta(days=days)).strftime(TS_FMT)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT outcome, COUNT(*) AS n, MAX(ts) AS last_seen
                FROM audit_log WHERE ts >= ?
                GROUP BY outcome ORDER BY n DESC
                """,
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_logs(self, keep_days: int = 30) -> dict[str, int]:
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime(TS_FMT)
        with self._lock:
            usage = self._conn.execute("DELETE FROM usage_log WHERE ts < ?", (cutoff,))
            audit = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return {"usage_log": usage.rowcount, "audit_log": audit.rowcount}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
