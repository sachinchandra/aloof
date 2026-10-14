"""DB layer: Postgres (default when PAAS_DB_URL or data/database_url is set) or SQLite fallback.
SQL is written with '?' placeholders and portable `on conflict` upserts; versioned migrations below."""
import os, time, threading, sqlite3
ROOT = os.path.join(os.path.dirname(__file__), "..")
_url_file = os.path.join(ROOT, "data", "database_url")
DB_URL = os.environ.get("PAAS_DB_URL") or (open(_url_file).read().strip() if os.path.exists(_url_file) else "")
if os.environ.get("PAAS_DB"): DB_URL = ""          # explicit sqlite path (tests/preview)
PG = DB_URL.startswith("postgres")
MIGRATIONS = [
 (1, """
create table if not exists accounts(id AUTOPK, email text unique, api_key_hash text unique, api_key_prefix text, password_hash text,
  email_verified int default 0, created REAL, credit_inr REAL default 500, billing_name text, billing_state text, gstin text);
create table if not exists apps(name text primary key, account_id int, spec text, status text, provider text, created REAL, updated REAL, error text);
create table if not exists instances(id text primary key, app text, provider text, host text, port int, status text, started REAL, stopped REAL, node_id text);
create table if not exists events(id AUTOPK, app text, ts REAL, msg text);
create index if not exists events_app on events(app, id);
create table if not exists volumes(name text primary key, account_id int, size_gb int, app text, mount_path text, created REAL, node_id text);
create table if not exists domains(domain text primary key, app text, account_id int, verified int default 0, created REAL, txt_token text, routed int default 0, owned int default 0);
create table if not exists nodes(id text primary key, provider text, provider_ref text, status text, public_ip text, wg_ip text, wg_pubkey text,
  agent_url text, agent_token_enc text, vcpu REAL, mem_mb int, gpus int, gpu_model text, plan text, created REAL, last_seen REAL, error text, dedicated_app text);
create table if not exists sessions(token_hash text primary key, account_id int, expires REAL, created REAL);
create table if not exists magic_links(token_hash text primary key, email text, expires REAL, used int default 0);
create table if not exists payments(order_id text primary key, account_id int, amount_paise int, credit_inr REAL, status text, payment_id text unique, created REAL, updated REAL);
create table if not exists webhook_events(id text primary key, received REAL);
create table if not exists invoices(id AUTOPK, number text unique, account_id int, order_id text unique, payment_id text, taxable_inr REAL, cgst REAL, sgst REAL, igst REAL,
  total_inr REAL, place_of_supply text, gstin text, billing_name text, created REAL);
create table if not exists ledger(id AUTOPK, account_id int, ts REAL, amount_inr REAL, kind text, ref text);
create table if not exists meter(instance_id text primary key, last_billed REAL);
create table if not exists kv(kind text, k text, v text, exp REAL, primary key(kind, k));
"""),
    (2, """
create table if not exists api_keys(id TEXT primary key, account_id bigint not null, key_hash TEXT unique not null, prefix TEXT, label TEXT, created REAL, last_used REAL);
create index if not exists api_keys_acct on api_keys(account_id);
"""),
    (3, """
alter table accounts add column tier TEXT default 'free';
alter table accounts add column month_key TEXT;
create table if not exists usage_month(account_id bigint, month TEXT, kind TEXT, qty REAL default 0, cost_inr REAL default 0, primary key(account_id, month, kind));
"""),
    # v4: sell in USD. Wallet/ledger/usage in US cents (REAL, so per-second metering keeps sub-cent precision); Stripe.
    # Existing INR balances are converted once at ₹88/$ (the old display rate); *_inr columns are kept but unused.
    (4, """
alter table accounts add column credit_cents REAL default 500;
alter table accounts add column stripe_customer TEXT;
alter table accounts add column stripe_subscription TEXT;
alter table accounts add column country TEXT;
alter table accounts add column tax_id TEXT;
update accounts set credit_cents = round(cast(credit_inr * 100.0 / 88.0 as numeric), 4);
alter table ledger add column amount_cents REAL;
update ledger set amount_cents = amount_inr * 100.0 / 88.0;
alter table usage_month add column cost_cents REAL default 0;
update usage_month set cost_cents = cost_inr * 100.0 / 88.0;
create table if not exists stripe_sessions(id TEXT primary key, account_id bigint, kind TEXT, credit_cents REAL, amount_cents bigint, tier TEXT,
  status TEXT, payment_intent TEXT, invoice_id TEXT, invoice_url TEXT, created REAL, updated REAL);
create table if not exists stripe_events(id TEXT primary key, type TEXT, received REAL);
"""),
    # v5: Fly-parity: releases/rollback, managed Postgres, archives (pg backups + volume snapshots), audit log, log drains
    (5, """
create table if not exists releases(id AUTOPK, app TEXT, version int, image TEXT, spec TEXT, status TEXT, reason TEXT, created REAL);
create index if not exists releases_app on releases(app, version);
create table if not exists pg_dbs(name TEXT primary key, account_id bigint, app TEXT, password_enc TEXT, plan TEXT, disk_gb int, created REAL, attached TEXT);
create table if not exists archives(id TEXT primary key, kind TEXT, owner TEXT, account_id bigint, path TEXT, size_bytes bigint, created REAL, note TEXT);
create index if not exists archives_owner on archives(kind, owner);
create table if not exists audit(id AUTOPK, ts REAL, account_id bigint, app TEXT, action TEXT, detail TEXT, ip TEXT);
create index if not exists audit_acct on audit(account_id, id);
create table if not exists drains(id TEXT primary key, app TEXT, account_id bigint, kind TEXT, url TEXT, created REAL, last_ok REAL, last_error TEXT, sent bigint default 0);
"""),
    # v6: private registries/git, source uploads, deploy tokens, GitHub App, cron
    (6, """
create table if not exists registry_creds(id TEXT primary key, account_id bigint, app TEXT, server TEXT, kind TEXT, username TEXT, secret_enc TEXT, created REAL);
create index if not exists registry_creds_acct on registry_creds(account_id);
create table if not exists git_creds(id TEXT primary key, account_id bigint, app TEXT, host TEXT, kind TEXT, username TEXT, secret_enc TEXT, public_key TEXT, created REAL);
create index if not exists git_creds_acct on git_creds(account_id);
create table if not exists deploy_tokens(id TEXT primary key, account_id bigint, app TEXT, label TEXT, token_hash TEXT unique, prefix TEXT, expires REAL, created REAL, last_used REAL);
create table if not exists github_installs(installation_id bigint primary key, account_id bigint, login TEXT, created REAL);
create table if not exists github_links(app TEXT primary key, account_id bigint, installation_id bigint, repo TEXT, branch TEXT, previews int default 1, created REAL);
create table if not exists uploads(id TEXT primary key, account_id bigint, app TEXT, path TEXT, size_bytes bigint, created REAL);
create table if not exists cron_runs(id AUTOPK, app TEXT, job TEXT, ts REAL, exit_code int, output TEXT);
create index if not exists cron_runs_app on cron_runs(app, id);
""")
    ,(7, """
alter table accounts add column is_admin int default 0;
alter table accounts add column is_test int default 0;
alter table sessions add column mfa_verified int default 0;
create table if not exists status_checks(id AUTOPK, ts REAL, component TEXT, ok int, ms REAL, code int, error TEXT);
create index if not exists status_checks_c on status_checks(component, ts);
""")
    ,(8, """
create table if not exists waitlist(id AUTOPK, kind TEXT, email TEXT, item TEXT default '', gpu TEXT, region TEXT, company TEXT, note TEXT, source TEXT,
  consent_ts REAL, created REAL, updated REAL, ip TEXT, is_test int default 0, mail_user TEXT, mail_admin TEXT, unique(kind, email, item));
create index if not exists waitlist_kind on waitlist(kind, created);
""")
]
def _dialect(sql):
    if PG: return sql.replace("AUTOPK", "bigserial primary key").replace("REAL", "double precision")
    return sql.replace("AUTOPK", "integer primary key autoincrement")
