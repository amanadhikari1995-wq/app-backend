"""
wd_autolog.py — pre-imported by wd_runner.py before every bot's user code.

Installs lightweight monkey-patches on common HTTP and WebSocket libraries
so that bot activity is automatically captured into stdout (which WATCH-DOG
streams into the Live Logs panel). Bots get this for FREE — they don't have
to add any logging code.

Coverage:
  • requests              → every HTTP call + response/exception
  • httpx (sync + async)  → every HTTP call + response/exception
  • websocket-client      → every send/recv frame (truncated, 120 chars)

Bots can opt out by setting env var WATCHDOG_AUTOLOG=0 before running.
"""
import os
import time

# Allow opt-out — set this env var BEFORE the bot runs to disable hooks.
if os.environ.get("WATCHDOG_AUTOLOG", "1") == "0":
    pass
else:

    def _now():
        return time.strftime("%H:%M:%S", time.localtime())

    def _emit(line):
        # Always flush so the parent process (FastAPI) sees the line immediately.
        try:
            print(line, flush=True)
        except Exception:
            pass

    # Truncate verbose payloads so noisy WS streams don't drown the panel.
    def _short(obj, n=120):
        try:
            s = repr(obj)
        except Exception:
            s = "<unrepresentable>"
        return s if len(s) <= n else s[:n] + "…"

    # ── requests ──────────────────────────────────────────────────────────
    try:
        import requests.adapters
        _orig_send = requests.adapters.HTTPAdapter.send

        def _wd_requests_send(self, request, **kwargs):
            method = request.method
            url    = request.url
            _emit(f"{_now()} | INFO     | [HTTP →] {method} {url}")
            t0 = time.time()
            try:
                response = _orig_send(self, request, **kwargs)
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                _emit(f"{_now()} | ERROR    | [HTTP ✗] {method} {url} — {type(e).__name__}: {e}  ({ms}ms)")
                raise
            ms = int((time.time() - t0) * 1000)
            status = response.status_code
            tag = "[HTTP ←]" if status < 400 else "[HTTP ✗]"
            level = "INFO    " if status < 400 else "ERROR   "
            _emit(f"{_now()} | {level} | {tag} {status} {method} {url}  ({ms}ms)")
            return response

        requests.adapters.HTTPAdapter.send = _wd_requests_send
    except Exception:
        pass

    # ── httpx ─────────────────────────────────────────────────────────────
    try:
        import httpx
        _orig_httpx_send = httpx.Client.send

        def _wd_httpx_send(self, request, *args, **kwargs):
            method = request.method
            url    = str(request.url)
            _emit(f"{_now()} | INFO     | [HTTP →] {method} {url}")
            t0 = time.time()
            try:
                response = _orig_httpx_send(self, request, *args, **kwargs)
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                _emit(f"{_now()} | ERROR    | [HTTP ✗] {method} {url} — {type(e).__name__}: {e}  ({ms}ms)")
                raise
            ms = int((time.time() - t0) * 1000)
            status = response.status_code
            tag = "[HTTP ←]" if status < 400 else "[HTTP ✗]"
            level = "INFO    " if status < 400 else "ERROR   "
            _emit(f"{_now()} | {level} | {tag} {status} {method} {url}  ({ms}ms)")
            return response

        httpx.Client.send = _wd_httpx_send

        # Async client too
        try:
            _orig_async_send = httpx.AsyncClient.send

            async def _wd_httpx_async_send(self, request, *args, **kwargs):
                method = request.method
                url    = str(request.url)
                _emit(f"{_now()} | INFO     | [HTTP →] {method} {url}")
                t0 = time.time()
                try:
                    response = await _orig_async_send(self, request, *args, **kwargs)
                except Exception as e:
                    ms = int((time.time() - t0) * 1000)
                    _emit(f"{_now()} | ERROR    | [HTTP ✗] {method} {url} — {type(e).__name__}: {e}  ({ms}ms)")
                    raise
                ms = int((time.time() - t0) * 1000)
                status = response.status_code
                tag = "[HTTP ←]" if status < 400 else "[HTTP ✗]"
                level = "INFO    " if status < 400 else "ERROR   "
                _emit(f"{_now()} | {level} | {tag} {status} {method} {url}  ({ms}ms)")
                return response

            httpx.AsyncClient.send = _wd_httpx_async_send
        except Exception:
            pass
    except Exception:
        pass

    # ── websocket-client (the `websocket` package) ────────────────────────
    try:
        import websocket as _ws_mod
        if hasattr(_ws_mod, "WebSocket"):
            _orig_ws_send = _ws_mod.WebSocket.send
            _orig_ws_recv = _ws_mod.WebSocket.recv

            def _wd_ws_send(self, payload, *args, **kwargs):
                _emit(f"{_now()} | INFO     | [WS →] {_short(payload)}")
                return _orig_ws_send(self, payload, *args, **kwargs)

            def _wd_ws_recv(self, *args, **kwargs):
                msg = _orig_ws_recv(self, *args, **kwargs)
                _emit(f"{_now()} | INFO     | [WS ←] {_short(msg)}")
                return msg

            _ws_mod.WebSocket.send = _wd_ws_send
            _ws_mod.WebSocket.recv = _wd_ws_recv
    except Exception:
        pass

    # ── websockets (the asyncio-native `websockets` package) ──────────────
    # Hook the higher-level connect-context manager. Best-effort; the API
    # surface differs across versions, so we wrap defensively.
    try:
        import websockets.legacy.client as _wsl
        _orig_async_send = _wsl.WebSocketClientProtocol.send
        _orig_async_recv = _wsl.WebSocketClientProtocol.recv

        async def _wd_aws_send(self, message):
            _emit(f"{_now()} | INFO     | [WS →] {_short(message)}")
            return await _orig_async_send(self, message)

        async def _wd_aws_recv(self):
            msg = await _orig_async_recv(self)
            _emit(f"{_now()} | INFO     | [WS ←] {_short(msg)}")
            return msg

        _wsl.WebSocketClientProtocol.send = _wd_aws_send
        _wsl.WebSocketClientProtocol.recv = _wd_aws_recv
    except Exception:
        pass

    # ── Global uncaught-exception logger ─────────────────────────────────
    # Already covered for the most part by Python's default sys.excepthook
    # (which writes to stderr → captured into the bot log via stderr=STDOUT).
    # Augment to add our standard prefix so the dashboard color rules pick
    # it up as RED.
    import sys
    _orig_excepthook = sys.excepthook

    def _wd_excepthook(exc_type, exc_value, exc_tb):
        try:
            _emit(f"{_now()} | ERROR    | [ERROR] Uncaught {exc_type.__name__}: {exc_value}")
        finally:
            _orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _wd_excepthook
