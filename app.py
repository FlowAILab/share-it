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
            _annot = json.load(fh)
    except (OSError, json.JSONDecodeError):
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
    have = {a["path"] for a in arts}
    # fingerprint the file SET (path+size+mtime) so deletions/edits invalidate shares
    def fp(f):
        try:
            st = os.stat(f["path"])
            return [f["path"], st.st_size, round(st.st_mtime, 3)]
        except OSError:
            return [f["path"], None, None]
    rec = {"mtime": mtime, "v": ANNOT_VERSION, "cwd": cwd or "",
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
    ent["art_list"] = rec["art_list"]
    ent["read_list"] = rec["read_list"]
    ent["annot_v"] = rec["v"]
    ent["annot_fp"] = rec.get("fp")


def _merge_annotations():
    """Attach stored annotations to cache entries whose file hasn't changed."""
    for path, ent in _cache.items():
        rec = _annot.get(path)
        if (rec and rec["mtime"] == ent["mtime"] and rec["v"] == ANNOT_VERSION
                and rec.get("cwd", "") == (ent.get("cwd") or "")):
            _apply_annot(ent, rec)


def _load_cache():
    global _cache
    try:
        with open(CACHE_PATH) as fh:
            _cache = json.load(fh)
    except (OSError, json.JSONDecodeError):
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
    """Hash of the (path, size, mtime) set a share would include — any deletion,
    addition or edit changes it, so a stale bundle can never be served as cached."""
    import hashlib
    try:
        pool = _effective_files(session, opts)
    except ValueError:
        pool = []  # the share itself will surface the error
    parts = []
    for a in pool:
        try:
            st = os.stat(a["path"])
            parts.append(f"{a['path']}:{st.st_size}:{st.st_mtime:.3f}")
        except OSError:
            parts.append(f"{a['path']}:missing")
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


ANNOT_VERSION = 4  # +fingerprint set


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
        too_big = [a["name"] for a in picked
                   if (a.get("size") or 0) > parsers.ARTIFACT_MAX_BYTES]
        if too_big:
            raise ValueError(f"too large to share (25MB cap): {', '.join(too_big)}")
        if sum(a.get("size") or 0 for a in picked) > 100_000_000:
            raise ValueError("selection exceeds the 100MB bundle cap — drop some files")
        return picked
    if opts.get("mode") == "deep":
        arts = arts + [{**r, "kind": "referenced"}
                       for r in adapters.by_id(session["source"]).reads(
                           session["path"], cwd=session.get("cwd") or None)
                       if r.get("size") is not None and r["size"] <= parsers.ARTIFACT_MAX_BYTES
                       and not any(a["path"] == r["path"] for a in arts)]
    else:
        created = [a for a in arts if a["kind"] == "created"]
        try:
            _, last = _peek_texts(session["path"], session["mtime"])
        except Exception:
            last = ""
        prim = set(_primary_paths(created, last))
        arts = [a for a in created if a["path"] in prim] or created
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

    def _guard(self):
        """Reject DNS-rebinding / cross-origin requests to this local server."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self._json({"error": "bad origin"}, 403)
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
        if not self._guard():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/health":
            self._json({"app": "share-it", "version": VERSION})
        elif route == "/":
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                data = fh.read()
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
            answer = next((m["text"] for m in reversed(messages)
                           if m["role"] == "assistant" and m["text"].strip()), "")
            import re as _re
            blocks = _re.findall(r"```[a-zA-Z]*\n(.*?)```", answer, _re.S)
            self._json({"answer": answer, "code_blocks": blocks})
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
                result = share.upload_file(file, expires_hours=opts["expires_hours"])
                entry = share.record_share(session, result, opts,
                                           os.path.getsize(file), artifact=file)
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
                opts["art_mtime"] = _artifact_fingerprint(session, opts)
                if route == "/api/share":
                    cached = share.find_cached(session, opts)
                    if cached:
                        _s, _md, card = _render_md(path, opts, upload_artifacts=False)
                        return self._json({"url": cached["url"], "size": cached["size"],
                                           "cached": True, "provider": cached["provider"],
                                           "expires": cached["expires"], "card": card})
                session, md, card = _render_md(path, opts,
                                               upload_artifacts=route == "/api/share")
            except (OSError, ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            if route == "/api/preview":
                return self._json({"markdown": md, "card": card})
            try:
                result = share.upload(md, expires_hours=opts["expires_hours"],
                                      fmt=opts["fmt"])
            except Exception as e:  # network errors surface to the UI
                return self._json({"error": f"upload failed: {e}"}, 502)
            entry = share.record_share(session, result, opts, len(md))
            return self._json({"url": entry["url"], "size": len(md), "card": card,
                               "provider": entry["provider"], "expires": entry["expires"]})
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
        last = next((clip(" ".join(m["text"].split()), 480) for m in reversed(full)
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


_TEMPISH = re.compile(
    r"(^|/)(node_modules|__pycache__|dist|build|target|\.wrangler|\.git|\.venv|venv)(/|$)"
    r"|\.(log|tmp|lock|pyc|pyo|o|class|map|min\.js)$|(^|/)\.DS_Store$")


def _primary_paths(artifacts, last_answer):
    """The deliverable(s) a share should carry by default: files the final
    answer explicitly names (even dist/build outputs), else the freshest
    meaningful created files — temp/build noise never wins by default."""
    la = last_answer or ""
    named = [a["path"] for a in artifacts if a["name"] and a["name"] in la]
    if named:
        return named[:3]
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
                    and (("art_list" not in e) or e.get("annot_v") != ANNOT_VERSION)]
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
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            shutil.copy2(real, dest)
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


def main():
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
    url = f"http://127.0.0.1:{PORT}"
    print(f"share-it running at {url}")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
