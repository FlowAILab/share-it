"""Read-only parser for Cursor's chat/composer/agent history (SQLite state.vscdb).

Cursor is a VS Code fork: session metadata lives in ItemTable / cursorDiskKV
`composerData:*`, message bodies in `cursorDiskKV` `bubbleId:*` (type 1=user,
2=assistant). Schema has drifted across versions, so every field is optional and
a shape mismatch degrades to empty rather than crashing. Never opened read-write.

Sessions are addressed as "<db_path>#<composerId>" so they fit the path-based
adapter model without touching on-disk files.
"""
import json
import os
import sqlite3

CURSOR_ROOT = os.path.expanduser("~/Library/Application Support/Cursor/User")
_GLOBAL_DB = os.path.join(CURSOR_ROOT, "globalStorage", "state.vscdb")


def available():
    return os.path.isfile(_GLOBAL_DB)


def _rows(path, table):
    if not os.path.isfile(path):
        return
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            return
        seen = 0
        for k, v in con.execute(f"SELECT key, value FROM {table}"):
            seen += 1
            if seen > 50000:          # bound: a pathological DB can't stall us
                break
            if v is not None and len(v) > 5_000_000:  # skip absurd single blobs
                continue
            try:
                yield k, json.loads(v.decode("utf-8") if isinstance(v, bytes) else v)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    except sqlite3.Error:
        return
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _bubble_text(b):
    if not isinstance(b, dict):
        return "", "assistant"
    role = {1: "user", 2: "assistant"}.get(b.get("type"), "assistant")
    text = b.get("text") or ""
    if not text and isinstance(b.get("thinking"), dict):
        text = b["thinking"].get("text", "")
    if not text:
        tf = b.get("toolFormerData")
        if isinstance(tf, dict):
            text = tf.get("result") or (f"[tool: {tf.get('name', '?')}]" if tf.get("name") else "")
    return text, role


def _sessions_meta(db):
    """composerId -> {name, order:[bubbleId,…]} from cursorDiskKV/ItemTable."""
    meta = {}
    for k, v in _rows(db, "cursorDiskKV"):
        if k.startswith("composerData:") and isinstance(v, dict):
            cid = k.split(":", 1)[1]
            order = [h.get("bubbleId") for h in (v.get("fullConversationHeadersOnly") or [])
                     if isinstance(h, dict)]
            ts = v.get("lastUpdatedAt") or v.get("createdAt") or 0  # ms epoch
            meta[cid] = {"name": v.get("name") or "", "order": order,
                         "ts": ts / 1000.0 if isinstance(ts, (int, float)) else 0}
    # legacy inline composer list (ItemTable)
    for k, v in _rows(db, "ItemTable"):
        if k == "composer.composerData" and isinstance(v, dict):
            for c in v.get("allComposers") or []:
                if isinstance(c, dict) and c.get("composerId") and c["composerId"] not in meta:
                    meta[c["composerId"]] = {"name": c.get("name") or "", "order": []}
    return meta


def discover():
    """[{id, title}] for every Cursor session found. Empty if Cursor absent."""
    if not available():
        return []
    out = []
    for cid, m in _sessions_meta(_GLOBAL_DB).items():
        title = " ".join((m.get("name") or "").split())
        out.append({"id": f"{_GLOBAL_DB}#{cid}", "title": title, "composer": cid,
                    "ts": m.get("ts") or 0})
    return out


def parse(session_id):
    """'<db>#<composerId>' -> unified message list."""
    db, _, cid = session_id.partition("#")
    meta = _sessions_meta(db).get(cid, {"order": []})
    bubbles = {}
    for k, v in _rows(db, "cursorDiskKV"):
        if k.startswith(f"bubbleId:{cid}:"):
            bubbles[k.rsplit(":", 1)[1]] = v
    msgs = []
    ids = meta.get("order") or list(bubbles.keys())
    for bid in ids:
        text, role = _bubble_text(bubbles.get(bid))
        if text and text.strip():
            msgs.append({"role": role, "text": text})
    return msgs


def meta_for(session_id):
    """(title, cwd) — Cursor cwd is per-workspace and not reliably joinable here."""
    db, _, cid = session_id.partition("#")
    m = _sessions_meta(db).get(cid, {})
    title = " ".join((m.get("name") or "").split())
    if not title:
        for msg in parse(session_id):
            if msg["role"] == "user":
                title = " ".join(msg["text"].split())[:120]
                break
    return title or "(untitled)", ""
