"""Local web UI for tomeshift: `tomeshift ui` -> http://127.0.0.1:8787

Standard library only. Every action runs the normal CLI as a subprocess, so the UI and the CLI
always behave the same, and a run started here keeps going exactly like `tomeshift run`.
"""

import json
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLI = [sys.executable, str(Path(__file__).resolve().parent / "tomeshift.py")]
RUNS: dict[str, subprocess.Popen] = {}  # project name -> Popen of `tomeshift run`
JOBS: dict[str, dict] = {}  # project name -> {"step": ..., "ok": bool, "out": str} for prep/glossary/build


def safe(name):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name or "").strip(".-")
    if not name:
        raise ValueError("bad project name")
    return name


def make_handler(root, langs, backends):
    root = Path(root).resolve()

    def proj(q):
        return root / safe(q.get("p", [""])[0])

    def step(name, args):
        """Run a short CLI step in the background; the page polls /api/status for the result."""
        JOBS[name] = {"step": args[0], "ok": None, "out": ""}

        def go():
            r = subprocess.run(CLI + args, capture_output=True, text=True)
            JOBS[name] = {"step": args[0], "ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[-1500:]}

        threading.Thread(target=go, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = (
                body
                if isinstance(body, bytes)
                else (json.dumps(body) if ctype == "application/json" else body).encode()
            )
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/":
                    return self.send(200, PAGE, "text/html")
                if u.path == "/favicon.ico":
                    return self.send(200, ICON, "image/svg+xml")
                if u.path == "/api/meta":
                    return self.send(
                        200,
                        {
                            "langs": langs,
                            "backends": [b for b in backends if b in API_BACKENDS or shutil.which(CMD_OF.get(b, b))],
                            "all_backends": backends,
                            "root": str(root),
                        },
                    )
                if u.path == "/api/projects":
                    out = []
                    for d in sorted(root.glob("*/manifest.json")):
                        m = json.loads(d.read_text())
                        pr = d.parent / "progress.json"
                        out.append(
                            {
                                "name": d.parent.name,
                                **m,
                                "progress": json.loads(pr.read_text()) if pr.exists() else None,
                                "running": d.parent.name in RUNS and RUNS[d.parent.name].poll() is None,
                            }
                        )
                    return self.send(200, out)
                if u.path == "/api/status":
                    d = proj(q)
                    pr = d / "progress.json"
                    log = (d / "run.log").read_text().splitlines()[-12:] if (d / "run.log").exists() else []
                    review = (d / "review.txt").read_text().splitlines() if (d / "review.txt").exists() else []
                    chunks = sorted((d / "chunks").glob("*.md"))
                    done = sorted((d / "out").glob("*.md"))
                    files = [f.name for f in d.glob("book_*") if f.suffix in (".epub", ".md")]
                    total = (
                        json.loads((d / "manifest.json").read_text())["words"] if (d / "manifest.json").exists() else 0
                    )
                    return self.send(
                        200,
                        {
                            "progress": json.loads(pr.read_text())
                            if pr.exists()
                            else {"percent": 0, "status": "not started", "total_words": total},
                            "running": d.name in RUNS and RUNS[d.name].poll() is None,
                            "chunks": len(chunks),
                            "done": len(done),
                            "log": log,
                            "review": review,
                            "files": files,
                            "job": JOBS.get(d.name),
                            "last": done[-1].stem if done else None,
                            "has_glossary": (d / "glossary.json").exists(),
                        },
                    )
                if u.path == "/api/glossary":
                    g = proj(q) / "glossary.json"
                    return self.send(200, json.loads(g.read_text()) if g.exists() else {})
                if u.path == "/api/chunk":
                    d, n = proj(q), re.sub(r"\D", "", q.get("n", [""])[0])
                    src, out = d / "chunks" / f"{n}.md", d / "out" / f"{n}.md"
                    return self.send(
                        200,
                        {
                            "src": src.read_text() if src.exists() else "",
                            "out": out.read_text() if out.exists() else "",
                        },
                    )
                if u.path == "/api/download":
                    d, f = proj(q), Path(q.get("f", [""])[0]).name
                    path = d / f
                    if not (f.startswith("book_") and path.exists()):
                        return self.send(404, {"error": "not built yet"})
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "application/epub+zip" if f.endswith(".epub") else "text/markdown; charset=utf-8",
                    )
                    self.send_header("Content-Disposition", f'attachment; filename="{d.name}-{f}"')
                    data = path.read_bytes()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                self.send(404, {"error": "not found"})
            except (ValueError, OSError, json.JSONDecodeError) as e:
                self.send(400, {"error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)

            def val(k, dflt=""):
                return q.get(k, [dflt])[0]

            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                d = proj(q)
                name = d.name
                if u.path == "/api/prep":  # body = the raw book file
                    fname = Path(self.headers.get("X-Filename", "book.txt")).name
                    if Path(fname).suffix.lower() not in (".pdf", ".epub", ".txt", ".md"):
                        return self.send(400, {"error": "use a .pdf, .epub, .txt or .md file"})
                    d.mkdir(parents=True, exist_ok=True)
                    src = d / ("source" + Path(fname).suffix.lower())
                    src.write_bytes(body)
                    step(
                        name,
                        [
                            "prep",
                            str(src),
                            "-p",
                            str(d),
                            "--from",
                            val("from", "en"),
                            "--lang",
                            val("lang", "bn"),
                            "--words",
                            val("words", "2500"),
                        ],
                    )
                    return self.send(200, {"ok": True})
                if u.path == "/api/glossary":
                    if body:  # save edited glossary
                        data = json.loads(body)
                        (d / "glossary.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
                        return self.send(200, {"ok": True, "terms": len(data)})
                    args = ["glossary", "-p", str(d), "--agents", val("agents", "claude")]
                    if val("model"):
                        args += ["--model", val("model")]
                    step(name, args)
                    return self.send(200, {"ok": True})
                if u.path == "/api/run":
                    if name in RUNS and RUNS[name].poll() is None:
                        return self.send(200, {"ok": True, "already": True})
                    args = [*CLI, "run", "-p", str(d), "--agents", val("agents", "claude"), "-j", val("jobs", "3")]
                    if val("model"):
                        args += ["--model", val("model")]
                    if val("fallback") == "1":
                        args.append("--fallback")
                    with open(d / "ui-run.err", "w") as err:  # the child keeps its own copy of the handle
                        RUNS[name] = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=err)
                    return self.send(200, {"ok": True})
                if u.path == "/api/stop":
                    if name in RUNS and RUNS[name].poll() is None:
                        RUNS[name].terminate()
                    return self.send(200, {"ok": True})
                if u.path == "/api/redo":  # delete one translation so the next run redoes it
                    n = re.sub(r"\D", "", val("n"))
                    (d / "out" / f"{n}.md").unlink(missing_ok=True)
                    return self.send(200, {"ok": True})
                if u.path == "/api/build":
                    step(name, ["build", "-p", str(d)] + (["--title", val("title")] if val("title") else []))
                    return self.send(200, {"ok": True})
                self.send(404, {"error": "not found"})
            except (ValueError, OSError, json.JSONDecodeError) as e:
                self.send(400, {"error": str(e)})

    return H


ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#13232E"/>'
    '<path d="M256 168C214 140 150 132 92 140V392C150 384 214 392 256 420Z" fill="#F7F4EC"/>'
    '<path d="M256 168C298 140 362 132 420 140V392C362 384 298 392 256 420Z" fill="#E7F3F1"/>'
    '<path d="M150 112C210 52 302 52 362 112" fill="none" stroke="#F2A33A" stroke-width="26" stroke-linecap="round"/></svg>'
)
API_BACKENDS = {"gemini-api", "openai-api", "anthropic-api", "cmd"}
CMD_OF = {"claude": "claude", "agy": "agy", "opencode": "opencode", "codex": "codex", "gemini": "gemini"}


def serve(root, host, port, langs, backends, open_browser=True):
    Path(root).mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), make_handler(root, langs, backends))
    url = f"http://{host}:{port}"
    print(f"tomeshift UI on {url}  (projects in {Path(root).resolve()})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping UI; translation runs started here are stopped too (re-run continues where they left off)")
        for p in RUNS.values():
            if p.poll() is None:
                p.terminate()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tomeshift</title>
