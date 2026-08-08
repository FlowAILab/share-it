"""Parse Claude Code and Codex session transcripts into a unified message model.

Unified message: {"role": "user"|"assistant"|"thinking"|"tool", ...}
  user/assistant/thinking: {"role", "text"}
  tool: {"role": "tool", "name", "input", "output"}
"""
import datetime as _dt
import glob
import json
import os
import re
import sqlite3

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_CLAUDE_HOME = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
_CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CLAUDE_ROOT = os.path.join(_CLAUDE_HOME, "projects")
CODEX_ROOT = os.path.join(_CODEX_HOME, "sessions")
CODEX_ARCHIVE = os.path.join(_CODEX_HOME, "archived_sessions")
# Claude desktop (Cowork / agent mode) session metadata — points at CLI session ids
COWORK_META = os.path.expanduser("~/Library/Application Support/Claude")
# Rescue archive: Claude Code purges transcripts after ~30 days; we keep copies here
RESCUE_ROOT = os.path.expanduser("~/.shareit/archive")


def cowork_session_ids():
    """Session ids that originated from the Claude desktop app's agent mode (Cowork).

    Only local-agent-mode-sessions counts: claude-code-sessions also gains entries
    when the desktop merely imports/opens a CLI session (e.g. via claude://resume).
    """
    ids = set()
    for sub in ("local-agent-mode-sessions",):
        pattern = os.path.join(glob.escape(os.path.join(COWORK_META, sub)), "**", "local_*.json")
        for p in glob.glob(pattern, recursive=True):
            try:
                with open(p) as fh:
                    o = json.load(fh)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            for key in ("cliSessionId", "sessionId"):
                v = o.get(key)
                if isinstance(v, str):
                    ids.add(v.removeprefix("local_"))
    return ids

# Injected records that surface with role=user but are not the human speaking:
# plugin manifests, environment/browser context, command wrappers, notifications.
_INJECTED_RE = re.compile(
    r"^\s*<(?:environment_context|user_instructions|turn_context|ide_context|"
    r"permissions|app_context|recommended_plugins|browser_context|skills|"
    r"internal|aborted|turn_aborted|command-name|command-message|local-command-stdout|"
    r"local-command-caveat|system-reminder|task-notification|command-args|"
    r"in-app-browser-context|codex_internal_context|subagent_notification|"
    r"[a-z][a-z0-9_-]*(?:_context|_notification|_instructions|_manifest|_reminder))\b")


def _is_injected(text):
    return bool(_INJECTED_RE.match(text or ""))


_CODEX_CONTEXT_PREFIXES = ("<",)  # legacy alias; real check is _is_injected


def _content_text(content):
    """Flatten a Codex content list (input_text/output_text blocks) to text."""
    if isinstance(content, str):
        return _ANSI.sub("", content)
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
        elif t == "input_image":
            parts.append("[image]")
    return _ANSI.sub("", "\n".join(p for p in parts if p))


def _iter_jsonl(path):
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _is_claude_subagent_file(path):
    base = os.path.basename(path)
    return base.startswith("agent-") or f"{os.sep}subagents{os.sep}" in path


# Inline media limits: enough for screenshots/pastes, never a transcript bomb.
MEDIA_MAX_ONE = 5_000_000       # b64 chars per image (~3.7MB decoded)
MEDIA_MAX_PER_SESSION = 40

# Codex wraps pasted images in <image src="/Users/…/x.png"> markers — local
# paths leak usernames, so exports must never carry them.
_IMAGE_MARKER = re.compile(r"</?image\b[^>]*>")


def _media_entry(media_type, data_b64, counter):
    """Validated inline-image entry, or None (over cap / empty / malformed —
    remote-control sessions are known to persist empty base64 sources).
    Malformed data must not consume the session cap."""
    if not (isinstance(data_b64, str) and 0 < len(data_b64) <= MEDIA_MAX_ONE):
        return None
    if counter[0] >= MEDIA_MAX_PER_SESSION:
        return None
    import base64 as _b64
    try:
        if not _b64.b64decode(data_b64, validate=True):
            return None
    except (ValueError, TypeError):
        return None
    counter[0] += 1
    return {"media_type": media_type or "image/png", "data": data_b64}


