"""Read-only parser for GitHub Copilot Chat (VS Code core chat storage).

Sessions live at <base>/User/workspaceStorage/<hash>/chatSessions/<id>.json (flat
ISerializableChatData) or .jsonl (append-only mutation log; line 1 kind:0 Initial
holds the full snapshot — we read that and ignore later mutations, a slightly-
stale but safe degrade). Also globalStorage/emptyWindowChatSessions/. cwd from the
sibling workspace.json {folder}. Turn text: requests[].message.text (user),
requests[].response[].value (assistant markdown parts). Degrade to empty.
"""
import glob
import json
import os
from urllib.parse import unquote, urlparse

_BASES = [os.path.expanduser(f"~/Library/Application Support/{b}/User")
          for b in ("Code", "Code - Insiders", "VSCodium")]


def available():
    for b in _BASES:
        if glob.glob(os.path.join(b, "workspaceStorage", "*", "chatSessions", "*")) \
                or glob.glob(os.path.join(b, "globalStorage", "emptyWindowChatSessions", "*")):
            return True
    return False


def _session_files():
    out = []
    for b in _BASES:
        out += glob.glob(os.path.join(b, "workspaceStorage", "*", "chatSessions", "*.json"))
        out += glob.glob(os.path.join(b, "workspaceStorage", "*", "chatSessions", "*.jsonl"))
        out += glob.glob(os.path.join(b, "globalStorage", "emptyWindowChatSessions", "*.json*"))
    return out


def _apply(base, path, value, kind, index):
    """Apply one object-mutation-log op (Set/Push/Delete) to `base` by path."""
    if not path:
        return
    node = base
    for key in path[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return
    last = path[-1]
    try:
        if kind == 1:            # Set
            node[last] = value
        elif kind == 2:          # Push: v is a LIST; with i, truncate to i then extend
            if isinstance(node, dict) and not isinstance(node.get(last), list):
                node[last] = []
            target = node[last]
            vals = value if isinstance(value, list) else ([] if value is None else [value])
            if index is not None:
                del target[index:]
            target.extend(vals)
        elif kind == 3:          # Delete
            if isinstance(node, dict):
                node.pop(last, None)
            else:
                del node[last]
    except (KeyError, IndexError, TypeError, AttributeError):
        return


def _load(path):
    """Full ISerializableChatData dict from a .json (flat) or .jsonl (Initial
    snapshot + replayed Set/Push/Delete mutations — evolving conversations)."""
    try:
        if path.endswith(".jsonl"):
            base = None
            with open(path, errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(o, dict):
                        continue
                    kind = o.get("kind")
                    if kind == 0 and isinstance(o.get("v"), dict):
                        base = o["v"]            # snapshot (a later Initial replaces)
                    elif base is not None and kind in (1, 2, 3):
                        _apply(base, o.get("k") or [], o.get("v"), kind, o.get("i"))
                    elif base is None and "requests" in o:
                        base = o                 # bare object fallback
            return base or {}
        with open(path, errors="ignore") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _workspace_cwd(path):
    # …/workspaceStorage/<hash>/chatSessions/<id> → …/<hash>/workspace.json
    ws = os.path.join(os.path.dirname(os.path.dirname(path)), "workspace.json")
    try:
        with open(ws) as fh:
            folder = (json.load(fh) or {}).get("folder", "")
        if folder.startswith("file://"):
            folder = unquote(urlparse(folder).path)
        return folder
    except (OSError, json.JSONDecodeError):
        return ""


def _head_meta(path):
    """(title, wd, is_session) cheaply — flat-json head or the .jsonl Initial line,
    WITHOUT replaying the whole mutation log (that's parse()'s job)."""
    try:
        if path.endswith(".jsonl"):
            with open(path, errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    o = json.loads(line)
                    if not isinstance(o, dict):
                        return "", "", False
                    v = o.get("v") if o.get("kind") == 0 else o
                    if isinstance(v, dict):
                        return (v.get("customTitle") or v.get("computedTitle") or "",
                                _clean(v.get("workingDirectory")), "requests" in v)
                    return "", "", False
        with open(path, errors="ignore") as fh:
            head = fh.read(65536)
        import re as _re
        t = _re.search(r'"(?:customTitle|computedTitle)"\s*:\s*"((?:[^"\\]|\\.){0,200})"', head)
        wd = _re.search(r'"workingDirectory"\s*:\s*"((?:[^"\\]|\\.){0,400})"', head)
        # a file living in chatSessions/ IS a chat session regardless of head window
        return ((json.loads(f'"{t.group(1)}"') if t else ""),
                _clean(json.loads(f'"{wd.group(1)}"')) if wd else "", True)
    except (OSError, json.JSONDecodeError, ValueError):
        return "", "", False


def discover():
    if not available():
        return []
    out = []
    for path in _session_files():
        title, wd, is_sess = _head_meta(path)
        if not is_sess:
            continue
        ts = os.path.getmtime(path) if os.path.isfile(path) else 0
        out.append({"id": path, "title": " ".join(str(title).split()),
                    "cwd": _workspace_cwd(path) or wd, "ts": ts})
    return out


def _clean(w):
    w = w or ""
    if w.startswith("file://"):
        w = unquote(urlparse(w).path)
    return w


def _msg_text(message):
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return message.get("text", "")
    return ""


def _resp_text(response):
    parts = []
    for p in response if isinstance(response, list) else []:
        if isinstance(p, dict) and isinstance(p.get("value"), str):
            parts.append(p["value"])
        elif isinstance(p, str):
            parts.append(p)
    return "\n".join(parts)


def parse(path):
    data = _load(path)
    msgs = []
    for r in data.get("requests", []) if isinstance(data, dict) else []:
        if not isinstance(r, dict):
            continue
        u = _msg_text(r.get("message")).strip()
        if u:
            msgs.append({"role": "user", "text": u})
        a = _resp_text(r.get("response")).strip()
        if a:
            msgs.append({"role": "assistant", "text": a})
    return msgs
