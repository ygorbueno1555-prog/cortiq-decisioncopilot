"""thunder_healthcheck.py — Read-only / dry-run healthcheck for the GTM stack.

Checks: HubSpot, Supabase, Firecrawl, Apollo, Unipile, Clay.
No writes. Classifies each failure as: credential | not_connected | webhook | api | script.

Usage:
  python thunder_healthcheck.py            # print report, exit 1 if any failures
  python thunder_healthcheck.py --json     # machine-readable output
"""
import logging
import os
import sys
from typing import Optional

import httpx

log = logging.getLogger("thunder_cron")

_HTTP_TIMEOUT = int(os.environ.get("HEALTHCHECK_TIMEOUT", "10"))


# ── Result helpers ────────────────────────────────────────────────────────────

def _ok(service: str, msg: str = "ok") -> dict:
    return {"service": service, "status": "ok", "msg": msg}

def _fail(service: str, failure_type: str, msg: str) -> dict:
    return {"service": service, "status": "fail", "failure_type": failure_type, "msg": msg}

def _skip(service: str, reason: str) -> dict:
    return {"service": service, "status": "skip", "msg": reason}


# ── HubSpot ───────────────────────────────────────────────────────────────────

def check_hubspot() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return _fail("HubSpot", "credential", "HUBSPOT_ACCESS_TOKEN not set")
    try:
        r = httpx.get(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            params={"limit": 1, "properties": "email"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 401:
            return _fail("HubSpot", "credential", "token invalid or expired")
        if r.status_code == 403:
            return _fail("HubSpot", "credential", "token lacks contacts:read scope")
        if r.status_code == 200:
            total = r.json().get("total", "?")
            return _ok("HubSpot", f"read-only OK ({total} contacts visible)")
        return _fail("HubSpot", "api", f"HTTP {r.status_code}: {r.text[:120]}")
    except httpx.ConnectError as exc:
        return _fail("HubSpot", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("HubSpot", "script", str(exc)[:120])


# ── Supabase ──────────────────────────────────────────────────────────────────

def check_supabase() -> dict:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_ANON_KEY", os.environ.get("SUPABASE_KEY", ""))
    if not url:
        return _fail("Supabase", "credential", "SUPABASE_URL not set")
    if not key:
        return _fail("Supabase", "credential", "SUPABASE_ANON_KEY / SUPABASE_KEY not set")
    try:
        r = httpx.get(
            f"{url}/rest/v1/",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code in (200, 404):
            return _ok("Supabase", "REST endpoint reachable")
        if r.status_code == 401:
            return _fail("Supabase", "credential", "anon key rejected")
        return _fail("Supabase", "api", f"HTTP {r.status_code}")
    except httpx.ConnectError as exc:
        return _fail("Supabase", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("Supabase", "script", str(exc)[:120])


# ── Firecrawl ─────────────────────────────────────────────────────────────────

def check_firecrawl() -> dict:
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        return _skip("Firecrawl", "FIRECRAWL_API_KEY not set — assuming local containers")
    try:
        r = httpx.get(
            "https://api.firecrawl.dev/v1/team/credits",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            credits = r.json().get("credits", "?")
            return _ok("Firecrawl", f"credits remaining: {credits}")
        if r.status_code == 401:
            return _fail("Firecrawl", "credential", "API key rejected")
        return _fail("Firecrawl", "api", f"HTTP {r.status_code}")
    except httpx.ConnectError as exc:
        return _fail("Firecrawl", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("Firecrawl", "script", str(exc)[:120])


# ── Apollo ────────────────────────────────────────────────────────────────────

def check_apollo() -> dict:
    key = os.environ.get("APOLLO_API_KEY", "")
    if not key:
        return _fail("Apollo", "credential", "APOLLO_API_KEY not set — enrollment checks unavailable")
    try:
        # Lightweight endpoint: search with limit=0 to avoid consuming credits
        r = httpx.post(
            "https://api.apollo.io/v1/mixed_people/search",
            json={"api_key": key, "page": 1, "per_page": 1},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return _ok("Apollo", "auth check OK (read-only search)")
        if r.status_code in (401, 403):
            return _fail("Apollo", "credential", f"key rejected — HTTP {r.status_code}")
        return _fail("Apollo", "api", f"HTTP {r.status_code}: {r.text[:80]}")
    except httpx.ConnectError as exc:
        return _fail("Apollo", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("Apollo", "script", str(exc)[:120])


# ── Unipile ───────────────────────────────────────────────────────────────────

def check_unipile() -> dict:
    key     = os.environ.get("UNIPILE_API_KEY", "")
    acct_id = os.environ.get("UNIPILE_ACCOUNT_ID", "")
    if not key:
        return _fail("Unipile", "credential", "UNIPILE_API_KEY not set")
    if not acct_id:
        return _fail("Unipile", "not_connected",
                     "UNIPILE_ACCOUNT_ID not set — LinkedIn account not linked")
    try:
        dsn = os.environ.get("UNIPILE_DSN", "api4.unipile.com:13465")
        r = httpx.get(
            f"https://{dsn}/api/v1/accounts/{acct_id}",
            headers={"X-API-KEY": key},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            status = data.get("status", "?")
            if status != "OK":
                return _fail("Unipile", "not_connected",
                             f"account found but status={status}")
            return _ok("Unipile", f"account connected status={status}")
        if r.status_code == 401:
            return _fail("Unipile", "credential", "API key rejected")
        if r.status_code == 404:
            return _fail("Unipile", "not_connected",
                         "account not found — check UNIPILE_ACCOUNT_ID")
        return _fail("Unipile", "api", f"HTTP {r.status_code}")
    except httpx.ConnectError as exc:
        return _fail("Unipile", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("Unipile", "script", str(exc)[:120])


# ── Clay ──────────────────────────────────────────────────────────────────────

def check_clay() -> dict:
    key = os.environ.get("CLAY_API_KEY", "")
    if not key:
        return _fail("Clay", "credential", "CLAY_API_KEY not set")
    try:
        r = httpx.get(
            "https://api.clay.com/v1/sources",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return _ok("Clay", "read-only OK")
        if r.status_code == 401:
            return _fail("Clay", "credential", "key rejected")
        return _fail("Clay", "api", f"HTTP {r.status_code}")
    except httpx.ConnectError as exc:
        return _fail("Clay", "api", f"connection error: {exc}")
    except Exception as exc:
        return _fail("Clay", "script", str(exc)[:120])


# ── Aggregate ─────────────────────────────────────────────────────────────────

_CHECKS = [
    check_hubspot,
    check_supabase,
    check_firecrawl,
    check_apollo,
    check_unipile,
    check_clay,
]


def run_all() -> list:
    results = []
    for fn in _CHECKS:
        try:
            result = fn()
        except Exception as exc:
            result = _fail(fn.__name__, "script", str(exc)[:120])
        log.debug("healthcheck %s → %s", result["service"], result["status"])
        results.append(result)
    return results


def to_findings(results: list) -> list:
    """Convert healthcheck results to thunder_cron finding dicts."""
    findings = []
    for r in results:
        if r["status"] == "ok":
            continue
        if r["status"] == "skip":
            continue
        ft = r.get("failure_type", "")
        key = f"health:{r['service'].lower()}:{ft}" if ft else f"health:{r['service'].lower()}"
        findings.append({
            "level": "ERROR",
            "key": key,
            "msg": f"{r['service']}: {r['msg']}",
        })
    return findings


def format_report(results: list) -> str:
    icons = {"ok": "✅", "fail": "❌", "skip": "⏭️"}
    lines = []
    for r in results:
        icon   = icons.get(r["status"], "❓")
        label  = r.get("failure_type", "")
        suffix = f" [{label}]" if label else ""
        lines.append(f"{icon} {r['service']}{suffix}: {r['msg']}")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json as _json
    import logging as _logging
    _logging.basicConfig(level=_logging.WARNING, format="[%(levelname)s] %(message)s")
    log.setLevel(_logging.WARNING)

    parser = argparse.ArgumentParser(description="Thunder GTM stack healthcheck")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="output as JSON")
    cli_args = parser.parse_args()

    results = run_all()

    if cli_args.as_json:
        print(_json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_report(results))

    fails = [r for r in results if r["status"] == "fail"]
    sys.exit(1 if fails else 0)
