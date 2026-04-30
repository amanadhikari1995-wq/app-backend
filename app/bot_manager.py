"""
bot_manager.py  —  Per-bot filesystem isolation for WATCH-DOG
=============================================================
Every bot gets its own self-contained folder under:

    C:/WATCH-DOG/bots/bot-{id:04d}-{slug}/

Folder layout
─────────────
    bot.json              ← identity, name, description, created_at, status
    code.py               ← bot Python code (always current)
    settings.json         ← schedule, risk limits, auto_restart
    connections/
        connections.json  ← all API connections for this bot (keys + metadata)
        .env              ← auto-built env file from connections.json
    logs/
        2025-04.jsonl     ← live log lines, one file per month (JSONL)
        2025-03.jsonl     ← previous month archive
    training_data/
        ticks/            ← AI tick data (JSONL files)
        trades/           ← completed trade records
        sessions/         ← session summaries

Design rules
────────────
• SQLite DB stays the primary store for fast API queries (no change to API).
• This folder is the isolated, portable, backup-friendly mirror of each bot.
• Every DB write is echoed here — the two stay in sync automatically.
• Deleting a bot deletes its folder entirely.
• No bot can see or touch another bot's folder.
• Each bot gets WATCHDOG_BOT_DIR injected as an env var at runtime so its
  own code can read/write to its private folder.
"""

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

# ── Root directory for all bot folders ───────────────────────────────────────
BOTS_ROOT = Path("C:/WATCH-DOG/app/backend/bots")