<style>
:root{--bg:#F5F7F6;--card:#fff;--ink:#13232E;--muted:#5B6B72;--line:#D9E1E0;--teal:#0F766E;--teal-soft:#E2F1EF;--amber:#C97A12;--amber-soft:#FBEFD9;--bad:#B4442A;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0E171C;--card:#152129;--ink:#E4ECEB;--muted:#93A4A9;--line:#26363E;--teal:#4FC1B4;--teal-soft:#16332F;--amber:#F2A33A;--amber-soft:#3A2B14;--bad:#E9846B;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans)}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header b{font-size:18px}header span{color:var(--muted)}
main{max-width:1100px;margin:0 auto;padding:20px 16px;display:grid;grid-template-columns:260px 1fr;gap:20px}
@media(max-width:800px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px}
.plist{display:grid;gap:6px}.plist button{text-align:left;width:100%}
.plist .on{border-color:var(--teal);background:var(--teal-soft)}
button,select,input{font:inherit;color:inherit}
button{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:7px 12px;cursor:pointer}
button:hover{border-color:var(--teal)}button.primary{background:var(--teal);border-color:var(--teal);color:#fff}
button.warn{color:var(--bad)}button:disabled{opacity:.5;cursor:default}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--teal);outline-offset:1px}
select,input[type=text],input[type=number]{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;width:100%}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:grid;gap:3px;font-size:12px;color:var(--muted);font-weight:600}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.drop{border:2px dashed var(--line);border-radius:10px;padding:22px;text-align:center;color:var(--muted);cursor:pointer}
.drop.over{border-color:var(--teal);background:var(--teal-soft)}
.bar{height:14px;background:var(--line);border-radius:7px;overflow:hidden}.bar i{display:block;height:100%;background:var(--teal);transition:width .5s}
.big{font:700 34px/1 var(--sans);font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:var(--teal-soft);color:var(--teal)}
.pill.paused{background:var(--amber-soft);color:var(--amber)}.pill.idle{background:var(--line);color:var(--muted)}
.stack{display:grid;gap:16px;min-width:0}
pre{font:12px/1.5 var(--mono);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;max-height:160px;overflow:auto;white-space:pre-wrap;margin:0}
.head{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}.head h2{margin:0}.head select{width:auto}
.side{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:700px){.side{grid-template-columns:1fr}}
.side div{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;max-height:300px;overflow:auto;white-space:pre-wrap;font-size:14px;min-width:0}
table{width:100%;border-collapse:collapse}td{padding:3px 4px}td input{width:100%}
.muted{color:var(--muted);font-size:13px}.hidden{display:none!important}
</style></head><body>
<header><svg width="30" height="30" viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="112" fill="#13232E"/><path d="M256 168C214 140 150 132 92 140V392C150 384 214 392 256 420Z" fill="#F7F4EC"/><path d="M256 168C298 140 362 132 420 140V392C362 384 298 392 256 420Z" fill="#E7F3F1"/><path d="M150 112C210 52 302 52 362 112" fill="none" stroke="#F2A33A" stroke-width="26" stroke-linecap="round"/></svg>
<b>tomeshift</b><span>Translate a whole book for under a dollar.</span></header>
<main>
<aside class="stack" style="align-self:start">
  <div class="card"><h2>Books</h2><div class="plist" id="plist"></div>
  <button id="newbtn" style="margin-top:10px;width:100%">+ New book</button></div>
