"""Fixture tests for the client adapters (Pi, OpenCode, Goose, Continue, Cline,
Copilot). Builds synthetic stores in a tmpdir and asserts discover()/parse()
against each documented format. Run: python3 tests/clients.py"""
import json, os, sqlite3, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pi, opencode, goose, continuedev as cont, cline, copilot

TMP = tempfile.mkdtemp(prefix="shareit-clients-")
FAIL = []
def w(p, s): os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(s)
def ck(n, c, d=""):
    if not c: FAIL.append(n)
    print(f"  [{'ok' if c else 'FAIL'}] {n}" + ("" if c else f" — {d}"))

# Pi: tree walk (skip abandoned branch) + thinking separated
pd = os.path.join(TMP, "pi", "--p--"); os.makedirs(pd, exist_ok=True)
pi.ROOT = os.path.join(TMP, "pi")
w(os.path.join(pd, "s.jsonl"), "\n".join(json.dumps(x) for x in [
    {"type":"session","id":"S","cwd":"/p","timestamp":"2026-01-01T00:00:00Z"},
    {"type":"message","id":"e1","parentId":None,"message":{"role":"user","content":"q1"}},
    {"type":"message","id":"e2","parentId":"e1","message":{"role":"assistant","content":[{"type":"thinking","thinking":"secret"},{"type":"text","text":"a1"}]}},
    {"type":"message","id":"bX","parentId":"e1","message":{"role":"assistant","content":[{"type":"text","text":"ABANDONED"}]}},
    {"type":"message","id":"e3","parentId":"e2","message":{"role":"user","content":"q2"}},
]))
m = pi.parse(pi.discover()[0]["id"])
ck("pi leaf-ancestry (no abandoned)", "ABANDONED" not in [x["text"] for x in m])
ck("pi thinking separate role", any(x["role"]=="thinking" and x["text"]=="secret" for x in m))

# OpenCode v1 (message/part) and v2 (session_message)
for lay, build in (("v1", "mp"), ("v2", "sm")):
    od = os.path.join(TMP, "oc"+lay); os.makedirs(od, exist_ok=True)
    con = sqlite3.connect(os.path.join(od, "opencode.db"))
    con.execute("CREATE TABLE session(id TEXT,title TEXT,directory TEXT,time_created INT,time_updated INT)")
    con.execute("INSERT INTO session VALUES('s','OC','/o',1,1)")
    if build == "mp":
        con.execute("CREATE TABLE message(id TEXT,session_id TEXT,data TEXT)")
        con.execute("CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,data TEXT)")
        con.execute("INSERT INTO message VALUES('m1','s',?)", (json.dumps({"role":"user"}),))
        con.execute("INSERT INTO part VALUES('p1','m1','s',?)", (json.dumps({"type":"text","text":"hi"}),))
    else:
        con.execute("CREATE TABLE session_message(id TEXT,session_id TEXT,seq INT,type TEXT,data TEXT)")
        con.execute("INSERT INTO session_message VALUES('m1','s',1,'user',?)", (json.dumps({"text":"hi"}),))
        con.execute("INSERT INTO session_message VALUES('m2','s',2,'assistant',?)", (json.dumps({"content":[{"type":"reasoning","text":"hmm"},{"type":"text","text":"yo"}]}),))
    con.commit(); con.close()
    opencode._DATA = od
    m = opencode.parse(opencode.discover()[0]["id"])
    ck(f"opencode {lay} parse", m and m[0]["text"]=="hi", str(m))
    if lay=="v2":
        ck("opencode v2 assistant array-content", any(x["role"]=="assistant" and x["text"]=="yo" for x in m), str(m))
        ck("opencode v2 reasoning as thinking", any(x["role"]=="thinking" and x["text"]=="hmm" for x in m), str(m))

# Goose: RFC3339 text timestamps
gd = os.path.join(TMP, "goose", "sessions"); os.makedirs(gd, exist_ok=True)
con = sqlite3.connect(os.path.join(gd, "sessions.db"))
con.execute("CREATE TABLE sessions(id TEXT,name TEXT,description TEXT,working_dir TEXT,created_at TEXT,updated_at TEXT)")
con.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content_json TEXT)")
con.execute("INSERT INTO sessions VALUES('s','G',NULL,'/g','2026-08-08T00:00:00Z','2026-08-08T01:00:00Z')")
con.execute("INSERT INTO messages(session_id,role,content_json) VALUES('s','user',?)", (json.dumps([{"type":"text","text":"q"}]),))
con.commit(); con.close()
goose._DIR = gd; goose._DB = os.path.join(gd, "sessions.db")
d = goose.discover()
ck("goose text-ts no crash", d and d[0]["ts"] > 0, str(d))

