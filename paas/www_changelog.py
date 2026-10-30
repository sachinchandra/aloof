"""/changelog: weekly changelog kept as markdown in content/changelog/<date>.md (Marketing's copy, claims checked against
the code). HTML at /changelog, raw markdown at /changelog.md. Same converter and page shell as /compare.
PUBLISHED gates the route, sitemap entry and footer link: False until the owner confirms the publish."""
import os, html
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response, RedirectResponse
R = APIRouter()
DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "content", "changelog")
PUBLISHED = False
def entries(): return sorted((f for f in os.listdir(DIR) if f.endswith(".md")), reverse=True) if os.path.isdir(DIR) else []
def _md():
    if not PUBLISHED or not entries(): raise HTTPException(404)
    return "\n\n".join(open(os.path.join(DIR, f), encoding="utf-8").read().strip() for f in entries()) + "\n"
@R.get("/changelog.md")
def changelog_md(): return Response(_md(), media_type="text/markdown; charset=utf-8")
@R.get("/changelog", response_class=HTMLResponse)
def changelog():
    if not PUBLISHED: return RedirectResponse("/", status_code=301)   # unchanged behaviour (3e8f164) until published
    from .www import render, shell
    from .www_compare import to_html, CSS
    md = _md(); body = ""
    for block in md.split("\n# ")[0:]:
        block = block if block.startswith("# ") else "# " + block
        title = block.splitlines()[0].lstrip("# ").strip()
        body += f"<h2 style='margin-top:32px'>{html.escape(title)}</h2>" + to_html(block).replace("<h2>", "<h3>").replace("</h2>", "</h3>")
    body = CSS + body + "<p class=muted style='font-size:13px;margin-top:28px'>A plain-text version for AI agents is at <a href='/changelog.md'>/changelog.md</a>.</p>"
    return render("/changelog", "Changelog · Aloof", "What's new on Aloof each week: MCP, scoped access, release status, deploys, billing and security.",
                  shell("Changelog", "Changelog", "What shipped on Aloof, week by week.", body, narrow=True))
