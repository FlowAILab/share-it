#!/usr/bin/env python3
"""share-it — local viewer + sharer for Claude Code / Codex session transcripts.

Run: python3 app.py   (opens http://127.0.0.1:8749)
Nothing is uploaded unless you click Share.
"""
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import adapters
import parsers
import render
import search
import share

PORT = 8749
import secrets as _secrets
TOKEN = os.environ.get("SHAREIT_TOKEN") or _secrets.token_urlsafe(24)  # shell-provided, else dev
TOKEN_PATH = os.path.expanduser("~/.shareit/session_token")
CACHE_PATH = os.path.expanduser("~/.shareit/index.json")
ANNOT_PATH = os.path.expanduser("~/.shareit/annotations.json")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_lock = threading.Lock()
_cache = {}
_annot = {}           # path → {mtime, v, arts, art_list, read_list}
_peekmem = {}         # path → (mtime, messages, last_answer) — in-memory, cheap to rebuild
_annot_priority = []  # paths the UI is waiting on — worker serves these first


def _load_annot():
    global _annot
    try:
        with open(ANNOT_PATH) as fh:
            raw = json.load(fh)
        _annot = {k: v for k, v in raw.items() if isinstance(v, dict)} \
                 if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        _annot = {}


def _save_annot():
    share._state_file_write(ANNOT_PATH, json.dumps(_annot))


def _annotate_one(path, source, cwd, mtime):
    """Compute + persist annotations for one session; returns the record or None.

    A parse/IO failure NEVER persists an empty result — the session stays
    unannotated so it retries, instead of caching a wrong 'no files' forever.
    """
    try:
        arts = parsers.session_artifacts(path, source=source, cwd=cwd or None)
    except Exception:
        return None
    try:
        reads_all = parsers.session_reads(path, source=source, cwd=cwd or None, limit=12)
    except Exception:
        reads_all = []  # artifacts already succeeded — keep them
    n_msgs = n_images = 0
    last_text = ""
    try:
        msgs = adapters.by_id(source).parse(path)
        n_msgs = sum(1 for m in msgs if m["role"] in ("user", "assistant")
                     and ((m.get("text") or "").strip() or m.get("media")))
        n_images = sum(1 for m in msgs for _x in (m.get("media") or []))
        last_text = next((m["text"] for m in reversed(msgs)
                          if m["role"] == "assistant" and (m.get("text") or "").strip()), "")
    except Exception:
        pass
    have = {a["path"] for a in arts}
    # fingerprint the file SET (path+size+mtime) so deletions/edits invalidate shares
    def fp(f):
        try:
            st = os.stat(f["path"])
            return [f["path"], st.st_size, round(st.st_mtime, 3)]
        except OSError:
            return [f["path"], None, None]
    rec = {"mtime": mtime, "v": ANNOT_VERSION, "cwd": cwd or "",
           "n_msgs": n_msgs, "n_images": n_images,
           "n_primary": len(_primary_paths(arts, last_text[:4000])),
           "arts": sum(1 for a in arts if a["kind"] == "created"),
           "art_list": arts[:24],
           "read_list": [r for r in reads_all if r["path"] not in have],
           "fp": sorted(fp(a) for a in arts[:24])}
    with _lock:
        _annot[path] = rec
        live = _cache.get(path)
        if live is not None:
            _apply_annot(live, rec)
    return rec


def _apply_annot(ent, rec):
    ent["arts"] = rec["arts"]
    ent["n_primary"] = rec.get("n_primary", 0)
    ent["n_msgs"] = rec.get("n_msgs", 0)
    ent["n_images"] = rec.get("n_images", 0)
    ent["art_list"] = rec["art_list"]
    ent["read_list"] = rec["read_list"]
    ent["annot_v"] = rec["v"]
    ent["annot_fp"] = rec.get("fp")


def _merge_annotations():
    """Attach stored annotations to cache entries.

    A LIVE session grows faster than the annotator — an exact mtime match would
    keep its file list permanently invisible. A slightly-stale annotation (≤10
    min behind) still shows; the worker catches up within its 120s cycle."""
    for path, ent in _cache.items():
        rec = _annot.get(path)
        if not isinstance(rec, dict):
            continue
        rmt = rec.get("mtime")
        if (rec.get("v") == ANNOT_VERSION and isinstance(rmt, (int, float))
                and rec.get("cwd", "") == (ent.get("cwd") or "")
                and (rmt == ent["mtime"] or ent["mtime"] - rmt <= 600)):
            _apply_annot(ent, rec)


def _load_cache():
    global _cache
    try:
        with open(CACHE_PATH) as fh:
            raw = json.load(fh)
        # tolerate a corrupt/legacy shape without crashing the index
        _cache = {k: v for k, v in raw.items()
                  if isinstance(v, dict) and isinstance(v.get("mtime"), (int, float))
                  and isinstance(v.get("size"), int) and "source" in v} \
                 if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        _cache = {}


def _save_cache():
    share._state_file_write(CACHE_PATH, json.dumps(_cache))