# Thread-safe log appending: one lock per log file path
_log_locks: dict[str, threading.Lock] = {}
_log_locks_mu = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    """
    'Kalshi BTC 15-min' → 'kalshi-btc-15-min'
    Safe for use as a directory name component.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _env_prefix(name: str) -> str:
    """'Kalshi API' → 'KALSHI_API'"""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _write_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _log_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _log_locks_mu:
        if key not in _log_locks:
            _log_locks[key] = threading.Lock()
        return _log_locks[key]


# ─────────────────────────────────────────────────────────────────────────────
class BotFS:
    """
    Manages the isolated filesystem for ONE bot.

    Usage
    -----
        bfs = BotFS(bot_id=1, name="Kalshi BTC 15min")
        bfs.create(code="...", description="...")   # first time
        bfs.sync_code("updated code...")            # on every save
        bfs.append_log("INFO", "[BOT] tick #42")   # while running
        bfs.sync_connections([...])                 # after key changes
        bfs.delete()                                # on bot deletion

    The instance is stateless (no in-memory caching) — safe to create
    a new BotFS per request or per operation.
    """

    def __init__(self, bot_id: int, name: str):
        self.bot_id = bot_id
        self.name   = name
        self.slug   = _slug(name)
        self.root   = BOTS_ROOT / f"bot-{bot_id:04d}-{self.slug}"

    # ── Sub-paths (computed, never cached) ───────────────────────────────────

    @property
    def bot_json(self)         -> Path: return self.root / "bot.json"
    @property
    def code_file(self)        -> Path: return self.root / "code.py"
    @property
    def settings_json(self)    -> Path: return self.root / "settings.json"
    @property
    def connections_dir(self)  -> Path: return self.root / "connections"
    @property
    def connections_json(self) -> Path: return self.connections_dir / "connections.json"
    @property
    def env_file(self)         -> Path: return self.connections_dir / ".env"
    @property
    def logs_dir(self)         -> Path: return self.root / "logs"
    @property
    def training_dir(self)     -> Path: return self.root / "training_data"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create(self, code: str = "", description: str = "") -> None:
        """
        Bootstrap the full folder structure for a newly created bot.
        Safe to call multiple times (mkdir exist_ok=True).
        """
        # Create all required directories
        for d in [
            self.root,
            self.connections_dir,
            self.logs_dir,
            self.training_dir / "ticks",
            self.training_dir / "trades",
            self.training_dir / "sessions",
        ]:
            d.mkdir(parents=True, exist_ok=True)

        # bot.json — immutable identity metadata
        _write_json(self.bot_json, {
            "id":          self.bot_id,
            "name":        self.name,
            "description": description,
            "created_at":  datetime.now(timezone.utc).isoformat(),
            "status":      "IDLE",
        })

        # settings.json — default values, updated on every settings save
        _write_json(self.settings_json, {
            "schedule_type":           "always",
            "schedule_start":          None,
            "schedule_end":            None,
            "max_amount_per_trade":    None,
            "max_contracts_per_trade": None,
            "max_daily_loss":          None,
            "auto_restart":            False,
        })

        # code.py — always reflects the latest saved code
        self.code_file.write_text(code, encoding="utf-8")

        # connections.json + .env — start empty
        _write_json(self.connections_json, [])
        self.env_file.write_text(
            f"# API keys for bot-{self.bot_id} ({self.name})\n"
            "# Auto-generated by WATCH-DOG — do not edit manually\n",
            encoding="utf-8",
        )

    def delete(self) -> None:
        """Permanently remove this bot's entire folder and all its data."""
        if self.root.exists():
            shutil.rmtree(self.root)

    def exists(self) -> bool:
        return self.root.exists()

    # ── Code ──────────────────────────────────────────────────────────────────

    def sync_code(self, code: str) -> None:
        """Keep code.py up to date on every save — skip write if content unchanged."""
        if not self.root.exists():
            return
        # Only write when the content actually differs so uvicorn's watchfiles
        # reloader does not detect a spurious change and kill running bot threads.
        try:
            existing = self.code_file.read_text(encoding="utf-8")
            if existing == code:
                return
        except (OSError, FileNotFoundError):
            pass
        self.code_file.write_text(code, encoding="utf-8")

    # ── Settings ──────────────────────────────────────────────────────────────

    def sync_settings(self, **fields) -> None:
        """
        Merge updated settings fields into settings.json.
        Only provided (non-None) keys are written; others are preserved.

        Example:
            bfs.sync_settings(auto_restart=True, max_daily_loss=500.0)
        """
        if not self.root.exists():
            return
        current: dict = {}
        if self.settings_json.exists():
            try:
                current = json.loads(self.settings_json.read_text(encoding="utf-8"))
            except Exception:
                pass
        for k, v in fields.items():
            if v is not None:
                current[k] = v
            elif k in current:
                current[k] = v   # explicit None → write None (e.g. clear a limit)
        _write_json(self.settings_json, current)

    def sync_status(self, status: str) -> None:
        """Reflect the bot's runtime status in bot.json."""
        if not self.bot_json.exists():
            return
        try:
            data = json.loads(self.bot_json.read_text(encoding="utf-8"))
            data["status"] = status
            _write_json(self.bot_json, data)
        except Exception:
            pass

    # ── API Connections ───────────────────────────────────────────────────────

    def sync_connections(self, conns: list) -> None:
        """
        Rewrite connections.json and rebuild .env from the current active
        connections list.  Call this after any add or delete in the DB.

        conns: list of dicts with keys: id, name, base_url, api_key, api_secret
        """
        if not self.root.exists():
            return
        self.connections_dir.mkdir(exist_ok=True)

        # Write connections.json (includes all fields for full portability)
        _write_json(self.connections_json, [
            {
                "id":         c["id"],
                "name":       c["name"],
                "base_url":   c.get("base_url"),
                "api_key":    c.get("api_key"),
                "api_secret": c.get("api_secret"),
            }
            for c in conns
        ])

        # Rebuild .env — one block per connection
        header = [
            f"# API keys for bot-{self.bot_id} ({self.name})\n",
            "# Auto-generated by WATCH-DOG — do not edit manually\n",
            f"# Updated: {datetime.now(timezone.utc).isoformat()}\n\n",
        ]
        blocks = []
        for c in conns:
            prefix = _env_prefix(c["name"])
            lines = [f"# {c['name']}\n"]
            if c.get("api_key"):
                lines.append(f"{prefix}_KEY={c['api_key']}\n")
                lines.append(f"{prefix}={c['api_key']}\n")      # bare prefix alias
            if c.get("api_secret"):
                lines.append(f"{prefix}_SECRET={c['api_secret']}\n")
            if c.get("base_url"):
                lines.append(f"{prefix}_URL={c['base_url']}\n")
            blocks.append("".join(lines))

        self.env_file.write_text(
            "".join(header) + "\n".join(blocks),
            encoding="utf-8",
        )

    # ── Logs ──────────────────────────────────────────────────────────────────

    def append_log(self, level: str, message: str) -> None:
        """
        Append one log entry to the current month's JSONL file.
        Thread-safe — multiple bot threads can log concurrently.

        File: logs/YYYY-MM.jsonl  (one file per calendar month)
        """
        if not self.root.exists():
            return
        self.logs_dir.mkdir(exist_ok=True)
        month_file = self.logs_dir / datetime.now().strftime("%Y-%m.jsonl")
        entry = json.dumps({
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   level,
            "message": message,
        })
        with _log_lock(month_file):
            with month_file.open("a", encoding="utf-8") as fh:
                fh.write(entry + "\n")

    def clear_logs(self) -> None:
        """Remove all log files for this bot (called from the clear-logs endpoint)."""
        if self.logs_dir.exists():
            for f in self.logs_dir.glob("*.jsonl"):
                try:
                    f.unlink()
                except OSError:
                    pass

    # ── Debug ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"BotFS(id={self.bot_id}, name={self.name!r}, root={self.root})"


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_bot(bot_id: int, name: str) -> BotFS:
    """
    Primary factory used by routers.

    Example:
        from ..bot_manager import get_bot
        bfs = get_bot(bot.id, bot.name)
        bfs.sync_code(new_code)
    """
    return BotFS(bot_id, name)


def list_bot_folders() -> list[Path]:
    """Return sorted list of all existing bot root folders (for diagnostics)."""
    if not BOTS_ROOT.exists():
        return []
    return sorted(
        p for p in BOTS_ROOT.iterdir()
        if p.is_dir() and p.name.startswith("bot-")
    )
