{
  outputs =
    { ... }:
    {
      icedosModules =
        { icedosLib, ... }:
        icedosLib.scanModules {
          path = ./.;
          filename = "icedos.nix";
        };
    };
}