def _cleanup_state():
    """Startup housekeeping: orphan temp files, dead annotations, deleted-share pruning."""
    import glob as _glob
    import time as _t
    d = os.path.expanduser("~/.shareit")
    for tmp in _glob.glob(os.path.join(d, "*.tmp")) + _glob.glob(os.path.join(d, "**", "*.tmp"),
                          recursive=True):
        try:
            if _t.time() - os.path.getmtime(tmp) > 3600:  # older than an hour = orphan
                os.remove(tmp)
        except OSError:
            pass
    # drop share entries that are deleted AND expired (link is gone either way)
    try:
        shares = share.load_shares()
        now = _t.time()
        keep = [s for s in shares if not (s.get("deleted")
                and (s.get("expires") is None or s["expires"] < now - 86400))]
        if len(keep) != len(shares):
            share._write_shares(keep)
    except Exception:
        pass


def _fix_state_perms():
    d = os.path.expanduser("~/.shareit")
    if os.path.isdir(d):
        os.chmod(d, 0o700)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                os.chmod(p, 0o600)


def _allowed(path):
    real = os.path.realpath(path)
    if "#" in path:  # non-file harness ids (Cursor SQLite) — the adapter decides
        try:
            adapters.for_path(path)
            return True
        except ValueError:
            return False
    return real.endswith(".jsonl") and any(
        real.startswith(os.path.realpath(root) + os.sep)
        for root in adapters.allowed_roots())


def _session_entry(path):
    with _lock:
        ent = _cache.get(path)
    if ent:
        return ent
    raise KeyError("unknown session; refresh the list")


VERSION = "2.0.0"
EXPIRY_CHOICES = (0, 24, 72, 168)  # ∞ (public object), 1d, 3d, 7d


def _stores(sessions):
    """Which local session stores exist on this machine, for the UI legend."""
    counts = {}
    for s in sessions:
        counts[s["app"]] = counts.get(s["app"], 0) + 1
    cowork_dir = os.path.isdir(os.path.join(parsers.COWORK_META, "local-agent-mode-sessions"))
    return [
        {"id": "claude-code", "label": "Claude Code", "count": counts.get("claude-code", 0),
         "available": os.path.isdir(parsers.CLAUDE_ROOT), "note": "CLI · desktop · IDE"},
        {"id": "cowork", "label": "Cowork", "count": counts.get("cowork", 0),
         "available": cowork_dir or counts.get("cowork", 0) > 0,
         "note": "Claude desktop agent" if cowork_dir else "not detected on this Mac"},
        {"id": "codex", "label": "Codex", "count": counts.get("codex", 0),
         "available": os.path.isdir(parsers.CODEX_ROOT), "note": "CLI · desktop"},
        {"id": "chatgpt", "label": "ChatGPT", "count": 0, "available": False,
         "note": "cloud-only — no local transcripts"},
    ]


def _render_opts(body):
    hours = body.get("expires_hours", share.EXPIRES_HOURS)
    if hours not in EXPIRY_CHOICES:
        hours = 168
    mode = body.get("mode", "agent")
    mode = {"msgs": "human", "full": "agent"}.get(mode, mode)  # legacy names
    if mode not in ("human", "agent", "deep"):
        mode = "agent"
    # the mode decides the format and whether reasoning ships — callers cannot mix
    fmt = "html" if mode == "human" else "md"
    files = body.get("files")
    if not (isinstance(files, list) and all(isinstance(f, str) for f in files)):
        files = None
    return dict(redact=body.get("redact", True),
                thinking=mode == "deep",
                mode=mode, messages_only=mode == "human", expires_hours=hours,
                artifacts=body.get("artifacts", True), fmt=fmt, files=files)


def _open_in_terminal(cmd, cwd):
    """Run cmd in macOS Terminal.app — the stock, always-present choice."""
    esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
    try:
        script = f'tell application "Terminal"\nactivate\ndo script "{esc}"\nend tell'
        ok = subprocess.run(["osascript", "-e", script], capture_output=True,
                            timeout=10).returncode == 0
        return ok, "terminal"
    except (OSError, subprocess.SubprocessError):
        return False, None


