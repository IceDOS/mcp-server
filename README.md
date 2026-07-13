# IceDOS MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes the IceDOS ecosystem —
configuration, modules, options, and system operations — to AI agents. Built on
[FastMCP](https://github.com/jlowin/fastmcp).

## Running

Packaged as an IceDOS module (`icedos.nix`). Once the system is rebuilt it is
available as a toolset command:

```sh
icedos mcp serve      # start the server over stdio
```

The command sets `ICEDOS_CONFIG_DIR` to the active configuration root before
launching. Run directly for development:

```sh
ICEDOS_CONFIG_DIR=/path/to/config icedos-mcp-server
```

### Config discovery

The server locates the IceDOS config root by honoring `ICEDOS_CONFIG_DIR`, then
falling back to walking up from the working directory for a `flake.nix` root
(`config.toml` is **optional** — a root may be all `configs/*.toml` + `modules/`; the
walk-up also requires one of `config.toml`/`configs/`/`modules/` to skip unrelated flakes).
Config edits use `tomlkit` for round-trip preservation, and new tables are spaced with one
blank line between blocks to match the hand-written style.

## Capabilities

### Tools (27)

**Config** — user config is the *merged* set `config.toml` + every enabled `*.toml` under `icedos.system.extraConfigs` (default `configs/`); hidden `configs/.*.toml` load too but are gitignored; a file opts out with top-level `enable = false`. Reads span the whole set; writes land in the file that declares the key (else `config.toml`), or a `file=` you name.
- `get_config` — read the merged config (whole or by `section`); `file=` reads one file
- `get_config_value` — read one value by dotted path (merged); `resolved=true` returns the effective option value/type from the index (not just declared TOML)
- `set_config_value` — write a value to its owning file (or `file=`); optional eval-validation via genflake, auto-revert on failure
- `delete_config_value` — remove a key, or an array item via `path|item` syntax (type-coerced)
- `list_repositories` — list `[[icedos.repositories]]` entries (merged across all config files)
- `add_repository` — append a repository entry (`override_url`, `patches`, `fetch_deps`, `fetch_optional_deps`)
- `edit_user` — add/update an `[icedos.users.<name>]` entry

**Modules**
- `enable_module` / `disable_module` — add/remove a module in its owning repo's `modules` list (repo auto-resolved from the index)
- `module_graph` — forward deps + reverse dependents for a module

**Query & discovery**
- `search_options` / `search_modules` — relevance-ranked, paginated (`{total,count,offset,truncated,results}` + `limit`/`offset`)
- `get_option` — full option details + paste-ready TOML snippet
- `get_module` / `list_modules` — module details / list (markers: ● explicit, ◐ dependency, ○ available)

**System**
- `rebuild` — build the configuration (never activates); condensed summary by default, `logs=true` for the full log
- `get_diff` — pending `config.toml` changes since last build
- `rollback` — roll back to a previous generation (`dry` for a plan-only run)
- `doctor` — health checks (substituters, cache, hardware, store, gc, inputs)
- `list_inputs` — flake.lock inputs that built the running system: pinned rev + freshness
- `diff_generations` — config.toml diff between two generations

**Packages**
- `list_packages` — installed packages
- `run_package` — run a package without installing it (blocking; `timeout` configurable)

**Repo explorer**
- `explore_repo` — inspect a module repo (local override first, else GitHub API)
- `explore_local_repo` — inspect a local repo checkout

**System info**
- `get_system_info_tool` — hostname, kernel, uptime, generation, store space
- `get_generations_tool` — generations with dates + the config snapshot that built each

### Resources (7)

`icedos://config`, `icedos://config/files`, `icedos://options`, `icedos://modules`,
`icedos://generations`, `icedos://system-info`, and the templated
`icedos://repo/{url}` (URL is percent-encoded, e.g. `icedos://repo/github%3Aicedos%2Fapps`).

### Prompts (4)

`configure_module`, `troubleshoot`, `add_application`, `icedos_overview`.

## Layout

```
mcp-server/
  flake.nix          # IceDOS module scan entry point
  icedos.nix         # packages the server, wires the `icedos mcp serve` command
  src/
    pyproject.toml   # hatchling build; package = icedos_mcp
    icedos_mcp/
      server.py      # tool/resource/prompt definitions (FastMCP)
      config.py      # config read/edit across config.toml + configs/*.toml (tomlkit round-trip)
      config_files.py # config file-set enumeration + merge (mirrors core lib/config-files.nix)
      cli.py         # subprocess wrappers for the icedos CLI
      modules.py     # repo/module exploration
      system.py      # system info & generations
      prompts.py     # prompt templates
```

## Development

```sh
cd src
python -m build --wheel --no-isolation   # build a wheel (hatchling)
python -m py_compile icedos_mcp/*.py      # quick syntax check
```

After editing any `.nix` file, format the tree from the IceDOS root with `icedos nixf .`.