def _data_url_media(url, counter):
    """'data:image/png;base64,....' → media entry, else None."""
    if not (isinstance(url, str) and url.startswith("data:image/")):
        return None
    head, sep, b64 = url.partition(";base64,")
    if not sep:
        return None
    return _media_entry(head[5:], b64, counter)


def parse_claude(path):
    messages = []
    tools_by_id = {}
    media_count = [0]
    keep_sidechain = _is_claude_subagent_file(path)  # subagent files are all sidechain
    for obj in _iter_jsonl(path):
        t = obj.get("type")
        if t not in ("user", "assistant") or (obj.get("isSidechain") and not keep_sidechain):
            continue
        if obj.get("isMeta"):
            continue
        content = (obj.get("message") or {}).get("content")
        if t == "user":
            if isinstance(content, str):
                if content.strip():
                    messages.append({"role": "user", "text": content})
                continue
            for block in content or []:
                bt = block.get("type")
                if bt == "text" and block.get("text", "").strip():
                    messages.append({"role": "user", "text": block["text"]})
                elif bt == "image":
                    src = block.get("source") or {}
                    m = (_media_entry(src.get("media_type"), src.get("data"), media_count)
                         if src.get("type") == "base64" else None)
                    msg = {"role": "user", "text": "[image]"}
                    if m:
                        msg["media"] = [m]
                    messages.append(msg)
                elif bt == "tool_result":
                    tool = tools_by_id.get(block.get("tool_use_id"))
                    if tool is not None:
                        rc = block.get("content")
                        if isinstance(rc, list):
                            rc = _content_text(rc)
                        tool["output"] = rc if isinstance(rc, str) else json.dumps(rc)
                        tool["ok"] = not block.get("is_error", False)
        else:  # assistant
            for block in content or []:
                bt = block.get("type")
                if bt == "thinking":
                    messages.append({"role": "thinking", "text": block.get("thinking", "")})
                elif bt == "text":
                    messages.append({"role": "assistant", "text": block.get("text", "")})
                elif bt == "tool_use":
                    tool = {"role": "tool", "name": block.get("name", "?"),
                            "input": json.dumps(block.get("input", {}), indent=None)[:100000],
                            "output": "", "ok": False}
                    tools_by_id[block.get("id")] = tool
                    messages.append(tool)
    return messages


# response_item payload types (also seen bare, un-wrapped, in pre-2025 rollout files)
_CODEX_ITEM_TYPES = {"message", "agent_message", "reasoning", "function_call",
                     "custom_tool_call", "local_shell_call",
                     "function_call_output", "custom_tool_call_output"}