def _text_head(path, limit=120):
    """First line-ish of a text file for hover previews; None for binaries."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1024)
        if b"\x00" in raw:
            return None
        text = " ".join(raw.decode("utf-8", errors="ignore").split())
        return text[:limit] or None
    except OSError:
        return None


def _artifact_fingerprint(session, opts):
    """Content hash of the file set a share would include — deletion, addition
    or ANY edit (even one that restores size+mtime) changes it, so a stale
    bundle can never be served as cached. Files are <=25MB, so full hashing is
    cheap; ValueError (missing/oversize pick) propagates rather than aliasing
    a broken selection to a chat-only cache hit."""
    import hashlib
    pool = _effective_files(session, opts)
    parts = []
    for a in sorted(pool, key=lambda x: x["path"]):
        h = hashlib.sha256()
        try:
            with open(a["path"], "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            parts.append(f"{a['path']}:{h.hexdigest()}")
        except OSError:
            parts.append(f"{a['path']}:missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


ANNOT_VERSION = 6  # +n_primary (payload-honest badge)


def _known_files(session):
    """Every file the UI may legitimately reference for this session."""
    files = set()
    for key in ("art_list", "read_list"):
        for a in session.get(key, []):
            files.add(a["path"])
    if not files:
        files = ({a["path"] for a in _session_artifacts(session)}
                 | {r["path"] for r in adapters.by_id(session["source"]).reads(
                     session["path"], cwd=session.get("cwd") or None)})
    return files


def _session_artifacts(session):
    return adapters.by_id(session["source"]).artifacts(
        session["path"], cwd=session.get("cwd") or None)


def _effective_files(session, opts):
    """The single source of truth for which files a share carries.

    Explicit selection (opts["files"]) wins — oversize picks are an error, not
    a silent omission. Otherwise: deep = created+modified+read; default modes =
    the primary deliverable(s), falling back to every created file."""
    arts = _session_artifacts(session)
    chosen = opts.get("files")
    if chosen is not None:
        pool = arts + [{**r, "kind": "referenced"}
                       for r in adapters.by_id(session["source"]).reads(
                           session["path"], cwd=session.get("cwd") or None)
                       if not any(a["path"] == r["path"] for a in arts)]
        fset = set(chosen)
        picked = [a for a in pool if a["path"] in fset]
        missing = fset - {a["path"] for a in picked}
        if missing:
            names = ", ".join(os.path.basename(p) for p in sorted(missing)[:4])
            raise ValueError(f"selected file(s) no longer found: {names} — refresh and retry")
        too_big = [a["name"] for a in picked
                   if (a.get("size") or 0) > parsers.ARTIFACT_MAX_BYTES]
        if too_big:
            raise ValueError(f"too large to share (25MB cap): {', '.join(too_big)}")
        if sum(a.get("size") or 0 for a in picked) > 100_000_000:
            raise ValueError("selection exceeds the 100MB bundle cap — drop some files")
        return picked
    if opts.get("mode") == "deep":
        # deep carries created+modified; READ files ship as a path manifest in
        # the export, raw contents only when explicitly selected (leak safety)
        pass
    if opts.get("mode") != "deep":
        created = [a for a in arts if a["kind"] == "created"]
        try:
            _, last = _peek_texts(session["path"], session["mtime"])
        except Exception:
            last = ""
        # rank across created AND modified — the deliverable the final answer
        # names can be a modified source file; the fallback stays created-only
        prim = set(_primary_paths(arts, last))
        picked = [a for a in arts if a["path"] in prim] or created
        if not picked and opts.get("mode") in ("agent", "deep"):
            # coding task with no created deliverable: the change IS the point —
            # carry up to 5 meaningful modified source files (human mode: none)
            picked = [a for a in arts
                      if a["kind"] == "modified" and _MEANINGFUL_EXT.search(a["path"])
                      and not _TEMPISH.search(a["path"])][:5]
        arts = picked
    budget = 100_000_000  # total bundle cap — defaults degrade, selections error above
    kept = []
    for a in arts:
        if budget - a["size"] < 0:
            continue
        budget -= a["size"]
        kept.append(a)
    return kept


def _artifact_links(session, opts, upload=True):
    """Share session artifacts (cached per file+expiry) → [{name,size,path,kind,url}]."""
    if not opts.get("artifacts"):
        return []
    art_opts = {"expires_hours": opts["expires_hours"]}
    arts = _effective_files(session, opts)

    def resolve(art):
        entry = share.find_cached(session, art_opts, artifact=art["path"])
        if entry is None and upload:
            try:
                result = share.upload_file(art["path"], expires_hours=opts["expires_hours"])
                entry = share.record_share(session, result, art_opts, art["size"],
                                           artifact=art["path"])
            except OSError:
                entry = None
        return {**art, "url": entry["url"] if entry else None}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(resolve, arts))


def _safe_obj_name(name, taken):
    """Worker-legal object name, unique inside the bundle."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:60] or "file"
    cand, n = base, 2
    while cand in taken or cand == "manifest.json":
        cand, n = f"{n}-{base}", n + 1
    taken.add(cand)
    return cand


def _assemble_bundle(path, opts):
    """Build the complete share: index doc + inline media + selected files.

    Returns (session, objects, index_name, card, shared_paths, media_count).
    Raises ValueError with a human message when the selection can't ship.
    """
    import mimetypes
    import media as _media
    session = _session_entry(path)
    adapter = adapters.by_id(session["source"])
    messages = adapter.parse(path)
    files = _effective_files(session, opts) if opts.get("artifacts") else []
    media_objs = _media.collect(messages)   # annotates messages' media names
    taken = {"manifest.json"} | {m["name"] for m in media_objs}
    index_name = "chat.html" if opts.get("fmt") == "html" else "chat.md"
    taken.add(index_name)

    fobjs, artifact_links, shared_paths = [], [], []
    explicit = opts.get("files") is not None
    for a in files:
        try:
            with open(a["path"], "rb") as fh:
                data = fh.read()
        except OSError:
            if explicit:  # you picked it — a share without it would be a lie
                raise ValueError(f"can't read {a['name']} — nothing was shared")
            artifact_links.append({**a, "url": None})
            continue
        obj = _safe_obj_name(a["name"], taken)
        ct = mimetypes.guess_type(a["name"])[0] or "application/octet-stream"
        fobjs.append({"name": obj, "data": data, "content_type": ct})
        artifact_links.append({**a, "url": obj})   # relative — absolutized below
        shared_paths.append(a["path"])

    stats = parsers.session_stats(path, messages)
    card = render.share_card(session, stats, len(shared_paths))
    if opts["redact"]:
        card = render.redact(card)
    reads = []
    if opts.get("mode") == "deep":
        reads = adapter.reads(path, cwd=session.get("cwd") or None)
    return session, messages, media_objs, fobjs, artifact_links, \
        index_name, card, shared_paths, stats, reads


