"""thunder_cron.py — Cron wrapper for Thunder GTM monitoring.

Prevents duplicate execution (lockfile), hard timeout, Telegram alerts on
failure, log rotation, compact report, and alert deduplication.

Usage:
  python thunder_cron.py               # full monitor run
  python thunder_cron.py --preflight   # preflight checks only
  python thunder_cron.py --healthcheck # stack healthcheck only
"""
import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("PERSISTENT_DATA_DIR", BASE_DIR / "data"))
LOG_DIR  = DATA_DIR / "logs"

LOCK_FILE  = DATA_DIR / "thunder_monitor.lock"
STATE_FILE = DATA_DIR / "thunder_alert_state.json"

THUNDER_DIR    = Path(os.environ.get("THUNDER_GTM_DIR", "/root/thunder-gtm"))
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_MONITOR_CHAT_ID",
                                os.environ.get("TELEGRAM_CHAT_ID", ""))

TIMEOUT_SECS     = int(os.environ.get("THUNDER_CRON_TIMEOUT", "600"))   # 10 min
MAX_FINDINGS     = int(os.environ.get("THUNDER_MAX_FINDINGS", "3"))
DEDUP_TTL_HOURS  = int(os.environ.get("THUNDER_DEDUP_TTL_HOURS", "24"))
LOG_MAX_BYTES    = 5 * 1024 * 1024   # 5 MB
LOG_BACKUP_COUNT = 3


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "thunder_monitor.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
    )
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    logger = logging.getLogger("thunder_cron")
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _setup_logger()


# ── Telegram ──────────────────────────────────────────────────────────────────

def _telegram_send(text: str, *, level: str = "INFO") -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    prefix = {"ERROR": "🔴", "WARN": "🟡", "OK": "🟢"}.get(level, "⚪")
    msg = f"{prefix} *Thunder Monitor*\n{text}"
    try:
        import httpx
        r = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("telegram sendMessage returned HTTP %d", r.status_code)
            return False
        return True
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
        return False


# ── Lockfile ──────────────────────────────────────────────────────────────────

class _AlreadyRunning(Exception):
    pass


class _FileLock:
    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self._path, "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except (IOError, OSError):
            if self._fh:
                self._fh.close()
                self._fh = None
            raise _AlreadyRunning("another thunder_cron instance is already running")
        return self

    def __exit__(self, *_):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Hard timeout ──────────────────────────────────────────────────────────────

class _Timeout(Exception):
    pass


class _HardTimeout:
    def __init__(self, seconds: int):
        self._secs = seconds

    def _handler(self, *_):
        raise _Timeout(f"exceeded {self._secs}s hard timeout")

    def __enter__(self):
        signal.signal(signal.SIGALRM, self._handler)
        signal.alarm(self._secs)
        return self

    def __exit__(self, *_):
        signal.alarm(0)


# ── Alert state (deduplication) ───────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": {}}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_new_finding(key: str, state: dict) -> bool:
    """Returns True (and records timestamp) only if this key is unseen within DEDUP_TTL_HOURS."""
    seen = state.setdefault("seen", {})
    now_ts = time.time()
    last_seen = seen.get(key)
    if last_seen and (now_ts - last_seen) < DEDUP_TTL_HOURS * 3600:
        return False
    seen[key] = now_ts
    return True


# ── Preflight ─────────────────────────────────────────────────────────────────

def _preflight() -> list:
    issues = []

    if not THUNDER_DIR.exists():
        issues.append(f"THUNDER_GTM_DIR not found: {THUNDER_DIR}")
    elif not os.access(THUNDER_DIR, os.R_OK):
        issues.append(f"THUNDER_GTM_DIR not readable: {THUNDER_DIR}")

    if not TELEGRAM_TOKEN:
        issues.append("TELEGRAM_BOT_TOKEN not set — Telegram alerts disabled")
    if not TELEGRAM_CHAT:
        issues.append("TELEGRAM_MONITOR_CHAT_ID not set — Telegram alerts disabled")

    # Gateway token: check env + well-known file paths
    token_env = os.environ.get("THUNDER_GATEWAY_TOKEN", "")
    token_path_env = os.environ.get("THUNDER_TOKEN_PATH", "")
    candidates = [
        Path(token_path_env) if token_path_env else None,
        THUNDER_DIR / ".gateway_token",
        THUNDER_DIR / "config" / "gateway_token",
        BASE_DIR / ".thunder_gateway_token",
    ]
    token_found = bool(token_env) or any(
        p and p.exists() for p in candidates
    )
    if not token_found:
        issues.append(
            "gateway token not found — set THUNDER_GATEWAY_TOKEN or THUNDER_TOKEN_PATH "
            f"(tried: {', '.join(str(p) for p in candidates if p)})"
        )

    return issues


