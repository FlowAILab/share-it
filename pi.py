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

def _root():
    env = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if env:
        return os.path.expanduser(env)
    base = os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi"
    return os.path.join(os.path.expanduser(base), "agent", "sessions")


ROOT = _root()


def available():
    return os.path.isdir(ROOT)


def _files():
    return glob.glob(os.path.join(ROOT, "*", "*.jsonl"))


def _split_blocks(content):
    """(visible_text, thinking_text) — thinking is a distinct block, kept apart
    so default (non-deep) exports can hide reasoning."""
    if isinstance(content, str):
        return content, ""
    text, think = [], []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "output_text", "input_text") and b.get("text"):
            text.append(b["text"])
        elif t == "thinking":
            v = b.get("thinking")
            if isinstance(v, dict):
                v = v.get("text", "")
            if isinstance(v, str) and v:
                think.append(v)
    return "\n".join(text), "\n".join(think)


def _header(path):
    """(cwd, title, ts) from the first line + any session_info name."""
    cwd = title = first_user = ""
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
                elif (not first_user and o.get("type") == "message"
                      and (o.get("message") or {}).get("role") == "user"):
                    txt, _ = _split_blocks((o.get("message") or {}).get("content"))
                    if txt.strip():
                        first_user = " ".join(txt.split())[:120]
    except OSError:
        pass
    return cwd, title or first_user, ts


def discover():
    if not available():
        return []
    out = []
    for path in _files():
        cwd, title, ts = _header(path)
        mt = os.path.getmtime(path) if os.path.isfile(path) else ts
        out.append({"id": path, "title": title or "(untitled)",
                    "cwd": cwd, "ts": mt})
    return out


def _first_user(path):
    for m in parse(path):
        if m["role"] == "user" and m["text"].strip():
            return " ".join(m["text"].split())[:120]
    return ""


def parse(path):
    # Pi files are branching trees (id/parentId). Walk the newest leaf's ancestry
    # so we render one coherent conversation, not merged sibling branches.
    entries = {}
    order = []
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
                eid = o.get("id")
                if eid is not None:
                    entries[eid] = o
                    order.append(eid)
    except OSError:
        return []
    if not entries:
        return []
    parents = {e.get("id"): e.get("parentId") for e in entries.values()}
    referenced = {p for p in parents.values() if p is not None}
    leaves = [eid for eid in order if eid not in referenced]
    leaf = leaves[-1] if leaves else order[-1]
    chain, seen, cur = [], set(), leaf   # walk to root, then reverse
    while cur is not None and cur in entries and cur not in seen:
        seen.add(cur)
        chain.append(entries[cur])
        cur = parents.get(cur)
    chain.reverse()
    msgs = []
    for o in chain:
        if o.get("type") != "message":
            continue
        m = o.get("message") or {}
        role = m.get("role")
        text, think = _split_blocks(m.get("content"))
        if role == "user" and text.strip():
            msgs.append({"role": "user", "text": text})
        elif role == "assistant":
            if think.strip():
                msgs.append({"role": "thinking", "text": think})
            if text.strip():
                msgs.append({"role": "assistant", "text": text})
        elif role == "toolResult" and text.strip():
            msgs.append({"role": "tool", "name": "tool", "input": "",
                         "output": text[:4000], "ok": True})
    return msgs
