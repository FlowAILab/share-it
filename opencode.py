"""Read-only parser for OpenCode (sst/opencode) — SQLite store.

~/.local/share/opencode/opencode.db (drizzle-orm). Sessions in `session`,
messages in `message` (data JSON carries role), text in `part` rows whose
data JSON is {"type":"text","text":...}. Global store (all projects). Every
field optional; shape mismatch degrades to empty rather than raising.
"""
import glob
import json
import os
import sqlite3

_DATA = os.environ.get("OPENCODE_DATA") or os.path.expanduser("~/.local/share/opencode")


def _db_path():
    if os.environ.get("OPENCODE_DB") and os.path.isfile(os.environ["OPENCODE_DB"]):
        return os.environ["OPENCODE_DB"]
    for cand in [os.path.join(_DATA, "opencode.db")] + sorted(
            glob.glob(os.path.join(_DATA, "opencode-*.db"))):
        if os.path.isfile(cand):
            return cand
    return None


def available():
    return _db_path() is not None


def _conn(path):
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def discover():
    path = _db_path()
    if not path:
        return []
    out = []
    try:
        con = _conn(path)
    except sqlite3.Error:
        return []
    try:
        if "session" not in _tables(con):
            return []
        for sid, title, directory, t in con.execute(
                "SELECT id, title, directory, COALESCE(time_updated, time_created, 0) FROM session"):
            ts = (t or 0) / 1000.0 if t and t > 1e11 else (t or 0)
            out.append({"id": f"{path}#{sid}", "title": " ".join((title or "").split()),
                        "cwd": directory or "", "ts": ts})
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return out


def _part_text(data):
    try:
        d = json.loads(data) if isinstance(data, (str, bytes)) else data
    except (json.JSONDecodeError, TypeError):
        return "", None
    if not isinstance(d, dict):
        return "", None
    if d.get("type") == "text" and d.get("text"):
        return d["text"], "text"
    if d.get("type") == "reasoning" and d.get("text"):
        return d["text"], "thinking"
    if d.get("type") == "tool":
        return "", "tool"
    return "", None


def parse(session_id):
    path, _, sid = session_id.partition("#")
    if not os.path.isfile(path):
        return []
    try:
        con = _conn(path)
    except sqlite3.Error:
        return []
    msgs = []
    try:
        tables = _tables(con)
        if not {"message", "part"} <= tables:
            return []
        roles = {}
        for mid, data in con.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY id", (sid,)):
            try:
                roles[mid] = (json.loads(data) or {}).get("role")
            except (json.JSONDecodeError, TypeError):
                roles[mid] = None
        cur = {}  # message_id -> accumulated text, role
        for mid, data in con.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY id", (sid,)):
            text, kind = _part_text(data)
            if not text.strip():
                continue
            role = roles.get(mid)
            if role == "user":
                msgs.append({"role": "user", "text": text})
            elif role == "assistant":
                msgs.append({"role": "assistant" if kind != "thinking" else "thinking",
                             "text": text})
    except sqlite3.Error:
        return msgs
    finally:
        con.close()
    return msgs
