"""
app/token_tracker.py  —  In-memory Anthropic API token counters
Resets on server restart; accumulates per-session only.
Imported by system_stats (to expose) and bots (to record).
"""
import threading
from datetime import datetime, timezone

_lock  = threading.Lock()
_start = datetime.now(timezone.utc)
_data  = {"input": 0, "output": 0, "requests": 0}


def record(input_tokens: int, output_tokens: int) -> None:
    """Call this after every successful Anthropic API call."""
    with _lock:
        _data["input"]    += input_tokens
        _data["output"]   += output_tokens
        _data["requests"] += 1


def snapshot() -> dict:
    """Return a copy of the current counters (thread-safe)."""
    with _lock:
        return {
            "token_input":   _data["input"],
            "token_output":  _data["output"],
            "token_total":   _data["input"] + _data["output"],
            "ai_requests":   _data["requests"],
            "session_start": _start.isoformat(),
        }
