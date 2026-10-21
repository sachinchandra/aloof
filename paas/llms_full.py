"""AI-readable docs, generated from the same sources as the website so they can't drift:
/llms-full.txt = the docs pages (site.docs) converted to markdown; /mcp-tools.md = every @mcp.tool() in mcp_server/server.py
(name, description, parameters) parsed with ast, not imported."""
import ast, html, os, re
from html.parser import HTMLParser
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_PAGES = ["quickstart", "deploy", "config", "domains", "postgres", "volumes", "billing", "mcp", "self-driving", "troubleshooting", "api"]
class _MD(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "table", "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "tr", "br", "hr", "details", "summary"}
    def __init__(self): super().__init__(convert_charrefs=True); self.o = []; self.href = []; self.pre = 0; self.lists = []; self.skip = 0; self.cell = 0
    def handle_starttag(self, t, a):
        a = dict(a)
        if t in ("script", "style", "svg", "button", "form", "nav"): self.skip += 1; return
        if self.skip: return
        if t in ("h1", "h2", "h3", "h4", "h5"): self.o.append("\n\n" + "#" * int(t[1]) + " ")
        elif t == "pre": self.pre += 1; self.o.append("\n\n```\n")
        elif t == "code" and not self.pre: self.o.append("`")
        elif t in ("b", "strong"): self.o.append("**")
        elif t in ("ul", "ol"): self.lists.append([t, 0]); self.o.append("\n")
        elif t == "li":
            l = self.lists[-1] if self.lists else ["ul", 0]; l[1] += 1
            self.o.append("\n" + "  " * max(0, len(self.lists) - 1) + (f"{l[1]}. " if l[0] == "ol" else "- "))
        elif t == "a": self.href.append(a.get("href") or ""); self.o.append("[")
        elif t == "tr": self.o.append("\n|"); self.cell = 0
        elif t in ("td", "th"): self.o.append(" ")
        elif t == "br": self.o.append("  \n")
        elif t in self.BLOCK: self.o.append("\n\n")
    def handle_endtag(self, t):
        if t in ("script", "style", "svg", "button", "form", "nav"): self.skip = max(0, self.skip - 1); return
        if self.skip: return
        if t == "pre": self.pre -= 1; self.o.append("\n```\n\n")
        elif t == "code" and not self.pre: self.o.append("`")
        elif t in ("b", "strong"): self.o.append("**")
        elif t in ("ul", "ol"): self.lists and self.lists.pop(); self.o.append("\n")
        elif t == "a":
            h = self.href.pop() if self.href else ""
            self.o.append(f"]({h})" if h and not h.startswith("#") and not h.startswith("javascript") else "]")
        elif t in ("td", "th"): self.o.append(" |")
        elif t == "tr" and self.cell == 0: pass
        elif t in self.BLOCK: self.o.append("\n\n")
    def handle_data(self, d):
        if self.skip: return
        self.o.append(d if self.pre else re.sub(r"\s+", " ", d))
    def text(self):
        s = "".join(self.o)
        s = re.sub(r"\[\]\([^)]*\)", "", s)
        s = re.sub(r"[ \t]+\n", "\n", s); s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip() + "\n"
def to_md(fragment):
    p = _MD(); p.feed(fragment); p.close(); return p.text()
def _absolutize(md, site_url):
    return re.sub(r"\]\((/[^)]*)\)", lambda m: f"]({site_url}{m.group(1)})", md)
def page_md(page, docs, brand, base, api, mcp, site_url):
    h = docs(page, brand, base, api, mcp)
    m = re.search(r"<article[^>]*>(.*)</article>", h or "", re.S)
    return _absolutize(to_md(m.group(1) if m else ""), site_url) if m else ""
def full(docs, brand, base, api, mcp, site_url, llms_txt):
    out = [f"# {brand}: full documentation\n", f"> Plain-markdown copy of {site_url}/docs, generated from the website's own source on every request.",
           f"> Short index: {api}/llms.txt · MCP tool reference: {api}/mcp-tools.md · OpenAPI: {api}/api/openapi.json\n",
           "## Overview (llms.txt)\n", re.sub(r"^# .*\n", "", llms_txt, count=1).strip(), ""]
    for p in FULL_PAGES:
        md = page_md(p, docs, brand, base, api, mcp, site_url)
        if md: out += [f"\n---\n<!-- source: {site_url}/docs/{p} -->\n", re.sub(r"(?m)^(#+) ", lambda m: "#" + m.group(1) + " ", md)]
    return "\n".join(out).rstrip() + "\n"
def mcp_tools(path=None):
    """[{name, description, params:[{name, type, default, required}]}] for every @mcp.tool() function, in source order."""
    path = path or os.path.join(ROOT, "mcp_server", "server.py")
    src = open(path).read(); tree = ast.parse(src); out = []
    is_tool = lambda d: isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
    for f in tree.body:
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(is_tool(d) for d in f.decorator_list): continue
        args = f.args.args; defs = [None] * (len(args) - len(f.args.defaults)) + list(f.args.defaults); ps = []
        for a, d in zip(args, defs):
            ps.append({"name": a.arg, "type": ast.unparse(a.annotation) if a.annotation else "any",
                       "default": None if d is None else ast.unparse(d), "required": d is None})
        out.append({"name": f.name, "description": " ".join((ast.get_docstring(f) or "").split()), "params": ps})
    return out
def mcp_tools_md(brand, mcp_url, api):
    ts = mcp_tools()
    o = [f"# {brand} MCP tools reference\n", f"{len(ts)} tools, generated from the server source on every request. Remote server: `{mcp_url}` "
         f"(streamable HTTP, OAuth 2.1 + PKCE, or `Authorization: Bearer pk_...`). Every tool also takes an optional `api_key`; "
         f"over OAuth leave it empty. Index: {api}/llms.txt\n"]
    for t in ts:
        o.append(f"## `{t['name']}`\n\n{t['description']}\n")
        ps = [p for p in t["params"] if p["name"] != "api_key"]
        if ps:
            o.append("| Parameter | Type | Required | Default |\n|---|---|---|---|")
            o += [f"| `{p['name']}` | `{p['type'].replace('|', '/')}` | {'yes' if p['required'] else 'no'} | {'' if p['default'] is None else '`' + p['default'].replace('|', '/') + '`'} |" for p in ps]
            o.append("")
        else: o.append("No parameters (besides `api_key`).\n")
    return "\n".join(o).rstrip() + "\n"
