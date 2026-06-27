"""thunder_monitor.py — Thunder GTM monitoring checks.

Called by thunder_cron.py. Reads /root/thunder-gtm read-only, validates the
gateway token, and pings the local agent with retry/fallback.

Each finding dict: {"level": "ERROR"|"WARN"|"OK", "key": str, "msg": str}
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("thunder_cron")

THUNDER_DIR   = Path(os.environ.get("THUNDER_GTM_DIR", "/root/thunder-gtm"))
AGENT_URL     = os.environ.get("THUNDER_AGENT_URL", "http://localhost:8080")
AGENT_TIMEOUT = int(os.environ.get("THUNDER_AGENT_TIMEOUT", "15"))
AGENT_RETRIES = int(os.environ.get("THUNDER_AGENT_RETRIES", "3"))


# ── Token resolution ──────────────────────────────────────────────────────────

def _load_gateway_token() -> str:
    """Resolve gateway token from env or well-known file paths."""
    token_env = os.environ.get("THUNDER_GATEWAY_TOKEN", "")
    if token_env:
        return token_env

    token_path_env = os.environ.get("THUNDER_TOKEN_PATH", "")
    candidates = [
        Path(token_path_env) if token_path_env else None,
        THUNDER_DIR / ".gateway_token",
        THUNDER_DIR / "config" / "gateway_token",
        Path(__file__).parent / ".thunder_gateway_token",
    ]
    for p in candidates:
        if p and p.exists():
            try:
                token = p.read_text(encoding="utf-8").strip()
                if token:
                    log.debug("loaded gateway token from %s", p)
                    return token
            except Exception as exc:
                log.warning("could not read token from %s: %s", p, exc)
    return ""


# ── Read-only snapshot ────────────────────────────────────────────────────────

def read_snapshot() -> dict:
    """Read thunder-gtm directory read-only. Returns a snapshot dict."""
    snap: dict = {
        "thunder_dir": str(THUNDER_DIR),
        "exists": THUNDER_DIR.exists(),
        "files": [],
        "config": {},
        "errors": [],
    }

    if not THUNDER_DIR.exists():
        snap["errors"].append(f"thunder dir not found: {THUNDER_DIR}")
        return snap

    if not os.access(THUNDER_DIR, os.R_OK):
        snap["errors"].append(f"thunder dir not readable: {THUNDER_DIR}")
        return snap

    try:
        for entry in sorted(THUNDER_DIR.iterdir()):
            info: dict = {"name": entry.name, "is_dir": entry.is_dir()}
            if entry.is_file():
                info["size"] = entry.stat().st_size
            snap["files"].append(info)
    except PermissionError as exc:
        snap["errors"].append(f"permission denied listing thunder dir: {exc}")
        return snap

    # Read config (first match wins)
    for cfg_name in ("config.json", "settings.json", "thunder.json"):
        cfg_path = THUNDER_DIR / cfg_name
        if cfg_path.exists():
            try:
                snap["config"] = json.loads(cfg_path.read_text(encoding="utf-8"))
                snap["config_file"] = cfg_name
                break
            except json.JSONDecodeError as exc:
                snap["errors"].append(f"invalid JSON in {cfg_name}: {exc}")
            except Exception as exc:
                snap["errors"].append(f"could not read {cfg_name}: {exc}")

    return snap


# ── Local agent call with retry ────────────────────────────────────────────────

def _call_agent(endpoint: str, payload: Optional[dict] = None,
                method: str = "GET") -> Optional[dict]:
    """Call local agent with retry + backoff. Returns None if unreachable."""
    import httpx

    token = _load_gateway_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{AGENT_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    last_exc = None

    for attempt in range(1, AGENT_RETRIES + 1):
        try:
            if method == "POST":
                r = httpx.post(url, json=payload or {}, headers=headers,
                               timeout=AGENT_TIMEOUT)
            else:
                r = httpx.get(url, headers=headers, timeout=AGENT_TIMEOUT)

            if r.status_code == 401:
                log.error(
                    "agent auth failed (attempt %d/%d) — gateway token mismatch at %s",
                    attempt, AGENT_RETRIES, url,
                )
                return None  # auth failures won't improve with retries

            r.raise_for_status()
            return r.json()

        except Exception as exc:
            last_exc = exc
            log.warning("agent call attempt %d/%d failed: %s", attempt, AGENT_RETRIES, exc)
            if attempt < AGENT_RETRIES:
                time.sleep(2 ** (attempt - 1))   # 1s, 2s, 4s

    log.error("agent unreachable after %d attempts: %s", AGENT_RETRIES, last_exc)
    return None


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_dir(snap: dict) -> list:
    findings = []

    if not snap["exists"]:
        findings.append({
            "level": "ERROR",
            "key": "thunder_dir_missing",
            "msg": f"thunder-gtm dir not found at {THUNDER_DIR} — "
                   "set THUNDER_GTM_DIR env var",
        })
        return findings

    for err in snap.get("errors", []):
        findings.append({
            "level": "ERROR",
            "key": f"thunder_dir:{hash(err) & 0xFFFFFF:06x}",
            "msg": err,
        })

    if not snap.get("files"):
        findings.append({
            "level": "WARN",
            "key": "thunder_dir_empty",
            "msg": "thunder-gtm directory exists but is empty",
        })

    return findings


def _check_gateway_token() -> list:
    token = _load_gateway_token()
    if not token:
        return [{
            "level": "ERROR",
            "key": "gateway_token_missing",
            "msg": (
                "gateway token not found — set THUNDER_GATEWAY_TOKEN env var "
                "or place token in THUNDER_TOKEN_PATH / thunder-gtm/.gateway_token"
            ),
        }]
    return []


def _check_agent_liveness() -> list:
    """Ping /health on the local agent. Classify: unreachable vs auth vs ok."""
    import httpx

    token = _load_gateway_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{AGENT_URL.rstrip('/')}/health"
    try:
        r = httpx.get(url, headers=headers, timeout=AGENT_TIMEOUT)
        if r.status_code == 200:
            log.info("agent liveness OK: %s", url)
            return []
        if r.status_code == 401:
            return [{
                "level": "ERROR",
                "key": "agent_auth_fail",
                "msg": f"agent returned 401 at {url} — gateway token mismatch",
            }]
        return [{
            "level": "WARN",
            "key": f"agent_status_{r.status_code}",
            "msg": f"agent health check returned HTTP {r.status_code} at {url}",
        }]
    except httpx.ConnectError:
        return [{
            "level": "WARN",
            "key": "agent_unreachable",
            "msg": f"agent not reachable at {url} — is it running? "
                   "set THUNDER_AGENT_URL if URL differs",
        }]
    except Exception as exc:
        return [{
            "level": "WARN",
            "key": "agent_error",
            "msg": f"agent check raised {type(exc).__name__}: {exc}",
        }]


# ── Main ──────────────────────────────────────────────────────────────────────

def run_checks(snap: dict) -> list:
    """Run all Thunder monitoring checks. Returns list of findings."""
    findings = []
    findings.extend(_check_dir(snap))
    findings.extend(_check_gateway_token())
    findings.extend(_check_agent_liveness())
    return findings
