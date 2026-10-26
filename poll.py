#!/usr/bin/env python3
"""One-shot JSON snapshot of omaserver via SSH."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

STATS = Path(__file__).with_name("host-stats.py")
TIMEOUT = 8
HIST_HOURS = 1.0
HIST_MAX = 8640  # safety cap only; age window is the real limit
HIST_DRAW = 120
GPU_MAX = 6
DISK_MAX = 16


def empty_hist() -> dict:
    return {
        "cpu": [],
        "mem": [],
        "disk": [],
        "pwr": [],
        "gpu": [],
        "vram": [],
        "cpu_t": [],
        "mem_t": [],
        "disk_t": [],
        "disk_ids": [],
        "gpu_t": [],
        "vram_t": [],
    }


def fail(host: str, err: str) -> dict:
    return {
        "ok": False,
        "reachable": False,
        "host": host,
        "cpu": {"percent": 0, "temp": None, "name": "", "cores": 0},
        "mem": {"percent": 0, "used_gb": 0, "total_gb": 0},
        "disk": {"percent": 0, "used_gb": 0, "total_gb": 0, "path": "/", "drives": []},
        "docker": {"available": False, "items": []},
        "gpu": {
            "percent": 0,
            "temp": None,
            "name": "",
            "vram_percent": 0,
            "vram_used_mb": 0,
            "vram_total_mb": 0,
            "power_w": None,
            "power_cap_w": None,
        },
        "power": {
            "watts": None,
            "cpu_w": None,
            "gpu_w": None,
            "disk_w": None,
            "disk_n": 0,
            "disk_spin": 0,
            "cap_w": None,
            "cpu_source": "",
        },
        "hist": empty_hist(),
        "error": err[:180],
    }


def write_json_atomic(path: Path, data) -> None:
    """Write via tmp + rename so a reader never sees a partial file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def ssh_control_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    directory = os.path.join(runtime, "omaserver-ssh")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        directory = "/tmp"
    return os.path.join(directory, "%C")


def hist_path(host: str) -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
    return Path(runtime) / f"sabinho.omaserver.{safe}.hist.json"


def load_hist(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"boot_id": "", "s": []}
    if not isinstance(data, dict):
        return {"boot_id": "", "s": []}
    samples = data.get("s")
    if not isinstance(samples, list):
        samples = []
    return {"boot_id": str(data.get("boot_id") or ""), "s": samples}


def _opt_f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _peak_opt(vals) -> float | None:
    nums = [v for v in vals if v is not None]
    return max(nums) if nums else None


def disk_pairs(sample: dict) -> list[tuple[str, float | None]]:
    raw = sample.get("d")
    if isinstance(raw, list) and raw:
        out: list[tuple[str, float | None]] = []
        for item in raw[:DISK_MAX]:
            try:
                if isinstance(item, (list, tuple)) and item:
                    did = str(item[0] or "")
                    if not did:
                        continue
                    temp = _opt_f(item[1]) if len(item) > 1 else None
                    out.append((did, temp))
            except (TypeError, ValueError):
                continue
        if out:
            return out
    t = _opt_f(sample.get("dt"))
    return [("disk", t)] if t is not None else []


def gpu_pairs(sample: dict) -> list[tuple[float, float, float | None, float | None]]:
    """Per-GPU (usage, vram_pct, gpu_temp, mem_temp) for one sample."""
    raw = sample.get("g")
    if not isinstance(raw, list):
        try:
            return [
                (
                    float(sample.get("gpu") or 0),
                    float(sample.get("vram") or 0),
                    None,
                    None,
                )
            ]
        except (TypeError, ValueError):
            return []
    pairs: list[tuple[float, float, float | None, float | None]] = []
    for item in raw[:GPU_MAX]:
        try:
            if isinstance(item, (list, tuple)):
                util = float(item[0] or 0)
                vram = float(item[1] or 0) if len(item) > 1 else 0.0
                gt = _opt_f(item[2]) if len(item) > 2 else None
                mt = _opt_f(item[3]) if len(item) > 3 else None
                pairs.append((util, vram, gt, mt))
            else:
                pairs.append((float(item or 0), 0.0, None, None))
        except (TypeError, ValueError):
            pairs.append((0.0, 0.0, None, None))
    return pairs


