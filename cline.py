"""Read-only parser for Cline and its fork Roo Code (both VS Code extensions).

Each task is a dir <base>/globalStorage/<extid>/tasks/<taskId>/ (Cline v4 also
~/.cline/data/tasks/) holding api_conversation_history.json (Anthropic Messages)
and ui_messages.json (webview log; source of the title). Parameterized by extid
so one module serves both. Read-only, degrade to empty on any mismatch.
"""
import glob
import json
import os
import re

# VS Code family app-support bases (extension subtree is identical across forks)
_BASES = [os.path.expanduser(f"~/Library/Application Support/{b}/User/globalStorage")
          for b in ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf")]

_TASK_TEXT = re.compile(r'"say"\s*:\s*"task".{0,40}?"text"\s*:\s*"((?:[^"\\]|\\.){0,300})"', re.S)


def _task_dirs(extid):
    dirs = []
    roots = [os.path.join(b, extid, "tasks") for b in _BASES]
    if extid == "saoudrizwan.claude-dev":
        roots.append(os.path.expanduser(
            os.path.join(os.environ.get("CLINE_DATA_DIR")
                         or os.path.expanduser("~/.cline/data"), "tasks")))
    for r in roots:
        if os.path.isdir(r):
            for d in glob.glob(os.path.join(r, "*")):
                if os.path.isfile(os.path.join(d, "api_conversation_history.json")):
                    dirs.append(d)
    return dirs


def available(extid):
    return any(os.path.isdir(os.path.join(b, extid, "tasks")) for b in _BASES) \
        or (extid == "saoudrizwan.claude-dev"
            and os.path.isdir(os.path.expanduser("~/.cline/data/tasks")))


def _title(task_dir):
    ui = os.path.join(task_dir, "ui_messages.json")
    try:
        if os.path.getsize(ui) < 512_000:      # small → parse cleanly
            with open(ui, errors="ignore") as fh:
                arr = json.load(fh)
            for e in arr if isinstance(arr, list) else []:
                if isinstance(e, dict) and e.get("say") == "task" and e.get("text"):
                    return " ".join(e["text"].split())[:120]
        else:                                   # big (images) → bounded regex
            with open(ui, errors="ignore") as fh:
                head = fh.read(256_000)
            m = _TASK_TEXT.search(head)
            if m:
                return " ".join(json.loads(f'"{m.group(1)}"').split())[:120]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return ""


def discover(extid):
    out = []
    for d in _task_dirs(extid):
        tid = os.path.basename(d)
        ts = os.path.getmtime(d)   # dir mtime reflects the latest write
        out.append({"id": d, "title": _title(d), "cwd": "", "ts": ts})
    return out


_INJECT = re.compile(r"</?task>|<environment_details>.*?</environment_details>", re.S)


def _content(c):
    if isinstance(c, str):
        return c
    parts = []
    for b in c if isinstance(c, list) else []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts)


def parse(task_dir):
    path = os.path.join(task_dir, "api_conversation_history.json")
    try:
        with open(path, errors="ignore") as fh:
            arr = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    msgs = []
    for m in arr if isinstance(arr, list) else []:
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), _content(m.get("content"))
        text = _INJECT.sub("", text).strip()
        if not text:
            continue
        if role in ("user", "assistant"):
            msgs.append({"role": role, "text": text})
    return msgs
