import os

# Everything here reads directly from /proc and /sys. Docker doesn't
# namespace either by default (no --pid=host needed), so a plain container
# on TrueNAS SCALE normally sees the host's real memory/thermal/network
# figures - which is what's actually useful for a home-server admin page.
# Every reader is defensive: some hosts/kernels don't expose a given file
# (e.g. no thermal zone in a VM), and that should show as "unavailable"
# rather than break the admin page.


def format_bytes(n):
    if n is None:
        return "—"
    n = float(n)
    if n < 1024:
        return f"{int(n)} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        n /= 1024
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} ТБ"


def _read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if not parts:
                    continue
                try:
                    info[key] = int(parts[0]) * 1024  # values are in kB
                except ValueError:
                    continue
    except OSError:
        return None
    return info or None


def get_memory_stats():
    info = _read_meminfo()
    if not info:
        return None
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    cached = (info.get("Buffers") or 0) + (info.get("Cached") or 0)
    used = (total - available) if (total is not None and available is not None) else None
    return {"total": total, "available": available, "used": used, "cached": cached}


def get_cpu_temperature():
    """Returns the highest reading across thermal zones (°C), or None if
    /sys/class/thermal isn't exposed to this container."""
    base = "/sys/class/thermal"
    try:
        zones = os.listdir(base)
    except OSError:
        return None
    readings = []
    for zone in zones:
        try:
            with open(os.path.join(base, zone, "temp")) as f:
                raw = int(f.read().strip())
            readings.append(raw / 1000.0)
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


def get_network_stats():
    """Cumulative rx/tx bytes since container start, summed across all
    non-loopback interfaces. Obelisk is the only process in this container,
    so this is effectively the service's own network usage."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    rx_total = 0
    tx_total = 0
    found = False
    for line in lines:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
            found = True
        except ValueError:
            continue
    return {"rx_bytes": rx_total, "tx_bytes": tx_total} if found else None


def get_cpu_count():
    return os.cpu_count()


def get_load_average():
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None
