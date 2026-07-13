"""Enumerate + load the user config file set.

Python mirror of ``core/lib/config-files.nix`` — KEEP THE TWO IN SYNC. The load
order, the ``icedos.system.extraConfigs`` default (``["configs"]``), inclusion of
hidden ``.*.toml``, the top-level ``enable = false`` opt-out (stripped before
merge), and the deep-merge semantics (lists concatenate, tables recurse) must
match core, or the server's view will drift from what the system actually builds.

Reads use stdlib ``tomllib`` (plain dict/list) — format-preserving writes stay in
``config.py`` via tomlkit. This module is import-safe (no top-level import of
``config``; ``find_owning_file`` imports the path helpers lazily).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def extra_config_dirs(root: Path) -> list[str]:
    """The ``icedos.system.extraConfigs`` dirs (default ``["configs"]``).

    Bootstrap value — read from ``config.toml`` only, mirroring core (which reads
    it the same way, like ``system.arch``), never from the extra configs it
    selects.
    """
    try:
        main = _read(root / "config.toml")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return ["configs"]
    dirs = main.get("icedos", {}).get("system", {}).get("extraConfigs", ["configs"])
    return list(dirs) if isinstance(dirs, list) else [str(dirs)]


def list_config_files(root: Path) -> list[Path]:
    """Ordered config files: ``config.toml`` (base) then every enabled ``*.toml``
    under each ``extraConfigs`` dir (name-sorted).

    Uses ``iterdir()`` (not ``glob("*.toml")``) so hidden ``.*.toml`` are
    included, matching core's ``readDir``. A file whose top-level ``enable`` is
    ``false`` is skipped; ``config.toml`` is the base and never gated.

    ``config.toml`` is OPTIONAL — a root may be defined entirely by
    ``configs/*.toml`` and/or ``modules/`` — so it is only included when present.
    """
    files = []
    main = root / "config.toml"
    if main.is_file():
        files.append(main)
    for d in extra_config_dirs(root):
        dir_path = root / d
        if not dir_path.is_dir():
            continue
        tomls = sorted(
            p for p in dir_path.iterdir() if p.is_file() and p.name.endswith(".toml")
        )
        for p in tomls:
            try:
                if _read(p).get("enable") is False:
                    continue
            except tomllib.TOMLDecodeError:
                continue
            files.append(p)
    return files


def _strip_enable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k != "enable"}


def _deep_merge(a: Any, b: Any) -> Any:
    """Merge ``b`` onto ``a``: tables recurse, lists concatenate, scalars: ``b``
    wins. (Core rejects duplicate scalars across files; b-wins here is a lenient
    superset — the real strict check still runs in the core eval on rebuild.)"""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a[k], v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    return b


def merged_config(root: Path) -> dict[str, Any]:
    """``config.toml`` + every enabled ``configs/*.toml``, strict-merged — the
    Python analog of ``load-user-config.nix``. Returns plain dict/list."""
    result: dict[str, Any] = {}
    for f in list_config_files(root):
        result = _deep_merge(result, _strip_enable(_read(f)))
    return result


def resolve_file(root: Path, file: str | None) -> Path:
    """Absolute path for a ``file`` argument.

    ``None``/``""`` → ``config.toml``. Otherwise the arg must resolve to
    ``config.toml`` or a ``.toml`` directly under one of the ``extraConfigs``
    dirs, inside ``root`` (prevents writes outside the config tree). The path may
    not exist yet — creating a new ``configs/<name>.toml`` is allowed.
    """
    if not file:
        return root / "config.toml"
    root_r = root.resolve()
    p = (root / file).resolve()
    if p == root_r / "config.toml":
        return p
    if p.suffix != ".toml":
        raise ValueError(f"file '{file}' must be a .toml file")
    allowed = {(root_r / d).resolve() for d in extra_config_dirs(root)}
    if p.parent not in allowed:
        raise ValueError(
            f"file '{file}' must be config.toml or a .toml under one of: "
            + ", ".join(sorted(str(a.relative_to(root_r)) for a in allowed))
        )
    return p


def find_owning_file(root: Path, dotted_path: str) -> Path | None:
    """The config file that declares ``dotted_path`` (first in load order), for
    targeted edits/deletes. ``None`` if no file declares it."""
    from .config import _parse_path, _get_by_tokens  # lazy: avoid import cycle

    tokens = _parse_path(dotted_path)
    for f in list_config_files(root):
        try:
            _get_by_tokens(_strip_enable(_read(f)), tokens, dotted_path)
            return f
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return None


def find_repo_file(root: Path, url: str) -> Path | None:
    """The config file whose ``[[icedos.repositories]]`` contains ``url``."""
    for f in list_config_files(root):
        repos = _read(f).get("icedos", {}).get("repositories", []) or []
        if any(r.get("url") == url for r in repos):
            return f
    return None