def parse_codex(path):
    messages = []
    tools_by_call = {}
    media_count = [0]
    fallback_users = []  # event_msg user_message, used only if response_items had none
    saw_user = False
    for obj in _iter_jsonl(path):
        t = obj.get("type")
        payload = obj.get("payload") or {}
        if t in _CODEX_ITEM_TYPES:  # old bare format: the line IS the payload
            t, payload = "response_item", obj
        if t == "event_msg":
            if payload.get("type") == "user_message":
                fallback_users.append({"role": "user", "text": payload.get("message", "")})
            continue
        if t != "response_item":
            continue
        pt = payload.get("type")
        if pt == "message":
            text = _IMAGE_MARKER.sub("", _content_text(payload.get("content")))
            role = payload.get("role")
            media = [m for m in (
                _data_url_media(b.get("image_url"), media_count)
                for b in (payload.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "input_image"
            ) if m]
            if role == "user":
                stripped = text.lstrip()
                if (stripped and not stripped.startswith(_CODEX_CONTEXT_PREFIXES)) or media:
                    msg = {"role": "user", "text": text}
                    if media:
                        msg["media"] = media
                    messages.append(msg)
                    saw_user = True
            elif role == "assistant" and text.strip():
                messages.append({"role": "assistant", "text": text})
        elif pt == "agent_message":
            text = _content_text(payload.get("content")) or payload.get("message", "")
            if text.strip():
                messages.append({"role": "assistant", "text": text})
        elif pt == "reasoning":
            summary = "\n".join(s.get("text", "") for s in payload.get("summary") or [])
            if summary.strip():
                messages.append({"role": "thinking", "text": summary})
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            tool = {"role": "tool", "name": payload.get("name", pt),
                    "input": str(payload.get("arguments") or payload.get("input") or "")[:100000],
                    "output": ""}
            tools_by_call[payload.get("call_id") or payload.get("id")] = tool
            messages.append(tool)
        elif pt in ("function_call_output", "custom_tool_call_output"):
            tool = tools_by_call.get(payload.get("call_id"))
            if tool is not None:
                out = payload.get("output")
                if isinstance(out, list):
                    out = _content_text(out)
                elif isinstance(out, dict):
                    out = out.get("output") or json.dumps(out)
                tool["output"] = out if isinstance(out, str) else str(out)
        elif pt and pt.endswith("_call"):  # web_search_call, image_generation_call, …
            action = payload.get("action") or {}
            messages.append({"role": "tool", "name": pt.replace("_call", ""),
                             "input": json.dumps(action)[:2000] if action else "",
                             "output": str(payload.get("status") or "")})
    if not saw_user and fallback_users:
        messages = fallback_users + messages
    return messages


def parse_session(path):
    real = os.path.realpath(path)
    if (real.startswith(os.path.realpath(CLAUDE_ROOT) + os.sep)
            or real.startswith(os.path.realpath(RESCUE_ROOT) + os.sep)):
        return parse_claude(real)
    if (real.startswith(os.path.realpath(CODEX_ROOT) + os.sep)
            or real.startswith(os.path.realpath(CODEX_ARCHIVE) + os.sep)):
        return parse_codex(real)
    if "#" in path:
        import adapters as _ad
        for ad in _ad.ADAPTERS:
            if ad.owns(path):
                return ad.parse(path)
    raise ValueError("path outside transcript roots")


_CODEX_PATCH_RE = re.compile(r"\*\*\* (Add|Update) File: (.+)")
# codex writes most outputs via shell, not apply_patch — sniff artifact-looking paths
_ARTIFACT_EXT = r"(?:pdf|png|jpe?g|svg|gif|webp|md|html?|csv|tsv|json|zip|docx?|pptx?|xlsx?|txt|mp4|webm|ipynb)"
_CODEX_PATH_RES = [re.compile(r'"(/[^"\n]{3,240}?\.' + _ARTIFACT_EXT + r')"'),
                   re.compile(r"(?<![\w\"'])(/[^\s\"'`,;)\]}]{3,240}?\." + _ARTIFACT_EXT + r")\b")]
# tool name → (input key, artifact kind)
_WRITE_TOOLS = {"Write": ("file_path", "created"), "Edit": ("file_path", "modified"),
                "MultiEdit": ("file_path", "modified"),
                "NotebookEdit": ("notebook_path", "modified")}


ARTIFACT_MAX_BYTES = 25_000_000


def _codex_structured_writes(path):
    """Authoritative file writes: patch_apply_end events carry a changes map."""
    found = {}
    for obj in _iter_jsonl(path):
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") not in ("patch_apply_end", "patch_apply_begin"):
            continue
        if payload.get("type") == "patch_apply_end" and payload.get("success") is False:
            continue
        changes = payload.get("changes") or {}
        if not isinstance(changes, dict):
            continue
        for fpath, change in changes.items():
            if not isinstance(change, dict):
                continue
            kind = "created" if "add" in change else "modified"
            if found.get(fpath) != "created":
                found[fpath] = kind
    return found


def _codex_generated_images(path):
    """Codex Desktop stores generated images per thread under generated_images/."""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
                  os.path.basename(path))
    if not m:
        return []
    return glob.glob(os.path.join(glob.escape(
        os.path.join(_CODEX_HOME, "generated_images", m.group(1))), "*"))


