"""Customer-facing output never shows our internal failures (UX, Nov 3): OSError/Errno, docker daemon/socket/SDK errors,
Python tracebacks and exceptions, and timeouts talking to our own services become one plain sentence. Genuine customer
errors (their Dockerfile/build output, a wrong image name, failing health checks) are left exactly as they are.
Raw text stays in the DB, server logs and /v1/admin/*: this runs only on customer /v1 responses (api middleware)."""
import re, json
MSG = "We couldn't start the build on our side. Retry, or contact support."
_INTERNAL = re.compile(r"\[Errno -?\d+\]|No such file or directory: '[^']*'|Traceback \(most recent call last\)|docker\.errors\.|"
    r"DockerException|APIError\(|http\+docker://|Cannot connect to the Docker daemon|docker daemon|docker\.sock|"
    r"Error while fetching server API version|\b(?:OSError|IOError|PermissionError|FileNotFoundError|ConnectionRefusedError|"
    r"ConnectionResetError|BrokenPipeError|TimeoutError|KeyError|TypeError|AttributeError|NameError|IndexError|"
    r"UnboundLocalError|RecursionError|MemoryError|sqlite3\.\w+|psycopg\w*\.\w+)\b|'NoneType' object|object has no attribute|"
    r"ReadTimeout|ConnectTimeout|Read timed out|HTTPConnectionPool|urllib3\.|httpx\.\w+Error|requests\.exceptions|"
    r"Max retries exceeded|Connection aborted|\bEOF occurred\b", re.I)
_GENUINE = re.compile(r"pull access denied|manifest unknown|manifest for .* not found|repository does not exist|"
    r"no such image|invalid reference format|unauthorized: |denied: |The command '.*' returned a non-zero code|"
    r"did not complete successfully: exit code|executor failed running|failed to solve|Dockerfile|exited with code", re.I)
_PREFIX = re.compile(r"^(.*?(?:ERROR:|failed:|failed \(|error:)\s*)", re.I)
_TAIL = re.compile(r"(\)?;\s*v\d+ still serving.*|;\s*still serving v\d+.*)$", re.I)
def internal(s): return isinstance(s, str) and bool(_INTERNAL.search(s)) and not _GENUINE.search(s)
def clean_line(line):
    if not internal(line): return line
    p = _PREFIX.match(line); t = _TAIL.search(line)
    return (p[1] if p else "") + MSG + (t[1] if t else "")
def clean_text(s):
    if not isinstance(s, str) or not _INTERNAL.search(s): return s
    out, skipping = [], False
    for ln in s.split("\n"):
        if ln.startswith("Traceback (most recent call last)"): skipping = True; out.append(MSG); continue
        if skipping:
            if ln.startswith((" ", "\t")) or not ln.strip(): continue
            skipping = False; continue   # the exception line closing the traceback
        out.append(clean_line(ln))
    return "\n".join(out)
def clean(o):
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list): return [clean(x) for x in o]
    return clean_text(o)
def applies(path): return path.startswith("/v1/") and not path.startswith("/v1/admin")
