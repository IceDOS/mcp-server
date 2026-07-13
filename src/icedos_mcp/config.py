"""Config read/edit operations using tomlkit for round-trip preservation.

User config is a *set* of files: ``config.toml`` (global base) plus every enabled
``*.toml`` under the ``icedos.system.extraConfigs`` dirs (default ``configs/``) —
see ``config_files.py`` (the Python mirror of core ``lib/config-files.nix``).

Reads default to the *merged* view across all files; writes target the file that
already declares the key (or ``config.toml`` for new keys), so edits land in the
right place instead of shadowing a value that lives in ``configs/*.toml``. A
specific file can always be forced with the ``file`` argument (e.g. write a secret
into a hidden ``configs/.local.toml``).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import loads, dumps

from . import config_files


def _normalize_blank_lines(content: str) -> str:
    """Ensure exactly one blank line before every table header.

    tomlkit appends newly-created tables (new repos, users, sections) with no
    leading blank line, jamming them against the preceding block. This restores
    the one-blank-line-between-blocks style the hand-written configs use. It is
    idempotent: unchanged, correctly-spaced files round-trip untouched. Only
    column-0 `[`/`[[` header lines are affected; indented array rows are not.
    """
    out: list[str] = []
    for line in content.split("\n"):
        if line.startswith("[") and out:
            while out and out[-1].strip() == "":
                out.pop()
            if out:
                out.append("")
        out.append(line)
    return "\n".join(out)


def _coerce_candidates(raw: str) -> list[Any]:
    """Interpretations of a string arg to match against typed array items.

    An array-item delete arrives as a string; a `[1, 2]` array holds ints, so
    try bool/int/float forms in addition to the literal string.
    """
    candidates: list[Any] = [raw]
    low = raw.lower()
    if low in ("true", "false"):
        candidates.append(low == "true")
    try:
        candidates.append(int(raw))
    except ValueError:
        pass
    try:
        candidates.append(float(raw))
    except ValueError:
        pass
    return candidates


def _is_config_root(p: Path) -> bool:
    """A config root is a flake (has flake.nix, the mkIceDOS entry). config.toml
    is optional — a root may be defined by configs/*.toml and/or modules/ — so we
    also require one of config.toml / configs/ / modules/ to avoid matching an
    unrelated flake during the cwd walk-up."""
    if not (p / "flake.nix").exists():
        return False
    return (
        (p / "config.toml").exists()
        or (p / "configs").is_dir()
        or (p / "modules").is_dir()
    )


def find_config_root() -> Path:
    """Find the IceDOS config root by walking up from cwd for a flake.nix root
    (config.toml is optional). Respects the ICEDOS_CONFIG_DIR env var if set.
    """
    env_dir = os.environ.get("ICEDOS_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        if (p / "flake.nix").exists():
            return p
        raise FileNotFoundError(f"ICEDOS_CONFIG_DIR={env_dir} does not contain flake.nix")

    current = Path.cwd()
    for parent in [current, *current.parents]:
        if _is_config_root(parent):
            return parent
    raise FileNotFoundError(
        "Cannot find IceDOS config root (flake.nix + config.toml/configs/modules). "
        "Set ICEDOS_CONFIG_DIR or run from within an IceDOS config directory."
    )


def _read_plain(path: Path) -> dict[str, Any]:
    """Parse one TOML file to a plain dict (stdlib tomllib — no round-trip)."""
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    """Round-trip tomlkit document for a specific file; empty doc if it does not
    exist yet (so writing to a new ``configs/<name>.toml`` creates it)."""
    return loads(path.read_text()) if path.exists() else tomlkit.document()


def resolve_write_target(
    file: str | None = None,
    *,
    dotted_path: str | None = None,
    repo_url: str | None = None,
    for_repos: bool = False,
) -> Path:
    """The file a mutation should write to.

    Explicit ``file`` wins. Otherwise: the file that already declares the target
    (``find_owning_file`` for a key, ``find_repo_file`` for a repo, the first file
    holding any ``[[icedos.repositories]]`` for ``for_repos``), falling back to
    ``config.toml`` for brand-new entries. Deterministic, so the server can
    resolve the same target for its revert snapshot.
    """
    root = find_config_root()
    if file:
        return config_files.resolve_file(root, file)
    if repo_url is not None:
        return config_files.find_repo_file(root, repo_url) or (root / "config.toml")
    if for_repos:
        for f in config_files.list_config_files(root):
            if _read_plain(f).get("icedos", {}).get("repositories"):
                return f
        return root / "config.toml"
    if dotted_path is not None:
        return config_files.find_owning_file(root, dotted_path) or (root / "config.toml")
    return root / "config.toml"


def read_config(file: str | None = None) -> dict[str, Any]:
    """Parse config into a dict. ``file=None`` → the merged view across all config
    files; an explicit ``file`` → just that file."""
    root = find_config_root()
    if file is None:
        return config_files.merged_config(root)
    return _read_plain(config_files.resolve_file(root, file))


_APPEND = object()  # sentinel for the `path[]` append accessor (set only)
_SEG = re.compile(r"^([^\[\]]*)((?:\[[^\]]*\])*)$")
_IDX = re.compile(r"\[([^\]]*)\]")


def _parse_path(dotted_path: str) -> list[Any]:
    """Tokenize a dotted path with optional `[N]` / `[]` accessors.

    'icedos.repositories[0].modules[1]' -> ['icedos','repositories',0,'modules',1]
    'icedos.system.packages[]'          -> ['icedos','system','packages',_APPEND]
    Bare keys still parse to plain string tokens (backward compatible).
    """
    tokens: list[Any] = []
    for seg in dotted_path.split("."):
        m = _SEG.match(seg)
        if not m:
            raise ValueError(f"invalid path segment '{seg}' in '{dotted_path}'")
        base, brackets = m.group(1), m.group(2)
        if base:
            tokens.append(base)
        elif not brackets:
            raise ValueError(f"empty path segment in '{dotted_path}'")
        for raw in _IDX.findall(brackets):
            raw = raw.strip()
            if raw == "":
                tokens.append(_APPEND)
            else:
                try:
                    tokens.append(int(raw))
                except ValueError:
                    raise ValueError(f"invalid array index '[{raw}]' in '{dotted_path}'")
    if any(t is _APPEND for t in tokens[:-1]):
        raise ValueError(f"'[]' append is only valid as the final accessor in '{dotted_path}'")
    return tokens


def _get_by_tokens(root: Any, tokens: list[Any], dotted_path: str) -> Any:
    """Descend `root` through parsed tokens (str keys and int indices)."""
    current = root
    for tok in tokens:
        if tok is _APPEND:
            raise ValueError("'[]' append is only valid for set operations")
        try:
            current = current[tok]
        except (KeyError, IndexError, TypeError):
            raise KeyError(f"Path '{dotted_path}' not found in config")
    return current


def get_value(dotted_path: str, file: str | None = None) -> Any:
    """Get a value by dotted path (e.g. 'icedos.system.arch').

    ``file=None`` looks it up in the merged config (so values in any
    ``configs/*.toml`` are found); an explicit ``file`` reads just that file.
    Supports array indexing: 'icedos.system.packages[0]',
    'icedos.repositories[0].modules[1]' (negative indices allowed).
    """
    config = read_config(file=file)
    return _get_by_tokens(config, _parse_path(dotted_path), dotted_path)


def _table_add(table: Any, key: str, value: Any) -> None:
    """Add a key to a tomlkit table, handling OutOfOrderTableProxy."""
    try:
        table.add(key, value)
    except AttributeError:
        table[key] = value


def set_value(dotted_path: str, value: Any, file: str | None = None) -> str:
    """Set a value by dotted path. Returns the updated TOML content.

    Writes to the file that already declares the key, else ``config.toml`` (or the
    explicit ``file``). Supports array accessors:
    - 'icedos.system.packages[2]' — replace element 2 (must exist; negatives ok)
    - 'icedos.system.packages[]'  — append a new element (creates the array if missing)
    - 'icedos.repositories[0].modules[1]' — nested list / array-of-tables
    """
    target = resolve_write_target(file, dotted_path=dotted_path)
    doc = _read_doc(target)
    tokens = _parse_path(dotted_path)
    if not tokens:
        raise ValueError("empty path")

    # Navigate to the container that holds the last accessor, creating missing
    # string-key intermediates (as arrays or tables per the next token's kind).
    current = doc
    for i, tok in enumerate(tokens[:-1]):
        if isinstance(tok, str):
            if tok not in current:
                nxt = tokens[i + 1]
                new = tomlkit.array() if (nxt is _APPEND or isinstance(nxt, int)) else tomlkit.table()
                _table_add(current, tok, new)
            current = current[tok]
        else:  # int index — must already exist
            try:
                current = current[tok]
            except (IndexError, KeyError, TypeError):
                raise KeyError(f"Path '{dotted_path}' not found in config")

    last = tokens[-1]
    if last is _APPEND:
        if not isinstance(current, list):
            raise TypeError(f"'[]' append target is not an array: '{dotted_path}'")
        current.append(value)
    elif isinstance(last, int):
        if not isinstance(current, list):
            raise TypeError(f"'[{last}]' index target is not an array: '{dotted_path}'")
        try:
            current[last] = value
        except IndexError:
            raise IndexError(f"index [{last}] out of range for '{dotted_path}' (len {len(current)})")
    else:
        if last in current:
            current[last] = value
        else:
            _table_add(current, last, value)

    content = _normalize_blank_lines(dumps(doc))
    target.write_text(content)
    return content


def delete_value(dotted_path: str, file: str | None = None) -> str:
    """Delete a value by dotted path from the file that declares it (or ``file``).

    Supports:
    - Removing a key from a table: 'icedos.system.ssh'
    - Removing an array item by value: 'icedos.system.packages|signal-desktop'
    - Removing an array element by index: 'icedos.system.packages[2]'

    Returns the updated TOML content.
    """
    root = find_config_root()
    lookup = dotted_path.split("|", 1)[0]
    if file:
        target = config_files.resolve_file(root, file)
    else:
        target = config_files.find_owning_file(root, lookup)
        if target is None:
            raise KeyError(f"'{dotted_path}' not found in any config file")
    doc = _read_doc(target)

    # Array item deletion by value (path|value format).
    if "|" in dotted_path:
        array_path, item_value = dotted_path.split("|", 1)
        arr = _get_by_tokens(doc, _parse_path(array_path), array_path)
        if not isinstance(arr, list):
            raise TypeError(f"Expected array at '{array_path}', got {type(arr).__name__}")
        # Match against array items, coercing the string arg to the item type
        # for non-string arrays (ints, floats, bools).
        for candidate in _coerce_candidates(item_value):
            if candidate in arr:
                arr.remove(candidate)
                break
        else:
            raise ValueError(f"Item '{item_value}' not found in array '{array_path}'")
        content = _normalize_blank_lines(dumps(doc))
        target.write_text(content)
        return content

    # Key or array-index deletion.
    tokens = _parse_path(dotted_path)
    if not tokens:
        raise ValueError("empty path")
    current = _get_by_tokens(doc, tokens[:-1], dotted_path)
    last = tokens[-1]
    if last is _APPEND:
        raise ValueError("'[]' append is not valid for delete")
    if isinstance(last, int):
        if not isinstance(current, list):
            raise TypeError(f"'[{last}]' index target is not an array: '{dotted_path}'")
        try:
            del current[last]
        except IndexError:
            raise IndexError(f"index [{last}] out of range for '{dotted_path}' (len {len(current)})")
    else:
        if last not in current:
            raise KeyError(f"Key '{dotted_path}' not found in config")
        del current[last]

    content = _normalize_blank_lines(dumps(doc))
    target.write_text(content)
    return content


def list_repositories() -> list[dict[str, Any]]:
    """List all [[icedos.repositories]] entries across the merged config (so
    repos declared in configs/*.toml — including hidden ones — are included)."""
    config = read_config()
    repos = config.get("icedos", {}).get("repositories", [])
    return [
        {
            "url": repo.get("url", ""),
            "override_url": repo.get("overrideUrl", ""),
            "modules": repo.get("modules", []),
            "fetch_dependencies": repo.get("fetchDependencies", True),
            "fetch_optional_dependencies": repo.get("fetchOptionalDependencies", False),
            "patches": repo.get("patches", []),
        }
        for repo in repos
    ]


def set_module_enabled(module: str, repo_url: str, enabled: bool, file: str | None = None) -> str:
    """Add or remove a module name in a repository's `modules` array.

    `repo_url` identifies which [[icedos.repositories]] entry owns the module; the
    edit lands in whichever config file declares that repo (or the explicit
    ``file``). Raises if the repo is not present. Returns the updated TOML content.
    """
    root = find_config_root()
    target = config_files.resolve_file(root, file) if file else config_files.find_repo_file(root, repo_url)
    if target is None:
        raise KeyError(f"repository '{repo_url}' not found in any config file")
    doc = _read_doc(target)

    repos = doc.get("icedos", {}).get("repositories", None)
    if repos is None:
        raise KeyError("no [[icedos.repositories]] entries in config")

    repo_target = next((r for r in repos if r.get("url") == repo_url), None)
    if repo_target is None:
        raise KeyError(f"repository '{repo_url}' not found in config")

    if "modules" not in repo_target:
        repo_target.add("modules", tomlkit.array())
    mods = repo_target["modules"]

    if enabled:
        if module in mods:
            raise ValueError(f"module '{module}' already enabled in {repo_url}")
        mods.append(module)
    else:
        if module not in mods:
            raise ValueError(f"module '{module}' not enabled in {repo_url}")
        mods.remove(module)

    content = _normalize_blank_lines(dumps(doc))
    target.write_text(content)
    return content


def add_repository(
    url: str,
    modules: list[str] | None = None,
    fetch_optional_deps: bool = False,
    fetch_deps: bool = True,
    override_url: str = "",
    patches: list[str] | None = None,
    file: str | None = None,
) -> str:
    """Add a new [[icedos.repositories]] entry.

    Appends to the file that already holds repositories (keeping them together),
    else ``config.toml`` — or the explicit ``file``. Duplicate-url check spans the
    whole merged config.
    """
    root = find_config_root()
    target = resolve_write_target(file, for_repos=True)

    merged = read_config()
    for repo in merged.get("icedos", {}).get("repositories", []) or []:
        if repo.get("url") == url:
            raise ValueError(f"Repository '{url}' already exists in config")

    doc = _read_doc(target)
    if "icedos" not in doc:
        doc.add("icedos", tomlkit.table())
    icedos = doc["icedos"]
    if "repositories" not in icedos:
        icedos.add("repositories", tomlkit.aot())

    repos = icedos["repositories"]
    entry = tomlkit.table()
    entry.add("url", url)
    if override_url:
        entry.add("overrideUrl", override_url)
    if modules:
        mods = tomlkit.array()
        mods.extend(modules)
        entry.add("modules", mods)
    if not fetch_deps:
        entry.add("fetchDependencies", False)
    if fetch_optional_deps:
        entry.add("fetchOptionalDependencies", True)
    if patches:
        parr = tomlkit.array()
        parr.extend(patches)
        entry.add("patches", parr)

    repos.append(entry)

    content = _normalize_blank_lines(dumps(doc))
    target.write_text(content)
    return content


def edit_user(
    username: str,
    settings: dict[str, Any],
    file: str | None = None,
) -> str:
    """Add or update an [icedos.users.<username>] entry in the file that declares
    it (or ``config.toml`` / the explicit ``file``)."""
    root = find_config_root()
    target = resolve_write_target(file, dotted_path=f"icedos.users.{username}")
    doc = _read_doc(target)

    if "icedos" not in doc:
        doc.add("icedos", tomlkit.table())
    icedos = doc["icedos"]
    if "users" not in icedos:
        icedos.add("users", tomlkit.table())

    users = icedos["users"]
    if username not in users:
        users.add(username, tomlkit.table())

    user_table = users[username]
    for key, value in settings.items():
        if key in user_table:
            user_table[key] = value
        else:
            user_table.add(key, value)

    content = _normalize_blank_lines(dumps(doc))
    target.write_text(content)
    return content


def get_config_toml_string(file: str | None = None) -> str:
    """Raw config content. ``file=None`` → the merged config serialized to TOML;
    an explicit ``file`` → that file's raw text (comments/formatting preserved)."""
    root = find_config_root()
    if file:
        return config_files.resolve_file(root, file).read_text()
    return dumps(config_files.merged_config(root))