def session_artifacts(path, limit=50, source=None, cwd=None):
    """Files the agent successfully wrote during this session that still exist.

    Containment: only files under the session's cwd (when known) are offered —
    a stray write elsewhere on disk is not a shareable "artifact".
    """
    found = {}     # path → kind ("created" beats "modified")
    declared = {}  # realpath → True: files the session formally published
    real = os.path.realpath(path)
    is_claude = (source == "claude" if source
                 else real.startswith(os.path.realpath(CLAUDE_ROOT) + os.sep))
    root = os.path.realpath(cwd) + os.sep if cwd else None
    if not is_claude:
        found.update(_codex_structured_writes(real))
    for m in (parse_claude if is_claude else parse_codex)(real) if source else parse_session(path):
        if m["role"] != "tool":
            continue
        candidates = []
        if is_claude and m["name"] == "Artifact" and m.get("ok"):
            # OFFICIALLY DECLARED artifact — the session published this file.
            # Highest rank, containment-exempt (scratchpads live outside cwd).
            try:
                fp = json.loads(m["input"]).get("file_path")
            except (json.JSONDecodeError, AttributeError):
                fp = None
            if fp:
                declared[os.path.realpath(fp)] = True
            continue
        if is_claude and m["name"] in _WRITE_TOOLS:
            if not m.get("ok"):  # denied or failed writes are not artifacts
                continue
            key, kind = _WRITE_TOOLS[m["name"]]
            try:
                candidates = [(json.loads(m["input"]).get(key), kind)]
            except (json.JSONDecodeError, AttributeError):
                candidates = []
        elif not is_claude:
            if not (m.get("output") or "").strip():
                continue
            candidates = [(p, "created" if op == "Add" else "modified")
                          for op, p in _CODEX_PATCH_RE.findall(m["input"] or "")]
            # heuristic for shell writes: only paths in known OUTPUT positions count
            # (redirect target, -o/--output value, tee target, final cp/mv argument)
            for line in ((m["input"] or "") + "\n" + (m.get("output") or "")[:4000]).splitlines():
                paths = [mm.group(1) for pat in _CODEX_PATH_RES
                         for mm in pat.finditer(line)]
                if not paths:
                    continue
                low = line.lower()
                out_markers = (">", "-o ", "--output", "output=", "tee ")
                if any(k in low for k in out_markers):
                    for p in paths:
                        i = line.find(p)
                        before = line[max(0, i - 14):i].lower()
                        if any(k in before for k in out_markers):
                            candidates.append((p, "created"))
                elif re.match(r"\s*(?:cp|mv)\b", low) and len(paths) >= 2:
                    candidates.append((paths[-1], "created"))  # destination only
        for c, kind in candidates:
            if not c:
                continue
            if found.get(c) == "created":
                continue
            found[c] = "created" if kind == "created" or found.get(c) == "created" else kind
    gen_images = [] if is_claude else _codex_generated_images(real)
    if not root and not gen_images and not declared:
        return []  # unknown project root → no safe containment → no auto-artifacts
    out = []
    for d in declared:  # formally published — always first, wherever they live
        try:
            st = os.stat(d)
        except OSError:
            continue
        out.append({"path": d, "name": os.path.basename(d), "size": st.st_size,
                    "kind": "created", "declared": True, "mtime": st.st_mtime})
    for g in gen_images:  # our own store — containment-exempt, always created
        try:
            st = os.stat(g)
        except OSError:
            continue
        out.append({"path": g, "name": os.path.basename(g), "size": st.st_size,
                    "kind": "created", "mtime": st.st_mtime})
    out.sort(key=lambda a: -a.get("mtime", 0))  # freshest-first even root-less
    if not root:
        return out[:limit]
    seen_rc = {a["path"] for a in out}
    home = os.path.realpath(os.path.expanduser("~")) + os.sep
    resolved = {}  # realpath → kind; "created" wins over "modified" across path spellings
    for c, kind in found.items():
        rc = os.path.realpath(c if os.path.isabs(c) else os.path.join(cwd, c))
        # CREATED files count wherever they live (the agent verifiably made
        # them) as long as they're the user's own; modified stay root-contained
        contained = rc.startswith(root)
        if not contained and not (kind == "created" and rc.startswith(home)
                                  and "/Library/" not in rc and ".app/" not in rc
                                  and "/Applications/" not in rc
                                  and "/." not in rc[len(home) - 1:]):
            continue
        if rc in seen_rc:
            continue
        if kind == "created" or rc not in resolved:
            resolved[rc] = kind
    for rc, kind in resolved.items():
        if os.path.isfile(rc):
            try:
                st = os.stat(rc)
            except OSError:
                continue
            if st.st_size > ARTIFACT_MAX_BYTES:
                continue
            out.append({"path": rc, "name": os.path.basename(rc), "size": st.st_size,
                        "kind": kind, "mtime": st.st_mtime})
            if len(out) >= limit:
                break
    out.sort(key=lambda a: -a.get("mtime", 0))  # most-recently-written first
    return out


