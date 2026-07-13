"""Module discovery: fetch metadata from online repos (GitHub API) or local checkouts."""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import httpx

from . import config_files

logger = logging.getLogger("icedos-mcp.modules")

# In-memory cache for explored repos
_repo_cache: dict[str, list[dict[str, Any]]] = {}


def _parse_github_url(url: str) -> tuple[str, str, str | None]:
    """Parse a flake-style github URL into (owner, repo, ref)."""
    rest = url.removeprefix("github:")
    parts = rest.split("/", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")
    return parts[0], parts[1], parts[2] if len(parts) > 2 else None


def _parse_meta_from_icedos_nix(content: str) -> dict[str, Any]:
    """Extract meta.* fields from an icedos.nix file using regex."""
    result: dict[str, Any] = {}

    m = re.search(r'meta\.name\s*=\s*"([^"]*)"', content)
    if m:
        result["name"] = m.group(1)

    m = re.search(r'meta\.description\s*=\s*"([^"]*)"', content)
    if m:
        result["description"] = m.group(1)

    for field in ("meta.dependencies", "meta.optionalDependencies"):
        key = field.split(".")[-1]
        deps = _extract_dependency_list(content, field)
        if deps:
            result[key] = deps

    return result


def _extract_dependency_list(content: str, field: str) -> list[dict[str, Any]]:
    """Extract a dependency list from icedos.nix content."""
    pattern = rf'{re.escape(field)}\s*=\s*\['
    m = re.search(pattern, content)
    if not m:
        return []

    start = m.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "[":
            depth += 1
        elif content[i] == "]":
            depth -= 1
        i += 1

    block = content[start : i - 1]
    deps = []
    for entry_match in re.finditer(r"\{([^}]*)\}", block):
        entry_text = entry_match.group(1)
        dep: dict[str, Any] = {}

        url_match = re.search(r'url\s*=\s*"([^"]*)"', entry_text)
        if url_match:
            dep["url"] = url_match.group(1)

        modules_match = re.search(r'modules\s*=\s*\[([^\]]*)\]', entry_text)
        if modules_match:
            dep["modules"] = re.findall(r'"([^"]*)"', modules_match.group(1))

        if dep:
            deps.append(dep)

    return deps


async def _fetch_raw_file(
    client: httpx.AsyncClient, owner: str, repo: str, branch: str, path: str
) -> str | None:
    """Fetch a single file's raw content from GitHub."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    resp = await client.get(url, timeout=15)
    return resp.text if resp.status_code == 200 else None


def _resolve_override_path(url: str) -> str | None:
    """Check the config for a local path: override for the given repo URL.

    Searches the full config set (config.toml + configs/*.toml), so a repo
    declared in configs/repositories.toml is found. Returns the local path if
    overrideUrl starts with 'path:', else None.
    """
    try:
        env_dir = os.environ.get("ICEDOS_CONFIG_DIR")
        if env_dir:
            root = Path(env_dir)
        else:
            current = Path.cwd()
            root = None
            for parent in [current, *current.parents]:
                # config.toml is optional — key on flake.nix + a config marker.
                if (parent / "flake.nix").exists() and (
                    (parent / "config.toml").exists()
                    or (parent / "configs").is_dir()
                    or (parent / "modules").is_dir()
                ):
                    root = parent
                    break
            if root is None:
                return None

        config = config_files.merged_config(root)
        repos = config.get("icedos", {}).get("repositories", [])
        for repo_entry in repos:
            if repo_entry.get("url") == url:
                override = repo_entry.get("overrideUrl", "")
                if override.startswith("path:"):
                    path = override[5:]
                    if Path(path).exists():
                        logger.info("Resolved %s -> local path %s", url, path)
                        return path
                    logger.warning("Override path %s does not exist, falling back to GitHub", path)
                    return None
        return None
    except Exception as e:
        logger.debug("Could not resolve override for %s: %s", url, e)
        return None


async def explore_github_repo(url: str) -> list[dict[str, Any]]:
    """Explore an IceDOS module repo on GitHub.

    Checks config.toml for a local path: override first. If found, reads
    the local checkout. Otherwise fetches from GitHub API.
    """
    if url in _repo_cache:
        return _repo_cache[url]

    local_path = _resolve_override_path(url)
    if local_path:
        modules = explore_local_repo(local_path)
        _repo_cache[url] = modules
        return modules

    owner, repo, ref = _parse_github_url(url)
    branch = ref or "main"
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    async with httpx.AsyncClient(timeout=30) as client:
        tree_resp = await client.get(
            f"{api_base}/git/trees/{branch}",
            params={"recursive": "1"},
            headers={"Accept": "application/vnd.github.v3+json"},
        )

        if tree_resp.status_code != 200:
            return await _explore_github_raw_fallback(client, owner, repo, branch, url)

        tree = tree_resp.json()
        module_dirs: dict[str, list[str]] = {}

        for item in tree.get("tree", []):
            path = item["path"]
            m = re.match(r"^(?:modules/)?([^/]+)/((?:icedos\.nix|config\.toml))$", path)
            if m:
                d, f = m.group(1), m.group(2)
                module_dirs.setdefault(d, []).append(f)
            elif path in ("icedos.nix", "config.toml"):
                module_dirs.setdefault("_root", []).append(path)

        modules = []
        for dir_path, files in module_dirs.items():
            module_info: dict[str, Any] = {
                "repo": url,
                "dir": dir_path,
                "name": dir_path if dir_path != "_root" else "default",
                "description": "",
                "dependencies": [],
                "optionalDependencies": [],
                "defaults": {},
            }

            prefix = f"modules/{dir_path}" if dir_path != "_root" else ""

            if "config.toml" in files:
                try:
                    raw = await _fetch_raw_file(
                        client, owner, repo, branch,
                        f"{prefix}/config.toml" if prefix else "config.toml",
                    )
                    if raw:
                        parsed = tomllib.loads(raw)
                        icedos_data = parsed.get("icedos", {})
                        for cat in ("applications", "hardware", "desktop", "tweaks", "providers"):
                            if module_info["name"] in icedos_data.get(cat, {}):
                                module_info["defaults"] = icedos_data[cat][module_info["name"]]
                                break
                        else:
                            module_info["defaults"] = icedos_data
                except Exception:
                    pass

            if "icedos.nix" in files:
                try:
                    raw = await _fetch_raw_file(
                        client, owner, repo, branch,
                        f"{prefix}/icedos.nix" if prefix else "icedos.nix",
                    )
                    if raw:
                        module_info.update(_parse_meta_from_icedos_nix(raw))
                except Exception:
                    pass

            modules.append(module_info)

    _repo_cache[url] = modules
    return modules


async def _explore_github_raw_fallback(
    client: httpx.AsyncClient, owner: str, repo: str, branch: str, url: str
) -> list[dict[str, Any]]:
    """Fallback: try well-known module names via raw content API."""
    known = [
        "btop", "steam", "me3", "sunshine", "firefox", "discord",
        "radeon", "nvidia", "intel", "pipewire", "bluetooth", "kernel", "zram",
        "gdm", "stylix", "displays", "plm",
    ]

    modules = []
    for name in known:
        try:
            config_raw = await _fetch_raw_file(client, owner, repo, branch, f"modules/{name}/config.toml")
            icedos_raw = await _fetch_raw_file(client, owner, repo, branch, f"modules/{name}/icedos.nix")
        except Exception:
            continue

        if config_raw is None and icedos_raw is None:
            continue

        module_info: dict[str, Any] = {
            "repo": url, "dir": f"modules/{name}", "name": name,
            "description": "", "dependencies": [], "optionalDependencies": [], "defaults": {},
        }

        if config_raw:
            try:
                parsed = tomllib.loads(config_raw)
                icedos_data = parsed.get("icedos", {})
                for cat in ("applications", "hardware", "desktop", "tweaks", "providers"):
                    if name in icedos_data.get(cat, {}):
                        module_info["defaults"] = icedos_data[cat][name]
                        break
                else:
                    module_info["defaults"] = icedos_data
            except Exception:
                pass

        if icedos_raw:
            module_info.update(_parse_meta_from_icedos_nix(icedos_raw))

        modules.append(module_info)

    if modules:
        _repo_cache[url] = modules
    return modules


def explore_local_repo(repo_path: str) -> list[dict[str, Any]]:
    """Explore a local IceDOS module repo checkout."""
    root = Path(repo_path)
    if not root.exists():
        raise FileNotFoundError(f"Local repo path not found: {repo_path}")

    modules: list[dict[str, Any]] = []
    modules_dir = root / "modules"

    if modules_dir.exists() and modules_dir.is_dir():
        for module_dir in sorted(modules_dir.iterdir()):
            if not module_dir.is_dir():
                continue

            icedos_nix = module_dir / "icedos.nix"
            config_toml = module_dir / "config.toml"

            if not icedos_nix.exists() and not config_toml.exists():
                continue

            module_info: dict[str, Any] = {
                "repo": f"path:{root}", "dir": f"modules/{module_dir.name}",
                "name": module_dir.name, "description": "",
                "dependencies": [], "optionalDependencies": [], "defaults": {},
            }

            if config_toml.exists():
                try:
                    parsed = tomllib.loads(config_toml.read_text())
                    icedos_data = parsed.get("icedos", {})
                    for cat in ("applications", "hardware", "desktop", "tweaks", "providers"):
                        if module_dir.name in icedos_data.get(cat, {}):
                            module_info["defaults"] = icedos_data[cat][module_dir.name]
                            break
                    else:
                        module_info["defaults"] = icedos_data
                except Exception:
                    pass

            if icedos_nix.exists():
                try:
                    module_info.update(_parse_meta_from_icedos_nix(icedos_nix.read_text()))
                except Exception:
                    pass

            modules.append(module_info)

    root_icedos = root / "icedos.nix"
    if root_icedos.exists():
        module_info: dict[str, Any] = {
            "repo": f"path:{root}", "dir": "_root", "name": root.name,
            "description": "", "dependencies": [], "optionalDependencies": [], "defaults": {},
        }
        try:
            module_info.update(_parse_meta_from_icedos_nix(root_icedos.read_text()))
        except Exception:
            pass
        modules.insert(0, module_info)

    return modules


# ---------------------------------------------------------------------------
# Raw file access — actual source, not just metadata
# ---------------------------------------------------------------------------

def _resolve_repo_local_root(repo: str) -> str | None:
    """Local filesystem root for a `repo` arg, or None if it's an online GitHub ref.

    Accepts an absolute/relative local path, a 'path:...' prefix, or a 'github:' URL
    that has a `path:` overrideUrl in config.toml (local override wins over GitHub).
    """
    if repo.startswith("path:"):
        p = repo[len("path:") :]
        return p if Path(p).exists() else None
    if repo.startswith("github:"):
        return _resolve_override_path(repo)
    return repo if Path(repo).exists() else None


def _safe_join(root: str, rel: str) -> Path:
    """Join `rel` under `root`, refusing paths that escape the repo root."""
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:
        raise ValueError(f"path escapes repo root: {rel}")
    return target


def _path_candidates(path: str) -> list[str]:
    """Try `path` as given, then under `modules/`.

    Module `source`/`declaredAt` from the option index are module-root-relative
    (e.g. 'btop/icedos.nix') because the scanned store copy drops the source repo's
    `modules/` wrapper, while a source checkout has 'modules/btop/icedos.nix'. Accept
    either so those pointers are directly usable.
    """
    return [path] if path.startswith("modules/") else [path, f"modules/{path}"]


async def read_repo_file(repo: str, path: str) -> str:
    """Return the raw text of a single file in an IceDOS repo (local or online).

    Local/override repos are read from disk; a bare 'github:' ref is fetched from
    raw.githubusercontent.com. `path` may be repo-relative ('modules/btop/icedos.nix')
    or module-root-relative ('btop/icedos.nix'). Raises FileNotFoundError if absent.
    """
    local_root = _resolve_repo_local_root(repo)
    if local_root is not None:
        for cand in _path_candidates(path):
            target = _safe_join(local_root, cand)
            if target.is_file():
                return target.read_text()
        raise FileNotFoundError(f"'{path}' not found in {repo}")

    owner, repo_name, ref = _parse_github_url(repo)
    branch = ref or "main"
    async with httpx.AsyncClient(timeout=30) as client:
        for cand in _path_candidates(path):
            content = await _fetch_raw_file(client, owner, repo_name, branch, cand)
            if content is not None:
                return content
    raise FileNotFoundError(f"'{path}' not found in {repo} (branch {branch})")


async def list_repo_tree(repo: str, subdir: str = "") -> list[str]:
    """List every file path in an IceDOS repo (local or online), optionally under `subdir`.

    Local/override repos are walked on disk (skipping .git); a bare 'github:' ref uses
    the GitHub trees API (recursive). Returns repo-relative paths, sorted.
    """
    local_root = _resolve_repo_local_root(repo)
    if local_root is not None:
        base = Path(local_root).resolve()
        start = _safe_join(local_root, subdir) if subdir else base
        if not start.exists():
            raise FileNotFoundError(f"'{subdir}' not found in {repo}")
        skip = {".git", "__pycache__"}
        return sorted(
            str(rel)
            for p in start.rglob("*")
            if p.is_file() and not (skip & set((rel := p.relative_to(base)).parts))
        )

    owner, repo_name, ref = _parse_github_url(repo)
    branch = ref or "main"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo_name}/git/trees/{branch}",
            params={"recursive": "1"},
            headers={"Accept": "application/vnd.github.v3+json"},
        )
    if resp.status_code != 200:
        raise FileNotFoundError(f"could not list {repo} (branch {branch}): HTTP {resp.status_code}")
    files = [item["path"] for item in resp.json().get("tree", []) if item.get("type") == "blob"]
    if subdir:
        pref = subdir.strip("/")
        files = [f for f in files if f == pref or f.startswith(pref + "/")]
    return sorted(files)


