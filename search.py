"""Full-text search over sessions — SQLite FTS5, one weighted row per session.

Columns are weighted via bm25 so titles beat user text beat assistant text beat
tool traces; reasoning is not indexed. Schema is versioned: bumping FTS_VERSION
rebuilds the index transparently.
"""
import os
import sqlite3

DB_PATH = os.path.expanduser("~/.shareit/fts.sqlite")
FTS_VERSION = 3  # coupled to parser SCHEMA_VERSION + size-aware invalidation
_CAPS = {"user_text": 400_000, "assistant_text": 400_000, "tools": 120_000}


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), mode=0o700, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = conn.execute("SELECT v FROM meta WHERE k = 'version'").fetchone()
    if row is None or row[0] != str(FTS_VERSION):
        conn.execute("DROP TABLE IF EXISTS sess")
        conn.execute("DROP TABLE IF EXISTS files")
        conn.execute("DROP TABLE IF EXISTS msgs")  # v1 layout
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)",
                     (str(FTS_VERSION),))
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sess USING fts5("
                 "path UNINDEXED, title, user_text, assistant_text, tools)")
    return conn


def needs_index(path, mtime, size=None):
    with _conn() as conn:
        row = conn.execute("SELECT mtime, size FROM files WHERE path = ?", (path,)).fetchone()
    return row is None or row[0] != mtime or (size is not None and row[1] != size)


def index_session(path, mtime, messages, title="", extra="", size=None):
    cols = {"user_text": [], "assistant_text": [], "tools": []}
    for m in messages:
        if m["role"] == "user":
            cols["user_text"].append(m["text"])
        elif m["role"] == "assistant":
            cols["assistant_text"].append(m["text"])
        elif m["role"] == "tool":
            cols["tools"].append(m.get("name", "") + " " + (m.get("input") or "")[:400])
    joined = {k: "\n".join(v)[:_CAPS[k]] for k, v in cols.items()}
    with _conn() as conn:
        conn.execute("DELETE FROM sess WHERE path = ?", (path,))
        conn.execute("INSERT INTO sess (path, title, user_text, assistant_text, tools) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (path, (title + " " + extra).strip(), joined["user_text"],
                      joined["assistant_text"], joined["tools"]))
        conn.execute("INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)",
                     (path, mtime, size))


def prune(live_paths):
    with _conn() as conn:
        for (p,) in conn.execute("SELECT path FROM files").fetchall():
            if p not in live_paths:
                conn.execute("DELETE FROM sess WHERE path = ?", (p,))
                conn.execute("DELETE FROM files WHERE path = ?", (p,))


def indexed_count():
    try:
        with _conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    except sqlite3.Error:
        return 0


def search(query, limit=12):
    terms = [t.replace('"', "") for t in query.split() if len(t.strip()) >= 2]
    if not terms:
        return []
    match = " ".join(f'"{t}"*' for t in terms)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT path, snippet(sess, -1, '«', '»', '…', 14) FROM sess "
                "WHERE sess MATCH ? "
                "ORDER BY bm25(sess, 0, 8.0, 4.0, 2.0, 0.5) LIMIT ?",
                (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"path": p, "snippet": s} for p, s in rows]
