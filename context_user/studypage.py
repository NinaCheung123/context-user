"""Local comparison page for the field study. One self-contained HTML file; nothing is uploaded.

Step 1 compares three answers to your own tasks (no skills / direct / method), shown as A, B, C.
Step 2 compares two skill sets (X, Y) and lets you mark each skill. The page saves to a choices file
you send back; `cu study decode` turns the letters into conditions on your computer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skill Comparison</title>
<style>
:root{--bg:#f6f7f5;--fg:#1c2220;--mut:#66706b;--line:#dce1dd;--card:#fff;--acc:#2a5fa8;--accfg:#fff;--bad:#a94a3a;--ok:#2e7d5b}
@media (prefers-color-scheme:dark){:root{--bg:#121615;--fg:#e5eae7;--mut:#8e9993;--line:#2a312e;--card:#1a1f1d;--acc:#7fb0ee;--accfg:#0d1320;--bad:#e0917f;--ok:#6fcf9f;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Segoe UI",sans-serif;padding:20px max(16px,env(safe-area-inset-right)) 80px max(16px,env(safe-area-inset-left))}
main{max-width:760px;margin:0 auto}h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:30px 0 8px}
.intro{color:var(--mut);margin:0 0 12px}.bar{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:2}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:14px;margin:12px 0;display:grid;gap:10px}.card.done{border-color:var(--acc)}
.prompt{background:var(--bg);border-radius:5px;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere}
.ans{border:1px solid var(--line);border-radius:5px;padding:10px;min-width:0}.ans h3{margin:0 0 6px;font-size:14px;color:var(--acc)}
.txt{white-space:pre-wrap;overflow-wrap:anywhere;max-height:380px;overflow:auto}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px}.row>span{color:var(--mut);min-width:5em}
button{font:inherit;font-size:13px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:5px;padding:5px 12px;cursor:pointer}
button[aria-pressed="true"]{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button.w[aria-pressed="true"]{background:var(--bad);border-color:var(--bad);color:#fff}
input[type=text]{font:inherit;font-size:13px;flex:1;min-width:140px;border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:5px;padding:5px 8px}
.set{border:1px solid var(--line);border-radius:6px;padding:12px;background:var(--card);margin:10px 0}.sk{border-top:1px solid var(--line);padding:8px 0}.sk:first-of-type{border-top:0}
details summary{cursor:pointer;color:var(--mut);font-size:13px}.lock{color:var(--mut);padding:12px;border:1px dashed var(--line);border-radius:6px}
</style></head><body><main>
<h1>Skill Comparison</h1>
<p class="intro">Everything stays on your computer. Compare, do not score: pick what is better, or say you cannot tell. Answers are shuffled and unlabeled. When you finish, press <b>Save choices</b> and send the file.</p>
<div class="bar"><span>Done <b id="done">0</b> / <span id="tot"></span></span><button id="save" type="button">Save choices</button><span id="st"></span></div>
<h2 id="h1">Step 1: which answer is better?</h2><div id="tasks"></div>
<h2>Step 2: compare the two skill sets</h2><div id="stage2"></div>
</main><script>
var D=__DATA__,T=D.tasks,S=D.skills,KEY="cu-study-"+D.run,st={};
try{st=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
function persist(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}
function btn(label,cls,on){var b=el("button",cls||"",label);b.type="button";b.onclick=on;return b}
function press(b,v){b.setAttribute("aria-pressed",v?"true":"false")}
document.getElementById("tot").textContent=T.length;if(!T.length)document.getElementById("h1").style.display="none";
function tdone(t){var s=st["t"+t.id];return !!(s&&(s.skip||s.tie||(s.best&&s.worst&&s.best!==s.worst)))}
function count(){document.getElementById("done").textContent=T.filter(tdone).length}
var root=document.getElementById("tasks");
T.forEach(function(t){
 var c=el("div","card"),k="t"+t.id;st[k]=st[k]||{};var s=st[k];
 c.appendChild(el("div","prompt",t.prompt));
 ["A","B","C"].forEach(function(L){var a=el("div","ans");a.appendChild(el("h3","","Answer "+L));a.appendChild(el("div","txt",t.answers[L]));c.appendChild(a)});
 var bb={},wb={},rb=el("div","row"),rw=el("div","row"),ro=el("div","row");rb.appendChild(el("span","","Best"));rw.appendChild(el("span","","Worst"));
 function paint(){["A","B","C"].forEach(function(L){press(bb[L],s.best===L);press(wb[L],s.worst===L)});press(tie,!!s.tie);press(skip,!!s.skip);c.className="card"+(tdone(t)?" done":"");count();persist()}
 ["A","B","C"].forEach(function(L){bb[L]=btn(L,"",function(){s.tie=s.skip=false;s.best=s.best===L?"":L;if(s.worst===s.best)s.worst="";paint()});rb.appendChild(bb[L]);
  wb[L]=btn(L,"w",function(){s.tie=s.skip=false;s.worst=s.worst===L?"":L;if(s.best===s.worst)s.best="";paint()});rw.appendChild(wb[L])});
 var tie=btn("Cannot tell (no difference)","",function(){s.tie=!s.tie;if(s.tie){s.skip=false;s.best=s.worst=""}paint()});
 var skip=btn("Skip: not a fair task","",function(){s.skip=!s.skip;if(s.skip){s.tie=false;s.best=s.worst=""}paint()});
 var n=el("input");n.type="text";n.placeholder="Why? (optional)";n.value=s.note||"";n.oninput=function(){s.note=n.value;persist()};
 ro.appendChild(tie);ro.appendChild(skip);ro.appendChild(n);c.appendChild(rb);c.appendChild(rw);c.appendChild(ro);root.appendChild(c);paint()});
var s2=document.getElementById("stage2");
function build2(){s2.textContent="";st.s2=st.s2||{marks:{}};
 ["X","Y"].forEach(function(K){var d=el("div","set");d.appendChild(el("h3","","Skill set "+K+" ("+S[K].length+")"));
  S[K].forEach(function(x,i){var id=K+i,r=el("div","sk");r.appendChild(el("b","",x.name));r.appendChild(el("div","",x.description||""));
   var dt=el("details");dt.appendChild(el("summary","","Show rules"));dt.appendChild(el("div","txt",x.body||x.statement||""));r.appendChild(dt);
   var row=el("div","row"),bs={};[["me","Sounds like me"],["no","Not me"],["unsure","Unsure"]].forEach(function(p){bs[p[0]]=btn(p[1],"",function(){st.s2.marks[id]=st.s2.marks[id]===p[0]?"":p[0];Object.keys(bs).forEach(function(q){press(bs[q],st.s2.marks[id]===q)});persist()});press(bs[p[0]],st.s2.marks[id]===p[0]);row.appendChild(bs[p[0]])});
   r.appendChild(row);d.appendChild(r)});s2.appendChild(d)});
 s2.appendChild(el("p","intro","Overall, which set is closer to how you want an AI to work with you?"));
 var row=el("div","row"),ob={};["X","Y","No difference"].forEach(function(v){ob[v]=btn(v,"",function(){st.s2.better=st.s2.better===v?"":v;Object.keys(ob).forEach(function(q){press(ob[q],st.s2.better===q)});persist()});press(ob[v],st.s2.better===v);row.appendChild(ob[v])});s2.appendChild(row);
 var n=el("input");n.type="text";n.placeholder="Why? (optional)";n.value=st.s2.note||"";n.oninput=function(){st.s2.note=n.value;persist()};s2.appendChild(n)}
function upd(){if(T.filter(tdone).length===T.length){if(!s2.firstChild||s2.firstChild.className==="lock")build2()}else if(!s2.firstChild||s2.firstChild.className==="lock"){s2.textContent="";s2.appendChild(el("div","lock","Shown after Step 1 is finished (use Cannot tell or Skip when needed)."))}}
setInterval(upd,500);upd();
document.getElementById("save").onclick=function(){var out={run:D.run,tasks:T.map(function(t){var s=st["t"+t.id]||{};return {id:t.id,best:s.best||"",worst:s.worst||"",tie:!!s.tie,skip:!!s.skip,note:s.note||""}}),skills:st.s2||{}};
 var a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,1)],{type:"application/json"}));a.download="choices.json";a.click();document.getElementById("st").textContent="Saved choices.json"};
</script></body></html>'''


def build(root: Path, run_dir: Path) -> Path:
    sets = json.loads((run_dir / "skills_blind.json").read_text(encoding="utf-8"))
    tasks_path = run_dir / "answers_blind.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8")) if tasks_path.exists() else []
    clean = lambda s: re.sub(r"\s*\(m\d{3}(,\s*m\d{3})*\)", "", s or "")
    skills = {k: [{"name": x["name"], "description": x.get("description", ""), "body": clean(x.get("body") or x.get("statement"))} for x in v] for k, v in sets.items()}
    data = json.dumps({"run": run_dir.parent.parent.name or "run", "tasks": tasks, "skills": skills}, ensure_ascii=False).replace("<", "\\u003c")
    out = run_dir / "compare.html"
    out.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    return out


def decode(run_dir: Path, choices: Path, out: Path) -> Path:
    """Turn letters into conditions locally and write ratings.csv (no text from your conversations)."""
    import csv
    key = json.loads((run_dir / "key.json").read_text())
    akey = json.loads((run_dir / "answers_key.json").read_text()) if (run_dir / "answers_key.json").exists() else {}
    c = json.loads(choices.read_text(encoding="utf-8"))
    pf = run_dir / "participant.txt"
    pid = pf.read_text(encoding="utf-8").strip() if pf.exists() else "unknown"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["participant", "kind", "item", "best", "worst", "result", "mark", "reason"])
        for t in c["tasks"]:
            k = akey.get(str(t["id"]), {})
            res = "skip" if t["skip"] else "tie" if t["tie"] else ""
            w.writerow([pid, "answer", t["id"], k.get(t["best"], ""), k.get(t["worst"], ""), res, "", t["note"]])
        s = c["skills"]
        w.writerow([pid, "skill_set", "overall", "", "", key.get(s.get("better", ""), s.get("better", "")), "", s.get("note", "")])
        for sid, mark in (s.get("marks") or {}).items():
            w.writerow([pid, "skill", f"{key.get(sid[0], '?')}:{sid[1:]}", "", "", "", mark, ""])
    return out
