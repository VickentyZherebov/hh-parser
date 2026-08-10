"""
БД: SQLite для пользователей и пресетов.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
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

        CREATE TABLE IF NOT EXISTS placement_maps (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_placement_maps_user
            ON placement_maps(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS placement_vacancy_cache (
            hh_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            checked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS placement_geocode_cache (
            query TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    _seed_users()


def _seed_users():
    """Создать начальных пользователей если БД пустая."""
    from web.auth import hash_password
    users = get_user_by_login("admin")
    if users:
        return  # юзеры уже есть

    seed = [
        ("admin", "HHparser2026!", "Викентий", "admin"),
        ("masha", "HHparser2026!", "Маша Истомина", "user"),
        ("efim", "HHparser2026!", "Ефим Заковряшин", "user"),
        ("dima", "HHparser2026!", "Дмитрий Дмитриев", "user"),
    ]
    for login, pw, name, role in seed:
        if not get_user_by_login(login):
            create_user(login, hash_password(pw), name, role)


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


# ---------- Placement maps ----------

def save_placement_map(map_id: str, user_id: int, name: str, payload: dict) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO placement_maps (id, user_id, name, payload_json) VALUES (?, ?, ?, ?)",
        (map_id, user_id, name, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def get_placement_map(map_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, user_id, name, payload_json, created_at FROM placement_maps WHERE id = ?",
        (map_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def list_placement_maps(user_id: int, limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, name, created_at
        FROM placement_maps
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_cached_placements(hh_ids: list[str], ttl_hours: int | None = None) -> dict[str, dict]:
    if not hh_ids:
        return {}
    if ttl_hours is None:
        ttl_hours = int(os.environ.get("HH_PLACEMENT_CACHE_TTL_HOURS", "6"))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
    placeholders = ",".join("?" for _ in hh_ids)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT hh_id, payload_json FROM placement_vacancy_cache "
        f"WHERE hh_id IN ({placeholders}) AND checked_at >= ?",
        (*hh_ids, cutoff),
    ).fetchall()
    conn.close()
    return {row["hh_id"]: json.loads(row["payload_json"]) for row in rows}


def save_cached_placements(payloads: list[dict]) -> None:
    if not payloads:
        return
    checked_at = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    conn.executemany(
        """
        INSERT INTO placement_vacancy_cache (hh_id, payload_json, checked_at)
        VALUES (?, ?, ?)
        ON CONFLICT(hh_id) DO UPDATE SET
            payload_json = excluded.payload_json,
            checked_at = excluded.checked_at
        """,
        [
            (payload["hh_id"], json.dumps(payload, ensure_ascii=False), checked_at)
            for payload in payloads
        ],
    )
    conn.commit()
    conn.close()


def get_cached_geocodes(queries: list[str]) -> dict[str, dict]:
    if not queries:
        return {}
    placeholders = ",".join("?" for _ in queries)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT query, latitude, longitude, display_name FROM placement_geocode_cache "
        f"WHERE query IN ({placeholders})",
        queries,
    ).fetchall()
    conn.close()
    return {
        row["query"]: {
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "display_name": row["display_name"],
        }
        for row in rows
    }


def save_cached_geocode(query: str, payload: dict) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO placement_geocode_cache
            (query, latitude, longitude, display_name, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(query) DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            display_name = excluded.display_name,
            checked_at = excluded.checked_at
        """,
        (
            query, payload["latitude"], payload["longitude"],
            payload.get("display_name", ""), datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
