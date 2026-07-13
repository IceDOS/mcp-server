{ ... }:

{
  outputs.nixosModules =
    { ... }:
    [
      (
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          icedosMcpServer = pkgs.python3Packages.buildPythonApplication {
            pname = "icedos-mcp-server";
            version = "0.1.0";
            pyproject = true;

            src = builtins.path {
              path = ./src;
              name = "icedos-mcp-server-src";
              filter =
                path: _:
                let
                  rel = lib.removePrefix (toString ./src + "/") (toString path);
                in
                rel == "pyproject.toml" || rel == "icedos_mcp" || lib.hasPrefix "icedos_mcp/" rel;
            };

            build-system = [ pkgs.python3Packages.hatchling ];

            dependencies = with pkgs.python3Packages; [
              mcp
              tomlkit
              httpx
            ];

            pythonImportsCheck = [ "icedos_mcp" ];

            mainProgram = "icedos-mcp-server";
          };
        in
        {
          environment.systemPackages = [ icedosMcpServer ];

          icedos.system.toolset.commands = [
            {
              command = "mcp";
              help = "MCP server for AI agent integration";

              commands = [
                {
                  command = "serve";
                  help = "start the IceDOS MCP server (stdio transport)";
                  script = "ICEDOS_CONFIG_DIR=${config.icedos.configurationLocation}/.. exec ${icedosMcpServer}/bin/icedos-mcp-server";
                  completion.files = false;
                }
              ];
            }
          ];
        }
      )
    ];

  meta = {
    name = "default";
    description = "MCP server for AI agent integration with IceDOS";
  };
}