</aside>
<section class="stack">
  <div class="card" id="newcard">
    <h2>1 · Add a book</h2>
    <div class="grid" style="margin-bottom:12px">
      <label for="pname">Name<input type="text" id="pname" placeholder="my-book"></label>
      <label for="from">From<select id="from"></select></label>
      <label for="lang">To<select id="lang"></select></label>
      <label for="words">Words per chunk<input type="number" id="words" value="2500" min="300" step="100"></label>
    </div>
    <div class="drop" id="drop" tabindex="0">Drop a .pdf, .epub, .txt or .md file here, or click to choose<input type="file" id="file" accept=".pdf,.epub,.txt,.md" hidden></div>
    <p class="muted" id="prepmsg"></p>
  </div>

  <div class="card hidden" id="projcard">
    <div class="row" style="justify-content:space-between"><h2 id="ptitle" style="margin:0"></h2><span class="pill idle" id="state">idle</span></div>
    <div style="margin:14px 0 8px" class="row"><span class="big" id="pct">0%</span><span class="muted" id="words2"></span></div>
    <div class="bar"><i id="barfill" style="width:0"></i></div>
    <p class="muted" id="eta"></p>

    <h2 style="margin-top:16px">2 · Translate with</h2>
    <div class="grid">
      <label for="agent">Backend<select id="agent"></select></label>
      <label for="model">Model (optional)<input type="text" id="model" placeholder="e.g. sonnet, haiku, gemini-2.5-flash-lite"></label>
      <label for="jobs">Parallel chunks<input type="number" id="jobs" value="3" min="1" max="32"></label>
      <label for="fallback">Fallback agent<select id="fallback"><option value="">none</option></select></label>
    </div>
    <p class="muted" id="keyhint"></p>
    <div class="row" style="margin-top:10px">
      <button id="glossbtn">Build glossary</button>
      <button class="primary" id="runbtn">Start translating</button>
      <button class="warn hidden" id="stopbtn">Pause</button>
      <button id="buildbtn">Build EPUB</button>
      <span id="downloads" class="row"></span>
    </div>
    <p class="muted" id="jobmsg"></p>
  </div>

  <div class="card hidden" id="glosscard">
    <div class="row" style="justify-content:space-between"><h2 style="margin:0">Glossary: names and terms stay consistent</h2><button id="glosssave">Save glossary</button></div>
    <table id="glosstab"></table>
  </div>

  <div class="card hidden" id="previewcard">
    <div class="head"><h2>Preview: original and translation side by side</h2>
      <span class="row"><select id="chunksel" aria-label="Chunk"></select><button id="redobtn" class="warn">Redo this chunk</button></span></div>
    <div class="side" style="margin-top:10px"><div id="srcbox"></div><div id="outbox"></div></div>
  </div>

  <div class="card hidden" id="logcard"><h2>Activity</h2><pre id="log"></pre></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);let cur=null,meta=null,lastShown=null;
