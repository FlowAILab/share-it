"""Read-only parser for Goose (block/goose).

Current: SQLite ~/.local/share/goose/sessions/sessions.db (`sessions` +
`messages`, role column, content_json blocks). Legacy: per-session JSONL in the
same dir (line 1 = meta, lines 2..N = {role, content}). We read the DB when
present, else the JSONL. Every field optional; degrade to empty on mismatch.
"""
import glob
import json
import os
import sqlite3

def _sessions_dir():
    root = os.environ.get("GOOSE_PATH_ROOT")
    cands = []
    if root:
        cands += [os.path.join(root, "data", "sessions"), os.path.join(root, "sessions")]
    cands += [os.path.expanduser("~/.local/share/goose/sessions"),
              os.path.expanduser("~/Library/Application Support/goose/sessions"),
              os.path.expanduser("~/Library/Application Support/Block/goose/sessions")]
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


_DIR = _sessions_dir()
_DB = os.path.join(_DIR, "sessions.db")


def available():
    return os.path.isfile(_DB) or bool(glob.glob(os.path.join(_DIR, "*.jsonl")))


def _conn():
    return sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)


def _content_text(blocks):
    try:
        arr = json.loads(blocks) if isinstance(blocks, (str, bytes)) else blocks
    except (json.JSONDecodeError, TypeError):
        return ""
    parts = []
    for b in arr if isinstance(arr, list) else []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts)


def discover():
    out = []
    if os.path.isfile(_DB):
        try:
            con = _conn()
            try:
                names = {r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "sessions" in names:
                    for sid, name, desc, wd, upd in con.execute(
                            "SELECT id, name, description, working_dir, "
                            "COALESCE(updated_at, created_at, 0) FROM sessions"):
                        title = " ".join((name or desc or "").split())
                        out.append({"id": f"{_DB}#{sid}", "title": title,
                                    "cwd": wd or "", "ts": _sec(upd)})
            finally:
                con.close()
        except sqlite3.Error:
            pass
    if not out:  # legacy JSONL
        for path in glob.glob(os.path.join(_DIR, "*.jsonl")):
            meta = _jsonl_meta(path)
            out.append({"id": path, "title": meta[0],
                        "cwd": meta[1], "ts": meta[2]})
    return out


def _sec(t):
    t = t or 0
    return t / 1000.0 if t > 1e11 else t


def _jsonl_meta(path):
    try:
        with open(path, errors="ignore") as fh:
            first = fh.readline().strip()
        o = json.loads(first)
        return (" ".join((o.get("description") or "").split()) or os.path.basename(path),
                o.get("working_dir", ""), _sec(o.get("updated_at") or o.get("created_at") or
                                                os.path.getmtime(path)))
    except (OSError, json.JSONDecodeError):
        return os.path.basename(path), "", os.path.getmtime(path)


def parse(session_id):
    msgs = []
    if "#" in session_id and os.path.isfile(_DB):
        ref = session_id.split("#", 1)[1]
        try:
            con = _conn()
            try:
                for role, content in con.execute(
                        "SELECT role, content_json FROM messages WHERE session_id = ? "
                        "ORDER BY id", (ref,)):
                    text = _content_text(content)
                    if text.strip() and role in ("user", "assistant"):
                        msgs.append({"role": role, "text": text})
            finally:
                con.close()
        except sqlite3.Error:
            return msgs
    elif os.path.isfile(session_id):
        try:
            with open(session_id, errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i == 0:
                        continue  # meta line
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = o.get("role")
                    text = _content_text(o.get("content"))
                    if text.strip() and role in ("user", "assistant"):
                        msgs.append({"role": role, "text": text})
        except OSError:
            return msgs
    return msgs
