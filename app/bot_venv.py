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