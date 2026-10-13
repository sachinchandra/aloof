#!/usr/bin/env python3
"""Health monitor: checks API and MCP /healthz endpoints and sends email alerts on failure.
Run periodically via cron or systemd timer. Uses existing SMTP config from paas.auth.

Environment variables:
  HEALTH_CHECK_ENABLED=1       Enable health checks (off by default)
  HEALTH_API_URL               API base URL (default: http://localhost:8000)
  HEALTH_MCP_URL               MCP base URL (default: http://localhost:8001)
  HEALTH_ALERT_EMAIL           Email address for alerts (required if enabled)
  HEALTH_TIMEOUT               Request timeout in seconds (default: 10)
  
SMTP settings are inherited from paas/ config (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, MAIL_FROM).
"""
import os
import sys
import time
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def check_health(url: str, timeout: int = 10) -> tuple[bool, str]:
    """Check a /healthz endpoint. Returns (ok, detail)."""
    try:
        r = httpx.get(f"{url.rstrip('/')}/healthz", timeout=timeout)
        if r.status_code == 200:
            return True, f"{r.status_code} OK"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.TimeoutException:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, str(e)[:200]

def main():
    if os.environ.get("HEALTH_CHECK_ENABLED", "0") != "1":
        print("Health checks disabled (set HEALTH_CHECK_ENABLED=1 to enable)")
        return 0

    alert_email = os.environ.get("HEALTH_ALERT_EMAIL", "").strip()
    if not alert_email:
        print("ERROR: HEALTH_ALERT_EMAIL is required when HEALTH_CHECK_ENABLED=1")
        return 1

    api_url = os.environ.get("HEALTH_API_URL", "http://localhost:8000")
    mcp_url = os.environ.get("HEALTH_MCP_URL", "http://localhost:8001")
    timeout = int(os.environ.get("HEALTH_TIMEOUT", "10"))

    failures = []
    for name, url in [("API", api_url), ("MCP", mcp_url)]:
        ok, detail = check_health(url, timeout)
        status = "OK" if ok else "FAIL"
        print(f"{name}: {status} - {detail}")
        if not ok:
            failures.append((name, url, detail))

    if failures:
        from paas.auth import send_mail
        
        brand = os.environ.get("PAAS_BRAND", "Aloof")
        hostname = os.environ.get("HOSTNAME", "unknown")
        
        subject = f"[{brand}] Health check FAILED on {hostname}"
        body = f"""Health check failure detected at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}

Host: {hostname}

Failed checks:
"""
        for name, url, detail in failures:
            body += f"\n  {name} ({url}/healthz): {detail}"
        
        body += f"""

This alert was sent by the {brand} health monitor.
Check the server logs for more details.
"""
        try:
            method = send_mail(alert_email, subject, body)
            print(f"Alert sent via {method} to {alert_email}")
        except Exception as e:
            print(f"ERROR: Failed to send alert: {e}")
            return 2
        
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