const KEYS={"gemini-api":"needs GEMINI_API_KEY in the environment you started `tomeshift ui` from","openai-api":"needs OPENAI_API_KEY (and OPENAI_BASE_URL for OpenRouter/DeepSeek/Ollama)","anthropic-api":"needs ANTHROPIC_API_KEY","cmd":"needs TOMESHIFT_CMD"};
async function api(path,opts){const r=await fetch(path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
function opt(sel,items,val){sel.innerHTML=items.map(([v,t])=>`<option value="${v}">${t}</option>`).join("");if(val)sel.value=val}
async function init(){meta=await api("/api/meta");const L=Object.entries(meta.langs).map(([c,n])=>[c,`${n} (${c})`]);
 opt($("from"),L,"en");opt($("lang"),L,"bn");const B=meta.all_backends.map(b=>[b,b+(meta.backends.includes(b)?"":" (not installed)")]);
 opt($("agent"),B,meta.backends[0]||"claude");opt($("fallback"),[["","none"],...B]);$("agent").onchange=hint;hint();loadProjects()}
function hint(){$("keyhint").textContent=KEYS[$("agent").value]||"Uses your existing login for this CLI. No API key needed."}
async function loadProjects(){const ps=await api("/api/projects");$("plist").innerHTML=ps.length?"":'<p class="muted">No books yet.</p>';
 for(const p of ps){const b=document.createElement("button");b.className=p.name===cur?"on":"";const pc=p.progress?p.progress.percent:0;
  b.innerHTML=`<b>${p.name}</b><br><span class="muted">${p.from||"en"} → ${p.lang} · ${pc}%${p.running?" · running":""}</span>`;b.onclick=()=>open_(p.name);$("plist").appendChild(b)}}
function open_(name){cur=name;lastShown=null;$("newcard").classList.add("hidden");["projcard","previewcard","logcard"].forEach(i=>$(i).classList.remove("hidden"));
 $("ptitle").textContent=name;loadProjects();loadGloss();poll()}
$("newbtn").onclick=()=>{cur=null;$("newcard").classList.remove("hidden");["projcard","glosscard","previewcard","logcard"].forEach(i=>$(i).classList.add("hidden"));loadProjects()};
const drop=$("drop");drop.onclick=()=>$("file").click();drop.onkeydown=e=>{if(e.key==="Enter"||e.key===" ")$("file").click()};
drop.ondragover=e=>{e.preventDefault();drop.classList.add("over")};drop.ondragleave=()=>drop.classList.remove("over");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");if(e.dataTransfer.files[0])upload(e.dataTransfer.files[0])};
$("file").onchange=e=>{if(e.target.files[0])upload(e.target.files[0])};
async function upload(f){const name=($("pname").value||f.name.replace(/\.[^.]+$/,"")).replace(/[^A-Za-z0-9_.-]+/g,"-");
 $("prepmsg").textContent=`Uploading ${f.name}…`;try{await api(`/api/prep?p=${name}&from=${$("from").value}&lang=${$("lang").value}&words=${$("words").value}`,{method:"POST",headers:{"X-Filename":f.name},body:f});
 open_(name)}catch(e){$("prepmsg").textContent="Error: "+e.message}}
function q(){return `p=${cur}&agents=${encodeURIComponent([$("agent").value,$("fallback").value].filter(Boolean).join(","))}&model=${encodeURIComponent($("model").value)}&jobs=${$("jobs").value}&fallback=${$("fallback").value?1:0}`}
$("runbtn").onclick=async()=>{await api("/api/run?"+q(),{method:"POST"});poll()};
$("stopbtn").onclick=async()=>{await api("/api/stop?p="+cur,{method:"POST"});setTimeout(poll,500)};
$("glossbtn").onclick=async()=>{await api("/api/glossary?"+q(),{method:"POST"});$("jobmsg").textContent="Building glossary (one model call)…"};
$("buildbtn").onclick=async()=>{await api("/api/build?p="+cur,{method:"POST"});$("jobmsg").textContent="Building book…"};
async function loadGloss(){const g=await api("/api/glossary?p="+cur);const e=Object.entries(g);$("glosscard").classList.toggle("hidden",!e.length);
 $("glosstab").innerHTML=e.map(([k,v],i)=>`<tr><td><input type="text" value="${k.replace(/"/g,"&quot;")}" aria-label="Source term ${i+1}"></td><td><input type="text" value="${v.replace(/"/g,"&quot;")}" aria-label="Translation ${i+1}"></td></tr>`).join("")}
$("glosssave").onclick=async()=>{const g={};for(const tr of $("glosstab").rows){const[a,b]=tr.querySelectorAll("input");if(a.value.trim())g[a.value.trim()]=b.value.trim()}
 const r=await api("/api/glossary?p="+cur,{method:"POST",body:JSON.stringify(g)});$("jobmsg").textContent=`Glossary saved (${r.terms} terms).`};
$("chunksel").onchange=()=>showChunk($("chunksel").value);
$("redobtn").onclick=async()=>{await api(`/api/redo?p=${cur}&n=${$("chunksel").value}`,{method:"POST"});showChunk($("chunksel").value);$("jobmsg").textContent="Removed; it will be translated again on the next run."};
async function showChunk(n){const c=await api(`/api/chunk?p=${cur}&n=${n}`);$("srcbox").textContent=c.src;$("outbox").textContent=c.out||"Not translated yet."}
let lastJob="",tick=0;
async function poll(){if(!cur)return;let s;try{s=await api("/api/status?p="+cur)}catch(e){return}
 const p=s.progress||{};const pct=p.percent||0;$("pct").textContent=pct.toFixed(1)+"%";$("barfill").style.width=pct+"%";
 $("words2").textContent=`${(p.done_words||0).toLocaleString()} / ${(p.total_words||0).toLocaleString()} words · ${s.done}/${s.chunks} chunks`;
 const st=s.running?(p.status||"running"):(pct>=100?"done":"idle");const el=$("state");el.textContent=st;
 el.className="pill "+(/PAUSED/.test(st)?"paused":s.running||pct>=100?"":"idle");
 $("eta").textContent=s.running&&p.tokens_in?`Tokens so far: ${p.tokens_in.toLocaleString()} in / ${p.tokens_out.toLocaleString()} out`:(/PAUSED/.test(st)?"Usage limit reached. It will continue by itself when the limit resets.":"");
 $("runbtn").classList.toggle("hidden",s.running);$("stopbtn").classList.toggle("hidden",!s.running);
 $("downloads").innerHTML=s.files.sort().map(f=>`<a href="/api/download?p=${cur}&f=${f}">⬇ ${f}</a>`).join(" ");
 if(s.job){const j=s.job.ok===null?`Running ${s.job.step}…`:(s.job.ok?`${s.job.step} finished. `:`${s.job.step} failed: `)+(s.job.ok===null?"":s.job.out.split("\n").slice(-2).join(" "));
  if(j!==lastJob){lastJob=j;$("jobmsg").textContent=j;if(s.job.ok&&s.job.step==="glossary")loadGloss();if(s.job.ok!==null)loadProjects()}}
 if(++tick%5===0)loadProjects();
 $("log").textContent=(s.review.length?"Needs review:\n"+s.review.join("\n")+"\n\n":"")+s.log.join("\n")||"Nothing yet.";
 const n=[...Array(s.chunks).keys()].map(i=>String(i+1).padStart(4,"0"));if($("chunksel").options.length!==n.length)opt($("chunksel"),n.map(x=>[x,"Chunk "+x]));
 if(s.last&&s.last!==lastShown){lastShown=s.last;$("chunksel").value=s.last;showChunk(s.last)}else if(!lastShown&&n.length){lastShown=n[0];showChunk(n[0])}}
setInterval(poll,2000);init();
</script></body></html>
"""