def downsample(samples: list, n: int) -> dict:
    if not samples:
        return empty_hist()
    if len(samples) <= n:
        pick = samples
    else:
        pick = []
        size = len(samples) / n
        for i in range(n):
            lo = int(i * size)
            hi = max(lo + 1, int((i + 1) * size))
            chunk = samples[lo:hi]
            pairs = [gpu_pairs(x) for x in chunk]
            width = max((len(p) for p in pairs), default=0)
            gpu_peak = []
            for gi in range(width):
                col = [p[gi] for p in pairs if gi < len(p)]
                gpu_peak.append(
                    [
                        max(c[0] for c in col),
                        max(c[1] for c in col),
                        _peak_opt(c[2] for c in col),
                        _peak_opt(c[3] for c in col),
                    ]
                    if col
                    else [0.0, 0.0, None, None]
                )
            dpeak: dict[str, float] = {}
            for x in chunk:
                for did, t in disk_pairs(x):
                    if t is None:
                        continue
                    if did not in dpeak or t > dpeak[did]:
                        dpeak[did] = t
            pick.append(
                {
                    "cpu": max(float(x.get("cpu") or 0) for x in chunk),
                    "mem": max(float(x.get("mem") or 0) for x in chunk),
                    "disk": max(float(x.get("disk") or 0) for x in chunk),
                    "pwr": max(float(x.get("pwr") or 0) for x in chunk),
                    "ct": _peak_opt(_opt_f(x.get("ct")) for x in chunk),
                    "mt": _peak_opt(_opt_f(x.get("mt")) for x in chunk),
                    "d": [[did, dpeak[did]] for did in dpeak],
                    "dt": max(dpeak.values()) if dpeak else None,
                    "g": gpu_peak,
                }
            )
    ngpu = max((len(gpu_pairs(x)) for x in pick), default=0)
    gpu_series: list[list[float]] = []
    vram_series: list[list[float]] = []
    gpu_t_series: list[list[float | None]] = []
    vram_t_series: list[list[float | None]] = []
    for gi in range(ngpu):
        util: list[float] = []
        vram: list[float] = []
        gpu_t: list[float | None] = []
        mem_t: list[float | None] = []
        for sample in pick:
            pairs = gpu_pairs(sample)
            if gi < len(pairs):
                util.append(round(pairs[gi][0], 1))
                vram.append(round(pairs[gi][1], 1))
                gt, mt = pairs[gi][2], pairs[gi][3]
                gpu_t.append(None if gt is None else round(gt, 1))
                mem_t.append(None if mt is None else round(mt, 1))
        gpu_series.append(util)
        vram_series.append(vram)
        gpu_t_series.append(gpu_t)
        vram_t_series.append(mem_t)
    disk_ids: list[str] = []
    seen_disk: set[str] = set()
    for sample in pick:
        for did, _t in disk_pairs(sample):
            if did not in seen_disk:
                seen_disk.add(did)
                disk_ids.append(did)
    disk_t_series: list[list[float | None]] = []
    for did in disk_ids:
        col: list[float | None] = []
        for sample in pick:
            mapped = {k: v for k, v in disk_pairs(sample)}
            v = mapped.get(did)
            col.append(None if v is None else round(v, 1))
        disk_t_series.append(col)

    def series_opt(key: str) -> list[float | None]:
        out: list[float | None] = []
        for x in pick:
            v = _opt_f(x.get(key))
            out.append(None if v is None else round(v, 1))
        return out

    return {
        "cpu": [round(float(x.get("cpu") or 0), 1) for x in pick],
        "mem": [round(float(x.get("mem") or 0), 1) for x in pick],
        "disk": [round(float(x.get("disk") or 0), 1) for x in pick],
        "pwr": [round(float(x.get("pwr") or 0), 1) for x in pick],
        "gpu": gpu_series,
        "vram": vram_series,
        "cpu_t": series_opt("ct"),
        "mem_t": series_opt("mt"),
        "disk_t": disk_t_series,
        "disk_ids": disk_ids,
        "gpu_t": gpu_t_series,
        "vram_t": vram_t_series,
    }


