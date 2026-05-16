"""
bot_venv.py — per-bot uv-managed virtualenv lifecycle.

Each bot can declare a `requirements` text field on its Supabase row. When
the bot runs, the backend:
  1. Hashes the requirements string.
  2. Compares against the `requirements_hash` stored in Supabase.
  3. If unchanged AND the venv exists -> reuse it. (Fast path.)
  4. Otherwise: create/refresh the venv with uv, install requirements,
     stamp the new hash.

Bots without `requirements` keep using the bundled PyInstaller Python
(backwards compat — no venv created).

We use `uv` (https://docs.astral.sh/uv/) instead of stdlib pip because:
  - Installs are 5-10x faster (parallel resolver + global cache).
  - It manages its own Python interpreter (no need to bundle python-embed).
  - Single 10 MB binary, well-tested cross-platform.

uv.exe is bundled into the installer via electron-builder extraResources.
At runtime we resolve its path relative to the watchdog-backend.exe location.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("watchdog.bot_venv")

# Where bot venvs live. One subdir per bot UUID. Survives app upgrades
# unless the user wipes their data dir manually.
def _venvs_root() -> Path:
    base = os.environ.get("WATCHDOG_DATA_DIR")
    if not base:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            base = str(Path(base) / "WatchDog")
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            base = str(Path(base) / "WatchDog")
    p = Path(base) / "bot-venvs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find_uv() -> Optional[Path]:
    """Locate the bundled uv.exe.

    In a frozen PyInstaller bundle, uv.exe is shipped alongside the exe via
    electron-builder extraResources. The exact location depends on the
    Electron packaging layout:
        Program Files\\WatchDog\\resources\\backend\\watchdog-backend\\uv.exe
        Program Files\\WatchDog\\resources\\uv.exe
    Fall back to PATH for dev mode.
    """
    candidates: list[Path] = []
    exe_dir = Path(sys.executable).parent
    candidates.append(exe_dir / "uv.exe")
    candidates.append(exe_dir.parent / "uv.exe")
    # If the backend exe is in resources/backend/watchdog-backend/, uv may be
    # at resources/uv.exe (one extraResources sibling).
    if "resources" in exe_dir.parts:
        idx = exe_dir.parts.index("resources")
        resources_root = Path(*exe_dir.parts[: idx + 1])
        candidates.append(resources_root / "uv.exe")
        candidates.append(resources_root / "bin" / "uv.exe")
    # Dev fallback: PATH lookup
    path_uv = shutil.which("uv")
    if path_uv:
        candidates.append(Path(path_uv))

    for c in candidates:
        if c.exists() and c.is_file():
            log.debug("Using uv at: %s", c)
            return c
    log.warning("uv.exe not found in any of: %s", [str(c) for c in candidates])
    return None


# Mapping for the cases where the import name in code differs from the
# PyPI install name. Most packages ARE the same — we only list the
# differences here. Falls back to "install name == import name".
# Add common trading / data libs that don't follow the standard naming.
_IMPORT_TO_PYPI: dict[str, str] = {
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "binance": "python-binance",
    "discord": "discord.py",
    "telegram": "python-telegram-bot",
    "dotenv": "python-dotenv",
    "pandas_ta": "pandas-ta",
    "kalshi_python": "kalshi-python",
    "kalshi_python_sync": "kalshi-python-sync",
    "alpaca": "alpaca-py",
}

# Imports already provided by the bundled PyInstaller backend exe — no need
# to install these. If the bot ONLY uses these (plus stdlib), we skip the
# whole venv and run on bundled Python (fast path).
_BUNDLED: set[str] = {
    "httpx", "websockets", "ccxt", "cryptography", "requests", "urllib3",
    "anyio", "starlette", "fastapi", "uvicorn", "sqlalchemy", "pydantic",
    "jose", "passlib", "psutil", "apscheduler", "dotenv", "json", "asyncio",
    "langchain", "langchain_core", "langchain_community", "langchain_anthropic",
    "langgraph",
}


def _stdlib_modules() -> set:
    """Modules that ship with Python (no install needed). 3.10+ has
    sys.stdlib_module_names as the source of truth."""
    return set(getattr(sys, "stdlib_module_names", set()))


def detect_requirements_from_code(code: str) -> list[str]:
    """Parse bot code → return list of pip-install names that need a venv.

    Strategy: AST-walk the code, collect top-level imports, drop stdlib +
    already-bundled libs, map to PyPI names where they differ.

    Returns empty list if everything's stdlib/bundled (caller can skip venv).
    """
    if not code:
        return []
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # Bot code with a syntax error — let it run via bundled python and fail
        # there (the user wants to SEE the error, not have setup fail).
        return []

    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top: seen.add(top)
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (level > 0) — those are within the script
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                if top: seen.add(top)

    stdlib = _stdlib_modules()
    needs_install: list[str] = []
    for mod in seen:
        if mod in stdlib:    continue
        if mod in _BUNDLED:  continue
        if mod.startswith("_"):  continue
        # Map to PyPI name; if not in map, assume PyPI name == import name
        pypi = _IMPORT_TO_PYPI.get(mod, mod)
        needs_install.append(pypi)
    return sorted(set(needs_install))

def hash_requirements(requirements: str) -> str:
    """Stable hash of normalised requirements text. Whitespace + comments
    don't trigger a reinstall."""
    if not requirements:
        return ""
    cleaned: list[str] = []
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cleaned.append(line.lower())
    cleaned.sort()
    h = hashlib.sha256("\n".join(cleaned).encode("utf-8")).hexdigest()
    return h[:16]