def _render_index(session, messages, opts, artifact_links, stats, reads, media_base):
    """The bundle's index document (chat.md / chat.html)."""
    deep = opts.get("mode") == "deep"
    expiry_label = ("no expiry" if opts["expires_hours"] == 0
                    else "expires tomorrow" if opts["expires_hours"] == 24
                    else f"expires in {opts['expires_hours'] // 24}d")
    links = [{**a, "url": (f"{media_base}/{a['url']}" if media_base and a.get("url") else None)}
             for a in artifact_links]
    card = render.share_card(session, stats, sum(1 for a in links if a["url"]))
    if opts["redact"]:
        card = render.redact(card)
    if opts.get("fmt") == "html":
        return render.render_html(
            session, messages, redact_secrets=opts["redact"],
            include_thinking=opts["thinking"], messages_only=opts["messages_only"],
            tool_output_limit=10000 if deep else 2000,
            tool_input_limit=4000 if deep else 800,
            artifact_links=links or None, card=card,
            mode_label=opts["mode"], expiry_label=expiry_label,
            media_base=media_base)
    last_request = next((m["text"] for m in reversed(messages)
                         if m["role"] == "user" and m["text"].strip()), "")
    return render.render_markdown(
        session, messages, redact_secrets=opts["redact"],
        include_thinking=opts["thinking"], messages_only=opts["messages_only"],
        tool_output_limit=10000 if deep else 1200,
        tool_input_limit=4000 if deep else 500,
        artifact_links=links or None, stats=stats, mode=opts["mode"],
        last_request=last_request, cwd=session.get("cwd") or None,
        expiry_label=expiry_label, media_base=media_base,
        read_files=reads or None)