# ── Run monitor ────────────────────────────────────────────────────────────────

def _run_monitor() -> list:
    from thunder_monitor import run_checks
    from thunder_monitor import read_snapshot
    snap = read_snapshot()
    return run_checks(snap)


def _run_healthcheck() -> list:
    from thunder_healthcheck import run_all, to_findings
    results = run_all()
    return to_findings(results)


# ── Report builder ─────────────────────────────────────────────────────────────

def _build_report(findings: list, state: dict) -> Optional[str]:
    new_findings = [f for f in findings if _is_new_finding(f["key"], state)]
    if not new_findings:
        return None

    icons = {"ERROR": "🔴", "WARN": "🟡", "OK": "🟢", "INFO": "ℹ️"}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"*Thunder Monitor — {ts}*", ""]

    for f in new_findings[:MAX_FINDINGS]:
        icon = icons.get(f.get("level", ""), "⚪")
        lines.append(f"{icon} `{f['key']}`: {f['msg']}")

    remaining = len(new_findings) - MAX_FINDINGS
    if remaining > 0:
        lines.append(f"_... e mais {remaining} achado(s) — ver log completo_")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Thunder GTM cron monitor")
    p.add_argument("--preflight",   action="store_true", help="run preflight checks only")
    p.add_argument("--healthcheck", action="store_true", help="run stack healthcheck only")
    p.add_argument("--no-telegram", action="store_true", help="suppress Telegram output")
    return p.parse_args()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()

    # ── Preflight-only mode ──────────────────────────────────────────────────
    if args.preflight:
        issues = _preflight()
        if issues:
            for issue in issues:
                log.warning("preflight: %s", issue)
            return 1
        log.info("preflight OK")
        return 0

    start_ts = datetime.now(timezone.utc)
    log.info("thunder_cron start pid=%d timeout=%ds", os.getpid(), TIMEOUT_SECS)

    pre_issues = _preflight()
    for issue in pre_issues:
        log.warning("preflight: %s", issue)

    state = _load_state()

    try:
        with _FileLock(LOCK_FILE):
            with _HardTimeout(TIMEOUT_SECS):
                if args.healthcheck:
                    findings = _run_healthcheck()
                else:
                    findings = _run_monitor()
                    findings += _run_healthcheck()

        elapsed = (datetime.now(timezone.utc) - start_ts).total_seconds()
        error_count = sum(1 for f in findings if f.get("level") == "ERROR")
        log.info("thunder_cron done in %.1fs — %d finding(s) %d error(s)",
                 elapsed, len(findings), error_count)

        if not args.no_telegram:
            report = _build_report(findings, state)
            if report:
                level = "ERROR" if error_count else "WARN"
                _telegram_send(report, level=level)

        _save_state(state)
        return 1 if error_count else 0

    except _AlreadyRunning as exc:
        log.warning("skipped: %s", exc)
        return 0

    except _Timeout:
        msg = f"timed out after {TIMEOUT_SECS}s"
        log.error("thunder_cron %s", msg)
        if not args.no_telegram and _is_new_finding("cron:timeout", state):
            _telegram_send(f"TIMEOUT: {msg}", level="ERROR")
        _save_state(state)
        return 2

    except Exception as exc:
        tb = traceback.format_exc()
        log.error("thunder_cron unhandled exception: %s\n%s", exc, tb)
        exc_key = f"cron:exception:{type(exc).__name__}"
        if not args.no_telegram and _is_new_finding(exc_key, state):
            _telegram_send(f"ERRO: `{type(exc).__name__}: {exc}`", level="ERROR")
        _save_state(state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
