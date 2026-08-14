"""解读结果的 SQLite 缓存。

缓存键与文件路径解耦（基于内容哈希），同一段代码在不同位置/不同时间
只要内容不变就能命中缓存；file_path 字段仅用于按文件清理与导出。
"""
import hashlib
import sqlite3
import threading
import time
from typing import Dict, Optional, Tuple

from .config import data_dir

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(data_dir() / "cache.db"), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS explanations (
                key TEXT PRIMARY KEY,
                file_path TEXT,
                kind TEXT,
                content TEXT,
                model TEXT,
                created_at REAL
            )"""
        )
        _conn.commit()
    return _conn


def make_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def get(key: str) -> Optional[str]:
    with _lock:
        row = _get_conn().execute(
            "SELECT content FROM explanations WHERE key=?", (key,)
        ).fetchone()
    return row[0] if row else None


def get_many(keys) -> Dict[str, str]:
    if not keys:
        return {}
    marks = ",".join("?" for _ in keys)
    with _lock:
        rows = _get_conn().execute(
            f"SELECT key, content FROM explanations WHERE key IN ({marks})", list(keys)
        ).fetchall()
    return {k: v for k, v in rows}


def get_newest(keys) -> Optional[Tuple[str, str]]:
    """在给定键中返回最近生成的一条 (key, content)；全部未命中时返回 None。

    用于同一段代码存在多种解读模式缓存时，选取用户最近生成的那份（如导出报告）。
    """
    if not keys:
        return None
    marks = ",".join("?" for _ in keys)
    with _lock:
        row = _get_conn().execute(
            f"SELECT key, content FROM explanations WHERE key IN ({marks}) "
            "ORDER BY created_at DESC LIMIT 1", list(keys)
        ).fetchone()
    return (row[0], row[1]) if row else None


def put(key: str, file_path: str, kind: str, content: str, model: str) -> None:
    if not content or not content.strip():
        return
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO explanations VALUES (?,?,?,?,?,?)",
            (key, file_path, kind, content, model, time.time()),
        )
        conn.commit()


def close() -> None:
    """Close the process-local connection (application shutdown and tests)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def delete_keys(keys) -> None:
    if not keys:
        return
    marks = ",".join("?" for _ in keys)
    with _lock:
        conn = _get_conn()
        conn.execute(f"DELETE FROM explanations WHERE key IN ({marks})", list(keys))
        conn.commit()


def delete_for_file(file_path: str) -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM explanations WHERE file_path=?", (file_path,))
        conn.commit()
        return cur.rowcount


def stats() -> Dict[str, int]:
    with _lock:
        row = _get_conn().execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(content)),0) FROM explanations"
        ).fetchone()
    return {"entries": row[0], "chars": row[1]}
