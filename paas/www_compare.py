"""/compare/<rival>: comparison pages kept as markdown in content/compare/*.md (published verbatim from marketing copy).
HTML at /compare/<rival>, raw markdown at /compare/<rival>.md for agents. Tiny converter for the subset the files use."""
import os, re, html
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
R = APIRouter()
DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "content", "compare")
NAMES = {"fly": "Fly.io", "render": "Render"}          # railway held until approved
def _md(k):
    if k not in NAMES: raise HTTPException(404)
    return open(os.path.join(DIR, f"{k}.md"), encoding="utf-8").read()
def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m[2]}">{m[1]}</a>', s)
    return re.sub(r"(?<![\"=>])(https?://[^\s<)]+)", r'<a href="\1">\1</a>', s)
def to_html(md):
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"): rows.append([c.strip() for c in lines[i].strip("|").split("|")]); i += 1
            head, body = rows[0], [r for r in rows[1:] if not set("".join(r)) <= set("-: ")]
            out.append("<div style='overflow-x:auto'><table class=tbl><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table></div>"); continue
        if l.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "): items.append(f"<li>{_inline(lines[i][2:])}</li>"); i += 1
            out.append("<ul>" + "".join(items) + "</ul>"); continue
        if l.startswith("## "): out.append(f"<h2>{_inline(l[3:])}</h2>")
        elif l.startswith("# "): pass                                   # page title goes in the hero
        elif l.startswith("> "): out.append(f"<p class=muted style='font-size:13px'>{_inline(l[2:])}</p>")
        elif l.strip(): out.append(f"<p>{_inline(l)}</p>")
        i += 1
    return "\n".join(out)
@R.get("/compare/{k}.md")
def compare_md(k: str): return Response(_md(k), media_type="text/markdown; charset=utf-8")
@R.get("/compare/{k}", response_class=HTMLResponse)
def compare(k: str):
    from .www import render, shell
    md = _md(k); title = md.splitlines()[0].lstrip("# ").strip()
    return render(f"/compare/{k}", f"{title} · Aloof", f"{title}: prices, regions, MCP and self-healing deploys side by side.",
                  shell("Compare", html.escape(title), f"Side by side, with sources. Markdown for agents: <a href='/compare/{k}.md'>/compare/{k}.md</a>", to_html(md), narrow=True))
