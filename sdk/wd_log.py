"""
wd_log.py — explicit colored-log helpers for bots that want clean output.

Optional. Bots can `from wd_log import ok, warn, err, info, buy, sold, exit_`
and call any of these to emit a tagged line that the dashboard color-rules
pick up. This is for cases where the auto-instrumentation in wd_autolog.py
isn't enough — e.g. logging trade decisions or computed signals.

  ok("Order filled at 57c")        → green  in the Live Logs panel
  buy("YES x5 @ 57c")               → green  ([BUY] tag)
  sold("YES x5 @ 89c profit +$1.60") → green  ([SOLD] tag)
  exit_("stop_loss YES @ 50c")      → orange ([EXIT] tag)
  warn("Stop-loss approaching")     → orange
  err("API timeout")                → red    ([ERROR] tag)
  info("Just a status update")      → default

All output goes through the bot's stdout, which WATCH-DOG captures and
streams into the Live Logs panel. No setup required — just import and call.
"""
import time


def _stamp():
    return time.strftime("%H:%M:%S", time.localtime())


def info(msg: str) -> None:
    print(f"{_stamp()} | INFO     | {msg}", flush=True)


def ok(msg: str) -> None:
    """Generic success — green tint."""
    print(f"{_stamp()} | INFO     | [OK] {msg}", flush=True)


def buy(msg: str) -> None:
    """Trade entry — green."""
    print(f"{_stamp()} | INFO     | [BUY] {msg}", flush=True)


def sold(msg: str) -> None:
    """Trade exit with profit — green."""
    print(f"{_stamp()} | INFO     | [SOLD] {msg}", flush=True)


def filled(msg: str) -> None:
    """Order filled by exchange — green."""
    print(f"{_stamp()} | INFO     | [FILLED] {msg}", flush=True)


def exit_(msg: str) -> None:
    """Position close — orange (note: name is `exit_` because `exit` shadows builtin)."""
    print(f"{_stamp()} | INFO     | [EXIT] {msg}", flush=True)


def closed(msg: str) -> None:
    """Order/position closed — orange."""
    print(f"{_stamp()} | INFO     | [CLOSED] {msg}", flush=True)


def warn(msg: str) -> None:
    """Warning — orange."""
    print(f"{_stamp()} | WARN     | [WARN] {msg}", flush=True)


def err(msg: str) -> None:
    """Error — red."""
    print(f"{_stamp()} | ERROR    | [ERROR] {msg}", flush=True)


# Convenience aliases
error   = err
success = ok
