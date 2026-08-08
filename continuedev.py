"""Read-only parser for Continue.dev (continuedev/continue).

~/.continue/sessions/<id>.json + sessions.json index (IDE-agnostic; VS Code and
JetBrains share it). Turn text is two levels deep: history[].message.content
(string or [{type:"text",text}]). cwd = workspaceDirectory. Degrade to empty on
any shape mismatch.
"""
import json
import os

ROOT = os.path.expanduser(os.environ.get("CONTINUE_GLOBAL_DIR") or "~/.continue")
_SESS = os.path.join(ROOT, "sessions")


def available():
    return os.path.isdir(_SESS)


def _clean_cwd(w):
    w = w or ""
    if w.startswith("file://"):
        from urllib.parse import unquote, urlparse
        w = unquote(urlparse(w).path)
    return w


def discover():
    if not available():
        return []
    out = []
    try:
        with open(os.path.join(_SESS, "sessions.json")) as fh:
            idx = json.load(fh)
    except (OSError, json.JSONDecodeError):
        idx = []
    for e in idx if isinstance(idx, list) else []:
        if not isinstance(e, dict) or not e.get("sessionId"):
            continue
        try:
            ts = float(e.get("dateCreated") or 0) / 1000.0
        except (TypeError, ValueError):
            ts = 0
        p = os.path.join(_SESS, f"{e['sessionId']}.json")
        mt = os.path.getmtime(p) if os.path.isfile(p) else ts   # updates → freshness
        out.append({"id": p, "title": " ".join((e.get("title") or "").split()),
                    "cwd": _clean_cwd(e.get("workspaceDirectory")), "ts": mt})
    return out


def _content(c):
    if isinstance(c, str):
        return c
    parts = []
    for p in c if isinstance(c, list) else []:
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
            parts.append(p["text"])
    return "\n".join(parts)


def parse(path):
    try:
        with open(path, errors="ignore") as fh:
            sess = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    msgs = []
    for item in sess.get("history", []) if isinstance(sess, dict) else []:
        m = item.get("message") if isinstance(item, dict) else None
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), _content(m.get("content"))
        if not text.strip():
            continue
        if role == "user":
            msgs.append({"role": "user", "text": text})
        elif role == "assistant":
            msgs.append({"role": "assistant", "text": text})
        elif role == "thinking":
            msgs.append({"role": "thinking", "text": text})
    return msgs
