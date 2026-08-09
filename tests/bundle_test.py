"""Unit tests for bundle.py + the Send-result completion rule.
Run: python3 tests/bundle_test.py"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bundle
import render

FAIL = []
def ck(n, c, d=""):
    if not c:
        FAIL.append(n)
    print(f"  [{'ok' if c else 'FAIL'}] {n}" + ("" if c else f" — {d}"))

TMP = tempfile.mkdtemp(prefix="shareit-bundle-")
bundle.EXPORT_ROOT = os.path.join(TMP, "exports")

SESSION = {"source": "claude", "path": "/x/s1.jsonl", "title": "test sess",
           "cwd": "/proj", "mtime": 1000.0}

def msgs_basic():
    return [
        {"role": "user", "text": "first request: build the thing"},
        {"role": "assistant", "text": "on it", "stop": "tool_use"},
        {"role": "tool", "name": "Bash", "input": '{"command": "make"}',
         "output": "built ok", "ok": True},
        {"role": "assistant", "text": "done — thing built", "stop": "end_turn"},
    ]

# ---- threshold: measured on the COMPOSED payload in UTF-8 bytes -------------
built = bundle.build(SESSION, msgs_basic())
kind, text = bundle.compose_clipboard(SESSION, built, [])
ck("small session → inline", kind == "inline")
ck("inline carries file pointer footer", "Full session on disk" in text and built["md_path"] in text)

big = msgs_basic()
big[0]["text"] = "🦆" * 6000   # multibyte: 4 bytes/char → ~24KB, chars ~6k
built2 = bundle.build(SESSION, big)
kind2, text2 = bundle.compose_clipboard(SESSION, built2, [{"path": "/proj/out.pdf"}])
ck("multibyte payload over 16KiB → pointer (byte-measured)", kind2 == "pointer", kind2)
ck("pointer names the md path", built2["md_path"] in text2)
ck("pointer lists artifacts", "/proj/out.pdf" in text2)
ck("pointer is neutral (no continue/retention noise)",
   "continue" not in text2.lower() and "7 day" not in text2.lower()
   and "Context of a" in text2, text2[:80])

# ---- generations: immutable, distinct, atomic -------------------------------
ck("distinct generations per build", built["dir"] != built2["dir"])
ck("gen dir exists with session.md", os.path.isfile(built["md_path"]))
ck("no tmp dirs left behind", not [d for d in os.listdir(os.path.dirname(built["dir"]))
                                   if d.startswith(".tmp-")])
mode = os.stat(built["dir"]).st_mode & 0o777
ck("gen dir 0700", mode == 0o700, oct(mode))

# concurrent builds race → both publish, different gens
outs = []
def _one():
    outs.append(bundle.build(SESSION, msgs_basic())["dir"])
ts = [threading.Thread(target=_one) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
ck("4 concurrent builds → 4 distinct gens", len(set(outs)) == 4, outs)

# ---- truncation budgets -----------------------------------------------------
def tool(name, out_len, ok=True, marker="X"):
    # spaced filler — must NOT look like a base64 run, or the b64 stripper eats it
    return {"role": "tool", "name": name, "input": '{"command": "x"}',
            "output": (marker + " ") * out_len, "ok": ok}

msgs = [{"role": "user", "text": "task"}]
msgs += [tool("Bash", 50_000, ok=True, marker=f"B{i}") for i in range(3)]
msgs += [tool("Bash", 50_000, ok=False, marker="F1")]      # failed → 4×
msgs += [tool("Read", 50_000, ok=True, marker="R1")]
msgs += [{"role": "assistant", "text": "end", "stop": "end_turn"}]
body, _ = bundle.render_transcript(SESSION, msgs, deep=False)
def seg(marker):
    return sum(l.count(marker) for l in body.splitlines())
ck("failed Bash keeps more output than ok Bash", seg("F1") > seg("B0") * 1.5,
   f"F1={seg('F1')} B0={seg('B0')}")
ck("Read tighter than Bash", seg("R1") <= seg("B0"), f"R1={seg('R1')} B0={seg('B0')}")
ck("read truncation names the re-read path", "re-read at" in body)
ck("failed call marked in header", "✗ FAILED" in body)

# Write inputs keep more than Read outputs
msgs_w = [{"role": "user", "text": "t"},
          {"role": "tool", "name": "Write",
           "input": '{"file_path": "/proj/a.py", "content": "' + "W " * 10_000 + '"}',
           "output": "ok", "ok": True},
          {"role": "tool", "name": "Read", "input": '{"file_path": "/proj/b.py"}',
           "output": "r " * 10_000, "ok": True}]
bw, _ = bundle.render_transcript(SESSION, msgs_w, deep=False)
ck("Write input budget > Read output budget", bw.count("W") > bw.count("r") * 2,
   f"W={bw.count('W')} r={bw.count('r')}")

# ---- Tier A survives a tiny global cap --------------------------------------
manym = [{"role": "user", "text": "INITIAL-REQ build the exporter"}]
for i in range(60):
    manym.append(tool("Bash", 3000, marker=f"T{i:02d}"))
    manym.append({"role": "assistant", "text": f"note {i} " + "a " * 750})
manym.append({"role": "user", "text": "LATEST-REQ now fix the bug"})
manym.append({"role": "assistant", "text": "FINAL-ANSWER fixed", "stop": "end_turn"})
tiny, meta = bundle.render_transcript(SESSION, manym, deep=False, global_cap=40_000)
ck("cap → items elided", meta["elided"] > 0, meta)
ck("initial user request survives cap", "INITIAL-REQ" in tiny)
ck("latest user request survives cap", "LATEST-REQ" in tiny)
ck("final answer survives cap", "FINAL-ANSWER" in tiny)
ck("elision notice present", "one-line headers" in tiny)
ck("newest tool favored over oldest", ("T59" in tiny.replace("### Tool", ""))
   or tiny.count("T59") >= tiny.count("T00"))

# ---- redact BEFORE truncate: secret can't straddle a boundary ---------------
secret = "api_key=sk-" + "a" * 40
# secret in the surviving head region: with redact-first it MUST appear as
# REDACTED; with truncate-first a boundary could split it and leak half
padded = "x " * 200 + secret + " y" * 4000
msgs_s = [{"role": "user", "text": "t"},
          {"role": "tool", "name": "Bash", "input": "{}", "output": padded, "ok": True}]
bs, _ = bundle.render_transcript(SESSION, msgs_s, deep=False)
ck("secret fully redacted despite truncation", "sk-aaa" not in bs and "REDACTED" in bs)

# ---- local vs remote header asymmetry ---------------------------------------
arts = [{"path": "/proj/report.pdf", "size": 10, "kind": "created", "name": "report.pdf"},
        {"path": "/elsewhere/keys.txt", "size": 5, "kind": "modified", "name": "keys.txt"}]
reads = [{"path": "/proj/src/a.py", "size": 1}, {"path": "/private/other.txt", "size": 2}]
loc = bundle.header_md(SESSION, msgs_basic(), arts, reads, remote=False, resume_cmd="claude --resume z")
rem = bundle.header_md(SESSION, msgs_basic(), arts, reads, remote=True, resume_cmd="claude --resume z")
ck("local header has resume", "claude --resume z" in loc)
ck("remote header omits resume", "claude --resume z" not in rem)
ck("local shows absolute cwd", "`/proj`" in loc)
ck("remote shows basename cwd only", "`proj`" in rem and "`/proj`" not in rem)
ck("remote: in-workspace path relative", "`report.pdf`" in rem)
ck("remote: outside-workspace marked, no abs path",
   "outside workspace" in rem and "/elsewhere" not in rem and "/private" not in rem)
ck("local keeps absolute artifact paths", "/proj/report.pdf" in loc)

# remote transcript BODY: home prefix must not leak the username
import os as _os
home_msgs = [{"role": "user", "text": "t"},
             {"role": "tool", "name": "Bash",
              "input": '{"command": "cat ' + _os.path.expanduser("~") + '/notes.txt"}',
              "output": "from " + _os.path.expanduser("~") + "/notes.txt", "ok": True}]
rb, _ = bundle.render_transcript(SESSION, home_msgs, remote=True)
lb, _ = bundle.render_transcript(SESSION, home_msgs, remote=False)
ck("remote body: home prefix → ~", _os.path.expanduser("~") not in rb and "~/notes.txt" in rb)
ck("local body keeps real home path", _os.path.expanduser("~") + "/notes.txt" in lb)

# ---- media refs: sniff + unavailability markers -----------------------------
png = os.path.join(TMP, "ok.png")
open(png, "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
notimg = os.path.join(TMP, "evil.txt")
open(notimg, "w").write("root:x:0:0")
msgs_m = [{"role": "user", "text": "here",
           "media_refs": [{"path": png}, {"path": notimg}, {"path": "/nope/gone.png"}]}]
objs, skipped = bundle.resolve_media(msgs_m)
ck("valid png resolved", any(o["content_type"] == "image/png" for o in objs))
ck("non-image + missing skipped", skipped == 2, skipped)
bm, _ = bundle.render_transcript(SESSION, msgs_m)
ck("[image unavailable] marker for skipped refs", bm.count("[image unavailable]") == 2, bm)
ck("resolved image gets a media link", "![pasted image]" in bm)

# ---- hard cap: final body NEVER exceeds global_cap --------------------------
for cap in (1024, 8_192, 40_000):
    hb, hm = bundle.render_transcript(SESSION, manym, deep=False, global_cap=cap)
    ck(f"hard cap holds at {cap}", len(hb.encode()) <= cap,
       f"body={len(hb.encode())}B > cap={cap}")

# ---- tool-header redaction (secrets in commands) ----------------------------
hdr_msgs = [{"role": "user", "text": "t"},
            {"role": "tool", "name": "Bash",
             "input": '{"command": "curl -H \'Authorization: Bearer sk-aaaaaaaaaaaaaaaaaaaaaaaaa\' api.x.com"}',
             "output": "ok", "ok": True}]
hb2, _ = bundle.render_transcript(SESSION, hdr_msgs)
ck("tool header redacts inline secrets", "sk-aaaa" not in hb2 and "REDACTED" in hb2)

# ---- Edit tool renders old/new hunks ----------------------------------------
em = [{"role": "user", "text": "t"},
      {"role": "tool", "name": "Edit",
       "input": '{"file_path": "/proj/x.py", "old_string": "OLDCODE line", "new_string": "NEWCODE line"}',
       "output": "done", "ok": True}]
eb, _ = bundle.render_transcript(SESSION, em)
ck("edit renders old/new hunks", "--- old" in eb and "+++ new" in eb
   and "OLDCODE" in eb and "NEWCODE" in eb)

# ---- media-ref containment: system paths refused even when they're images ---
import shutil as _sh
sys_img = "/tmp/shareit-test-root.png"       # /tmp allowed locally, NOT remotely
_sh.copy(png, sys_img)
m_loc = [{"role": "user", "text": "x", "media_refs": [{"path": sys_img}]}]
o1, s1 = bundle.resolve_media([dict(m) for m in m_loc], remote=False)
m_rem = [{"role": "user", "text": "x", "media_refs": [{"path": sys_img}]}]
o2, s2 = bundle.resolve_media([dict(m) for m in m_rem], remote=True)
ck("local mode allows /tmp image ref", s1 == 0 and len(o1) == 1, (s1, len(o1)))
ck("remote mode refuses non-agent-dir ref", s2 == 1 and len(o2) == 0, (s2, len(o2)))
etc_ref = [{"role": "user", "text": "x", "media_refs": [{"path": "/etc/hosts"}]}]
o3, s3 = bundle.resolve_media(etc_ref, remote=False)
ck("system path refused everywhere", s3 == 1 and not o3)
os.remove(sys_img)

# ---- GC: lease + tmp cleanup ------------------------------------------------
old_gen = os.path.join(bundle.EXPORT_ROOT, "claude-deadbeef0000", "aaaa11112222")
os.makedirs(old_gen)
open(os.path.join(old_gen, "session.md"), "w").write("x")
os.utime(old_gen, (100, 100))                       # ancient → beyond lease
stale_tmp = os.path.join(bundle.EXPORT_ROOT, "claude-deadbeef0000", ".tmp-1-zz")
os.makedirs(stale_tmp)
os.utime(stale_tmp, (100, 100))
bundle.MAX_GENERATIONS = 3                          # force LRU pressure
bundle.gc()
ck("fresh generations survive GC (lease)", os.path.isdir(built["dir"]))
ck("ancient gen collected under pressure", not os.path.isdir(old_gen))
ck("stale tmp collected", not os.path.isdir(stale_tmp))

# ---- completion rule (_completed_result) ------------------------------------
import importlib.util
spec = importlib.util.spec_from_file_location(
    "app_mod", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"))
app_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_mod)
cr = app_mod._completed_result

ck("claude completed → final end_turn",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "working", "stop": "tool_use"},
       {"role": "assistant", "text": "DONE", "stop": "end_turn"}])["text"] == "DONE")
ck("claude interrupted (tool_use tail) → None",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "let me check", "stop": "tool_use"}]) is None)
ck("codex stale prior-turn final_answer NOT reused",
   cr([{"role": "user", "text": "q1"},
       {"role": "assistant", "text": "ANSWER-1", "phase": "final_answer"},
       {"role": "user", "text": "q2"},
       {"role": "assistant", "text": "hmm working", "phase": "commentary"}]) is None)
ck("codex completed turn-2 → its final_answer",
   cr([{"role": "user", "text": "q1"},
       {"role": "assistant", "text": "ANSWER-1", "phase": "final_answer"},
       {"role": "user", "text": "q2"},
       {"role": "assistant", "text": "ANSWER-2", "phase": "final_answer"}])["text"] == "ANSWER-2")
ck("metadata-less client → last assistant best-effort",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "A"},
       {"role": "assistant", "text": "B"}])["text"] == "B")
ck("no assistant after last user → None",
   cr([{"role": "assistant", "text": "old"}, {"role": "user", "text": "q"}]) is None)
ck("media_refs-only user message counts as latest request",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "A1", "phase": "final_answer"},
       {"role": "user", "text": "", "media_refs": [{"path": "/x.png"}]},
       {"role": "assistant", "text": "hmm", "phase": "commentary"}]) is None)
ck("null-metadata tail with metadata elsewhere → incomplete (None)",
   cr([{"role": "user", "text": "q1"},
       {"role": "assistant", "text": "done", "stop": "end_turn"},
       {"role": "user", "text": "q2"},
       {"role": "assistant", "text": "streaming...", "stop": None}]) is None)
ck("FIRST-turn claude interruption (only stop:null) → None",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "partial explanation", "stop": None}]) is None)
ck("old codex without phase field anywhere → fallback works",
   cr([{"role": "user", "text": "q"},
       {"role": "assistant", "text": "OLD-FMT-ANSWER"}])["text"] == "OLD-FMT-ANSWER")

# ---- result renderer safety -------------------------------------------------
hostile = ('# Title\n<script>alert(1)</script>\n[x](javascript:alert(1))\n'
           '![img](https://evil.example/x.png)\n[ok](https://good.example)\n`code`')
h = render.result_clipboard_html(hostile)
ck("raw html escaped", "<script>" not in h and "&lt;script&gt;" in h)
ck("javascript: link not linkified", 'href="javascript:' not in h)
ck("no <img> ever emitted", "<img" not in h)
ck("https link allowed", 'href="https://good.example"' in h)


# ---- provenance: spoofed text-marker refs vs structured refs (remote) -------
bundle._REF_ROOTS_REMOTE = (TMP,)          # containment root = our tmp sandbox
bundle._REF_ROOTS_LOCAL = (TMP,)
spoof = [{"role": "user", "text": "x",
          "media_refs": [{"path": png, "structured": False}]}]
o_s, k_s = bundle.resolve_media([dict(m) for m in spoof], remote=True)
ck("remote: spoofed text-marker ref inside allowed root still refused",
   k_s == 1 and not o_s)
o_l, k_l = bundle.resolve_media([dict(m) for m in spoof], remote=False)
ck("local: same ref resolves", k_l == 0 and len(o_l) == 1)
struct = [{"role": "user", "text": "x",
           "media_refs": [{"path": png, "structured": True}]}]
o_t, k_t = bundle.resolve_media(struct, remote=True)
ck("remote: structured ref inside allowed root resolves", k_t == 0 and len(o_t) == 1)

# ---- file-backed refs get metadata-stripped ---------------------------------
import struct as _st
def _chunk(t, d):
    return _st.pack(">I", len(d)) + t + d + b"\x00\x00\x00\x00"
meta_png = os.path.join(TMP, "meta.png")
open(meta_png, "wb").write(b"\x89PNG\r\n\x1a\n" + _chunk(b"tEXt", b"GPS secret loc")
                           + _chunk(b"IEND", b""))
mm = [{"role": "user", "text": "x", "media_refs": [{"path": meta_png, "structured": True}]}]
om, _ = bundle.resolve_media(mm, remote=True)
ck("EXIF/text chunks stripped from file-backed refs",
   om and b"GPS secret loc" not in om[-1]["data"])

# ---- sub-notice caps + exact inline boundary --------------------------------
tb, _ = bundle.render_transcript(SESSION, manym, deep=False, global_cap=100)
ck("sub-notice cap (100B) holds", len(tb.encode()) <= 100, len(tb.encode()))
pad_doc = bundle.build(SESSION, [{"role": "user", "text": "q"},
                                 {"role": "assistant", "text": "a", "stop": "end_turn"}])
FOOTER = "\n---\nFull session on disk: " + pad_doc["md_path"] + "\n"
k_at, _t1 = bundle.compose_clipboard(SESSION, pad_doc, [],
    inline_limit=len((pad_doc["doc"] + FOOTER).encode()))
k_under, _t2 = bundle.compose_clipboard(SESSION, pad_doc, [],
    inline_limit=len((pad_doc["doc"] + FOOTER).encode()) - 1)
ck("payload == limit → inline", k_at == "inline", k_at)
ck("payload == limit+1 → pointer", k_under == "pointer", k_under)

# ---- FTS: title-only change triggers reindex --------------------------------
import search
search.DB_PATH = os.path.join(TMP, "fts.sqlite")
search.index_session("/x/p.jsonl", 5.0, [{"role": "user", "text": "hello"}],
                     title="old name", size=10)
ck("fts: unchanged → no reindex",
   not search.needs_index("/x/p.jsonl", 5.0, 10, title="old name"))
ck("fts: title-only change → reindex",
   search.needs_index("/x/p.jsonl", 5.0, 10, title="Official New Name"))

# ---- share cache: title change invalidates ----------------------------------
import share as _share
_share.SHARES_PATH = os.path.join(TMP, "shares.json")
sess_t = {"source": "codex", "path": "/x/t.jsonl", "title": "derived words",
          "mtime": 7.0}
_share._src_mtime = lambda s, artifact=None: 7.0
rec = _share.record_share(sess_t, {"url": "https://x/1", "provider": "hosted",
                                   "ref": "b/1", "hours": None}, {"expires_hours": 0,
                                   "redact": True, "mode": "agent", "fmt": "md",
                                   "art_mtime": 0, "artifacts": True,
                                   "thinking": False}, 100, file_paths=[])
opts_t = {"expires_hours": 0, "redact": True, "mode": "agent", "fmt": "md",
          "art_mtime": 0, "artifacts": True, "thinking": False}
ck("share cache hit with same title", _share.find_cached(sess_t, opts_t) is not None)
sess_t2 = dict(sess_t, title="Official Thread Name")
ck("share cache MISS after title-only rename",
   _share.find_cached(sess_t2, opts_t) is None)

# ---- codex official-title sources (sqlite + session_index overlay) ----------
import sqlite3 as _sq, parsers as _p
fake_home = os.path.join(TMP, "codexhome"); os.makedirs(fake_home, exist_ok=True)
rollout = os.path.join(TMP, "rollout-2026-01-01T00-00-00-"
                       + "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000.jsonl")
open(rollout, "w").write("{}")
con = _sq.connect(os.path.join(fake_home, "state_9.sqlite"))
con.execute("CREATE TABLE threads (rollout_path TEXT, title TEXT, name TEXT, "
            "first_user_message TEXT, preview TEXT, cwd TEXT, source TEXT, "
            "model TEXT, tokens_used INT, git_branch TEXT)")
con.execute("INSERT INTO threads VALUES (?, 'DB Title', NULL, 'raw first msg', "
            "'', '/w', 'user', 'gpt', 5, 'main')", (rollout,))
con.commit(); con.close()
open(os.path.join(fake_home, "session_index.jsonl"), "w").write(
    '{"id": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000", "thread_name": "Sidebar Name", "updated_at": "x"}\n')
_old_home = _p._CODEX_HOME; _p._CODEX_HOME = fake_home
try:
    idx = _p._codex_sqlite_index()
    hit = idx.get(os.path.realpath(rollout))
    ck("codex sqlite title read", hit is not None and hit[0] in ("DB Title", "Sidebar Name"))
    ck("session_index thread_name overlays sqlite title",
       hit is not None and hit[0] == "Sidebar Name", hit and hit[0])
finally:
    _p._CODEX_HOME = _old_home



# ---- forbidden refs are never opened (FIFO would block any open()) ----------
fifo = os.path.join(TMP, "trap.fifo")
os.mkfifo(fifo)
import time as _tm
t0 = _tm.time()
try:
    bundle.read_ref({"path": fifo, "structured": True}, remote=False)
    opened = True
except ValueError:
    opened = False
ck("FIFO ref refused WITHOUT opening (no hang)", not opened and _tm.time() - t0 < 1.0)
try:
    bundle.read_ref({"path": "/dev/zero", "structured": True}, remote=False)
    dev_ok = False
except (ValueError, OSError):
    dev_ok = True
ck("/dev/zero refused by regular-file gate", dev_ok)


# ---- fingerprint caller never opens forbidden refs (instrumented open) ------
import builtins
app_mod._effective_files = lambda sess, opts: []
class _StubAd:
    def parse(self, p):
        return [{"role": "user", "text": "x",
                 "media_refs": [{"path": "/etc/hosts", "structured": False},
                                {"path": "/etc/hosts", "structured": True}]}]
app_mod.adapters.by_id = lambda src: _StubAd()
_real_open = builtins.open
_opened = []
def _spy(path, *a, **k):
    if isinstance(path, str) and path.startswith("/etc/"):
        _opened.append(path)
    return _real_open(path, *a, **k)
builtins.open = _spy
try:
    app_mod._artifact_fingerprint({"source": "codex", "path": "/x/f.jsonl",
                                   "title": "t", "mtime": 1.0}, {"files": None})
finally:
    builtins.open = _real_open
ck("fingerprint never opens forbidden refs", not _opened, _opened)

print()
if FAIL:
    print(f"{len(FAIL)} FAILURES: {FAIL}")
    sys.exit(1)
print("all bundle tests passed")
