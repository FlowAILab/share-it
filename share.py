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
import urllib.parse
import urllib.request

USER_AGENT = "share-it/0.1 (local session viewer)"
EXPORT_SCHEMA_VERSION = 4  # v4: hosted R2 backend only
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
        status, _ = _http(hosted["url"].rstrip("/") + "/" + entry["ref"], method="DELETE",
                          headers={"X-Share-Token": hosted["token"]})
        if status != 200:
            raise OSError(f"hosted delete {status}")
        return
    raise OSError("this link's backend is retired; it expires on its own")


# ---------------- local share log ----------------

def load_shares():
    try:
        with open(SHARES_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
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


def record_share(session, result, opts, size, artifact=None):
    entry = {
        "url": result["url"], "provider": result["provider"], "ref": result["ref"],
        "title": (os.path.basename(artifact) if artifact else session["title"]),
        "source": session["source"], "path": session["path"], "artifact": artifact,
        "src_mtime": _src_mtime(session, artifact),
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
    }
    with _LOCK:
        shares = load_shares()
        shares.insert(0, entry)
        _state_file_write(SHARES_PATH, json.dumps(shares, indent=1))
    return entry


def find_cached(session, opts, artifact=None):
    """A live earlier share of the same bytes with the same options."""
    live_mtime = _src_mtime(session, artifact)
    now = time.time()
    for s in load_shares():
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
                and s.get("export_v") == EXPORT_SCHEMA_VERSION
                and s["thinking"] == bool(opts.get("thinking"))):
            return s
    return None


def mark_deleted(url):
    with _LOCK:
        shares = load_shares()
        for s in shares:
            if s["url"] == url:
                s["deleted"] = True
        _state_file_write(SHARES_PATH, json.dumps(shares, indent=1))
