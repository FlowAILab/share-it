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
ck("inline carries bundle footer", "Full bundle" in text and built["md_path"] in text)

big = msgs_basic()
big[0]["text"] = "🦆" * 6000   # multibyte: 4 bytes/char → ~24KB, chars ~6k
built2 = bundle.build(SESSION, big)
kind2, text2 = bundle.compose_clipboard(SESSION, built2, [{"path": "/proj/out.pdf"}])
ck("multibyte payload over 16KiB → pointer (byte-measured)", kind2 == "pointer", kind2)
ck("pointer names the md path", built2["md_path"] in text2)
ck("pointer lists artifacts", "/proj/out.pdf" in text2)
ck("pointer notes retention lease", "7 days" in text2)

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

# ---- result renderer safety -------------------------------------------------
hostile = ('# Title\n<script>alert(1)</script>\n[x](javascript:alert(1))\n'
           '![img](https://evil.example/x.png)\n[ok](https://good.example)\n`code`')
h = render.result_clipboard_html(hostile)
ck("raw html escaped", "<script>" not in h and "&lt;script&gt;" in h)
ck("javascript: link not linkified", 'href="javascript:' not in h)
ck("no <img> ever emitted", "<img" not in h)
ck("https link allowed", 'href="https://good.example"' in h)

print()
if FAIL:
    print(f"{len(FAIL)} FAILURES: {FAIL}")
    sys.exit(1)
print("all bundle tests passed")
