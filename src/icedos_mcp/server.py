"""IceDOS MCP Server — exposes the IceDOS ecosystem to AI agents."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mcp.server.fastmcp import Context, FastMCP

from . import config_files
from .config import (
    add_repository as config_add_repository,
    delete_value,
    edit_user as config_edit_user,
    find_config_root,
    get_config_toml_string,
    get_value,
    list_repositories as config_list_repositories,
    resolve_write_target,
    set_module_enabled as config_set_module_enabled,
    set_value,
)
from .cli import (
    doctor as cli_doctor,
    export_search_index,
    genflake_only as cli_genflake_only,
    get_diff as cli_get_diff,
    list_packages as cli_list_packages,
    rebuild as cli_rebuild,
    rollback as cli_rollback,
    run_package as cli_run_package,
)
from .modules import (
    explore_github_repo,
    explore_local_repo as _explore_local_repo,
    list_repo_tree as modules_list_repo_tree,
    read_repo_file as modules_read_repo_file,
)
from .prompts import get_prompt
from .system import (
    _state_dir,
    diff_generations as sys_diff_generations,
    get_current_generation,
    get_generations,
    get_system_info,
    list_flake_inputs as sys_list_flake_inputs,
)

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger("icedos-mcp")

mcp = FastMCP("IceDOS MCP Server")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ensure_search_index() -> None:
    """Regenerate the options/modules search index if stale or missing."""
    try:
        root = find_config_root()
        cache = root / ".state" / ".cache"
        docs = [cache / "options-doc.json", cache / "modules-doc.json"]
        stale = any(not d.exists() for d in docs)
        if not stale:
            oldest = min(d.stat().st_mtime for d in docs)
            # Sources that invalidate the index: every loaded config file
            # (config.toml + enabled configs/*.toml, mirroring core's
            # configuration.nix staleness check) plus the lock and the generated
            # `.state/flake.nix` (regenerated on every genflake/rebuild but NOT by
            # --export-search-index itself, so a rebuild after editing core under a
            # path: override refreshes the index on the next call).
            sources = config_files.list_config_files(root) + [
                root / "flake.lock",
                root / ".state" / "flake.nix",
            ]
            for src in sources:
                if src.exists() and src.stat().st_mtime > oldest:
                    stale = True
                    break
        if stale:
            logger.info("Regenerating search index...")
            await export_search_index()
    except Exception as e:
        logger.warning("Could not ensure search index: %s", e)


def _load_index(filename: str) -> list[dict[str, Any]]:
    try:
        path = find_config_root() / ".state" / ".cache" / filename
        return json.loads(path.read_text()) if path.exists() else []
    except Exception:
        return []


def _toml_snippet(opt: dict[str, Any]) -> str:
    name, value = opt.get("name", ""), opt.get("value")
    parts = name.split(".")
    section, key = (".".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else ("", name)
    if isinstance(value, str):
        val = f'"{value}"'
    elif isinstance(value, bool):
        val = "true" if value else "false"
    elif value is None:
        val = '""'
    elif isinstance(value, list):
        val = "[ " + ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in value) + " ]"
    else:
        val = str(value)
    return f"[{section}]\n{key} = {val}" if section else f"{key} = {val}"


def _score(query: str, name: str, description: str) -> int:
    """Relevance score for a query against an item's name/description.

    Higher is better; 0 means no match (filtered out). Exact/prefix/word-boundary
    name matches rank above mid-name substring, which ranks above description-only.
    """
    q, n, d = query.lower(), name.lower(), (description or "").lower()
    if not q:
        return 1
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        # earlier match position scores higher; word-boundary gets a bump
        pos = n.index(q)
        bonus = 10 if (pos == 0 or not n[pos - 1].isalnum()) else 0
        return 60 + bonus - min(pos, 20)
    if q in d:
        return 20
    return 0


def _ranked(items: list[dict[str, Any]], query: str, key_name: str,
            limit: int, offset: int) -> dict[str, Any]:
    """Score, sort, and paginate search results with a total count."""
    if query:
        scored = [(s, it) for it in items if (s := _score(query, it.get(key_name, ""), it.get("description", "")))]
        scored.sort(key=lambda si: (-si[0], si[1].get(key_name, "")))
        ordered = [it for _, it in scored]
    else:
        ordered = sorted(items, key=lambda it: it.get(key_name, ""))
    total = len(ordered)
    window = ordered[offset:offset + limit]
    return {
        "total": total,
        "count": len(window),
        "offset": offset,
        "truncated": offset + len(window) < total,
        "results": window,
    }


def _err(kind: str, message: str) -> str:
    """Structured error payload agents can branch on."""
    return json.dumps({"ok": False, "error": {"kind": kind, "message": message}}, indent=2)


async def _revalidate(target: Path, original: str | None) -> str | None:
    """Eval-validate the just-written config; revert `target` on failure.

    `original` is the target file's prior text, or None if the write created the
    file (a failed validation then removes it). Uses `--genflake-only` (eval +
    lock, no closure realize) — catches schema/type/duplicate-key errors far
    cheaper than a full build. Returns an error message if validation failed
    (after reverting), or None on success.
    """
    rc, _, stderr = await cli_genflake_only()
    if rc != 0:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(original)
        return stderr.strip() or "unknown eval error"
    return None


# ===========================================================================
# CONFIG TOOLS
# ===========================================================================

@mcp.tool()
async def get_config(section: str = "", file: str = "") -> str:
    """Read the IceDOS config (merged across config.toml + configs/*.toml).

    Args:
        section: Optional dotted path to a specific section (e.g. 'icedos.system.arch').
        file: Optional config-root-relative file to read in isolation
              (e.g. 'configs/opencode.toml'); default reads the merged config.
    """
    try:
        f = file or None
        if section:
            value = get_value(section, file=f)
            return json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
        return get_config_toml_string(file=f)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_config_value(path: str, resolved: bool = False, file: str = "") -> str:
    """Get a value by dotted path — either declared in config, or the resolved option.

    Array indexing is supported: 'icedos.system.packages[0]',
    'icedos.repositories[0].modules[1]' (negative indices allowed).

    Args:
        path: Dotted path (e.g. 'icedos.system.arch'), with optional '[N]' accessors.
        resolved: If True, return the resolved option value/type from the options
                  index (i.e. the effective default) instead of the declared TOML.
                  Use this for options not written in config.
        file: Optional config-root-relative file to read in isolation; default
              looks the path up in the merged config (values in any
              configs/*.toml are found).
    """
    if resolved:
        await _ensure_search_index()
        for opt in _load_index("options-doc.json"):
            if opt.get("name") == path:
                return json.dumps(
                    {"path": path, "value": opt.get("value"), "type": opt.get("type"), "source": "resolved"},
                    indent=2,
                )
        return _err("not_found", f"option '{path}' not in options index")
    try:
        return json.dumps({"path": path, "value": get_value(path, file=file or None), "source": "declared"}, indent=2)
    except KeyError:
        return _err("not_found", f"'{path}' not declared in config (try resolved=true)")
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
async def set_config_value(path: str, value: str, file: str = "", validate: bool = True) -> str:
    """Set a value in the config by dotted path.

    Writes to the config file that already declares the key (so edits to values
    now living in configs/*.toml land there), else config.toml — or the file you
    pass. Array accessors are supported in the path:
      - 'icedos.system.packages[2]'  — replace element 2 (must exist; negatives ok)
      - 'icedos.system.packages[]'   — append a new element
      - 'icedos.repositories[0].modules[1]' — nested list / array-of-tables

    Args:
        path: Dotted path (e.g. 'icedos.system.arch'), with optional '[N]'/'[]' accessors.
        value: New value as JSON (string, number, boolean, array, or object).
        file: Config-root-relative target file (e.g. 'configs/gaming.toml', or a
              hidden 'configs/.local.toml' for secrets). Default = auto (the owning
              file, else config.toml).
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value

    try:
        target = resolve_write_target(file or None, dotted_path=path)
        original = target.read_text() if target.exists() else None
        set_value(path, parsed, file=file or None)

        if validate:
            err = await _revalidate(target, original)
            if err:
                return f"Validation failed — change reverted:\n{err}"
            return f"Set {path} = {json.dumps(parsed)} in {target.name} (validated)"
        return f"Set {path} = {json.dumps(parsed)} in {target.name} (not validated)"
    except Exception as e:
        return _err("write_error", str(e))


@mcp.tool()
async def delete_config_value(path: str, file: str = "", validate: bool = True) -> str:
    """Delete a value from the config by dotted path.

    Removes it from the file that declares it (or the file you pass). Handles a
    table key, an array element by index, or an array item by value:
      - 'icedos.system.packages'            — delete the key
      - 'icedos.system.packages[2]'         — delete element 2 (negatives ok)
      - 'icedos.system.packages|signal-desktop' — delete the item matching a value

    Args:
        path: Dotted path (optionally with '[N]'), or 'path|value' for value-match removal.
        file: Optional config-root-relative file to delete from; default = the file
              that declares the key.
        validate: If True, eval-validate (genflake) after deleting; revert on failure.
    """
    try:
        lookup = path.split("|", 1)[0]
        target = resolve_write_target(file or None, dotted_path=lookup)
        original = target.read_text() if target.exists() else None
        delete_value(path, file=file or None)

        if validate:
            err = await _revalidate(target, original)
            if err:
                return f"Validation failed — change reverted:\n{err}"
            return f"Deleted '{path}' from {target.name} (validated)"
        return f"Deleted '{path}' from {target.name} (not validated)"
    except Exception as e:
        return _err("write_error", str(e))


@mcp.tool()
async def list_repositories() -> str:
    """List all [[icedos.repositories]] entries (merged across config.toml + configs/*.toml)."""
    try:
        return json.dumps(config_list_repositories(), indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def add_repository(
    url: str,
    modules: list[str] | None = None,
    fetch_optional_deps: bool = False,
    fetch_deps: bool = True,
    override_url: str = "",
    patches: list[str] | None = None,
    file: str = "",
) -> str:
    """Add a new [[icedos.repositories]] entry.

    Appends to the config file that already holds repositories (keeping them
    together), else config.toml — or the file you pass.

    Args:
        url: Repository URL (e.g. 'github:icedos/apps').
        modules: Module names to enable (e.g. ['steam', 'btop']).
        fetch_optional_deps: Also pull optional dependencies.
        fetch_deps: Pull required dependencies (default True; set False to pull none).
        override_url: Local/alternate source, e.g. 'path:/home/ice/.code/icedos/apps'.
        patches: Paths to repo-wide patch files (config-root-relative).
        file: Optional config-root-relative target file (e.g. 'configs/repositories.toml').
    """
    try:
        config_add_repository(
            url=url,
            modules=modules or [],
            fetch_optional_deps=fetch_optional_deps,
            fetch_deps=fetch_deps,
            override_url=override_url,
            patches=patches or [],
            file=file or None,
        )
        return f"Added repository {url}"
    except Exception as e:
        return _err("write_error", str(e))


def _module_repo(name: str) -> str | None:
    """Find which configured repo owns a module, via the modules index."""
    for m in _load_index("modules-doc.json"):
        if m.get("name") == name:
            return m.get("repo")
    return None


@mcp.tool()
async def enable_module(module: str, repo: str = "", file: str = "", validate: bool = False) -> str:
    """Enable a module by adding it to its repository's `modules` list.

    Args:
        module: Module name (e.g. 'steam').
        repo: Owning repo URL. If omitted, resolved from the module index.
        file: Optional config-root-relative file holding the repo; default = the
              file that declares it (e.g. 'configs/repositories.toml').
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    await _ensure_search_index()
    repo_url = repo or _module_repo(module)
    if not repo_url:
        return _err("not_found", f"cannot resolve owning repo for '{module}'; pass repo=")
    try:
        target = resolve_write_target(file or None, repo_url=repo_url)
        original = target.read_text() if target.exists() else None
        config_set_module_enabled(module, repo_url, enabled=True, file=file or None)
        if validate:
            err = await _revalidate(target, original)
            if err:
                return f"Validation failed — change reverted:\n{err}"
        return f"Enabled '{module}' in {repo_url}"
    except Exception as e:
        return _err("write_error", str(e))


@mcp.tool()
async def disable_module(module: str, repo: str = "", file: str = "", validate: bool = False) -> str:
    """Disable a module by removing it from its repository's `modules` list.

    Args:
        module: Module name (e.g. 'steam').
        repo: Owning repo URL. If omitted, resolved from the module index.
        file: Optional config-root-relative file holding the repo; default = the
              file that declares it (e.g. 'configs/repositories.toml').
        validate: If True, eval-validate (genflake) after writing; revert on failure.
    """
    await _ensure_search_index()
    repo_url = repo or _module_repo(module)
    if not repo_url:
        return _err("not_found", f"cannot resolve owning repo for '{module}'; pass repo=")
    try:
        target = resolve_write_target(file or None, repo_url=repo_url)
        original = target.read_text() if target.exists() else None
        config_set_module_enabled(module, repo_url, enabled=False, file=file or None)
        if validate:
            err = await _revalidate(target, original)
            if err:
                return f"Validation failed — change reverted:\n{err}"
        return f"Disabled '{module}' in {repo_url}"
    except Exception as e:
        return _err("write_error", str(e))


@mcp.tool()
async def edit_user(username: str, settings: str = "{}", file: str = "") -> str:
    """Add or update a user under [icedos.users.<username>].

    Args:
        username: The username.
        settings: JSON object with keys: defaultPassword, sudo, extraGroups,
                  packages, trusted, isNormalUser, isSystemUser, home, description.
        file: Optional config-root-relative target file; default = the file that
              declares the user, else config.toml.
    """
    try:
        config_edit_user(username, json.loads(settings), file=file or None)
        return f"Updated user '{username}'"
    except json.JSONDecodeError:
        return "Error: settings must be a valid JSON object"
    except Exception as e:
        return f"Error: {e}"


# ===========================================================================
# QUERY & DISCOVERY
# ===========================================================================

@mcp.tool()
async def search_options(query: str = "", category: str = "", limit: int = 50, offset: int = 0) -> str:
    """Search IceDOS options by name/description, ranked, with pagination.

    Returns {total, count, offset, truncated, results}. Results are relevance-ranked
    (exact > prefix > substring > description). Raise `offset` to page through `total`.

    Args:
        query: Search term.
        category: Substring filter on the option name (e.g. 'hardware', 'system').
        limit: Max results in this page (default 50).
        offset: Skip this many ranked results (for pagination).
    """
    await _ensure_search_index()
    opts = _load_index("options-doc.json")
    if not opts:
        return _err("no_index", "no options index; run 'icedos rebuild' first")
    if category:
        c = category.lower()
        opts = [o for o in opts if c in o.get("name", "").lower()]
    return json.dumps(_ranked(opts, query, "name", limit, offset), indent=2)


@mcp.tool()
async def get_option(name: str) -> str:
    """Get full details for a specific IceDOS option with a paste-ready TOML snippet.

    Args:
        name: Option name (e.g. 'icedos.system.arch').
    """
    await _ensure_search_index()
    for opt in _load_index("options-doc.json"):
        if opt.get("name") == name:
            return json.dumps({**opt, "toml_snippet": _toml_snippet(opt)}, indent=2)
    return f"Option '{name}' not found."


@mcp.tool()
async def search_modules(query: str = "", repo: str = "", status: str = "all",
                         limit: int = 50, offset: int = 0) -> str:
    """Search IceDOS modules, ranked, with pagination.

    Returns {total, count, offset, truncated, results}, relevance-ranked.

    Args:
        query: Search term.
        repo: Filter by repo URL (e.g. 'github:icedos/apps').
        status: 'enabled', 'available', or 'all' (default).
        limit: Max results in this page (default 50).
        offset: Skip this many ranked results (for pagination).
    """
    await _ensure_search_index()
    mods = _load_index("modules-doc.json")
    if not mods:
        return _err("no_index", "no modules index; run 'icedos rebuild' first")
    if repo:
        mods = [m for m in mods if m.get("repo") == repo]
    if status == "enabled":
        mods = [m for m in mods if m.get("enabled")]
    elif status == "available":
        mods = [m for m in mods if not m.get("enabled")]
    return json.dumps(_ranked(mods, query, "name", limit, offset), indent=2)


@mcp.tool()
async def module_graph(name: str) -> str:
    """Dependency graph for a module — forward (deps) and reverse (dependents).

    Forward = what `name` pulls in (recursively, deps + optionalDeps). Reverse =
    which other modules depend on `name`. Sourced from the module index; no eval.

    Args:
        name: Module name (e.g. 'steam').
    """
    await _ensure_search_index()
    mods = {m["name"]: m for m in _load_index("modules-doc.json") if m.get("name")}
    if name not in mods:
        return _err("not_found", f"module '{name}' not in index")

    def deps_of(mod: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for group in mod.get("dependencies", []) + mod.get("optionalDependencies", []):
            out.extend(group.get("modules", []) if isinstance(group, dict) else [])
        return out

    # forward, recursive
    forward, seen, stack = [], set(), list(deps_of(mods[name]))
    while stack:
        d = stack.pop(0)
        if d in seen:
            continue
        seen.add(d)
        node = mods.get(d, {})
        forward.append({"name": d, "enabled": node.get("enabled", False), "present": d in mods})
        stack.extend(x for x in deps_of(node) if x not in seen)

    reverse = sorted(other for other, m in mods.items() if name in deps_of(m))

    node = mods[name]
    return json.dumps(
        {
            "module": name,
            "repo": node.get("repo"),
            "enabled": node.get("enabled", False),
            "explicit": node.get("explicit", False),
            "depends_on": forward,
            "required_by": reverse,
        },
        indent=2,
    )


@mcp.tool()
async def get_module(name: str) -> str:
    """Get full details for a specific IceDOS module.

    Args:
        name: Module name (e.g. 'steam', 'btop').
    """
    await _ensure_search_index()
    for mod in _load_index("modules-doc.json"):
        if mod.get("name") == name:
            return json.dumps(mod, indent=2)
    return f"Module '{name}' not found."


@mcp.tool()
async def list_modules(repo: str = "", status: str = "all") -> str:
    """List all IceDOS modules with status markers (● explicit, ◐ dependency, ○ available).

    Args:
        repo: Filter by repo URL.
        status: 'enabled', 'available', or 'all' (default).
    """
    await _ensure_search_index()
    mods = _load_index("modules-doc.json")
    if not mods:
        return "No modules index found."
    if repo:
        mods = [m for m in mods if m.get("repo") == repo]
    if status == "enabled":
        mods = [m for m in mods if m.get("enabled")]
    elif status == "available":
        mods = [m for m in mods if not m.get("enabled")]
    lines = []
    for m in mods:
        marker = "●" if m.get("explicit") else ("◐" if m.get("enabled") else "○")
        lines.append(f"{marker} {m['name']}  ({m.get('repo', '?')})")
        if m.get("description"):
            lines.append(f"  {m['description']}")
    return "\n".join(lines) if lines else "No modules found."


# ===========================================================================
# SYSTEM OPERATIONS
# ===========================================================================

def _summarize_build(stdout: str, stderr: str, tail: int = 40) -> str:
    """Condense a nom build log: strip ANSI, keep summary lines + a short tail."""
    ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[\?2026[hl]")
    clean = ansi.sub("", stdout + "\n" + stderr)
    lines = [ln.rstrip() for ln in clean.splitlines()]
    # drop the per-frame progress spam (timer ticks, redraw graph frames)
    keep = [ln for ln in lines if ln.strip() and not re.match(r"^⏱|^┏|^┃|^┗|^┣", ln)]
    summary = [ln for ln in keep if re.search(r"PATHS|SIZE|DIFF|built|error|warning|Finished|Caching|<<<|>>>", ln)]
    body = summary if summary else keep[-tail:]
    if len(keep) > len(body):
        body = [f"… ({len(keep) - len(body)} lines elided; pass logs=true for full output)"] + body
    return "\n".join(body)


@mcp.tool()
async def rebuild(ctx: Context, flags: str = "", logs: bool = False) -> str:
    """Rebuild the IceDOS system (evaluates + builds the closure; never activates).

    Output is condensed to the build summary by default (the raw nom stream is huge);
    pass logs=true for the full, unabridged log. Build progress is streamed as MCP
    progress notifications so long closure builds keep the client's tool call alive.

    Args:
        flags: Additional build.sh flags, e.g. --boot, --update, --update-core,
               --update-nixpkgs, --update-repos, --build-vm, --genflake-only.
        logs: Return the full raw log instead of the condensed summary.
    """
    extra = flags.split() if flags else []

    ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\[\?2026[hl]")
    state = {"last": 0.0, "n": 0}

    async def _progress(line: str) -> None:
        # Throttle to one notification per 5s of meaningful output; skip nom's
        # per-frame redraw spam (timer ticks / graph frames).
        now = time.monotonic()
        if now - state["last"] < 5.0:
            return
        msg = ansi.sub("", line).strip()
        if not msg or re.match(r"^⏱|^┏|^┃|^┗|^┣", msg):
            return
        state["last"] = now
        state["n"] += 1
        try:
            await ctx.report_progress(progress=state["n"], total=None, message=msg[:120])
        except Exception:
            pass

    rc, stdout, stderr = await cli_rebuild(*extra, timeout=3600, on_line=_progress)
    out = (stdout + (f"\n--- stderr ---\n{stderr}" if stderr else "")) if logs else _summarize_build(stdout, stderr)
    return f"Build succeeded!\n{out}" if rc == 0 else f"Build failed (exit {rc}):\n{out}"


@mcp.tool()
async def get_diff() -> str:
    """Show pending config.toml changes since last rebuild."""
    rc, stdout, stderr = await cli_get_diff()
    if stdout:
        return stdout
    if "no config snapshots" in (stderr or ""):
        return "No previous build found."
    return "No pending changes."


@mcp.tool()
async def rollback(generation: int | None = None, dry: bool = False) -> str:
    """Roll back to a previous system generation.

    Args:
        generation: Target generation number (omit for previous).
        dry: Show the plan without changing anything.
    """
    rc, stdout, stderr = await cli_rollback(generation=generation, dry=dry)
    out = stdout + (f"\n{stderr}" if stderr else "")
    return out if rc == 0 else f"Rollback failed (exit {rc}):\n{out}"


@mcp.tool()
async def doctor() -> str:
    """Run IceDOS health checks (substituters, cache, hardware, store, gc, inputs)."""
    rc, stdout, stderr = await cli_doctor()
    out = stdout or stderr
    return out if rc == 0 else f"{out}\n\n(doctor reported failures — exit {rc})"


# ===========================================================================
# PACKAGE MANAGEMENT
# ===========================================================================

@mcp.tool()
async def list_packages() -> str:
    """List all installed packages on the system."""
    rc, stdout, stderr = await cli_list_packages()
    if rc != 0:
        return _err("cli_error", stderr.strip() or "pkgs list failed")
    return stdout


@mcp.tool()
async def run_package(attr: str, timeout: int = 300) -> str:
    """Run a package without installing it (blocks until it exits or times out).

    Not for long-lived GUI apps — the call blocks. For those, launch out-of-band.

    Args:
        attr: Package attribute (e.g. 'firefox', 'btop').
        timeout: Max seconds to wait before killing the process (default 300).
    """
    try:
        rc, stdout, stderr = await cli_run_package(attr, timeout=timeout)
    except TimeoutError as e:
        return _err("timeout", str(e))
    return stdout + (f"\n{stderr}" if stderr else "")


# ===========================================================================
# REPO EXPLORER
# ===========================================================================

@mcp.tool()
async def explore_repo(url: str) -> str:
    """Explore an IceDOS module repo — METADATA ONLY (checks local override, then GitHub).

    Returns per-module meta.name/description/dependencies (regex-scraped from icedos.nix)
    and config.toml default values. It does NOT return source: for actual file contents
    (scripts.nix, options.nix, packages.nix, ...) use `list_repo_tree` to see what exists
    and `read_repo_file` to read it.

    Args:
        url: Repository URL (e.g. 'github:icedos/apps').
    """
    try:
        modules = await explore_github_repo(url)
        if not modules:
            return f"No modules found in {url}."
        return json.dumps(modules, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def explore_local_repo(path: str) -> str:
    """Explore a local IceDOS module repo checkout — METADATA ONLY.

    Scans modules/*/icedos.nix for meta and reads config.toml defaults. It does NOT
    return source: for actual file contents use `list_repo_tree` + `read_repo_file`.

    Args:
        path: Absolute path to the local repo checkout.
    """
    try:
        modules = _explore_local_repo(path)
        if not modules:
            return f"No modules found in {path}."
        return json.dumps(modules, indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def read_repo_file(repo: str, path: str, offset: int = 0, limit: int = 0) -> str:
    """Read the raw contents of a single file in an IceDOS repo — local OR online.

    Returns ACTUAL FILE SOURCE (e.g. scripts.nix, options.nix, packages.nix), unlike
    explore_repo/explore_local_repo which return only metadata. Use this to read a
    module's real implementation instead of inferring it from meta.

    Args:
        repo: A local path ('/home/ice/Projects/icedos/apps' or 'path:...'), or a
              'github:owner/repo[/ref]' URL. GitHub URLs with a path: overrideUrl in
              config.toml are served from the local checkout automatically.
        path: Repo-relative file path (e.g. 'modules/steam/scripts.nix').
        offset: 0-based first line to return (default 0 = from start).
        limit: Max lines to return (0 = all).
    """
    try:
        text = await modules_read_repo_file(repo, path)
    except FileNotFoundError as e:
        return _err("not_found", str(e))
    except ValueError as e:
        return _err("bad_path", str(e))
    except Exception as e:
        return _err("read_error", str(e))
    if offset or limit:
        lines = text.splitlines()
        text = "\n".join(lines[offset : (offset + limit) if limit else len(lines)])
    return text


@mcp.tool()
async def list_repo_tree(repo: str, subdir: str = "") -> str:
    """List every file path in an IceDOS repo — local OR online.

    Use this to discover which files a module ships (scripts.nix, options.nix,
    packages.nix, ...) before fetching one with `read_repo_file`.

    Args:
        repo: Local path / 'path:...' / 'github:owner/repo[/ref]' (override-aware).
        subdir: Optional path prefix to filter (e.g. 'modules/steam').
    """
    try:
        files = await modules_list_repo_tree(repo, subdir)
    except FileNotFoundError as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("read_error", str(e))
    if not files:
        return _err("not_found", f"no files in {repo}" + (f"/{subdir}" if subdir else ""))
    return json.dumps({"repo": repo, "count": len(files), "files": files}, indent=2)


# ===========================================================================
# SYSTEM INFO
# ===========================================================================

@mcp.tool()
async def get_system_info_tool() -> str:
    """Get hostname, kernel, uptime, current generation, and store disk space."""
    try:
        return json.dumps(await get_system_info(), indent=2)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_generations_tool() -> str:
    """List all system generations with dates and the config snapshot that built each."""
    try:
        gens = await get_generations()
        if not gens:
            return "No generations found."
        current = await get_current_generation()
        lines = []
        for g in gens:
            marker = " (current)" if g["generation"] == current else ""
            snap = f"  [{g['config_snapshot']}]" if g.get("config_snapshot") else ""
            lines.append(f"  Generation {g['generation']}: {g['date']}{marker}{snap}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def list_inputs() -> str:
    """Flake inputs that built the running system — pinned rev + freshness.

    Parses .state/flake.lock: every repo, nixpkgs, home-manager, etc. with its
    locked rev, source, and age (how stale). Broader than doctor's two checks.
    """
    try:
        return json.dumps(sys_list_flake_inputs(), indent=2)
    except FileNotFoundError:
        return _err("no_lock", "no .state/flake.lock; run 'icedos rebuild' first")
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
async def diff_generations(generation_a: int, generation_b: int) -> str:
    """Diff the config set that built two generations.

    Compares every config file (config.toml + all extra-config dirs) from each
    generation's snapshot, not just config.toml.

    Args:
        generation_a: First generation number.
        generation_b: Second generation number.
    """
    try:
        return sys_diff_generations(generation_a, generation_b)
    except (KeyError, FileNotFoundError) as e:
        return _err("not_found", str(e))
    except Exception as e:
        return _err("read_error", str(e))


# ===========================================================================
# STATE FILES
# ===========================================================================

@mcp.tool()
async def list_state_files(subdir: str = "") -> str:
    """List files and directories under .state/.

    Shows what generated state files exist: flake.nix, flake.lock,
    .cache/ snapshots, generation records, etc.

    Args:
        subdir: Optional subdirectory path within .state/ (e.g. '.cache/generations').
    """
    try:
        state = _state_dir()
        target = state / subdir if subdir else state
        target_resolved = target.resolve()
        state_resolved = state.resolve()
        if not str(target_resolved).startswith(str(state_resolved)):
            return _err("bad_path", "path traversal not allowed")
        if not target.is_dir():
            return _err("not_found", f"{subdir or '.state/'} is not a directory or does not exist")
        entries = []
        for entry in sorted(target.iterdir()):
            entry_type = "dir" if entry.is_dir() else "file"
            size = entry.stat().st_size if entry.is_file() else None
            rel = entry.relative_to(state)
            entries.append({
                "name": str(rel),
                "type": entry_type,
                "size": size,
            })
        return json.dumps({"count": len(entries), "entries": entries}, indent=2)
    except Exception as e:
        return _err("read_error", str(e))


@mcp.tool()
async def read_state_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read a file from .state/.

    Useful for inspecting the generated flake.nix, flake.lock, build.sh,
    or cached snapshots and indices.

    Args:
        path: File path relative to .state/ (e.g. 'flake.nix', '.cache/options-doc.json').
        offset: 0-based first line to return (default 0 = from start).
        limit: Max lines to return (0 = all).
    """
    try:
        state = _state_dir()
        target = state / path
        target_resolved = target.resolve()
        state_resolved = state.resolve()
        if not str(target_resolved).startswith(str(state_resolved)):
            return _err("bad_path", "path traversal not allowed")
        if not target.is_file():
            return _err("not_found", f"'{path}' not found in .state/")
        text = target.read_text()
        if offset or limit:
            lines = text.splitlines()
            text = "\n".join(lines[offset : (offset + limit) if limit else len(lines)])
        return text
    except Exception as e:
        return _err("read_error", str(e))


# ===========================================================================
# RESOURCES
# ===========================================================================

@mcp.resource("icedos://config")
def resource_config() -> str:
    """The full merged config (config.toml + every enabled configs/*.toml)."""
    try:
        return get_config_toml_string()
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("icedos://config/files")
def resource_config_files() -> str:
    """The loaded config file set — config.toml + enabled configs/*.toml, in
    merge order, as config-root-relative paths."""
    try:
        root = find_config_root()
        return json.dumps(
            [str(p.relative_to(root)) for p in config_files.list_config_files(root)],
            indent=2,
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.resource("icedos://options")
async def resource_options() -> str:
    """All IceDOS options with types, descriptions, and values."""
    await _ensure_search_index()
    return json.dumps(_load_index("options-doc.json"), indent=2)


@mcp.resource("icedos://modules")
async def resource_modules() -> str:
    """Full module graph across all repositories."""
    await _ensure_search_index()
    return json.dumps(_load_index("modules-doc.json"), indent=2)


@mcp.resource("icedos://generations")
async def resource_generations() -> str:
    """System generations list."""
    return json.dumps(await get_generations(), indent=2)


@mcp.resource("icedos://system-info")
async def resource_system_info() -> str:
    """Current system state."""
    return json.dumps(await get_system_info(), indent=2)


@mcp.resource("icedos://repo/{url}")
async def resource_repo(url: str) -> str:
    """Module metadata for a specific repository.

    The URL is percent-encoded in the URI (e.g. 'github%3Aicedos%2Fapps');
    decode it before handing off to the GitHub explorer.
    """
    try:
        return json.dumps(await explore_github_repo(unquote(url)), indent=2)
    except Exception as e:
        return f"Error: {e}"


# ===========================================================================
# PROMPTS
# ===========================================================================

@mcp.prompt()
def configure_module(module_name: str, module_url: str = "") -> str:
    """Step-by-step guide to configure an IceDOS module."""
    return get_prompt("configure_module", module_name=module_name, module_url=module_url or "github:icedos/apps")


@mcp.prompt()
def troubleshoot(issue: str = "unknown issue") -> str:
    """Diagnostic workflow for common IceDOS issues."""
    return get_prompt("troubleshoot", issue=issue)


@mcp.prompt()
def add_application(app_name: str) -> str:
    """Guide to add a new application to the system."""
    return get_prompt("add_application", app_name=app_name)


@mcp.prompt()
def icedos_overview() -> str:
    """Overview of IceDOS architecture and capabilities."""
    return get_prompt("icedos_overview")


# ===========================================================================

def main() -> None:
    """Entry point."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
