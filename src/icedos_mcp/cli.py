"""Subprocess wrappers for the icedos CLI and related Nix commands."""

from __future__ import annotations

import asyncio
import os
import shutil

from .config import find_config_root


def _find_icedos_bin() -> str:
    """Find the icedos binary."""
    path = shutil.which("icedos")
    if path:
        return path
    raise FileNotFoundError(
        "icedos command not found. Ensure IceDOS is installed and on PATH."
    )


async def _communicate(proc: asyncio.subprocess.Process, timeout: float) -> tuple[int, str, str]:
    """Await a subprocess, killing (not orphaning) it if the timeout is hit."""
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        raise TimeoutError(f"command timed out after {timeout}s (process killed)")
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def run_icedos(*args: str, timeout: float = 300) -> tuple[int, str, str]:
    """Run an icedos CLI command and return (returncode, stdout, stderr)."""
    bin_path = _find_icedos_bin()
    config_root = find_config_root()

    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(config_root),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
    )
    return await _communicate(proc, timeout)


async def run_icedos_streaming(
    *args: str,
    timeout: float = 300,
    on_line=None,
) -> tuple[int, str, str]:
    """Like run_icedos, but stream stdout line-by-line to on_line as it arrives.

    stderr is merged into stdout so the log stays in order and a single stream can
    be pumped; the returned stderr is always empty. on_line is an async callback
    invoked with each decoded line (its failures must not abort the build — the
    caller is responsible for swallowing them).
    """
    bin_path = _find_icedos_bin()
    config_root = find_config_root()

    proc = await asyncio.create_subprocess_exec(
        bin_path, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(config_root),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
    )

    lines: list[str] = []

    async def _pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            lines.append(line)
            if on_line is not None:
                await on_line(line)
        await proc.wait()

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise TimeoutError(f"command timed out after {timeout}s (process killed)")
    return proc.returncode, "".join(lines), ""


async def rebuild(*flags: str, timeout: float = 3600, on_line=None) -> tuple[int, str, str]:
    """Run `icedos rebuild` with given flags. Always includes --build for safety.

    When on_line is given the build is streamed (each log line is passed to the
    callback as it arrives); otherwise the output is buffered until the process exits.
    """
    if on_line is not None:
        return await run_icedos_streaming("rebuild", "--build", *flags, timeout=timeout, on_line=on_line)
    return await run_icedos("rebuild", "--build", *flags, timeout=timeout)


async def get_diff() -> tuple[int, str, str]:
    """Run `icedos configuration diff`."""
    return await run_icedos("configuration", "diff")


async def rollback(
    generation: int | None = None, dry: bool = False
) -> tuple[int, str, str]:
    """Run `icedos configuration rollback`."""
    args = ["configuration", "rollback"]
    if generation is not None:
        args.extend(["--to", str(generation)])
    if dry:
        args.append("--dry")
    return await run_icedos(*args)


async def doctor() -> tuple[int, str, str]:
    """Run `icedos doctor`."""
    return await run_icedos("doctor")


async def list_packages() -> tuple[int, str, str]:
    """Run `icedos pkgs list`."""
    return await run_icedos("pkgs", "list")


async def run_package(attr: str, timeout: float = 300) -> tuple[int, str, str]:
    """Run `icedos pkgs run <attr>`."""
    return await run_icedos("pkgs", "run", attr, timeout=timeout)


async def _run_build_sh(*flags: str, timeout: float = 180) -> tuple[int, str, str]:
    """Run the generated `.state/build.sh` with flags (no closure realize)."""
    config_root = find_config_root()
    state_dir = config_root / ".state"
    build_sh = state_dir / "build.sh"

    if not build_sh.exists():
        raise FileNotFoundError(
            f"build.sh not found at {build_sh}. Run 'icedos rebuild' first."
        )

    proc = await asyncio.create_subprocess_exec(
        "bash", str(build_sh), *flags,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(state_dir),
        env={**os.environ, "ICEDOS_CONFIG_DIR": str(config_root)},
    )
    return await _communicate(proc, timeout)


async def export_search_index() -> tuple[int, str, str]:
    """Run build.sh --export-search-index to regenerate option/module docs."""
    return await _run_build_sh("--export-search-index", timeout=120)


async def genflake_only(timeout: float = 180) -> tuple[int, str, str]:
    """Evaluate + lock the config without realizing a closure.

    Catches schema/type/duplicate-key errors far cheaper than a full build —
    used as the fast validation path for config writes.
    """
    return await _run_build_sh("--genflake-only", timeout=timeout)
