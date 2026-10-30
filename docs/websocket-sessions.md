# Console and proxy websocket sessions

`paas/ws_registry.py` tracks every open console (`/v1/apps/{name}/console`) and proxy (`/v1/apps/{name}/proxy`) socket
by account and by the credential that opened it (API key or deploy token hash, or session).

- A socket is registered the moment auth passes, before any await, and then re-checked (R1).
- Suspending or deleting an account closes its sockets immediately (`close_account`).
- Revoking or rotating a key, revoking a deploy token or an OAuth connection, or a credentials reset calls
  `revalidate(account_id)`, which closes sockets whose credential no longer resolves (R2).
- Every socket also re-checks its account and credential every 30 s. A database error is retried once, then the
  socket is closed (fail closed, R4).

**Per-process (R3).** The registry lives in memory in one process. Production runs a single `paas.main` process, so the
immediate close reaches every socket. If the API is ever run with several workers or replicas, the immediate close only
reaches sockets in the worker that handled the admin or revoke request; the others close within 30 s via the watchdog.
Moving to more workers should add a shared signal (for example Postgres LISTEN/NOTIFY) before relying on instant closes.
