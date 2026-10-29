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
    md = open(os.path.join(DIR, f"{k}.md"), encoding="utf-8").read()
    from .mcp_tools import mcp_tool_names                 # the real tools/list count, never a hand-typed number
    return md.replace("__MCP_TOOL_COUNT__", str(len(mcp_tool_names())))
def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m[2]}">{m[1]}</a>', s)
    # bare URLs: stop before trailing punctuation (.,;:!?) so "see https://x.y/z." doesn't link the full stop
    return re.sub(r"(?<![\"=>])(https?://[^\s<)]*[^\s<).,;:!?'])", r'<a href="\1">\1</a>', s)
def to_html(md):
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"): rows.append([c.strip() for c in lines[i].strip("|").split("|")]); i += 1
            head, body = rows[0], [r for r in rows[1:] if not set("".join(r)) <= set("-: ")]
            lab = [html.escape(re.sub(r"\*\*", "", c), quote=True) for c in head]
            out.append("<div class=cmp-wrap><table class=\"tbl cmp\"><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td data-label=\"{lab[j] if j < len(lab) else ''}\">{_inline(c)}</td>" for j, c in enumerate(r)) + "</tr>" for r in body) + "</tbody></table></div>"); continue
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
    rival = NAMES[k]
    body = (CSS + to_html(md) + "<div class=cta style='margin-top:24px'><a class=\"btn pri\" href=\"/login#register\">Start free</a><a class=btn href=\"/pricing\">Pricing</a></div>"
            + f"<p class=muted style='font-size:13px;margin-top:28px'>A plain-text version for AI agents is at <a href='/compare/{k}.md'>/compare/{k}.md</a>.</p>")
    return render(f"/compare/{k}", f"{title} · Aloof", f"How Aloof and {rival} compare on regions, billing, scale to zero, MCP access for AI assistants and rollbacks, with sources.",
                  shell("Compare", html.escape(title), f"A feature-by-feature look at Aloof and {html.escape(rival)}, with sources.", body, narrow=True))
# Below 480px each row becomes a card: the feature name on top, then each provider with its label.
CSS = """<style>.cmp-wrap{overflow-x:auto}
@media (max-width:480px){.cmp thead{display:none}.cmp,.cmp tbody,.cmp tr,.cmp td{display:block;width:100%}
.cmp tr{border:1px solid var(--line,#e7e2da);border-radius:10px;padding:10px 12px;margin:0 0 12px}
.cmp td{border:0;padding:4px 0}.cmp td:first-child{font-weight:600;padding-bottom:6px}
.cmp td:not(:first-child)::before{content:attr(data-label);display:block;font-size:12px;color:var(--muted,#7a7168)}}</style>"""
