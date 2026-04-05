"""
БД: SQLite для пользователей и пресетов.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "hh_parser.db"))


def _ensure_dir():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Создать таблицы, если не существуют."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            tab TEXT NOT NULL DEFAULT 'geo',
            config_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_presets_user ON presets(user_id);
    """)
    conn.commit()
    conn.close()


# ---------- Users ----------

def get_user_by_login(login: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, login, display_name, role, created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(login: str, password_hash: str, display_name: str, role: str = "user") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO users (login, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        (login, password_hash, display_name, role),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def delete_user(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- Presets ----------

def list_presets(user_id: int, tab: str = None) -> list[dict]:
    conn = get_conn()
    if tab:
        rows = conn.execute(
            "SELECT * FROM presets WHERE user_id = ? AND tab = ? ORDER BY updated_at DESC",
            (user_id, tab),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM presets WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        result.append(d)
    return result


def create_preset(user_id: int, name: str, tab: str, config: dict) -> int:
    """Создать или обновить пресет (upsert по имени+табу)."""
    conn = get_conn()
    # Проверяем существующий пресет с таким же именем
    existing = conn.execute(
        "SELECT id FROM presets WHERE user_id = ? AND name = ? AND tab = ?",
        (user_id, name, tab),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE presets SET config_json = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(config, ensure_ascii=False), existing["id"]),
        )
        conn.commit()
        preset_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO presets (user_id, name, tab, config_json) VALUES (?, ?, ?, ?)",
            (user_id, name, tab, json.dumps(config, ensure_ascii=False)),
        )
        conn.commit()
        preset_id = cur.lastrowid
    conn.close()
    return preset_id


def delete_preset(preset_id: int, user_id: int) -> bool:
    """Удалить пресет (только свой)."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted
