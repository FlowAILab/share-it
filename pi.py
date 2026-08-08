"""Read-only parser for the Pi coding agent (badlogic/pi-mono, pi.dev).

Sessions are JSONL under ~/.pi/agent/sessions/--<cwd>--/<ts>_<uuid>.jsonl:
  line 1     {"type":"session","id","timestamp","cwd", ...}
  message    {"type":"message","message":{"role","content"}, "timestamp", ...}
  info       {"type":"session_info","name": <human title>}
`content` is a string OR a list of typed blocks (text blocks carry "text").
The file is a branching tree; we render it in file order (good enough for a
readable transcript). Every field is optional — a shape mismatch degrades to
empty rather than raising.
"""
import glob
import json
import os

ROOT = os.path.expanduser("~/.pi/agent/sessions")


def available():
    return os.path.isdir(ROOT)


def _files():
    return glob.glob(os.path.join(ROOT, "*", "*.jsonl"))


def _block_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "output_text", "input_text") and b.get("text"):
            parts.append(b["text"])
        elif t == "thinking" and isinstance(b.get("thinking"), str):
            parts.append(b["thinking"])
    return "\n".join(p for p in parts if p)


def _header(path):
    """(cwd, title, ts) from the first line + any session_info name."""
    cwd = title = ""
    ts = os.path.getmtime(path) if os.path.isfile(path) else 0
    try:
        with open(path, errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 400:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") == "session" and not cwd:
                    cwd = o.get("cwd", "") or ""
                elif o.get("type") == "session_info" and o.get("name"):
                    title = o["name"]
    except OSError:
        pass
    return cwd, title, ts


def discover():
    if not available():
        return []
    out = []
    for path in _files():
        cwd, title, ts = _header(path)
        if not title:  # fall back to the first user line
            title = _first_user(path)
        out.append({"id": path, "title": title or "(untitled)",
                    "cwd": cwd, "ts": ts})
    return out


def _first_user(path):
    for m in parse(path):
        if m["role"] == "user" and m["text"].strip():
            return " ".join(m["text"].split())[:120]
    return ""


def parse(path):
    msgs = []
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") != "message":
                    continue
                m = o.get("message") or {}
                role = m.get("role")
                text = _block_text(m.get("content"))
                if not text.strip():
                    continue
                if role == "user":
                    msgs.append({"role": "user", "text": text})
                elif role == "assistant":
                    msgs.append({"role": "assistant", "text": text})
                elif role == "toolResult":
                    msgs.append({"role": "tool", "name": "tool",
                                 "input": "", "output": text[:4000], "ok": True})
    except OSError:
        pass
    return msgs
