"""Names of the @mcp.tool() functions in mcp_server/server.py, read statically (the MCP server runs as its own process),
so the manifest, docs and blog never drift from the real tool list. A tool's name is its @mcp.tool(name=...) if given, else
the function name. legacy_map() returns the pre-v2 names (@_legacy("replacement")) that still work but aren't listed by default."""
import ast, os, functools
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_server", "server.py")
def _is_tool(d): return isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
def tool_name(f):
    for d in f.decorator_list:
        if _is_tool(d):
            for k in d.keywords:
                if k.arg == "name" and isinstance(k.value, ast.Constant): return k.value.value
    return f.name
def tool_defs(path=None):
    tree = ast.parse(open(path or SERVER).read())
    return [f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(_is_tool(d) for d in f.decorator_list)]
@functools.lru_cache(maxsize=1)
def _names(): return tuple(tool_name(f) for f in tool_defs())
def mcp_tool_names(): return list(_names())
def legacy_map(path=None):
    tree = ast.parse(open(path or SERVER).read()); out = {}
    for f in tree.body:
        if not isinstance(f, ast.FunctionDef): continue
        for d in f.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "_legacy" and d.args and isinstance(d.args[0], ast.Constant):
                out[f.name] = d.args[0].value
    return out