_lock = threading.RLock()
import psycopg, logging, time as _t
_log = logging.getLogger("paas.db")
def wait_for_db(url=DB_URL, timeout=None, connect=None, sleep=_t.sleep, now=_t.monotonic):
    """Block until Postgres accepts connections (it runs in Docker and may still be starting after a reboot or a dockerd
    restart). Exponential backoff 0.5 s -> 5 s, up to PAAS_DB_WAIT_SECONDS (default 120); then raise so supervisord retries."""
    timeout = float(os.environ.get("PAAS_DB_WAIT_SECONDS", "120")) if timeout is None else timeout
    connect = connect or (lambda: psycopg.connect(url, connect_timeout=5).close())
    end, delay, n = now() + timeout, 0.5, 0
    while True:
        n += 1
        try: connect(); (n > 1 and _log.warning("database ready after %d attempts", n)); return n
        except psycopg.OperationalError as e:
            if now() >= end: raise RuntimeError(f"database not reachable after {timeout:.0f}s ({n} attempts): {e}") from e
            if n == 1 or n % 5 == 0: _log.warning("waiting for database (attempt %d): %s", n, str(e).splitlines()[0])
            sleep(min(delay, max(0.0, end - now()))); delay = min(delay * 2, 5.0)
if PG:
    from psycopg_pool import ConnectionPool
    from psycopg.rows import dict_row
    wait_for_db()
    _pool = ConnectionPool(DB_URL, min_size=2, max_size=20, kwargs={"autocommit": True, "row_factory": dict_row}, open=True, check=ConnectionPool.check_connection)
    import atexit; atexit.register(_pool.close)
    def q(sql, args=(), one=False, _retries=3):
        """Connection-level failures (DB restarting, connection killed) are retried with backoff; the statement is autocommit,
        so a statement cut off by a disconnect was rolled back and is safe to re-run."""
        for attempt in range(_retries + 1):
            try:
                with _pool.connection() as c:
                    cur = c.execute(sql.replace("?", "%s"), args)
                    rows = cur.fetchall() if cur.description else []
                return (rows[0] if rows else None) if one else rows
            except (psycopg.OperationalError, psycopg.errors.AdminShutdown) as e:
                if attempt == _retries: raise
                _log.warning("db connection error, retrying (%d/%d): %s", attempt + 1, _retries, str(e).splitlines()[0]); _t.sleep(0.5 * 2 ** attempt)
    def executescript(sql):
        with _pool.connection() as c:
            for stmt in [s for s in sql.split(";") if s.strip()]: c.execute(stmt)