def _render_md(path, opts, upload_artifacts=False):
    session = _session_entry(path)
    adapter = adapters.by_id(session["source"])
    messages = adapter.parse(path)
    artifact_links = _artifact_links(session, opts, upload=upload_artifacts)
    deep = opts.get("mode") == "deep"
    stats = parsers.session_stats(path, messages)
    card = render.share_card(session, stats, len(artifact_links))
    if opts["redact"]:
        card = render.redact(card)  # titles can carry pasted secrets too
    expiry_label = ("no expiry" if opts["expires_hours"] == 0
                    else "expires tomorrow" if opts["expires_hours"] == 24
                    else f"expires in {opts['expires_hours'] // 24}d")
    if opts.get("fmt") == "html":
        doc = render.render_html(
            session, messages, redact_secrets=opts["redact"],
            include_thinking=opts["thinking"], messages_only=opts["messages_only"],
            tool_output_limit=10000 if deep else 2000,
            tool_input_limit=4000 if deep else 800,
            artifact_links=artifact_links or None, card=card,
            mode_label=opts["mode"], expiry_label=expiry_label)
    else:
        last_request = next((m["text"] for m in reversed(messages)
                             if m["role"] == "user" and m["text"].strip()), "")
        doc = render.render_markdown(
            session, messages, redact_secrets=opts["redact"],
            include_thinking=opts["thinking"], messages_only=opts["messages_only"],
            tool_output_limit=10000 if deep else 1200,
            tool_input_limit=4000 if deep else 500,
            artifact_links=artifact_links or None, stats=stats, mode=opts["mode"],
            last_request=last_request, cwd=session.get("cwd") or None,
            expiry_label=expiry_label,
            read_files=None)  # read paths are metadata — deep uploads them as links instead
    return session, doc, card


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _guard(self, need_token=True):
        """Reject DNS-rebinding / cross-origin requests, and gate /api on the
        per-launch token so a co-logged-in user can't drive this server."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self._json({"error": "bad origin"}, 403)
            return False
        if need_token:
            tok = self.headers.get("X-Shareit-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
            if not _secrets.compare_digest(tok or "", TOKEN):
                self._json({"error": "unauthorized"}, 401)
                return False
        return True

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        if not isinstance(body.get("path", ""), str) or not isinstance(body.get("file", ""), str):
            raise ValueError("bad field types")
        return body

    def do_GET(self):
        try:
            self._route_get()
        except Exception as e:  # keep the connection JSON-clean on any bug
            try:
                self._json({"error": f"internal: {e}"}, 500)
            except OSError:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            try:
                self._json({"error": f"internal: {e}"}, 500)
            except OSError:
                pass

    def _route_get(self):
        parsed = urlparse(self.path)
        route = parsed.path
        open_route = route in ("/", "/api/health")
        if not self._guard(need_token=not open_route):
            return
        if route == "/api/health":
            self._json({"app": "share-it", "version": VERSION})
        elif route == "/":
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                data = fh.read()   # never carries the token; the page reads it
                                   # from injection (app) or the ?t= URL (dev)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif route == "/api/sessions":
            with _lock:
                sessions = parsers.scan_sessions(_cache)
                _merge_annotations()
                _save_cache()
            caps = {"claude_app": os.path.isdir("/Applications/Claude.app"),
                    "codex_app": os.path.isdir("/Applications/ChatGPT.app")}
            self._json({"sessions": sessions, "stores": _stores(sessions), "caps": caps})
        elif route == "/api/peek":
            path = parse_qs(parsed.query).get("path", [""])[0]
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            try:
                session = _session_entry(path)
                messages, last_answer = _peek_texts(path, session["mtime"])
                # cached lists (background-annotated) keep peek instant on huge sessions
                pending = False
                if "art_list" in session:
                    artifacts = [a for a in session["art_list"] if os.path.isfile(a["path"])]
                    have = {a["path"] for a in artifacts}
                    reads = [r for r in session.get("read_list", [])
                             if os.path.isfile(r["path"]) and r["path"] not in have]
                elif session["size"] > 8_000_000:
                    with _lock:
                        if path not in _annot_priority:
                            _annot_priority.append(path)  # worker handles it next pass
                    artifacts, reads = [], []
                    pending = True
                else:
                    rec = _annotate_one(path, session["source"],
                                        session.get("cwd"), session["mtime"])
                    if rec is None:  # transient failure — don't cache emptiness
                        artifacts, reads, pending = [], [], True
                    else:
                        artifacts = [a for a in rec["art_list"] if os.path.isfile(a["path"])]
                        reads = [r for r in rec["read_list"] if os.path.isfile(r["path"])]
                for a in artifacts:
                    a["head"] = _text_head(a["path"])
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            shown = last_answer[:480] + ("…" if len(last_answer) > 480 else "")
            self._json({"messages": messages, "last": shown,
                        "artifacts": artifacts, "reads": reads, "pending": pending,
                        "primary": _primary_paths(artifacts, last_answer)})
        elif route == "/api/search":
            q = parse_qs(parsed.query).get("q", [""])[0]
            hits = search.search(q) if len(q.strip()) >= 2 else []
            with _lock:
                known = {p: e for p, e in _cache.items()}
            results = [{"snippet": h["snippet"], "path": h["path"]}
                       for h in hits if h["path"] in known]
            self._json({"results": results, "indexed": search.indexed_count()})
        elif route == "/api/last_answer":
            path = parse_qs(parsed.query).get("path", [""])[0]
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            try:
                messages = adapters.by_id(_session_entry(path)["source"]).parse(path)
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            last_msg = next((m for m in reversed(messages)
                             if m["role"] == "assistant" and m["text"].strip()), None)
            answer = last_msg["text"] if last_msg else ""
            blocks = re.findall(r"```[a-zA-Z]*\n(.*?)```", answer, re.S)
            # rich flavor + the files this answer explicitly names
            session = _session_entry(path)
            html = render.clipboard_html(session, [last_msg] if last_msg else [],
                                         include_tools=False)
            mentioned = []
            try:
                for a in _session_artifacts(session):
                    if a["name"] and a["name"] in answer and os.path.isfile(a["path"]):
                        mentioned.append(a["path"])
            except Exception:
                pass
            self._json({"answer": answer, "code_blocks": blocks,
                        "html": html, "files": mentioned[:5]})
        elif route == "/api/shares":
            self._json({"shares": share.load_shares()})
        else:
            self._json({"error": "not found"}, 404)

    def _route_post(self):
        if not self._guard():
            return
        route = urlparse(self.path).path
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)
        if route == "/api/share/artifact":
            path, file = body.get("path", ""), body.get("file", "")
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            try:
                session = _session_entry(path)
                known = _known_files(session)
                if file not in known:
                    return self._json({"error": "not a file of this session"}, 400)
                if os.path.getsize(file) > parsers.ARTIFACT_MAX_BYTES:
                    return self._json({"error": "file exceeds the 25MB share limit"}, 413)
                opts = _render_opts(body)
                cached = share.find_cached(session, opts, artifact=file)
                if cached:
                    return self._json({"url": cached["url"], "cached": True,
                                       "provider": cached["provider"], "expires": cached["expires"]})
                pre_size = os.path.getsize(file)
                pre_mt = os.path.getmtime(file)   # capture BEFORE upload
                result = share.upload_file(file, expires_hours=opts["expires_hours"])
                entry = share.record_share(session, result, opts, pre_size, artifact=file,
                                           src_mtime=pre_mt)
                return self._json({"url": entry["url"], "provider": entry["provider"],
                                   "expires": entry["expires"]})
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 502)
        if route in ("/api/preview", "/api/share"):
            path = body.get("path", "")
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            opts = _render_opts(body)
            try:
                session = _session_entry(path)
                pre_mtime = share._src_mtime(session)  # mtime of the bytes we're about to render
                opts["art_mtime"] = _artifact_fingerprint(session, opts)
                if route == "/api/share":
                    cached = share.find_cached(session, opts)
                    if cached:
                        _s, _md, card = _render_md(path, opts, upload_artifacts=False)
                        return self._json({"url": cached["url"], "size": cached["size"],
                                           "cached": True, "provider": cached["provider"],
                                           "expires": cached["expires"], "card": card,
                                           "n_files": cached.get("n_files", 0),
                                           "n_images": cached.get("n_images", 0)})
                (session, messages, media_objs, fobjs, artifact_links,
                 index_name, card, shared_paths, stats, reads) = _assemble_bundle(path, opts)
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            if route == "/api/preview":
                md = _render_index(session, messages, opts, artifact_links,
                                   stats, reads, media_base=None)
                return self._json({"markdown": md, "card": card})
            try:
                bundle_id = share.new_bundle_id()
                media_base = share._hosted_config()["url"].rstrip("/") + f"/b/{bundle_id}"
                doc = _render_index(session, messages, opts, artifact_links,
                                    stats, reads, media_base=media_base)
                ct = ("text/html; charset=utf-8" if opts["fmt"] == "html"
                      else "text/markdown; charset=utf-8")
                objects = ([{"name": index_name, "data": doc.encode(), "content_type": ct}]
                           + media_objs + fobjs)
                # preflight the REAL bundle (index included) before any upload
                if len(objects) > 64:
                    return self._json({"error": f"too many objects for one share ({len(objects)}/64) — drop some files"}, 400)
                over = [o["name"] for o in objects if len(o["data"]) > 25_000_000]
                if over:
                    return self._json({"error": f"too large to share (25MB cap): {', '.join(over)}"}, 413)
                if sum(len(o["data"]) for o in objects) > 100_000_000:
                    return self._json({"error": "share exceeds the 100MB bundle cap — drop some files"}, 400)
                result = share.upload_bundle(objects, index_name,
                                             expires_hours=opts["expires_hours"],
                                             bundle_id=bundle_id)
            except Exception as e:  # nothing was shared — the client hears exactly why
                return self._json({"error": f"share failed: {e}"}, 502)
            entry = share.record_share(session, result, opts, len(doc),
                                       file_paths=shared_paths, src_mtime=pre_mtime,
                                       counts={"n_files": len(shared_paths),
                                               "n_images": len(media_objs)})
            return self._json({"url": entry["url"], "size": len(doc), "card": card,
                               "provider": entry["provider"], "expires": entry["expires"],
                               "n_files": len(shared_paths), "n_images": len(media_objs)})
        if route == "/api/copy_chat":
            # ⌘C copies THE CHAT — identical in every mode, never a network
            # call. ONE pasteboard item: plain = full markdown, html/rtf carry
            # the images inline. Links are what the share modes are for.
            path = body.get("path", "")
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            opts = _render_opts(body)
            try:
                import media as _media
                session = _session_entry(path)
                adapter = adapters.by_id(session["source"])
                messages = adapter.parse(path)
                media_objs = _media.collect(messages)   # annotates names for token cleanup
                n_msgs = sum(1 for m in messages if m["role"] in ("user", "assistant")
                             and ((m.get("text") or "").strip() or m.get("media")))
                html = render.clipboard_html(session, messages,
                                             include_tools=False,
                                             include_thinking=False,
                                             redact_secrets=opts["redact"])
                stats = parsers.session_stats(path, messages)
                md = render.render_markdown(
                    session, messages, redact_secrets=opts["redact"],
                    include_thinking=False, messages_only=True,
                    stats=stats, mode="human",
                    cwd=session.get("cwd") or None)
                return self._json({"kind": "rich", "html": html, "markdown": md,
                                   "messages": n_msgs, "images": len(media_objs)})
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
        if route in ("/api/file/copy", "/api/file/reveal", "/api/file/open", "/api/file/preview"):
            path, files = body.get("path", ""), body.get("files") or []
            if not _allowed(path) or not files:
                return self._json({"error": "invalid request"}, 400)
            session = _session_entry(path)
            known = _known_files(session)
            files = [f for f in files if f in known and os.path.isfile(f)]
            if not files:
                return self._json({"error": "no valid files"}, 400)
            try:
                if route == "/api/file/open":
                    ok = subprocess.run(["open", files[0]], capture_output=True,
                                        timeout=10).returncode == 0
                elif route == "/api/file/preview":
                    subprocess.Popen(["qlmanage", "-p", files[0]],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ok = True
                elif route == "/api/file/reveal":
                    ok = subprocess.run(["open", "-R", files[0]], capture_output=True,
                                        timeout=10).returncode == 0
                else:
                    items = ", ".join(
                        'POSIX file "' + f.replace("\\", "\\\\").replace('"', '\\"') + '"'
                        for f in files)
                    clip = items if len(files) > 1 else items
                    ok = subprocess.run(
                        ["osascript", "-e", f"set the clipboard to {{{clip}}}"],
                        capture_output=True, timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            return self._json({"ok": ok, "count": len(files)})
        if route == "/api/export":
            # local export: a real file on disk (→ clipboard), never an upload
            path = body.get("path", "")
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            opts = _render_opts(body)
            opts["artifacts"] = False  # local copy: no hosted links inside
            try:
                session, doc, _card = _render_md(path, opts, upload_artifacts=False)
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            import re as _re, hashlib as _h
            slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", session["title"]).strip("-")[:50] or "session"
            ext = ".html" if opts["fmt"] == "html" else ".md"
            # disambiguate collisions (A/B vs A B, unicode-only titles) with a path hash
            tag = _h.sha1(path.encode()).hexdigest()[:6]
            exp_dir = os.path.join(os.path.expanduser("~/.shareit"), "exports")
            os.makedirs(exp_dir, mode=0o700, exist_ok=True)
            fname = f"{slug}-{tag}{ext}"
            fpath = os.path.join(exp_dir, fname)
            tmp = fpath + f".{os.getpid()}.tmp"
            with open(tmp, "w") as fh:
                fh.write(doc)
            os.replace(tmp, fpath)  # atomic — no truncated/hybrid file on kill
            n_msgs = sum(1 for m in adapters.by_id(session["source"]).parse(path)
                         if m["role"] in ("user", "assistant") and (m.get("text") or "").strip())
            return self._json({"file": fpath, "name": fname, "messages": n_msgs,
                               "mode": opts["mode"], "redacted": opts["redact"]})
        if route == "/api/open_url":
            u = body.get("url", "")
            if not (isinstance(u, str) and u.startswith("https://")):
                return self._json({"error": "invalid url"}, 400)
            ok = subprocess.run(["open", u], capture_output=True, timeout=10).returncode == 0
            return self._json({"ok": ok})
        if route == "/api/resume":
            path = body.get("path", "")
            if not _allowed(path):
                return self._json({"error": "invalid path"}, 400)
            try:
                session = _session_entry(path)
                adapter = adapters.by_id(session["source"])
                cmd = adapter.resume_command(path, cwd=session.get("cwd") or None)
                deep = adapter.app_link(path)
            except (KeyError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            if not cmd and not deep:
                return self._json({"error": "this session can't be resumed"}, 400)
            # Cowork → Claude app; codex → try the observed codex:// route; else terminal
            if deep and body.get("app", True) and (
                    session.get("app") == "cowork" or session["source"] == "codex"):
                try:
                    if subprocess.run(["open", deep], capture_output=True,
                                      timeout=10).returncode == 0:
                        target = "Codex" if session["source"] == "codex" else "Claude"
                        return self._json({"command": cmd, "launched": True,
                                           "app": True, "target": target})
                except (OSError, subprocess.SubprocessError):
                    pass
            launched, term = _open_in_terminal(cmd, session.get("cwd") or None)
            return self._json({"command": cmd, "launched": launched, "terminal": term})
        if route == "/api/share/delete":
            url = body.get("url", "")
            entry = next((s for s in share.load_shares()
                          if s["url"] == url and not s["deleted"]), None)
            if not entry:
                return self._json({"error": "unknown share"}, 400)
            try:
                share.delete(entry)
            except Exception as e:
                return self._json({"error": f"delete failed: {e}"}, 502)
            share.mark_deleted(url)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)


def _peek_texts(path, mtime):
    """Memoized head-messages + last answer for a session (invalidated on write)."""
    with _lock:
        hit = _peekmem.get(path)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    if "#" in path:  # non-file harness (Cursor) — parse via its adapter
        full = adapters.for_path(path).parse(path)
        clip = lambda t, n: t[:n] + ("…" if len(t) > n else "")
        texts = [{"role": m["role"], "text": clip(" ".join(m["text"].split()), 280)}
                 for m in full if m.get("text", "").strip()
                 and m["role"] in ("user", "assistant")]
        msgs = texts[:6]
        last = next((clip(" ".join(m["text"].split()), 4000) for m in reversed(full)
                     if m["role"] == "assistant" and m.get("text", "").strip()), "")
    else:
        msgs = parsers.peek_session(path)
        last = parsers.peek_last_answer(path, clip=4000)
    with _lock:
        _peekmem[path] = (mtime, msgs, last)
        if len(_peekmem) > 400:          # bound memory; oldest-inserted first
            for k in list(_peekmem)[:100]:
                del _peekmem[k]
    return msgs, last


# what counts as a hand-authored source/doc file for the modified-files rule
_MEANINGFUL_EXT = re.compile(
    r"\.(py|js|jsx|ts|tsx|swift|go|rs|rb|java|kt|c|h|cc|cpp|hpp|m|mm|cs|php|sh|zsh|bash|"
    r"sql|html|css|scss|vue|svelte|md|rst|txt|yaml|yml|toml|json|proto|graphql|tf)$", re.I)

_TEMPISH = re.compile(
    r"(^|/)(node_modules|__pycache__|dist|build|target|\.wrangler|\.git|\.venv|venv)(/|$)"
    r"|\.(log|tmp|lock|pyc|pyo|o|class|map|min\.js)$|(^|/)\.DS_Store$")


def _primary_paths(artifacts, last_answer):
    """The deliverable(s) a share should carry by default: files the final
    answer explicitly names (even dist/build outputs), else the freshest
    meaningful created files — temp/build noise never wins by default."""
    la = last_answer or ""
    declared = [a["path"] for a in artifacts if a.get("declared")]
    named = [a["path"] for a in artifacts
             if a["name"] and a["name"] in la and a["path"] not in declared]
    if declared or named:
        return (declared + named)[:5]
    meaningful = [a for a in artifacts if not _TEMPISH.search(a["path"])]
    created = [a["path"] for a in meaningful if a["kind"] == "created"]
    return created[:3]


def _peek_prewarm(n=40):
    """Warm the peek cache for the sessions the user will actually click."""
    with _lock:
        recent = sorted(_cache.values(),
                        key=lambda e: -e.get("last_used", e.get("mtime", 0)))[:n]
        recent = [(e["path"], e["mtime"]) for e in recent]
    for path, mtime in recent:
        try:
            _peek_texts(path, mtime)
        except Exception:
            continue


def _artifact_counter():
    """Background: annotate cached sessions with their artifact count (cheap UI hint)."""
    while True:
        with _lock:
            _merge_annotations()
            todo = [dict(e) for e in _cache.values()
                    if not e.get("subagent")
                    and (("art_list" not in e) or e.get("annot_v") != ANNOT_VERSION
                         # stale-merged live sessions still need a recompute
                         or (_annot.get(e["path"]) or {}).get("mtime") != e["mtime"])]
            prio = list(_annot_priority)
            _annot_priority.clear()
        if not todo and not prio:
            _peek_prewarm()
            return
        todo.sort(key=lambda e: (e["path"] not in prio, -e["mtime"]))
        for i, ent in enumerate(todo):
            _annotate_one(ent["path"], ent["source"], ent.get("cwd"), ent["mtime"])
            if i % 25 == 24:
                with _lock:
                    _save_annot()
                    _save_cache()
        with _lock:
            live_paths = set(_cache.keys())
            for dead in [p for p in _annot if p not in live_paths]:
                del _annot[dead]  # session gone → drop its annotation
            _save_annot()
            _save_cache()


RESCUE_AGE_DAYS = 20   # copy Claude transcripts before the ~30-day purge
RESCUE_CAP_BYTES = 500_000_000  # stop archiving past this; clear ~/.shareit/archive to reset


def _rescue_archive():
    import shutil
    import time as _t
    cfg = share._load_config()
    if cfg.get("rescue") is False:
        return
    try:
        used = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _, fs in os.walk(parsers.RESCUE_ROOT) for f in fs)
    except OSError:
        used = 0
    if used > RESCUE_CAP_BYTES:
        return
    cutoff = _t.time() - RESCUE_AGE_DAYS * 86400
    root = os.path.realpath(parsers.CLAUDE_ROOT)
    with _lock:
        ents = [dict(e) for e in _cache.values() if e["source"] == "claude"]
    for ent in ents:
        real = os.path.realpath(ent["path"])
        if not real.startswith(root + os.sep) or ent["mtime"] > cutoff:
            continue
        rel = os.path.relpath(real, root)
        dest = os.path.join(parsers.RESCUE_ROOT, rel)
        try:
            if (os.path.isfile(dest) and os.path.getsize(dest) == ent["size"]
                    and abs(os.path.getmtime(dest) - ent["mtime"]) < 1):
                continue
            if used + ent.get("size", 0) > RESCUE_CAP_BYTES:
                break  # would exceed the aggregate cap — stop before copying
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            shutil.copy2(real, dest)
            used += ent.get("size", 0)
        except OSError:
            continue


def _fts_indexer():
    with _lock:
        todo = [dict(e) for e in _cache.values() if not e.get("subagent")]
    todo.sort(key=lambda e: -e["mtime"])
    for ent in todo:
        try:
            if not search.needs_index(ent["path"], ent["mtime"], ent.get("size")):
                continue
            search.index_session(ent["path"], ent["mtime"],
                                 parsers.parse_session(ent["path"]),
                                 title=ent.get("title", ""),
                                 extra=f"{ent.get('project','')} {ent.get('branch','')}",
                                 size=ent.get("size"))
        except Exception:
            continue
    search.prune({e["path"] for e in todo})


def _background_worker():
    import time as _t
    while True:
        try:
            with _lock:
                parsers.scan_sessions(_cache)  # fresh install: populate before processing
                _save_cache()
            _artifact_counter()
            _rescue_archive()
            _fts_indexer()
        except Exception:
            pass
        for _ in range(24):  # 120s total, but wake early for priority requests
            with _lock:
                waiting = bool(_annot_priority)
            if waiting:
                break
            _t.sleep(5)


def _write_token():
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(TOKEN)


READY_PATH = os.path.expanduser("~/.shareit/ready")


def _signal_ready():
    """After a successful bind, write the shell's nonce so it knows WE own the
    port (a squatter that grabbed it first makes bind fail — we never get here)."""
    nonce = os.environ.get("SHAREIT_READY")
    if not nonce:
        return
    try:
        fd = os.open(READY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(nonce)
    except OSError:
        pass


def main():
    _write_token()
    _fix_state_perms()
    _cleanup_state()
    _load_cache()
    _load_annot()
    with _lock:
        _merge_annotations()
    threading.Thread(target=_background_worker, daemon=True).start()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"share-it: port {PORT} is already in use — is another instance running?")
        raise SystemExit(1)
    _signal_ready()  # bind succeeded → tell the shell it's really us
    url = f"http://127.0.0.1:{PORT}"
    print(f"share-it running at {url}")
    if "--no-browser" not in sys.argv:
        dev_url = url if os.environ.get("SHAREIT_TOKEN") else f"{url}/?t={TOKEN}"
        threading.Timer(0.4, lambda: webbrowser.open(dev_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
