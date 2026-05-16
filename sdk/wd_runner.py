"""
wd_runner.py — bot-script wrapper.

Invoked by the FastAPI bot manager instead of running the bot's code.py
directly:

    BEFORE: python -u code.py
    AFTER:  python -u wd_runner.py code.py

Three responsibilities:
  1. Best-effort install of auto-logging hooks (wd_autolog) — never blocks
     the bot if hook setup fails or the module isn't available (venv path).
  2. Run the user's code as __main__ via runpy.
  3. Wrap that run in a structured error layer:
       - On exception, walk the traceback to find the FIRST frame whose
         filename matches the user's script.
       - Emit ONE friendly [ERROR] line with the line number AND a hint
         from the built-in hint table (KeyError 'close' → DataFrame is
         missing column; ConnectionError to api.kalshi.com → check
         Connections; etc).
       - Then emit the raw traceback as [DEBUG] lines so power users
         still have everything.
     This keeps the UI clean ("[ERROR] line 42: KeyError 'close' — your
     DataFrame is missing the 'close' column. Check the symbol/timeframe
     or the API response.") while preserving full debuggability.

Pure stdlib — works inside any bot venv regardless of which packages the
user has declared.
"""
import os
import re
import runpy
import sys
import traceback


# ─────────────────────────────────────────────────────────────────────────────
# Friendly-error hint table.
# Tuple format: (exception_type_name, message_regex, hint_template).
# {match0} in the template is the FIRST captured group (or the whole match
# if no groups). Patterns are tried IN ORDER; first match wins.
# Keep this file pure-stdlib so it loads in every venv.
# ─────────────────────────────────────────────────────────────────────────────
HINTS = [
    # DataFrame / market-data column missing — the most common pandas pitfall
    ("KeyError",
     r"^'(close|open|high|low|volume|timestamp|date|time|symbol|price)'$",
     "Your DataFrame is missing the {match0!r} column. Check the symbol, "
     "timeframe, or the API response shape."),

    # Common config-dict KeyErrors
    ("KeyError",
     r"^'(api_key|secret|api_secret|token|access_key|password)'$",
     "Missing key {match0!r} in your config. Check the Connections panel "
     "and make sure the key name matches what your code expects."),

    # Network / API outages, by host
    ("ConnectionError",
     r"(api\.kalshi\.com|kalshi)",
     "Kalshi API unreachable. Verify your network and that KALSHI_KEY is "
     "set in the Connections panel."),
    ("ConnectionError",
     r"(api\.binance\.com|binance)",
     "Binance API unreachable. Check your network and "
     "BINANCE_KEY / BINANCE_SECRET in Connections."),
    ("ConnectionError",
     r"(coinbase|api\.coinbase|api\.exchange\.coinbase)",
     "Coinbase API unreachable. Check your network and "
     "COINBASE_KEY / COINBASE_SECRET in Connections."),
    ("ConnectionError",
     r".*",
     "Network/API connection error. Check your internet and that the "
     "service is online. Consider adding retry-with-backoff to your bot."),

    # Missing dependency — the runner retries this automatically (see
    # bot_venv.install_one_into_venv + bots._execute retry loop). Tell the
    # user so they don't think the bot died for good.
    ("ModuleNotFoundError",
     r"No module named '([A-Za-z0-9_.]+)'",
     "Missing dependency {match0!r}. The runner will auto-install it and "
     "retry (up to 3 attempts)."),

    # File issues
    ("FileNotFoundError",
     r".*\.pem.*",
     "Bot tried to open a PEM file that isn't there. If this is a private "
     "key, upload it via the Connections panel — the runner materialises "
     "it into the bot's working directory at runtime."),
    ("FileNotFoundError",
     r".*",
     "Bot tried to open a file that doesn't exist. Files placed via "
     "Connections live in the bot's working directory at runtime."),
    ("PermissionError",
     r".*",
     "OS denied access to a file. Try restarting the app; if it persists, "
     "check that no other process holds the file."),

    # Common numeric / conversion bugs
    ("ValueError",
     r"could not convert string to float: '(.+)'",
     "Couldn't parse {match0!r} as a number. An API likely returned an "
     "unexpected shape — log the raw response and inspect it."),
    ("ValueError",
     r"^invalid literal for int.*: '(.*)'",
     "Couldn't parse {match0!r} as an integer. Check whether the value is "
     "actually numeric before calling int()."),
    ("ZeroDivisionError",
     r".*",
     "Divided by zero. Guard the divisor (volume == 0, empty list, etc.) "
     "before computing ratios."),

    # Type confusion
    ("AttributeError",
     r"'NoneType' object has no attribute '([A-Za-z_]+)'",
     "Tried to call .{match0} on None. The previous step probably returned "
     "None — check for an empty/failed API response before using its result."),
    ("TypeError",
     r"unsupported operand type\(s\) for [+\-*/]: 'NoneType' and",
     "Tried to do arithmetic with None. A previous step returned None — "
     "check for an empty/failed API response before using its result."),

    # Timeouts
    ("TimeoutError",
     r".*",
     "An operation timed out. Usually a slow API. Add a longer timeout "
     "and/or retry-with-backoff."),

    # Auth
    ("PermissionError",
     r"(401|403|unauthor|forbidden)",
     "API rejected your credentials. Check the Connections panel — keys "
     "may have expired or been revoked."),
]