else:
    DB = os.environ.get("PAAS_DB", os.path.join(ROOT, "data", "paas.sqlite"))
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    _c = sqlite3.connect(DB, check_same_thread=False); _c.row_factory = sqlite3.Row
    def q(sql, args=(), one=False):
        with _lock:
            cur = _c.execute(sql, args); rows = [dict(r) for r in cur.fetchall()] if cur.description else []; _c.commit()
        return (rows[0] if rows else None) if one else rows
    def executescript(sql):
        with _lock: _c.executescript(sql); _c.commit()
def migrate():
    q("create table if not exists schema_migrations(version int primary key, applied double precision)" if PG else "create table if not exists schema_migrations(version int primary key, applied REAL)")
    done = {r["version"] for r in q("select version from schema_migrations")}
    for v, sql in MIGRATIONS:
        if v in done: continue
        executescript(_dialect(sql)); q("insert into schema_migrations values(?,?)", (v, time.time()))
migrate()
def audit(account_id, app, action, detail="", ip=None): q("insert into audit(ts,account_id,app,action,detail,ip) values(?,?,?,?,?,?)", (time.time(), account_id, app, action, (detail or "")[:2000], ip))
def event(app, msg): q("insert into events(app,ts,msg) values(?,?,?)", (app, time.time(), msg))
def kv_put(kind, k, v, ttl=None):
    q("insert into kv(kind,k,v,exp) values(?,?,?,?) on conflict(kind,k) do update set v=excluded.v, exp=excluded.exp", (kind, k, v, time.time()+ttl if ttl else None))
def kv_get(kind, k):
    r = q("select v, exp from kv where kind=? and k=?", (kind, k), one=True)
    return None if not r or (r["exp"] and r["exp"] < time.time()) else r["v"]
def kv_del(kind, k): q("delete from kv where kind=? and k=?", (kind, k))
