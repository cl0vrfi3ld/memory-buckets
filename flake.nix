{
  description = "memory-buckets: a Claude-style memory provider plugin for Hermes Agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    devshell = {
      url = "github:numtide/devshell";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [ inputs.devshell.flakeModule ];

      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      perSystem =
        { pkgs, self', ... }:
        let
          # For `nix build` and the checks. Hermes needs the package built with its own
          # interpreter: see README, Nix.
          python = pkgs.python314;
        in
        {
          formatter = pkgs.nixfmt-tree;

          packages = rec {
            memory-buckets = python.pkgs.callPackage ./nix/package.nix { };
            default = memory-buckets;
          };

          checks = {
            # Builds the wheel and runs the test suite.
            package = self'.packages.memory-buckets;
            # Token-for-token parity with Hugging Face's tokenizers, which the package's
            # own build doesn't pull in.
            tokenizer = self'.packages.memory-buckets.overridePythonAttrs (old: {
              nativeCheckInputs = old.nativeCheckInputs ++ [ python.pkgs.tokenizers ];
            });
          };

          devshells.default = {
            devshell = {
              name = "hermes-memory-buckets";
              motd = ''
                {202}🔨 hermes-memory-buckets{reset}

                $(type -p menu &>/dev/null && menu)
              '';
            };

            # Bare interpreter — uv manages the venv and dependencies.
            packages = [
              pkgs.python314
              pkgs.uv
              pkgs.ruff
            ];

            env = [
              # uv keeps its venv inside the project instead of ~/.cache.
              {
                name = "UV_PROJECT_ENVIRONMENT";
                eval = "$PRJ_ROOT/.venv";
              }
              # Use the devshell's Python, never a uv-downloaded one.
              {
                name = "UV_PYTHON_PREFERENCE";
                value = "only-system";
              }
              # Wheels with compiled extensions need their libraries findable.
              {
                name = "LD_LIBRARY_PATH";
                prefix = "${pkgs.lib.makeLibraryPath [
                  pkgs.stdenv.cc.cc.lib
                  pkgs.zlib
                ]}";
              }
            ];

            commands = [
              {
                name = "sync";
                category = "build";
                help = "uv sync — create/update the project venv";
                command = ''cd "$PRJ_ROOT" && uv sync "$@"'';
              }
              {
                name = "run";
                category = "run";
                help = "uv run — run a command in the project venv";
                command = ''cd "$PRJ_ROOT" && uv run "$@"'';
              }
              {
                name = "check";
                category = "test";
                help = "Run the test suite";
                command = ''cd "$PRJ_ROOT" && uv run pytest tests/ "$@"'';
              }
              {
                name = "lint";
                category = "test";
                help = "ruff check + format --check";
                command = ''cd "$PRJ_ROOT" && ruff check . && ruff format --check .'';
              }
            ];
          };
        };
    };
}
