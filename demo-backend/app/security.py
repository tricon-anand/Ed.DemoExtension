"""Login throttling (in memory, per process)."""
import os
import time

from fastapi import HTTPException

MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))
_fails: dict[str, list[float]] = {}


def _recent(key: str) -> list[float]:
    cutoff = time.time() - WINDOW_SECONDS
    kept = [t for t in _fails.get(key, []) if t > cutoff]
    _fails[key] = kept
    return kept


def check_login(key: str) -> None:
    if len(_recent(key)) >= MAX_FAILS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed logins. Try again in a few minutes.",
            headers={"Retry-After": str(WINDOW_SECONDS)},
        )


def record_failure(key: str) -> None:
    _recent(key).append(time.time())


def clear(key: str) -> None:
    _fails.pop(key, None)


def reset() -> None:
    _fails.clear()
