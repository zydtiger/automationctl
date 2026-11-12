{
  description = "Standalone scheduled tasks with systemd and launchd";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      # Dependencies come from uv.lock, so the Nix build installs exactly what
      # `uv sync` does.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      mkPackage =
        pkgs:
        let
          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              python = pkgs.python3;
            }).overrideScope
              (
                lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
                ]
              );
          inherit (pkgs.callPackage pyproject-nix.build.util { }) mkApplication;
          testEnv = pythonSet.mkVirtualEnv "automationctl-test-env" {
            automationctl = [ ];
            psutil = [ ];
            pytest = [ ];
          };
          schedulerPath =
            if pkgs.stdenv.hostPlatform.isDarwin then "/bin:/usr/bin" else lib.makeBinPath [ pkgs.systemd ];
        in
        (mkApplication {
          venv = pythonSet.mkVirtualEnv "automationctl-env" { automationctl = [ ]; };
          package = pythonSet.automationctl;
        }).overrideAttrs
          (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.makeWrapper ];
            # Activation scripts run with a restricted PATH; scheduler control is runtime work.
            postFixup = (old.postFixup or "") + ''
              for command in automationctl actl; do
                wrapProgram "$out/bin/$command" \
                  --suffix PATH : ${lib.escapeShellArg schedulerPath} \
                  --set-default AUTOMATIONCTL_EXECUTABLE "$out/bin/automationctl"
              done
            '';
            doInstallCheck = true;
            installCheckPhase = ''
              runHook preInstallCheck
              export HOME="$TMPDIR/home"
              mkdir -p "$HOME"
              cp -r --no-preserve=mode ${./tests} tests
              # tests/test_home_manager_sync.py loads the module's sync script.
              cp -r --no-preserve=mode ${./nix} nix
              ${lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
                # The Linux sandbox has no system coreutils or zoneinfo database.
                export TZDIR=${pkgs.tzdata}/share/zoneinfo
                for test in tests/*.py; do
                  substituteInPlace "$test" \
                    --replace-quiet /bin/echo ${pkgs.coreutils}/bin/echo \
                    --replace-quiet /bin/cat ${pkgs.coreutils}/bin/cat \
                    --replace-quiet /bin/sleep ${pkgs.coreutils}/bin/sleep
                done
                substituteInPlace tests/test_transient.py \
                  --replace-fail 'sleep 0.3' '${pkgs.coreutils}/bin/sleep 0.3'
                substituteInPlace tests/test_standalone_install.py \
                  --replace-fail '"cat"' '"${pkgs.coreutils}/bin/cat"'
              ''}
              PYTHONDONTWRITEBYTECODE=1 ${testEnv}/bin/pytest -p no:cacheprovider tests
              "$out/bin/automationctl" --version
              "$out/bin/actl" --version
              runHook postInstallCheck
            '';

            meta = {
              description = "Standalone scheduled tasks with systemd and launchd";
              homepage = "https://github.com/zydtiger/automationctl";
              license = lib.licenses.mit;
              platforms = lib.platforms.unix;
              mainProgram = "automationctl";
            };
          });
    in
    {
      packages = lib.genAttrs systems (
        system:
        let
          automationctl = mkPackage nixpkgs.legacyPackages.${system};
        in
        {
          inherit automationctl;
          default = automationctl;
        }
      );

      # Installs automationctl and keeps declared tasks installed: import it and
      # set `programs.automationctl`.
      homeManagerModules.default =
        { pkgs, ... }:
        {
          imports = [ ./nix/home-manager.nix ];
          programs.automationctl.package =
            lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    };
}
