"""
routers/system_stats.py  —  Live system resource metrics + token usage
"""
import sys
import psutil
from fastapi import APIRouter
from ..token_tracker import snapshot as _token_snapshot

router = APIRouter(prefix="/api/system", tags=["system"])

# Prime the CPU counter once at import so first request returns a real value.
psutil.cpu_percent(interval=None)

_DISK_PATH = "C:\\" if sys.platform == "win32" else "/"


@router.get("/stats")
def get_system_stats():
    """Return CPU, RAM, disk, network I/O, and Anthropic token usage."""
    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage(_DISK_PATH)
    net  = psutil.net_io_counters()
    tok  = _token_snapshot()

    return {
        # System resources
        "cpu_percent":  round(cpu, 1),
        "ram_used":     mem.used,
        "ram_total":    mem.total,
        "ram_percent":  round(mem.percent, 1),
        "disk_used":    disk.used,
        "disk_total":   disk.total,
        "disk_percent": round(disk.percent, 1),
        # Network I/O (cumulative since OS boot)
        "net_sent":     net.bytes_sent,
        "net_recv":     net.bytes_recv,
        # Anthropic token counters (cumulative since last server start)
        **tok,
    }