# Continue
cs = os.path.join(TMP, "cont", "sessions"); os.makedirs(cs, exist_ok=True)
w(os.path.join(cs, "sessions.json"), json.dumps([{"sessionId":"a","title":"C","dateCreated":"1","workspaceDirectory":"file:///c"}]))
w(os.path.join(cs, "a.json"), json.dumps({"history":[{"message":{"role":"user","content":"u"}},{"message":{"role":"assistant","content":[{"type":"text","text":"a"}]}}]}))
cont._SESS = cs
m = cont.parse(cont.discover()[0]["id"])
ck("continue parse", [x["role"] for x in m]==["user","assistant"], str(m))

# Cline: strips <task>, availability via env
tk = os.path.join(TMP, "clb", "saoudrizwan.claude-dev", "tasks", "1"); os.makedirs(tk, exist_ok=True)
w(os.path.join(tk, "ui_messages.json"), json.dumps([{"ts":1,"type":"say","say":"task","text":"Title here"}]))
w(os.path.join(tk, "api_conversation_history.json"), json.dumps([
    {"role":"user","content":[{"type":"text","text":"<task>Title here</task> go"}]},
    {"role":"assistant","content":[{"type":"text","text":"done"}]}]))
cline._BASES = [os.path.join(TMP, "clb")]
d = cline.discover("saoudrizwan.claude-dev"); m = cline.parse(d[0]["id"])
ck("cline title + strip", d[0]["title"]=="Title here" and "task" not in m[0]["text"].lower())

# Copilot: jsonl replay — truncate-only(no v), append(list), later Set(title)
cp = os.path.join(TMP, "vsc", "workspaceStorage", "h", "chatSessions"); os.makedirs(cp, exist_ok=True)
w(os.path.join(cp, "s.jsonl"), "\n".join(json.dumps(x) for x in [
    {"kind":0,"v":{"customTitle":"T","creationDate":1,"requests":[
        {"message":{"text":"q1"},"response":[{"value":"a1"}]},
        {"message":{"text":"junk"},"response":[{"value":"junk"}]}]}},
    {"kind":2,"k":["requests"],"i":1},                       # truncate to 1 (drop junk turn)
    {"kind":2,"k":["requests"],"v":[{"message":{"text":"q2"},"response":[{"value":"a2"}]}]},  # append q2
    {"kind":1,"k":["customTitle"],"v":"Renamed"}]))          # later Set of the title
copilot._BASES = [os.path.join(TMP, "vsc")]
data = copilot._load(copilot.discover()[0]["id"])
ck("copilot truncate-only drops element (no stray None)", len(data["requests"])==2, len(data["requests"]))
m = copilot.parse(copilot.discover()[0]["id"])
ck("copilot final turns", [x["text"] for x in m]==["q1","a1","q2","a2"], str([x["text"] for x in m]))
ck("copilot later Set updates title", copilot.discover()[0]["title"]=="Renamed", copilot.discover()[0]["title"])

# Pi PI_CODING_AGENT_DIR → <dir>/sessions (not <dir>/agent/sessions)
os.environ["PI_CODING_AGENT_DIR"] = os.path.join(TMP, "piagent")
try:
    ck("pi PI_CODING_AGENT_DIR depth", pi._root() == os.path.join(TMP, "piagent", "sessions"), pi._root())
finally:
    del os.environ["PI_CODING_AGENT_DIR"]

# non-dict JSON line must not crash discovery
nd = os.path.join(TMP, "pind", "--x--"); os.makedirs(nd, exist_ok=True)
pi.ROOT = os.path.join(TMP, "pind")
w(os.path.join(nd, "s.jsonl"), "\n".join([
    "[1,2,3]",
    json.dumps({"type":"session","id":"S","cwd":"/x"}),
    json.dumps({"type":"message","id":"e","parentId":None,"message":{"role":"user","content":"hey"}}),
    ""]))
try:
    d = pi.discover(); ck("pi tolerates non-dict line", bool(d) and pi.parse(d[0]["id"]))
except Exception as e:
    ck("pi tolerates non-dict line", False, repr(e))

print()
if FAIL:
    print(f"{len(FAIL)} FAILURES: {FAIL}"); sys.exit(1)
print("all client-adapter tests passed")
