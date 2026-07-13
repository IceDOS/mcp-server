"""Agent prompt templates for common IceDOS tasks."""

from __future__ import annotations


PROMPTS = {
    "configure_module": {
        "description": "Step-by-step guide to configure an IceDOS module",
        "template": """\
You are helping configure an IceDOS module.

Module: {module_name}
Repository: {module_url}

Steps:
1. Check if the module is already enabled: use `list_modules` (or `module_graph`) to see its status.
2. If not enabled, use `enable_module` (it adds the module to the owning repo's list). If the repo itself isn't in config yet, use `add_repository`.
3. Review the module's default options: use `get_module` to see its defaults.
4. Customize options as needed using `set_config_value`.
5. Validate the config by checking the build would succeed.
6. Inform the user to run `icedos rebuild` to apply changes.

Current config: use `get_config` to read the current state.
Available options: use `search_options` with the module name to find its options.
""",
    },
    "troubleshoot": {
        "description": "Diagnostic workflow for common IceDOS issues",
        "template": """\
You are diagnosing an IceDOS system issue.

Issue reported: {issue}

Diagnostic steps:
1. Run `doctor` to check system health (substituters, cache, hardware, store, gc, inputs).
2. Check pending changes with `get_diff` — recent config changes may be the cause.
3. Check the current system info with `get_system_info_tool`.
4. If it's a build failure, use `rebuild` with `logs=True` for verbose output.
5. If it's a config issue, use `search_options` to verify option names and types.
6. If the system is broken, suggest `rollback` to the last working generation.

Always check `doctor` first — it catches 80% of common issues.
""",
    },
    "add_application": {
        "description": "Guide to add a new application to the system",
        "template": """\
You are helping add an application to the IceDOS system.

Application to add: {app_name}

Steps:
1. Search for the module: use `search_modules` to find "{app_name}" across all repos.
2. If found, check which repo it's in and whether it's already enabled.
3. If not in config, use `explore_repo` on the repo to see the module's details.
4. Enable it with `enable_module` (adds it to the owning repo's `modules` list).
5. Review the module's options with `search_options` using the module name.
6. Configure any options the user needs.
7. Validate the configuration.
8. Inform the user to run `icedos rebuild` to install.

If the module isn't found, check:
- Is the repo listed in the config? Use `list_repositories` (spans config.toml + configs/*.toml).
- Try `explore_repo` on `github:icedos/apps` to see all available app modules.
""",
    },
    "icedos_overview": {
        "description": "Overview of IceDOS architecture and capabilities",
        "template": """\
IceDOS is a NixOS framework for reproducible, gaming-friendly Linux systems.

Architecture:
- Users describe their machine in `config.toml` + autoloaded `configs/*.toml`
- The `icedos` CLI turns it into a NixOS system via `icedos rebuild`
- Modules come from external repos (github:icedos/apps, etc.)
- Each module has options under `icedos.<category>.<name>`
- Raw NixOS options pass through directly (any top-level table not starting with `icedos.`)

Key concepts:
- **Repositories**: collections of modules fetched by URL
- **Modules**: features you toggle on by name (e.g. "steam", "btop")
- **Generations**: saved system versions, rollbackable
- **Config**: `config.toml` (base) + autoloaded `configs/*.toml` (merged), edited then rebuilt; hidden `configs/.*.toml` stay off git
- **Binary cache**: pre-built packages from the IceDOS server

Available tools:
- Config: `get_config`, `get_config_value`, `set_config_value`, `delete_config_value`, `list_repositories`, `add_repository`, `edit_user`
- Modules: `enable_module`, `disable_module`, `module_graph` (prefer these over hand-editing the repo `modules` list)
- Query: `search_options`, `get_option`, `search_modules`, `get_module`, `list_modules`
- System: `rebuild`, `get_diff`, `rollback`, `doctor`, `list_inputs`, `diff_generations`
- Packages: `list_packages`, `run_package`
- Explorer: `explore_repo`, `explore_local_repo`
- Info: `get_system_info_tool`, `get_generations_tool`

Resources: `icedos://config`, `icedos://config/files`, `icedos://options`, `icedos://modules`, `icedos://generations`, `icedos://system-info`, `icedos://repo/{url}`
""",
    },
}


def get_prompt(name: str, **kwargs: str) -> str:
    """Get a formatted prompt by name."""
    if name not in PROMPTS:
        available = ", ".join(PROMPTS.keys())
        raise ValueError(f"Unknown prompt '{name}'. Available: {available}")
    return PROMPTS[name]["template"].format(**kwargs)
