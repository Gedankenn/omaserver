#!/usr/bin/env python3
"""JSON snapshot of CPU, RAM, GPU, disk and temps. Runs on the server."""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time

DOCKER_TTL = 15.0
COUNTERS_MIN_AGE = 0.5
COUNTERS_MAX_AGE = 120.0


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or "/tmp"


def _host_tag() -> str:
    host = _read("/etc/hostname") or os.uname().nodename
    return "".join(c if c.isalnum() or c in ".-" else "_" for c in host) or "host"


def _boot_id() -> str:
    return _read("/proc/sys/kernel/random/boot_id")


def _cache_load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
    """Atomic write: never leaves a half-written file behind."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


_STATIC: dict | None = None


def _hw_cache_path() -> str:
    return os.path.join(_runtime_dir(), f"ohmystatus-{_host_tag()}.hwcache.json")


def _static_cache() -> dict:
    """Static hardware facts, valid until the next boot."""
    global _STATIC
    if _STATIC is None:
        boot = _boot_id()
        data = _cache_load(_hw_cache_path())
        if not boot or data.get("boot") != boot:
            data = {"boot": boot}
        _STATIC = data
    return _STATIC


def _cached(key: str, fn):
    data = _static_cache()
    if key in data:
        return data[key]
    value = fn()
    data[key] = value
    _write_json(_hw_cache_path(), data)
    return value


def _counters_path() -> str:
    return os.path.join(_runtime_dir(), f"ohmystatus-{_host_tag()}.counters.json")


def _counters_load() -> dict | None:
    data = _cache_load(_counters_path())
    if not data or data.get("boot") != _boot_id():
        return None
    return data


def _counters_save(idle: int, total: int, io: dict, energies: list, when: float) -> None:
    _write_json(
        _counters_path(),
        {
            "boot": _boot_id(),
            "t": round(when, 3),
            "idle": idle,
            "total": total,
            "io": {k: list(v) for k, v in io.items()},
            "energies": energies,
        },
    )


def _read_int(path: str) -> int | None:
    raw = _read(path)
    if not raw:
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _read_int_retry(path: str, attempts: int = 6) -> int | None:
    for i in range(attempts):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read().strip()
            return int(raw.split()[0])
        except OSError as exc:
            if getattr(exc, "errno", None) != 16 or i == attempts - 1:
                return None
            time.sleep(0.05)
        except ValueError:
            return None
    return None


def cpu_times() -> tuple[int, int]:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:8]]
            idle = parts[3] + parts[4]
            return idle, sum(parts)
    return 0, 0


def cpu_name() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            name = line.split(":", 1)[-1].strip()
            name = name.replace("(R)", "").replace("(TM)", "").replace("CPU ", "")
            return " ".join(name.split())
    return ""


def cpu_cores() -> int:
    n = 0
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("processor"):
            n += 1
    return n


_JUNK_DMI = {
    "",
    "default string",
    "to be filled by o.e.m.",
    "none",
    "not specified",
    "n/a",
    "unknown",
}
_DDR_RATES = (
    1600, 1866, 2133, 2400, 2666, 2800, 2933, 3000, 3200, 3466, 3600, 3800,
    4000, 4266, 4400, 4800, 5200, 5600, 6000, 6400, 7200, 8000,
)


def _clean_dmi(s: str) -> str:
    t = " ".join((s or "").split())
    if t.lower() in _JUNK_DMI:
        return ""
    return t


def board_name() -> str:
    vendor = _clean_dmi(_read("/sys/class/dmi/id/board_vendor"))
    name = _clean_dmi(_read("/sys/class/dmi/id/board_name"))
    if vendor and name:
        if name.lower().startswith(vendor.lower()):
            return name
        return f"{vendor} {name}"
    return name or vendor or _clean_dmi(_read("/sys/class/dmi/id/product_name"))


def _pci_short(slot: str) -> str:
    parts = slot.split(":")
    if len(parts) >= 3 and parts[0] == "0000":
        return ":".join(parts[1:])
    return slot


def gpu_market_name(uevent: str) -> str:
    slot = ""
    for line in uevent.splitlines():
        if line.startswith("PCI_SLOT_NAME="):
            slot = line.split("=", 1)[-1].strip()
    if not slot:
        return ""
    key = "gpu:" + slot
    cached = _static_cache().get(key)
    if isinstance(cached, str) and cached:
        return cached
    name = _gpu_market_lookup(slot)
    if name:
        _static_cache()[key] = name
        _write_json(_hw_cache_path(), _static_cache())
    return name


def _gpu_market_lookup(slot: str) -> str:
    try:
        proc = subprocess.run(
            ["lspci", "-s", _pci_short(slot), "-nn"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    line = (proc.stdout or "").strip()
    m = re.search(r"\[([^\[\]]+)\]\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]", line)
    if not m:
        return ""
    return m.group(1)[:56]


def _rate_from_text(s: str) -> int | None:
    found = [n for n in _DDR_RATES if re.search(rf"(?:^|\D){n}(?:\D|$)", s)]
    return max(found) if found else None


def ram_meta() -> tuple[str, int | None]:
    kind = ""
    rates: list[int] = []
    for drv in ("ee1004", "at24", "eeprom"):
        for path in glob.glob(f"/sys/bus/i2c/drivers/{drv}/*/eeprom"):
            try:
                with open(path, "rb") as fh:
                    spd = fh.read(512)
            except OSError:
                continue
            if len(spd) < 20:
                continue
            if spd[2] == 0x0C:
                kind = kind or "DDR4"
            elif spd[2] == 0x12:
                kind = kind or "DDR5"
            elif spd[2] == 0x0B:
                kind = kind or "DDR3"
            pn = ""
            if len(spd) >= 349:
                pn = bytes(spd[329:349]).decode("ascii", "replace")
            r = _rate_from_text(pn)
            if r:
                rates.append(r)
                continue
            if spd[2] == 0x0C and spd[11]:
                tck_ns = spd[18] * (spd[10] / spd[11])
                if tck_ns > 0:
                    mts = int(round(2000.0 / tck_ns))
                    rates.append(min(_DDR_RATES, key=lambda x: abs(x - mts)))
    return kind, (max(rates) if rates else None)


def mem() -> dict:
    total = avail = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    used = max(0, total - avail)
    pct = (used / total * 100.0) if total else 0.0
    kind, mt_s = _cached("ram", ram_meta)
    return {
        "percent": round(pct, 1),
        "used_gb": round(used / 1048576.0, 1),
        "total_gb": round(total / 1048576.0, 1),
        "kind": kind,
        "mt_s": mt_s,
    }


def _physical_disk_bytes() -> tuple[int, list[str]]:
    total = 0
    names: list[str] = []
    try:
        blocks = os.listdir("/sys/block")
    except OSError:
        return 0, []
    for name in sorted(blocks):
        if name.startswith(("loop", "ram", "zram", "sr", "md", "dm-")):
            continue
        sectors = _read_int(os.path.join("/sys/block", name, "size"))
        if not sectors:
            continue
        total += sectors * 512
        names.append(name)
    return total, names


def _unique_fs_used_bytes() -> tuple[int, list[str]]:
    skip_fs = {
        "tmpfs",
        "devtmpfs",
        "efivarfs",
        "proc",
        "sysfs",
        "cgroup",
        "cgroup2",
        "overlay",
        "squashfs",
        "autofs",
        "bpf",
        "tracefs",
        "debugfs",
        "securityfs",
        "pstore",
        "hugetlbfs",
        "mqueue",
        "configfs",
        "fusectl",
        "rpc_pipefs",
        "binfmt_misc",
        "devpts",
        "nsfs",
        "ramfs",
    }
    seen: set[str] = set()
    used = 0
    mounts: list[str] = []
    try:
        lines = open("/proc/mounts", encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return 0, []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        _src, target, fstype = parts[0], parts[1], parts[2]
        if fstype in skip_fs:
            continue
        if target.startswith(("/sys", "/proc", "/run", "/dev")):
            continue
        src_key = _src.split("[", 1)[0]
        if src_key in seen:
            continue
        try:
            st = os.statvfs(target)
        except OSError:
            continue
        seen.add(src_key)
        total_fs = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used += max(0, total_fs - free)
        mounts.append(target)
    return used, mounts


def disk(_path: str = "/") -> dict:
    total, names = _physical_disk_bytes()
    used, mounts = _unique_fs_used_bytes()
    if total <= 0:
        st = os.statvfs("/")
        total = st.f_frsize * st.f_blocks
        used = max(0, total - st.f_frsize * st.f_bavail)
        names, mounts = ["root"], ["/"]
    pct = (used / total * 100.0) if total else 0.0
    drives = disk_drives()
    temps = [d["temp"] for d in drives if d.get("temp") is not None]
    return {
        "percent": round(pct, 1),
        "used_gb": round(used / 1073741824.0, 1),
        "total_gb": round(total / 1073741824.0, 1),
        "path": "+".join(mounts) if mounts else "/",
        "disks": len(names),
        "mounts": mounts,
        "temp": max(temps) if temps else None,
        "drives": drives,
    }


def hwmon_by_name(name: str) -> str | None:
    key = "hwmon:" + name
    cached = _static_cache().get(key)
    if isinstance(cached, str) and _read(os.path.join(cached, "name")) == name:
        return cached
    for base in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if _read(os.path.join(base, "name")) == name:
            _static_cache()[key] = base
            _write_json(_hw_cache_path(), _static_cache())
            return base
    return None


def temp_c(hwmon: str | None, sensor: str = "temp1_input") -> int | None:
    if not hwmon:
        return None
    raw = _read_int_retry(os.path.join(hwmon, sensor))
    if raw is None:
        return None
    return int(raw / 1000)


def _sane_temp(v: int | None, lo: int = 1, hi: int = 125) -> int | None:
    if v is None or v < lo or v > hi:
        return None
    return v


def _disk_hwmon_temps() -> dict[str, int]:
    """hwmon device realpath -> Composite/temp1 °C for nvme and drivetemp."""
    out: dict[str, int] = {}
    for base in sorted(glob.glob("/sys/class/hwmon/hwmon[0-9]*")):
        name = _read(os.path.join(base, "name"))
        if name not in ("nvme", "drivetemp"):
            continue
        t = _sane_temp(temp_c(base), 1, 120)
        if t is None:
            continue
        out[os.path.realpath(os.path.join(base, "device"))] = t
    return out


def _udisks_disk_temps() -> dict[str, int]:
    """block name -> °C via UDisks2 SMART (Kelvin). Used when sysfs hwmon is missing."""
    out: dict[str, int] = {}
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except Exception:
        return out
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        proxy = Gio.DBusProxy.new_sync(
            bus,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.UDisks2",
            "/org/freedesktop/UDisks2",
            "org.freedesktop.DBus.ObjectManager",
            None,
        )
        packed = proxy.call_sync("GetManagedObjects", None, Gio.DBusCallFlags.NONE, 2000, None)
        objs = packed.unpack()[0]
    except Exception:
        return out
    kelvin: dict[str, float] = {}
    for path, ifaces in objs.items():
        ata = ifaces.get("org.freedesktop.UDisks2.Drive.Ata") or {}
        k = ata.get("SmartTemperature")
        if k:
            kelvin[path] = float(k)
    for _path, ifaces in objs.items():
        blk = ifaces.get("org.freedesktop.UDisks2.Block") or {}
        raw = blk.get("Device")
        if isinstance(raw, (bytes, bytearray, memoryview)):
            dev = bytes(raw).decode("utf-8", "replace").strip("\x00")
        elif isinstance(raw, (list, tuple)):
            try:
                dev = bytes(int(x) & 0xFF for x in raw).decode("utf-8", "replace").strip("\x00")
            except (TypeError, ValueError):
                continue
        else:
            dev = str(raw or "")
        if not dev.startswith("/dev/"):
            continue
        name = dev.rsplit("/", 1)[-1]
        if re.fullmatch(r"sd[a-z]+\d+", name) or re.fullmatch(r"vd[a-z]+\d+", name):
            continue
        if re.fullmatch(r"nvme\d+n\d+p\d+", name):
            continue
        drive = blk.get("Drive")
        k = kelvin.get(drive)
        if k is None:
            continue
        t = _sane_temp(int(round(k - 273.15)), 1, 120)
        if t is not None:
            out[name] = t
    return out


def disk_drives() -> list[dict]:
    """Physical disks with current temp. Skip empty card readers / 0-size nodes."""
    hwmap = _disk_hwmon_temps()
    udisks = _udisks_disk_temps()
    drives: list[dict] = []
    try:
        blocks = os.listdir("/sys/block")
    except OSError:
        return []
    for name in sorted(blocks):
        if name.startswith(("loop", "ram", "zram", "sr", "md", "dm-")):
            continue
        base = os.path.join("/sys/block", name)
        sectors = _read_int(os.path.join(base, "size")) or 0
        if sectors <= 0:
            continue
        model = " ".join(_read(os.path.join(base, "device", "model")).split())
        if not model:
            model = name
        dev = os.path.realpath(os.path.join(base, "device"))
        temp = hwmap.get(dev)
        if temp is None:
            for hwdev, t in hwmap.items():
                if dev.startswith(hwdev + os.sep) or hwdev.startswith(dev + os.sep):
                    temp = t
                    break
        if temp is None:
            temp = udisks.get(name)
        drives.append({"id": name, "model": model[:48], "temp": temp})
    return drives


def disk_temp() -> int | None:
    temps = [d["temp"] for d in disk_drives() if d.get("temp") is not None]
    return max(temps) if temps else None


def _uw_to_w(raw: int | None) -> float | None:
    if raw is None or raw < 0:
        return None
    return round(raw / 1_000_000.0, 1)


def _hwmon_power_w(hw: str | None) -> tuple[float | None, float | None]:
    if not hw:
        return None, None
    watts = None
    for sensor in ("power1_average", "power1_input"):
        watts = _uw_to_w(_read_int_retry(os.path.join(hw, sensor)))
        if watts is not None:
            break
    cap = _uw_to_w(_read_int(os.path.join(hw, "power1_cap")))
    return watts, cap


def rapl_packages() -> list[dict]:
    pkgs: list[dict] = []
    for base in sorted(glob.glob("/sys/class/powercap/*")):
        name = _read(os.path.join(base, "name"))
        if not name.startswith("package"):
            continue
        pkgs.append(
            {
                "path": base,
                "tdp_w": _uw_to_w(_read_int(os.path.join(base, "constraint_0_max_power_uw"))),
            }
        )
    return pkgs


def rapl_energy_uj(path: str) -> int | None:
    return _read_int(os.path.join(path, "energy_uj"))


def default_tdp() -> float | None:
    n = cpu_cores()
    if n >= 24:
        return 120.0
    if n >= 16:
        return 105.0
    if n >= 8:
        return 65.0
    if n >= 4:
        return 45.0
    return 35.0


def estimate_cpu_w(percent: float, tdp_w: float | None) -> float | None:
    if tdp_w is None or tdp_w <= 0:
        return None
    idle = min(tdp_w * 0.18, 30.0)
    busy = max(0.0, min(1.0, percent / 100.0))
    return round(idle + (tdp_w - idle) * busy, 1)


# idle, rw, sleep — datasheet-ish, not wall watts
_DRIVE_W = (
    (re.compile(r"SA400|KINGSTON SA", re.I), (0.4, 1.5, 0.1)),
    (re.compile(r"ST1000DM003", re.I), (5.4, 6.8, 0.8)),
    (re.compile(r"ST1000VM002", re.I), (3.7, 5.6, 0.8)),
    (re.compile(r"ST31000524NS", re.I), (6.2, 8.5, 1.0)),
    (re.compile(r"HTS5475", re.I), (0.85, 1.8, 0.2)),
    (re.compile(r"ST1000LM048", re.I), (0.7, 1.7, 0.2)),
    (re.compile(r"WD10JPVX", re.I), (0.65, 1.7, 0.2)),
    (re.compile(r"ST33000651NS", re.I), (7.0, 11.0, 1.0)),
    (re.compile(r"HD103SJ", re.I), (6.0, 6.5, 0.8)),
)
_25_RE = re.compile(
    r"HTS|LM0|JPVX|JPVT|BUP|MQ0|ST\d+LM|WD\d+JP|TRAVELSTAR|MOMENTUS",
    re.I,
)


def _diskstats() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for line in _read("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        name = parts[2]
        if re.fullmatch(r"sd[a-z]+", name) or re.fullmatch(r"nvme\d+n\d+", name):
            out[name] = (int(parts[3]), int(parts[7]))
    return out


def _drive_profile(name: str, model: str, rotational: bool) -> tuple[float, float, float]:
    for rx, watts in _DRIVE_W:
        if rx.search(model):
            return watts
    if not rotational:
        return (0.5, 1.6, 0.1)
    if _25_RE.search(model):
        return (1.5, 2.3, 0.2)
    sectors = _read_int(os.path.join("/sys/block", name, "size")) or 0
    if sectors * 512 >= 2_500_000_000_000:
        return (7.5, 10.5, 1.0)
    return (5.5, 7.5, 0.9)


def estimate_disks_w(io0: dict[str, tuple[int, int]], io1: dict[str, tuple[int, int]]) -> dict:
    total = 0.0
    cap = 0.0
    n = 0
    spinning = 0
    try:
        blocks = os.listdir("/sys/block")
    except OSError:
        return {"disk_w": None, "disk_cap_w": None, "disk_n": 0, "disk_spin": 0}
    for name in sorted(blocks):
        if name.startswith(("loop", "ram", "zram", "sr", "md", "dm-")):
            continue
        base = os.path.join("/sys/block", name)
        if not os.path.isdir(os.path.join(base, "device")):
            continue
        rot = (_read_int(os.path.join(base, "queue", "rotational")) or 0) == 1
        model = _read(os.path.join(base, "device", "model"))
        state = _read(os.path.join(base, "device", "state")).lower()
        runtime = _read(os.path.join(base, "device", "power", "runtime_status")).lower()
        idle_w, rw_w, sleep_w = _drive_profile(name, model, rot)
        cap += rw_w
        n += 1
        asleep = runtime == "suspended" or (state not in ("", "running"))
        a = io0.get(name, (0, 0))
        b = io1.get(name, a)
        busy = (b[0] - a[0]) + (b[1] - a[1]) > 0
        if asleep:
            total += sleep_w
        else:
            if rot:
                spinning += 1
            total += rw_w if busy else idle_w
    if n == 0:
        return {"disk_w": None, "disk_cap_w": None, "disk_n": 0, "disk_spin": 0}
    return {
        "disk_w": round(total, 1),
        "disk_cap_w": round(cap, 1),
        "disk_n": n,
        "disk_spin": spinning,
    }


def _wake_gpu() -> list[int]:
    fds: list[int] = []
    for path in sorted(glob.glob("/dev/dri/renderD*")):
        try:
            fds.append(os.open(path, os.O_RDWR))
        except OSError:
            continue
    return fds


def gpu_list() -> list[dict]:
    found: list[dict] = []
    fds = _wake_gpu()
    try:
        for entry in sorted(glob.glob("/sys/class/drm/card*/device")):
            name = os.path.basename(os.path.dirname(entry))
            if not name.startswith("card") or "-" in name:
                continue
            busy_path = os.path.join(entry, "gpu_busy_percent")
            if not os.path.isfile(busy_path):
                continue
            busy = _read_int_retry(busy_path) or 0
            used = _read_int_retry(os.path.join(entry, "mem_info_vram_used")) or 0
            total = _read_int_retry(os.path.join(entry, "mem_info_vram_total")) or 0
            vram_pct = (used / total * 100.0) if total else 0.0
            label = ""
            uevent = _read(os.path.join(entry, "uevent"))
            for line in uevent.splitlines():
                if line.startswith("DRIVER="):
                    label = line.split("=", 1)[-1]
            pretty = gpu_market_name(uevent)
            hw = None
            for h in glob.glob(os.path.join(entry, "hwmon/hwmon*")):
                hw = h
                break
            if not hw:
                hw = hwmon_by_name("amdgpu")
            gpu_w, gpu_cap = _hwmon_power_w(hw)
            found.append(
                {
                    "percent": int(max(0, min(100, busy))),
                    "temp": temp_c(hw),
                    "name": pretty or label or "gpu",
                    "vram_percent": round(vram_pct, 1),
                    "vram_used_mb": int(used / 1048576) if used else 0,
                    "vram_total_mb": int(total / 1048576) if total else 0,
                    "power_w": gpu_w,
                    "power_cap_w": gpu_cap,
                }
            )
        return found
    finally:
        for fd in fds:
            os.close(fd)


def gpus() -> list[dict]:
    """Every GPU exposed via DRM, biggest VRAM first."""
    return sorted(gpu_list(), key=lambda g: g.get("vram_total_mb") or 0, reverse=True)


def gpu() -> dict:
    found = gpus()
    if found:
        return found[0]
    return {
        "percent": 0,
        "temp": None,
        "name": "",
        "vram_percent": 0,
        "vram_used_mb": 0,
        "vram_total_mb": 0,
        "power_w": None,
        "power_cap_w": None,
    }


def bios_meta() -> dict:
    return {
        "version": _clean_dmi(_read("/sys/class/dmi/id/bios_version")),
        "date": _clean_dmi(_read("/sys/class/dmi/id/bios_date")),
    }


def uptime_s() -> int:
    raw = _read("/proc/uptime").split()
    try:
        return int(float(raw[0]))
    except (IndexError, ValueError):
        return 0


_WEB_PORTS = {
    80, 81, 443, 3000, 3001, 4173, 5000, 5055, 5173, 6767, 7878, 8000, 8001,
    8006, 8008, 8080, 8081, 8082, 8083, 8096, 8191, 8443, 8686, 8787, 8888, 8989, 9090, 9091,
    9443, 9696, 10443, 32400,
}


def _docker_web(ports: str) -> str:
    seen: set[int] = set()
    ranked: list[tuple[int, int]] = []
    for m in re.finditer(
        r"(?:0\.0\.0\.0|127\.0\.0\.1|\d+\.\d+\.\d+\.\d+|\[::+\]|::):(\d+)->(\d+)/tcp",
        ports or "",
    ):
        host_port = int(m.group(1))
        cont_port = int(m.group(2))
        if host_port in seen:
            continue
        seen.add(host_port)
        score = 2 if (host_port in _WEB_PORTS or cont_port in _WEB_PORTS) else 0
        if host_port in (80, 443) or cont_port in (80, 443):
            score = 3
        ranked.append((score, host_port))
    web = [p for s, p in sorted(ranked, key=lambda x: (-x[0], x[1])) if s >= 2]
    return ",".join(str(p) for p in web[:3])


def _docker_dot(state: str, health: str, status: str) -> str:
    st = (state or "").lower()
    h = (health or "").lower()
    s = (status or "").lower()
    if st in ("restarting", "paused", "dead") or "unhealthy" in s or h == "unhealthy":
        return "problem"
    if h == "starting" or "health: starting" in s:
        return "problem"
    if st == "running":
        return "ok"
    return "off"


def _dockers_probe() -> dict:
    empty = {"available": False, "items": []}
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return empty
    if proc.returncode != 0:
        return empty
    items = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(raw.get("Names") or raw.get("Name") or "")[:48]
        if not name:
            continue
        state = str(raw.get("State") or "")
        status = str(raw.get("Status") or "")[:48]
        health = str(raw.get("HealthStatus") or raw.get("Health") or "")
        items.append(
            {
                "name": name,
                "state": state,
                "status": status,
                "health": health,
                "dot": _docker_dot(state, health, status),
                "web": _docker_web(str(raw.get("Ports") or "")),
            }
        )
    rank = {"problem": 0, "ok": 1, "off": 2}
    items.sort(key=lambda c: (rank.get(c["dot"], 9), c["name"].lower()))
    return {"available": True, "items": items[:32]}


def dockers() -> dict:
    """Docker state is not a 5 s signal; keep a short TTL cache."""
    path = os.path.join(_runtime_dir(), f"ohmystatus-{_host_tag()}.docker.json")
    cached = _cache_load(path)
    age = time.time() - float(cached.get("t") or 0)
    data = cached.get("data")
    if isinstance(data, dict) and 0.0 <= age < DOCKER_TTL:
        return data
    data = _dockers_probe()
    _write_json(path, {"t": round(time.time(), 3), "data": data})
    return data


def _cpu_power(sample_s: float = 0.12) -> dict:
    pkgs = rapl_packages()
    tdp_w = 0.0
    for p in pkgs:
        if p["tdp_w"]:
            tdp_w += p["tdp_w"]
    tdp = tdp_w if tdp_w > 0 else None
    if tdp is None:
        tdp = default_tdp()

    idle1, total1 = cpu_times()
    io1 = _diskstats()
    e1 = [rapl_energy_uj(p["path"]) for p in pkgs]
    t1 = time.time()

    prev = _counters_load()
    wall = (t1 - float(prev.get("t") or 0)) if prev else -1.0
    if not prev or wall < COUNTERS_MIN_AGE or wall > COUNTERS_MAX_AGE:
        # Cold start or clock jump: take one short in-process sample.
        time.sleep(sample_s)
        idle0, total0, io0, e0 = idle1, total1, io1, e1
        t0 = t1
        idle1, total1 = cpu_times()
        io1 = _diskstats()
        e1 = [rapl_energy_uj(p["path"]) for p in pkgs]
        wall = max(time.time() - t0, 1e-6)
    else:
        idle0 = int(prev.get("idle") or 0)
        total0 = int(prev.get("total") or 0)
        io0 = {k: tuple(v) for k, v in (prev.get("io") or {}).items()}
        e0 = prev.get("energies") or []

    dt = total1 - total0
    di = idle1 - idle0
    cpu_pct = 0.0 if dt <= 0 else max(0.0, min(100.0, (1.0 - di / dt) * 100.0))
    cpu_w = None
    source = "est"
    if (
        pkgs
        and wall > 0.05
        and len(e0) == len(e1)
        and all(e is not None for e in list(e0) + list(e1))
    ):
        uj = 0
        for p, a, b in zip(pkgs, e0, e1):
            delta = b - a
            if delta < 0:
                rng = _read_int(os.path.join(p["path"], "max_energy_range_uj")) or 0
                delta += rng
            uj += max(0, delta)
        cpu_w = round(uj / wall / 1_000_000.0, 1)
        source = "rapl"
    if cpu_w is None:
        cpu_w = estimate_cpu_w(cpu_pct, tdp)
        source = "est"
    disks = estimate_disks_w(io0, io1)
    _counters_save(idle1, total1, io1, e1, time.time())
    return {
        "percent": cpu_pct,
        "cpu_w": cpu_w,
        "tdp_w": tdp,
        "cpu_source": source,
        **disks,
    }


def snapshot() -> dict:
    sampled = _cpu_power()
    cpu_pct = sampled["percent"]
    cpu_temp = temp_c(hwmon_by_name("coretemp"))
    if cpu_temp is None:
        cpu_temp = temp_c(hwmon_by_name("k10temp"))
    host = _read("/etc/hostname") or os.uname().nodename
    all_gpus = gpus()
    g = all_gpus[0] if all_gpus else gpu()
    gpu_w = g.get("power_w")
    disk_w = sampled.get("disk_w")
    parts = [w for w in (sampled["cpu_w"], gpu_w, disk_w) if w is not None]
    cap = 0.0
    if sampled["tdp_w"]:
        cap += sampled["tdp_w"]
    if g.get("power_cap_w"):
        cap += g["power_cap_w"]
    if sampled.get("disk_cap_w"):
        cap += sampled["disk_cap_w"]
    return {
        "ok": True,
        "reachable": True,
        "host": host,
        "cpu": {
            "percent": round(cpu_pct, 1),
            "temp": cpu_temp,
            "name": _cached("cpu_name", cpu_name),
            "cores": _cached("cpu_cores", cpu_cores),
        },
        "mem": mem(),
        "board": _cached("board", board_name),
        "bios": _cached("bios", bios_meta),
        "kernel": os.uname().release,
        "uptime_s": uptime_s(),
        "disk": disk("/"),
        "gpu": g,
        "gpus": all_gpus,
        "power": {
            "watts": round(sum(parts), 1) if parts else None,
            "cpu_w": sampled["cpu_w"],
            "gpu_w": gpu_w,
            "disk_w": disk_w,
            "disk_n": sampled.get("disk_n") or 0,
            "disk_spin": sampled.get("disk_spin") or 0,
            "cap_w": round(cap, 1) if cap else None,
            "cpu_source": sampled["cpu_source"],
        },
        "docker": dockers(),
        "boot_id": _read("/proc/sys/kernel/random/boot_id"),
        "error": "",
    }


def main() -> None:
    json.dump(snapshot(), sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