def prune(samples: list, now: float) -> list:
    """Keep only samples inside the retention window (newest last)."""
    cutoff = now - HIST_HOURS * 3600.0
    kept = []
    for s in samples:
        try:
            ts = float(s.get("t") or 0)
        except (TypeError, ValueError):
            continue
        # Legacy samples (no timestamp) and future/clock-skewed ones are dropped.
        if cutoff <= ts <= now + 300.0:
            kept.append(s)
    if len(kept) > HIST_MAX:
        kept = kept[-HIST_MAX:]
    return kept


def record(data: dict, host: str) -> dict:
    if not data.get("reachable"):
        data["hist"] = empty_hist()
        return data
    path = hist_path(host)
    hist = load_hist(path)
    boot = str(data.get("boot_id") or "")
    if boot and hist["boot_id"] and hist["boot_id"] != boot:
        hist = {"boot_id": boot, "s": []}
    elif boot:
        hist["boot_id"] = boot
    now = time.time()
    cards = data.get("gpus")
    if not isinstance(cards, list) or not cards:
        cards = [data.get("gpu") or {}]
    pairs = []
    for card in cards[:GPU_MAX]:
        try:
            pairs.append(
                [
                    float((card or {}).get("percent") or 0),
                    float((card or {}).get("vram_percent") or 0),
                    _opt_f((card or {}).get("temp")),
                    _opt_f((card or {}).get("temp_mem")),
                ]
            )
        except (TypeError, ValueError):
            pairs.append([0.0, 0.0, None, None])
    disk = data.get("disk") or {}
    drives = disk.get("drives") if isinstance(disk.get("drives"), list) else []
    d_pairs = []
    for drive in drives[:DISK_MAX]:
        drive = drive or {}
        d_pairs.append([str(drive.get("id") or ""), _opt_f(drive.get("temp"))])
    cpu = data.get("cpu") or {}
    mem = data.get("mem") or {}
    hist["s"].append(
        {
            "t": round(now, 3),
            "cpu": float(cpu.get("percent") or 0),
            "mem": float(mem.get("percent") or 0),
            "disk": float(disk.get("percent") or 0),
            "pwr": float((data.get("power") or {}).get("watts") or 0),
            "ct": _opt_f(cpu.get("temp")),
            "mt": _opt_f(mem.get("temp")),
            "dt": _opt_f(disk.get("temp")),
            "d": d_pairs,
            "g": pairs,
        }
    )
    hist["s"] = prune(hist["s"], now)
    write_json_atomic(path, hist)
    data["hist"] = downsample(hist["s"], HIST_DRAW)
    return data



def snapshot(user: str, host: str) -> dict:
    script = STATS.read_text(encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={ssh_control_path()}",
                "-o",
                "ControlPersist=120",
                "-o",
                "Compression=no",
                "-o",
                "LogLevel=ERROR",
                f"{user}@{host}",
                "python3",
                "-",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return fail(host, "timeout")
    except OSError as exc:
        return fail(host, str(exc))
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"ssh {proc.returncode}").strip()
        return fail(host, err)
    raw = (proc.stdout or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return fail(host, raw[:180] or "bad json")
    if not isinstance(data, dict):
        return fail(host, "bad json")
    data["reachable"] = True
    data["ok"] = True
    return data


def main() -> None:
    host = "omaserver"
    user = "server"
    args = sys.argv[1:]
    if args:
        host = args[0]
    if len(args) >= 2:
        user = args[1]
    json.dump(record(snapshot(user, host), host), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