def session_reads(path, source=None, cwd=None, limit=40):
    """Files the agent read (names+sizes only — context manifest, never uploaded)."""
    seen, out = set(), []
    real = os.path.realpath(path)
    is_claude = (source == "claude" if source
                 else real.startswith(os.path.realpath(CLAUDE_ROOT) + os.sep))
    if not is_claude:
        return []  # codex reads happen via shell; too noisy to attribute reliably
    root = os.path.realpath(cwd) + os.sep if cwd else None
    for m in parse_claude(real):
        if m["role"] != "tool" or m["name"] != "Read" or not m.get("ok"):
            continue
        try:
            fp = json.loads(m["input"]).get("file_path")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not fp or fp in seen:
            continue
        seen.add(fp)
        rfp = os.path.realpath(fp)
        if root and not rfp.startswith(root):
            continue
        try:
            size = os.stat(rfp).st_size
        except OSError:
            size = None
        out.append({"path": fp, "name": os.path.basename(fp), "size": size})
        if len(out) >= limit:
            break
    return out


def session_stats(path, messages):
    """Cheap stats for the share card: duration from first/last line timestamps."""
    first = last = None
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536).decode("utf-8", errors="ignore")
            fh.seek(max(0, os.path.getsize(path) - 65536))
            tail = fh.read().decode("utf-8", errors="ignore")
        for chunk, keep_first in ((head, True), (tail, False)):
            for line in chunk.splitlines():
                try:
                    ts = json.loads(line).get("timestamp")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(ts, str) and len(ts) >= 19:
                    if keep_first and first is None:
                        first = ts
                    elif not keep_first:
                        last = ts
    except OSError:
        pass
    minutes = None
    if first and last:
        try:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%S"
            minutes = max(0, int((datetime.strptime(last[:19], fmt)
                                  - datetime.strptime(first[:19], fmt)).total_seconds() // 60))
        except ValueError:
            minutes = None
    return {
        "turns": sum(1 for m in messages if m["role"] == "user"),
        "tools": sum(1 for m in messages if m["role"] == "tool"),
        "minutes": minutes,
    }


def peek_session(path, max_texts=6, clip=280, max_bytes=6_000_000):
    """First few user/assistant texts — streams the file head, never a full parse.

    Byte-capped, not line-capped: codex rollouts can carry multi-MB base64 lines.
    """
    real = os.path.realpath(path)
    is_claude = (real.startswith(os.path.realpath(CLAUDE_ROOT) + os.sep)
                 or real.startswith(os.path.realpath(RESCUE_ROOT) + os.sep))
    out, consumed = [], 0

    def emit(role, text):
        text = " ".join(_ANSI.sub("", text or "").split())
        if text:
            out.append({"role": role, "text": text[:clip] + ("…" if len(text) > clip else "")})

    def _lines():
        nonlocal consumed
        with open(real, errors="ignore") as fh:
            for line in fh:
                consumed += len(line)
                if len(line) > 2_000_000:
                    continue  # base64 blob line — never a chat message
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
                if consumed > max_bytes:
                    return

    for obj in _lines():
        if len(out) >= max_texts:
            break
        if is_claude:
            t = obj.get("type")
            if t not in ("user", "assistant") or obj.get("isSidechain"):
                continue
            c = (obj.get("message") or {}).get("content")
            if t == "user" and isinstance(c, str):
                if not _is_injected(_ANSI.sub("", c).lstrip()):
                    emit("user", c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = b.get("text")
                        if t == "user" and _is_injected(_ANSI.sub("", txt or "").lstrip()):
                            continue
                        emit("user" if t == "user" else "assistant", txt)
        else:
            payload = obj.get("payload") or {}
            if obj.get("type") != "response_item":
                continue
            pt = payload.get("type")
            if pt == "message":
                text = _content_text(payload.get("content"))
                role = payload.get("role")
                if role == "user" and not _is_injected(text.lstrip()):
                    emit("user", text)
                elif role == "assistant":
                    emit("assistant", text)
            elif pt == "agent_message":
                emit("assistant", _content_text(payload.get("content")) or payload.get("message", ""))
    return out[:max_texts]


def peek_last_answer(path, clip=480, tail_bytes=16_000_000):
    """The final assistant message, cheaply: scan the file TAIL only — never a
    full parse (transcripts reach tens of MB)."""
    real = os.path.realpath(path)
    is_claude = (real.startswith(os.path.realpath(CLAUDE_ROOT) + os.sep)
                 or real.startswith(os.path.realpath(RESCUE_ROOT) + os.sep))
    is_subagent = is_claude and _is_claude_subagent_file(real)
    try:
        size = os.path.getsize(real)
        with open(real, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            raw = fh.read()
    except OSError:
        return ""
    lines = raw.decode(errors="ignore").splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # first line is almost certainly cut mid-record
    last = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if is_claude:
            # sidechain records ARE the conversation inside a subagent transcript
            if obj.get("type") != "assistant" or (obj.get("isSidechain") and not is_subagent):
                continue
            c = (obj.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                        last = b["text"]
        else:
            payload = obj.get("payload") or {}
            # bare records (older codex rollouts have no response_item wrapper)
            if obj.get("type") == "message" and obj.get("role") == "assistant":
                t = _content_text(obj.get("content"))
                if t.strip():
                    last = t
                continue
            if obj.get("type") == "agent_message":
                t = _content_text(obj.get("content")) or obj.get("message", "")
                if t.strip():
                    last = t
                continue
            if obj.get("type") != "response_item":
                continue
            pt = payload.get("type")
            if pt == "message" and payload.get("role") == "assistant":
                t = _content_text(payload.get("content"))
                if t.strip():
                    last = t
            elif pt == "agent_message":
                t = _content_text(payload.get("content")) or payload.get("message", "")
                if t.strip():
                    last = t
    last = " ".join(_ANSI.sub("", last).split())
    return last[:clip] + ("…" if len(last) > clip else "")


# ---------------- index (session list) ----------------

def _slug_to_cwd(path):
    """Best-effort cwd from the Claude project dir slug (lossy: - was any non-alnum)."""
    slug = os.path.basename(os.path.dirname(os.path.realpath(path)))
    if not slug.startswith("-"):
        return ""
    cand = "/" + slug[1:].replace("-", "/")
    return cand if os.path.isdir(cand) else ""


def _claude_cwd_scan(path, cap=8_000_000):
    consumed = 0
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                consumed += len(line)
                if consumed > cap:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    c = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if c:
                    return c
    except OSError:
        pass
    return _slug_to_cwd(path)


def _claude_meta(path):
    """Cheap metadata: head for first user msg + cwd, tail for ai-title."""
    keep_sidechain = _is_claude_subagent_file(path)
    title, cwd, subagent = None, None, keep_sidechain
    first_user = None
    with open(path, "rb") as fh:
        head = fh.read(131072).decode("utf-8", errors="ignore")
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 65536))
        tail = fh.read().decode("utf-8", errors="ignore")
    for chunk in (tail, head):
        for line in chunk.splitlines():
            if '"ai-title"' in line or '"custom-title"' in line:
                try:
                    o = json.loads(line)
                    title = o.get("aiTitle") or o.get("customTitle") or o.get("title")
                    if title:
                        break
                except json.JSONDecodeError:
                    continue
        if title:
            break
    branch = model = None
    for line in head.splitlines():
        if first_user and cwd and branch and model:
            break
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = cwd or o.get("cwd")
        branch = branch or o.get("gitBranch")
        if not model and o.get("type") == "assistant":
            model = (o.get("message") or {}).get("model")
        if not first_user and o.get("type") == "user" and (keep_sidechain or not o.get("isSidechain")):
            c = (o.get("message") or {}).get("content")
            if isinstance(c, str):
                first_user = c
            elif isinstance(c, list):
                for b in c:
                    if b.get("type") == "text":
                        first_user = b.get("text")
                        break
    if not cwd:  # continued sessions bury cwd past the head window — stream for it
        cwd = _claude_cwd_scan(path)
    return title or first_user, cwd, subagent, {"branch": branch or "", "model": model or ""}


def _codex_meta(path):
    title, cwd, subagent, thread = None, None, False, ""
    n = 0
    for obj in _iter_jsonl(path):
        n += 1
        if n > 400:
            break
        t = obj.get("type")
        payload = obj.get("payload") or {}
        if t == "session_meta":
            cwd = payload.get("cwd")
            thread = payload.get("id") or ""
            src = payload.get("source")
            subagent = isinstance(src, dict) and "subagent" in src
        elif t == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            text = _content_text(payload.get("content")).lstrip()
            if text and not _is_injected(text):
                title = text
                break
        elif t == "event_msg" and payload.get("type") == "user_message" and not title:
            title = payload.get("message", "")
            break
    return title, cwd, subagent, thread


def _codex_sqlite_index():
    """Read Codex's own thread index (state_*.sqlite) → {realpath: (title, cwd, subagent)}.

    Codex CLI and Desktop both maintain this; it saves us scanning file heads.
    Best-effort: any failure returns {} and we fall back to scanning.
    """
    index = {}
    for db in sorted(glob.glob(os.path.join(_CODEX_HOME, "state_*.sqlite")), reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT rollout_path, COALESCE(title, name, first_user_message, preview), "
                    "cwd, source, model, tokens_used, git_branch FROM threads").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        for rollout_path, title, cwd, source, model, tokens, branch in rows:
            if rollout_path:
                sub = bool(source and "subagent" in str(source))
                index[os.path.realpath(rollout_path)] = (
                    title, cwd, sub,
                    {"model": model or "", "tokens": tokens or 0, "branch": branch or ""})
        if index:
            break
    return index


SCHEMA_VERSION = 5  # bump when entry shape or title/meta extraction changes


def _parse_iso_ts(iso):
    """ISO-8601 → epoch. Timestamps without an offset are treated as UTC (both
    Claude Code and Codex write trailing-Z UTC stamps)."""
    if not isinstance(iso, str) or not iso:
        return None
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def last_event_ts(path, mtime):
    """Canonical last-used time: the newest top-level `timestamp` the harness
    itself wrote into the transcript. Filesystem mtime lies — rewrites, copies
    and backup tools bump it in batches — so it is only the fallback.

    Walks complete JSONL records from the tail (a small window first, a bigger
    one if the final records are huge), so nested `timestamp` fields inside tool
    payloads can't win and partial first lines are skipped."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return mtime
    # widen until the window covers the file — a single final record can exceed
    # any fixed window (codex base64 image lines), and mtime is a bad fallback
    window = 65536
    windows = []
    while True:
        windows.append(window)
        if window >= size:
            break
        window *= 8
    for window in windows:
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, size - window))
                raw = fh.read()
        except OSError:
            return mtime
        lines = raw.decode(errors="ignore").splitlines()
        if size > window and lines:
            lines = lines[1:]  # first line is cut mid-record
        best = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                ts = _parse_iso_ts(obj.get("timestamp") or obj.get("ts") or "")
                if ts is not None:
                    best = ts if best is None else max(best, ts)
        if best is not None:
            return best
    return mtime


def _codex_thread_of(path):
    """Stable conversation id: session_meta.id (survives resumes), else the
    filename uuid."""
    try:
        with open(path, errors="ignore") as fh:
            first = fh.readline()
        obj = json.loads(first)
        tid = (obj.get("payload") or {}).get("id")
        if isinstance(tid, str) and tid:
            return tid
    except (OSError, json.JSONDecodeError):
        pass
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  os.path.basename(path))
    return m.group(1) if m else ""


def scan_sessions(cache):
    """Walk both roots; return list of session dicts. `cache` maps path -> entry."""
    sessions = []
    codex_idx = _codex_sqlite_index()
    cowork_ids = cowork_session_ids()
    seen_names = set()
    for root, source in ((CLAUDE_ROOT, "claude"), (RESCUE_ROOT, "claude"),
                         (CODEX_ROOT, "codex"), (CODEX_ARCHIVE, "codex")):
        if not os.path.isdir(root):
            continue
        rescue = root == RESCUE_ROOT
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                if source == "claude" and rescue and name in seen_names:
                    continue  # original still alive — rescue copy is a shadow
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = path
                ent = cache.get(key)
                if (ent and ent["mtime"] == st.st_mtime and ent["size"] == st.st_size
                        and ent.get("v") == SCHEMA_VERSION):
                    ent["app"] = _app_for(ent, cowork_ids)
                    sessions.append(ent)
                    if source == "claude" and not rescue:
                        seen_names.add(name)  # cached originals must still shadow rescues
                    continue
                extra = {}
                try:
                    if source == "claude":
                        title, cwd, subagent, extra = _claude_meta(path)
                    elif os.path.realpath(path) in codex_idx:
                        title, cwd, subagent, extra = codex_idx[os.path.realpath(path)]
                    else:
                        title, cwd, subagent, _thread = _codex_meta(path)
                        extra = {"thread": _thread}
                except Exception:  # one unreadable/corrupt file must not break the index
                    continue
                if source == "claude" and _is_claude_subagent_file(path):
                    subagent = True
                title = " ".join((title or "").split())[:120] or "(untitled)"
                ent = {"path": path, "source": source, "title": title,
                       "cwd": cwd or "", "project": os.path.basename(cwd) if cwd else "",
                       "mtime": st.st_mtime, "size": st.st_size,
                       "last_used": last_event_ts(path, st.st_mtime),
                       "subagent": subagent,
                       "model": extra.get("model", ""), "tokens": extra.get("tokens", 0),
                       "branch": extra.get("branch", ""),
                       "thread": (extra.get("thread") or _codex_thread_of(path))
                                 if source == "codex" else "",
                       "v": SCHEMA_VERSION}
                ent["app"] = _app_for(ent, cowork_ids)
                cache[key] = ent
                sessions.append(ent)
                if source == "claude" and not rescue:
                    seen_names.add(name)  # only successfully-indexed originals shadow rescues
    # non-file harnesses (e.g. Cursor SQLite) contribute via adapter.discover()
    try:
        import adapters as _ad
        for ad in _ad.ADAPTERS:
            for d in ad.discover():
                ent = cache.get(d["id"])
                if ent is not None and ent.get("v") != SCHEMA_VERSION:
                    ent = None  # old-schema entry (e.g. fake time.time() stamp) — rebuild
                if ent is None:
                    # the harness's own timestamp, or 0 — never time.time(), which
                    # would fake-freshen every undated session on every scan
                    ts = d.get("ts") or 0
                    ent = {"path": d["id"], "source": ad.id, "app": ad.id,
                           "title": (d.get("title") or "(untitled)")[:120],
                           "cwd": d.get("cwd", ""), "project": os.path.basename(d.get("cwd", "")),
                           "mtime": ts, "size": 0, "last_used": ts, "subagent": False,
                           "model": "", "tokens": 0, "branch": "", "v": SCHEMA_VERSION}
                    cache[d["id"]] = ent
                elif d.get("ts"):
                    ent["last_used"] = ent["mtime"] = d["ts"]  # cursor keeps updating
                sessions.append(ent)
    except Exception:
        pass
    live = {s["path"] for s in sessions}
    for stale in [k for k in cache if k not in live]:
        del cache[stale]
    sessions.sort(key=lambda s: -s.get("last_used", s["mtime"]))
    return sessions


def _app_for(ent, cowork_ids):
    if ent["source"] == "codex":
        return "codex"
    sid = os.path.splitext(os.path.basename(ent["path"]))[0]
    return "cowork" if sid in cowork_ids else "claude-code"
