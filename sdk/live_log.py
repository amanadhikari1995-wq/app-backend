"""
WATCH-DOG  live_log.py
─────────────────────────────────────────────────────────────
Smart live logger — auto-detects message type, prints in color.
Available in every bot automatically (no install needed).

Usage
-----
    from live_log import live_log

    live_log("Kalshi API Connected")          # [CONNECTED] ✅ green
    live_log("Claude AI Connected")           # [CONNECTED] ✅ green
    live_log("LONG BTC @ $45,200")            # [SIGNAL] ⚡ yellow
    live_log("Trade Exited  PnL: +$38.50")    # [EXIT] 🔴 red
    live_log("BTC price $45,123.00")          # [PRICE] cyan
    live_log("AI: RSI=67 bullish")            # [AI] purple
    live_log("Bought 25 YES @ 72c")           # [BUY] green
    live_log("Sold 25 YES  PnL: +$61.50")     # [SELL] green/red
    live_log("PnL: -$12.00")                  # [PNL] red
"""

import re
import sys

# ── ANSI colour codes ─────────────────────────────────────────────────────────
_G  = '\033[92m'   # bright green
_Y  = '\033[93m'   # bright yellow
_R  = '\033[91m'   # bright red
_C  = '\033[96m'   # bright cyan
_P  = '\033[95m'   # purple/violet
_DG = '\033[32m'   # dark green  (PnL positive)
_DR = '\033[31m'   # dark red    (PnL negative)
_B  = '\033[1m'    # bold
_X  = '\033[0m'    # reset all


def live_log(message: str) -> None:
    """
    Print a log line with automatic colour + [TAG] prefix based on content.
    [TAG] is always the FIRST token so the WATCH-DOG log viewer can parse it.
    Always flushes immediately — never clears the screen.
    """
    m = message.lower()

    # ── [CONNECTED] ✅  Any "... Connected" message → green ──────────────────
    if 'connected' in m:
        _emit(f'{_G}{_B}[CONNECTED]{_X}{_G} ✅ {message}{_X}')

    # ── [SIGNAL] ⚡  LONG ─────────────────────────────────────────────────────
    elif re.search(r'\blong\b', m):
        _emit(f'{_Y}{_B}[SIGNAL]{_X}{_Y} ⚡ LONG — {message}{_X}')

    # ── [SIGNAL] ⚡  SHORT ────────────────────────────────────────────────────
    elif re.search(r'\bshort\b', m):
        _emit(f'{_Y}{_B}[SIGNAL]{_X}{_Y} ⚡ SHORT — {message}{_X}')

    # ── [EXIT] 🔴  Trade exit ─────────────────────────────────────────────────
    elif ('trade exited' in m
          or re.search(r'\bexit(ed)?\b.{0,20}(trade|position)', m)
          or re.search(r'(trade|position).{0,20}\bexit(ed)?\b', m)):
        _emit(f'{_R}{_B}[EXIT]{_X}{_R} 🔴 {message}{_X}')

    # ── [BUY] 🟢  Buy / entry ─────────────────────────────────────────────────
    elif re.search(r'\bbuy\b|\bbought\b|\benter(ed)?\b|\bentry\b', m):
        _emit(f'{_G}[BUY] 🟢 {message}{_X}')

    # ── [SELL] 📈/📉  Sell ────────────────────────────────────────────────────
    elif re.search(r'\bsell\b|\bsold\b|\bclos(e|ed)\b', m):
        neg = bool(re.search(r'\bpnl\b.{0,10}-|\bloss\b|-\$', m))
        col = _R if neg else _G
        ico = '📉' if neg else '📈'
        _emit(f'{col}[SELL] {ico} {message}{_X}')

    # ── [PNL] 💰  PnL line ────────────────────────────────────────────────────
    elif re.search(r'\bpnl\b|\bprofit\b|\bnet\b.{0,8}\$', m):
        neg = bool(re.search(r'-\$|loss', m))
        col = _DR if neg else _DG
        _emit(f'{col}[PNL] 💰 {message}{_X}')

    # ── [PRICE] 💲  Price feed ────────────────────────────────────────────────
    elif re.search(r'\$[\d,]+\.?\d*|\bprice\b|\bask\b|\bbid\b|\bspread\b', m):
        _emit(f'{_C}[PRICE] {message}{_X}')

    # ── [AI] 🤖  AI / analysis ───────────────────────────────────────────────
    elif re.search(r'\bai\b|\bclaude\b|\bgpt\b|\banalys|\brsi\b|\bmacd\b'
                   r'|\bmomentum\b|\bindicator\b|\bsentiment\b|\bimbalance\b', m):
        _emit(f'{_P}[AI] 🤖 {message}{_X}')

    # ── [ERROR] ❌  Error ─────────────────────────────────────────────────────
    elif re.search(r'\berror\b|\bfailed\b|\bexception\b|\bfatal\b|\btraceback\b', m):
        _emit(f'{_R}[ERROR] ❌ {message}{_X}')

    # ── [WARNING] ⚠️  Warning ─────────────────────────────────────────────────
    elif re.search(r'\bwarn(ing)?\b|\bcaution\b|\blimit reached\b', m):
        _emit(f'{_Y}[WARNING] ⚠️  {message}{_X}')

    # ── [INFO]  Default ──────────────────────────────────────────────────────
    else:
        _emit(f'[INFO] {message}')


def _emit(text: str) -> None:
    """Print with immediate flush so the watchdog runner captures it live."""
    print(text, flush=True)
