"""System information queries: generations, hostname, kernel, uptime, store space."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config_files
from .config import find_config_root


def _read_icedos_config_path() -> Path | None:
    """Read the icedos-config path from .state/flake.lock.

    Returns the resolved config root path from the locked icedos-config
    input, or None if not available (pre-rebuild, missing lock, etc.).
    """
    lock = find_config_root() / ".state" / "flake.lock"
    if not lock.exists():
        return None
    try:
        data = json.loads(lock.read_text())
        node = data.get("nodes", {}).get("icedos-config", {})
        locked = node.get("locked", {})
        path_str = locked.get("path")
        if path_str:
            p = Path(path_str)
            if p.exists():
                return p
    except Exception:
        pass
    return None


def _config_root() -> Path:
    """Config root: prefer icedos-config locked path, fallback to cwd walk."""
    return _read_icedos_config_path() or find_config_root()


def _state_dir() -> Path:
    return find_config_root() / ".state"


def _generation_snapshots() -> dict[int, str]:
    """Map system generation number → config snapshot folder name (build time).

    Sourced from `.state/.cache/generations/<N>` (written by rebuild).
    """
    out: dict[int, str] = {}
    gens_dir = _state_dir() / ".cache" / "generations"
    if not gens_dir.exists():
        return out
    for entry in gens_dir.iterdir():
        if entry.name.isdigit():
            try:
                out[int(entry.name)] = entry.read_text().strip()
            except Exception:
                pass
    return out


def _age(last_modified: int) -> str:
    """Human 'Nd'/'Nh'/'Nm' age from a unix timestamp."""
    try:
        now = datetime.now(tz=timezone.utc).timestamp()
        secs = max(0, int(now - last_modified))
    except Exception:
        return "unknown"
    days, rem = divmod(secs, 86400)
    if days:
        return f"{days}d"
    hours, rem = divmod(rem, 3600)
    if hours:
        return f"{hours}h"
    return f"{rem // 60}m"


async def _run(cmd: str, *args: str) -> str:
    """Run a command and return stdout, or empty string on failure."""
    proc = await asyncio.create_subprocess_exec(
        cmd, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace").strip()


def get_hostname() -> str:
    """Read the system hostname."""
    try:
        return Path("/etc/hostname").read_text().strip()
    except Exception:
        return os.uname().nodename


async def get_kernel_version() -> str:
    """Get the running kernel version."""
    return await _run("uname", "-r")


async def get_uptime() -> str:
    """Get system uptime."""
    try:
        content = Path("/proc/uptime").read_text()
        seconds = float(content.split()[0])
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        mins = int((seconds % 3600) // 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)
    except Exception:
        return "unknown"


async def get_store_space() -> dict[str, Any]:
    """Get /nix/store disk space info."""
    try:
        output = await _run("df", "-P", "/nix/store")
        lines = output.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            return {
                "total_gb": round(int(parts[1]) / 1024 / 1024, 1),
                "used_gb": round(int(parts[2]) / 1024 / 1024, 1),
                "available_gb": round(int(parts[3]) / 1024 / 1024, 1),
                "use_percent": parts[4],
            }
    except Exception:
        pass
    return {"error": "could not read disk space"}


async def get_current_generation() -> int | None:
    """Get the current NixOS generation number."""
    try:
        link = Path("/nix/var/nix/profiles/system")
        if link.exists():
            m = re.search(r"system-(\d+)-link", os.readlink(str(link)))
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


async def get_generations() -> list[dict[str, Any]]:
    """List all system generations with dates and their config snapshot (if known)."""
    generations = []
    profiles_dir = Path("/nix/var/nix/profiles")
    if not profiles_dir.exists():
        return generations

    snapshots = _generation_snapshots()

    for entry in sorted(profiles_dir.iterdir()):
        m = re.match(r"system-(\d+)-link", entry.name)
        if not m:
            continue
        gen_num = int(m.group(1))
        try:
            dt = datetime.fromtimestamp(entry.lstat().st_mtime, tz=timezone.utc)
            date_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            date_str = "unknown"
        record = {"generation": gen_num, "date": date_str}
        if gen_num in snapshots:
            record["config_snapshot"] = snapshots[gen_num]
        generations.append(record)

    return generations


def list_flake_inputs() -> list[dict[str, Any]]:
    """Parse `.state/flake.lock` — pinned rev + freshness of every input.

    Exposes what actually built the running system (all repos, nixpkgs,
    home-manager, …), not just the two doctor checks.
    """
    lock_path = _state_dir() / "flake.lock"
    data = json.loads(lock_path.read_text())
    nodes = data.get("nodes", {})
    inputs = []
    for name, node in nodes.items():
        locked = node.get("locked")
        if not isinstance(locked, dict):
            continue
        lm = locked.get("lastModified")
        owner, repo = locked.get("owner"), locked.get("repo")
        kind = locked.get("type", "")
        # Some inputs (notably path: locks) record no real lastModified — flake.lock
        # stores 1 — so an "age" is nonsense. Suppress only those; keep real stamps.
        is_local = not isinstance(lm, int) or lm <= 1
        inputs.append(
            {
                "name": name,
                "type": kind,
                "ref": f"{owner}/{repo}" if owner and repo else locked.get("url", ""),
                "rev": (locked.get("rev") or locked.get("narHash", ""))[:12],
                "last_modified": None if is_local else lm,
                "age": "local" if is_local else _age(lm),
            }
        )
    inputs.sort(key=lambda i: i["name"])
    return inputs


def diff_generations(gen_a: int, gen_b: int) -> str:
    """Unified diff of the config set that built two generations.

    Compares the full config set (config.toml + every enabled
    configs/*.toml, including hidden .*.toml) from each generation's
    snapshot, not just config.toml.
    """
    snapshots = _generation_snapshots()
    cache = _state_dir() / ".cache"
    root = _config_root()
    extra_dirs = config_files.extra_config_dirs(root)

    def _config_for(gen: int) -> dict[str, str]:
        snap = snapshots.get(gen)
        if not snap:
            raise KeyError(f"no config snapshot recorded for generation {gen}")
        snap_path = cache / snap
        if not (snap_path / ".config-set").exists():
            raise FileNotFoundError(
                f"snapshot for generation {gen} has no .config-set marker"
            )
        files: dict[str, str] = {}
        # config.toml (optional)
        toml = snap_path / "config.toml"
        if toml.exists():
            files["config.toml"] = toml.read_text()
        # configs/*.toml and configs/.*.toml
        for d in extra_dirs:
            dpath = snap_path / d
            if not dpath.is_dir():
                continue
            for f in sorted(dpath.iterdir()):
                if f.is_file() and f.suffix == ".toml":
                    files[f"{d}/{f.name}"] = f.read_text()
        return files

    a, b = _config_for(gen_a), _config_for(gen_b)
    all_keys = sorted(set(a.keys()) | set(b.keys()))
    parts: list[str] = []
    for key in all_keys:
        lines_a = a.get(key, "").splitlines(keepends=True)
        lines_b = b.get(key, "").splitlines(keepends=True)
        diff = difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=f"generation {gen_a}/{key}",
            tofile=f"generation {gen_b}/{key}",
        )
        parts.extend(diff)
    return (
        "".join(parts)
        or f"config identical between generations {gen_a} and {gen_b}"
    )


async def get_system_info() -> dict[str, Any]:
    """Get comprehensive system information."""
    return {
        "hostname": get_hostname(),
        "kernel": await get_kernel_version(),
        "uptime": await get_uptime(),
        "current_generation": await get_current_generation(),
        "store": await get_store_space(),
    }
