"""
wd_runner.py — wrapper that auto-installs HTTP/WebSocket logging hooks
BEFORE running a user's bot code, so every bot gets Live Logs for free.

Invoked by the FastAPI bot manager instead of running the bot's code.py
directly:

    BEFORE: python -u code.py
    AFTER:  python -u wd_runner.py code.py

Behavior:
  1. Imports wd_autolog (installs monkey-patches on requests, httpx,
     websocket-client, websockets, and sys.excepthook)
  2. Runs the user's code as if it were the main script — `__name__`
     correctly resolves to '__main__' inside the user's file
  3. Any failure during hook setup is logged but doesn't block the bot
"""
import os
import sys


def main():
    if len(sys.argv) < 2:
        print("[wd_runner] usage: wd_runner.py <bot_code.py>", flush=True)
        sys.exit(2)

    code_path = sys.argv[1]

    # ── 1. Install auto-logging hooks (best-effort) ──────────────────────
    try:
        import wd_autolog  # noqa: F401  — side-effect import installs hooks
    except Exception as e:
        # Log a single-line warning but keep going. We never want a hook
        # failure to break the bot itself.
        print(f"[wd_runner] WARNING: auto-log hooks failed to install: {e}",
              flush=True)

    # ── 2. Make sys.argv look as if the bot was launched directly ────────
    # Some bots inspect sys.argv; reset it so they don't see wd_runner in [0].
    sys.argv = [code_path] + sys.argv[2:]

    # ── 3. Run the user's code as __main__ ───────────────────────────────
    import runpy
    runpy.run_path(code_path, run_name="__main__")


if __name__ == "__main__":
    main()