def venv_python(bot_uuid: str) -> Path:
    """Path to the venv's python.exe for a given bot."""
    return _venvs_root() / bot_uuid / "Scripts" / "python.exe"


def _hash_marker(bot_uuid: str) -> Path:
    return _venvs_root() / bot_uuid / ".watchdog_req_hash"


def is_venv_valid(bot_uuid: str, requirements_hash: str) -> bool:
    """True if the venv exists AND its stored hash matches. Otherwise the
    caller must (re)install."""
    py = venv_python(bot_uuid)
    if not py.exists():
        return False
    marker = _hash_marker(bot_uuid)
    if not marker.exists():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == requirements_hash
    except Exception:
        return False


def _stream(proc: subprocess.Popen, log_callback) -> int:
    """Read stdout line-by-line, push each to log_callback. Return exit code."""
    if proc.stdout is None:
        return proc.wait()
    for raw in proc.stdout:
        line = raw.rstrip()
        if line:
            log_callback(f"[setup] {line}")
    return proc.wait()


def prepare_venv(
    bot_uuid: str,
    requirements: str,
    log_callback=None,
) -> Tuple[Optional[Path], Optional[str]]:
    """Ensure a usable venv exists for this bot.

    Returns (python_exe, error_message). If error is set, python_exe is None.
    On success, error is None.

    log_callback is an optional fn called with each [setup] log line so the
    caller can stream install progress into the BotLog table.
    """
    cb = log_callback or (lambda s: log.info("%s", s))

    if not requirements or not requirements.strip():
        # No requirements declared -> caller should fall back to bundled python.
        return (None, None)

    uv = _find_uv()
    if uv is None:
        return (None, "uv.exe not bundled; bot has requirements but no installer")

    req_hash = hash_requirements(requirements)
    venv_dir = _venvs_root() / bot_uuid

    if is_venv_valid(bot_uuid, req_hash):
        cb(f"reusing cached venv (hash {req_hash})")
        return (venv_python(bot_uuid), None)

    # Build/refresh venv
    cb(f"preparing venv for bot (hash {req_hash})...")

    # 1. Create the venv if missing.
    if not venv_python(bot_uuid).exists():
        cb("creating Python 3.12 venv (uv will download it on first use)...")
        try:
            r = subprocess.run(
                [str(uv), "venv", "--python", "3.12", str(venv_dir)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=180,
            )
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip()[-1500:]
                return (None, f"uv venv failed: {msg}")
        except Exception as e:
            return (None, f"uv venv exception: {e}")

    # 2. Write requirements to a temp file.
    req_file = venv_dir / ".requirements.txt"
    try:
        req_file.write_text(requirements, encoding="utf-8")
    except Exception as e:
        return (None, f"failed to write requirements file: {e}")

    # 3. uv pip install -r requirements.txt --python <venv>/Scripts/python.exe
    cb("installing dependencies (this may take a few seconds)...")
    try:
        proc = subprocess.Popen(
            [str(uv), "pip", "install", "-r", str(req_file),
             "--python", str(venv_python(bot_uuid))],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        rc = _stream(proc, cb)
        if rc != 0:
            return (None, f"uv pip install exited with code {rc} — check requirements")
    except Exception as e:
        return (None, f"uv pip install exception: {e}")

    # 4. Stamp the marker so we can skip install next time.
    try:
        _hash_marker(bot_uuid).write_text(req_hash, encoding="utf-8")
    except Exception as e:
        log.warning("Failed to write hash marker (non-fatal): %s", e)

    cb("venv ready")
    return (venv_python(bot_uuid), None)


def remove_venv(bot_uuid: str) -> None:
    """Delete a bot's venv. Called when the bot is deleted."""
    p = _venvs_root() / bot_uuid
    if p.exists():
        try:
            shutil.rmtree(p, ignore_errors=True)
            log.info("Removed venv for bot %s", bot_uuid)
        except Exception as e:
            log.warning("Failed to remove venv for %s: %s", bot_uuid, e)


# ─────────────────────────────────────────────────────────────────────────────
# Self-heal helpers (Phase 2.1)
# ─────────────────────────────────────────────────────────────────────────────
#
# When a bot crashes with `ModuleNotFoundError: No module named 'X'`, the
# runner can call install_one_into_venv(bot_uuid, 'X') and retry. This catches
# the cases the AST scan misses:
#   - importlib.import_module("ta") at runtime
#   - imports inside try/except or conditional branches
#   - transitive failures where the top-level import succeeds but a sub-import
#     of an unlisted package fails

# Pattern: `ModuleNotFoundError: No module named 'foo'`
# (also handles the variant `ImportError: No module named 'foo'` from older Pythons)
_MISSING_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError):\s*No module named\s*['\"]([A-Za-z0-9_.]+)['\"]",
    re.IGNORECASE,
)


def parse_missing_module(text: str) -> Optional[str]:
    """Scan subprocess output for `ModuleNotFoundError: No module named 'X'`.

    Returns the TOP-LEVEL module name (e.g. 'requests' from 'requests.adapters'),
    or None if no such error is present. Returns the LAST match in the text so
    that wrapper errors don't shadow the underlying cause.
    """
    if not text:
        return None
    matches = _MISSING_MODULE_RE.findall(text)
    if not matches:
        return None
    raw = matches[-1].split(".")[0]
    return raw or None


# Defensive: refuse to auto-install module names that look like typos or
# stdlib aliases. Keeps a careless `import req` from pulling an arbitrary
# package from PyPI. Conservative — anything that doesn't look like a real
# package name gets rejected.
_AUTO_INSTALL_DENYLIST: set[str] = {
    "req",          # almost always a typo for `requests`
    "pip", "setuptools", "wheel",  # never installable as user code
    "pandas_compat",  # historical name confusion
}


def _looks_installable(name: str) -> bool:
    """Conservative sanity check before pip-installing arbitrary user input."""
    if not name or len(name) < 2 or len(name) > 40:
        return False
    if name.startswith("_") or name in _AUTO_INSTALL_DENYLIST:
        return False
    # PyPI names are letters/digits/-/_/.; require it look that way.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_.-]*$", name):
        return False
    # Stdlib? Skip — venv install won't help.
    if name in _stdlib_modules():
        return False
    return True


def install_one_into_venv(
    bot_uuid: str,
    import_name: str,
    log_callback=None,
) -> Tuple[bool, Optional[str]]:
    """Install a SINGLE missing dependency into an existing bot venv.

    Returns (ok, error_message). On success, error_message is None.

    Uses the same _IMPORT_TO_PYPI mapping as the AST scanner so that e.g.
    `ModuleNotFoundError: 'cv2'` triggers `uv pip install opencv-python`.

    No-ops (returns ok=False) if the module name looks unsafe to auto-install.
    """
    cb = log_callback or (lambda s: log.info("%s", s))

    if not _looks_installable(import_name):
        return (False, f"refusing to auto-install suspicious name: {import_name!r}")

    py = venv_python(bot_uuid)
    if not py.exists():
        return (False, f"venv for bot {bot_uuid} does not exist; cannot install {import_name}")

    uv = _find_uv()
    if uv is None:
        return (False, "uv.exe not bundled; cannot install missing dependency")

    pypi = _IMPORT_TO_PYPI.get(import_name, import_name)
    cb(f"auto-installing missing dependency: {pypi} (import name: {import_name})")

    try:
        proc = subprocess.Popen(
            [str(uv), "pip", "install", pypi, "--python", str(py)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        rc = _stream(proc, cb)
        if rc != 0:
            return (False, f"uv pip install {pypi} exited with code {rc}")
    except Exception as e:
        return (False, f"uv pip install {pypi} exception: {e}")

    # Invalidate the hash marker so the next FULL prepare_venv() rebuilds with
    # the union of declared + auto-installed packages (instead of resetting
    # back to the user's declared list and dropping the auto-installs).
    try:
        marker = _hash_marker(bot_uuid)
        if marker.exists():
            marker.unlink()
    except Exception:
        pass

    cb(f"installed {pypi}; will retry")
    return (True, None)