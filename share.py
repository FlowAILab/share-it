"""Upload rendered transcripts and keep a local log of shares.

One backend, zero fallbacks: the hosted R2 worker (Cloudflare). Expiry (∞/1d/3d/7d)
is enforced by the worker; delete is immediate. If the worker is unreachable,
sharing fails with a clear error — nothing else is tried.
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "share-it/0.1 (local session viewer)"
EXPORT_SCHEMA_VERSION = 6  # v6: absolute media urls, declared artifacts, honest counts
EXPIRES_HOURS = 0  # default: no expiry (public object)
STATE_DIR = os.path.expanduser("~/.shareit")
SHARES_PATH = os.path.join(STATE_DIR, "shares.json")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
_LOCK = threading.Lock()


def _state_file_write(path, data):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w") as fh:
        fh.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _http(url, data=None, method="GET", headers=None, timeout=30):
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# ---------------- config ----------------

BUNDLED_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_config.json")


def _load_config():
    cfg = {}
    try:  # bundled defaults (ship the hosted backend so the DMG works out of the box)
        with open(BUNDLED_CONFIG) as fh:
            cfg.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    try:  # user overrides win
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _save_config(cfg):
    _state_file_write(CONFIG_PATH, json.dumps(cfg, indent=1))



















# ---------------- providers ----------------

def _hosted_config():
    """{url, token} for the hosted R2 worker — the zero-setup backend."""
    h = _load_config().get("hosted")
    return h if h and h.get("url") and h.get("token") else None


def _up_hosted(body, ctype, expires_hours, name=""):
    hosted = _hosted_config()
    if not hosted:
        raise OSError("hosted uploader not configured")
    status, data = _http(hosted["url"].rstrip("/") + "/up", data=body, method="POST",
                         headers={"Content-Type": ctype,
                                  "X-Share-Token": hosted["token"],
                                  "X-Expiry-Hours": str(expires_hours),
                                  "X-Name": name,
                                  "Content-Length": str(len(body))})
    if status != 200:
        raise OSError(f"hosted upload {status}")
    out = json.loads(data)
    return {"url": out["url"], "provider": "hosted", "ref": out["key"],
            "hours": out["hours"]}







def _bundle_call(path_part, data=None, headers=None, method="POST"):
    hosted = _hosted_config()
    if not hosted:
        raise OSError("hosted uploader not configured")
    h = {"X-Share-Token": hosted["token"]}
    h.update(headers or {})
    try:
        status, body = _http(hosted["url"].rstrip("/") + path_part, data=data,
                             method=method, headers=h)
    except urllib.error.HTTPError as e:  # non-2xx must not escape rollback
        return e.code, e.read()
    return status, body


def upload_bundle(objects, index_name, expires_hours=EXPIRES_HOURS, bundle_id=None):
    """Commit-semantics multi-object share.

    objects: [{name, data(bytes), content_type}] — index included. All upload
    concurrently; manifest.json is written LAST (the commit). Any failure
    aborts loudly, listing exactly what failed, and clears staging.
    """
    if bundle_id is None:
        bundle_id = new_bundle_id()
    hosted = _hosted_config()
    base = hosted["url"].rstrip("/")
    from concurrent.futures import ThreadPoolExecutor

    def put(o):
        try:
            status, _ = _bundle_call(f"/bundle/{bundle_id}/{o['name']}", data=o["data"],
                                     headers={"Content-Type": o["content_type"],
                                              "Content-Length": str(len(o["data"]))})
            return o["name"] if status != 200 else None
        except OSError:
            return o["name"]

    def _abort(reason):
        try:  # best-effort staging cleanup — the daily sweep is the backstop
            _bundle_call(f"/b/{bundle_id}", method="DELETE")
        except Exception:
            pass
        err = OSError(f"{reason} — nothing was shared")
        err._shareit_final = True
        raise err

    try:  # ANY transport/parse failure must roll back, not just clean non-200s
        with ThreadPoolExecutor(max_workers=6) as pool:
            failed = [f for f in pool.map(put, objects) if f]
        if failed:
            _abort(f"upload failed for: {', '.join(failed)}")
        del_key = secrets.token_urlsafe(16)
        manifest = {"index": index_name, "hours": expires_hours, "delKey": del_key,
                    "objects": [{"name": o["name"], "size": len(o["data"])} for o in objects]}
        status, body = _bundle_call(f"/bundle/{bundle_id}/commit",
                                    data=json.dumps(manifest).encode(),
                                    headers={"Content-Type": "application/json"})
        if status != 200:
            _abort(f"share commit failed ({status})")
        out = json.loads(body)
        return {"url": out["url"], "provider": "hosted", "ref": f"b/{bundle_id}",
                "hours": out["hours"], "del_key": del_key}
    except Exception as e:
        if getattr(e, "_shareit_final", False):
            raise  # already rolled back
        _abort(f"share failed ({e.__class__.__name__})")


def new_bundle_id():
    status, body = _bundle_call("/bundle/new")
    if status != 200:
        raise OSError(f"bundle create failed ({status})")
    return json.loads(body)["id"]


def upload(text, expires_hours=EXPIRES_HOURS, fmt="md"):
    suffix = ".html" if fmt == "html" else ".md"
    ctype = ("text/html; charset=utf-8" if fmt == "html"
             else "text/markdown; charset=utf-8")
    return _up_hosted(text.encode(), ctype, expires_hours, name="")


def upload_file(path, expires_hours=EXPIRES_HOURS):
    """Share an arbitrary file (session artifact) — S3 first, hosted worker fallback."""
    import mimetypes
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        body = fh.read()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))
    return _up_hosted(body, ctype, expires_hours, name=name)


def delete(entry):
    provider = entry.get("provider")
    if provider == "hosted":
        hosted = _hosted_config()
        if not hosted:
            raise OSError("hosted uploader not configured")
        hdrs = {"X-Share-Token": hosted["token"]}
        if entry.get("del_key"):
            hdrs["X-Del-Key"] = entry["del_key"]
        status, _ = _http(hosted["url"].rstrip("/") + "/" + entry["ref"], method="DELETE",
                          headers=hdrs)
        if status != 200:
            raise OSError(f"hosted delete {status}")
        return
    raise OSError("this link's backend is retired; it expires on its own")


# ---------------- local share log ----------------

def load_shares():
    try:
        with open(SHARES_PATH) as fh:
            data = json.load(fh)
        return [s for s in data if isinstance(s, dict) and "url" in s] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        # corrupt history is precious — preserve it instead of overwriting with []
        try:
            os.replace(SHARES_PATH, SHARES_PATH + ".corrupt")
        except OSError:
            pass
        return []


def _src_mtime(session, artifact=None):
    """mtime of what was shared; synthetic ids (Cursor db#composer) aren't files."""
    try:
        return os.path.getmtime(artifact or session["path"])
    except OSError:
        return session.get("mtime", 0)


def _snapshot(paths):
    """sha256+size of each file at share time — what the link actually carried."""
    import hashlib
    out = []
    for p in paths or []:
        try:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out.append({"path": p, "sha256": h.hexdigest(), "size": os.path.getsize(p)})
        except OSError:
            out.append({"path": p, "sha256": None, "size": None})
    return out


def record_share(session, result, opts, size, artifact=None, file_paths=None,
                 counts=None, src_mtime=None):
    entry = {
        "url": result["url"], "provider": result["provider"], "ref": result["ref"],
        "title": (os.path.basename(artifact) if artifact else session["title"]),
        "source": session["source"], "path": session["path"], "artifact": artifact,
        # mtime CAPTURED BEFORE render — if the transcript grew during upload,
        # the cache stores the bytes we actually shipped, so a request at the
        # new mtime misses and re-renders instead of serving stale content
        "src_mtime": src_mtime if src_mtime is not None else _src_mtime(session, artifact),
        "created": time.time(),
        "expires": None if result["hours"] is None else time.time() + result["hours"] * 3600,
        "req_hours": opts.get("expires_hours", EXPIRES_HOURS),
        "redacted": bool(opts.get("redact", True)),
        "mode": opts.get("mode", "full"), "fmt": opts.get("fmt", "md"),
        "art_mtime": opts.get("art_mtime", 0),
        "with_files": bool(opts.get("artifacts", True)),
        "export_v": EXPORT_SCHEMA_VERSION,
        "messages_only": bool(opts.get("messages_only")),
        "thinking": bool(opts.get("thinking")), "size": size, "deleted": False,
        "del_key": result.get("del_key"),
        "snapshot": _snapshot(file_paths if file_paths is not None
                              else ([artifact] if artifact else [])),
        **(counts or {}),
    }
    with _LOCK:
        shares = load_shares()
        shares.insert(0, entry)
        _state_file_write(SHARES_PATH, json.dumps(shares, indent=1))
    return entry


def _write_shares(shares):
    with _LOCK:
        _state_file_write(SHARES_PATH, json.dumps(shares, indent=1))


def find_cached(session, opts, artifact=None):
    """A live earlier share of the same bytes with the same options."""
    live_mtime = _src_mtime(session, artifact)
    now = time.time()
    for s in load_shares():
        # export_v gate FIRST — legacy records lack the fields dereferenced below
        if s.get("export_v") != EXPORT_SCHEMA_VERSION:
            continue
        if (s["path"] == session["path"] and not s["deleted"]
                and s.get("artifact") == artifact
                and s.get("src_mtime") == live_mtime
                and s.get("req_hours") == opts.get("expires_hours", EXPIRES_HOURS)
                and (s["expires"] is None or s["expires"] > now + 1800)
                and s["redacted"] == bool(opts.get("redact", True))
                and s.get("mode", "full") == opts.get("mode", "full")
                and s.get("fmt", "md") == opts.get("fmt", "md")
                and (artifact or s.get("art_mtime", 0) == opts.get("art_mtime", 0))
                and (artifact or s.get("with_files", True) == bool(opts.get("artifacts", True)))
                and s.get("thinking") == bool(opts.get("thinking"))):
            return s
    return None


def mark_deleted(url):
    with _LOCK:
        shares = load_shares()
        for s in shares:
            if s["url"] == url:
                s["deleted"] = True
        _state_file_write(SHARES_PATH, json.dumps(shares, indent=1))
