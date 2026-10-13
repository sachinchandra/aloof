"""Names of the @mcp.tool() functions in mcp_server/server.py, read statically (the MCP server runs as its own process),
so the manifest, docs and blog never drift from the real tool list."""
import ast, os, functools
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_server", "server.py")
@functools.lru_cache(maxsize=1)
def _names():
    tree = ast.parse(open(SERVER).read())
    is_tool = lambda d: isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
    return tuple(f.name for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(is_tool(d) for d in f.decorator_list))
def mcp_tool_names(): return list(_names())
