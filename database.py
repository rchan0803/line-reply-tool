import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "line_chat.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def upsert_user(user_id: str, display_name: str):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO users (user_id, display_name, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
        (user_id, display_name, now),
    )
    conn.commit()
    conn.close()


def save_message(user_id: str, direction: str, content: str):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO messages (user_id, direction, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, direction, content, now),
    )
    conn.commit()
    conn.close()


def save_draft(user_id: str, content: str):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO drafts (user_id, content, created_at) VALUES (?, ?, ?)",
        (user_id, content, now),
    )
    conn.commit()
    conn.close()


def get_conversations():
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.user_id, u.display_name,
               m.content AS last_message, m.created_at AS last_at
        FROM users u
        LEFT JOIN messages m ON m.id = (
            SELECT id FROM messages WHERE user_id = u.user_id ORDER BY id DESC LIMIT 1
        )
        ORDER BY last_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages(user_id: str, limit: int = 50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT direction, content, created_at FROM messages"
        " WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def get_latest_draft(user_id: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT content, created_at FROM drafts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None