def _find_hint(exc_type_name: str, exc_msg: str):
    """Return a friendly explanation string, or None if no rule matches."""
    for t, pat, tmpl in HINTS:
        if t != exc_type_name:
            continue
        try:
            m = re.search(pat, exc_msg, re.IGNORECASE)
        except re.error:
            continue
        if not m:
            continue
        try:
            captured = m.group(1) if m.lastindex else m.group(0)
            return tmpl.format(match0=captured)
        except Exception:
            return tmpl
    return None


def _find_user_frame(tb, user_script_path: str):
    """Walk the traceback from outermost to innermost; return the line number
    of the LAST frame whose filename ends with the user's script path.

    We pick the LAST (deepest) user-code frame so the line number points to
    where the user's code actually triggered the failure, not where it
    initially called into a library.
    """
    user = os.path.normpath(user_script_path).lower().replace("\\", "/")
    chosen_line = None
    cur = tb
    while cur is not None:
        fname = (cur.tb_frame.f_code.co_filename or "")
        fname_norm = os.path.normpath(fname).lower().replace("\\", "/")
        if fname_norm.endswith(user) or fname_norm == user:
            chosen_line = cur.tb_lineno
        cur = cur.tb_next
    return chosen_line


def _emit_friendly(exc: BaseException, user_script_path: str) -> None:
    """Print the structured [ERROR] line + raw [DEBUG] traceback lines."""
    exc_type_name = type(exc).__name__
    exc_msg = str(exc)
    line_no = _find_user_frame(exc.__traceback__, user_script_path)
    hint = _find_hint(exc_type_name, exc_msg)
    where = f" (line {line_no})" if line_no else ""
    summary = f"[ERROR] {exc_type_name}{where}: {exc_msg}"
    if hint:
        summary += "  —  " + hint
    print(summary, flush=True)

    # Raw traceback as [DEBUG] lines so the UI can collapse them by default
    # but still expose everything for power users.
    try:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for piece in tb_text.rstrip("\n").splitlines():
            if piece:
                print(f"[DEBUG] {piece}", flush=True)
    except Exception:
        # Last-ditch — never let the error reporter itself crash the runner.
        pass


def main():
    if len(sys.argv) < 2:
        print("[wd_runner] usage: wd_runner.py <bot_code.py>", flush=True)
        sys.exit(2)

    code_path = sys.argv[1]

    # ── 1. Install auto-logging hooks (best-effort) ──────────────────────
    # wd_autolog isn't available in user venvs (it lives in the bundled
    # backend's sdk/). The import failure is harmless — bots still run.
    try:
        import wd_autolog  # noqa: F401  — side-effect import installs hooks
    except Exception as e:
        print(f"[wd_runner] auto-log hooks unavailable in this environment: {e}",
              flush=True)

    # ── 2. Make sys.argv look as if the bot was launched directly ────────
    sys.argv = [code_path] + sys.argv[2:]

    # ── 3. Run the user's code inside a friendly-error wrapper ───────────
    import runpy
    try:
        runpy.run_path(code_path, run_name="__main__")
    except KeyboardInterrupt:
        # User clicked Stop. CTRL_BREAK on Windows raises KeyboardInterrupt
        # inside the script. Keep silent so the outer log just shows the
        # clean exit and the [WATCHDOG] Process exited with code 0 line.
        sys.exit(0)
    except SystemExit:
        # Let normal sys.exit() flow through with its own code untouched.
        raise
    except BaseException as exc:
        _emit_friendly(exc, code_path)
        sys.exit(1)


if __name__ == "__main__":
    main()
