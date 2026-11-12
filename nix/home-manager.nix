# Home Manager module: installs automationctl and keeps the declared tasks
# installed in the user's scheduler. Tasks installed any other way are left alone.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.automationctl;
  toml = pkgs.formats.toml { };
  backend = if pkgs.stdenv.hostPlatform.isDarwin then "launchd" else "systemd";
  tasks = lib.mapAttrsToList (name: task: {
    file = "${name}.toml";
    path = toml.generate "${name}.toml" (
      task
      // {
        inherit name;
        # Scheduled runs do not inherit a login shell's PATH.
        path_prepend = lib.unique ([ "${config.home.profileDirectory}/bin" ] ++ (task.path_prepend or [ ]));
      }
    );
  }) cfg.tasks;
  # Linting at build time rejects a bad task before activation touches the scheduler.
  bundle = pkgs.runCommand "automationctl-tasks" { nativeBuildInputs = [ cfg.package ]; } ''
    mkdir -p "$out/tasks" "$TMPDIR/config/automationctl"
    ${lib.optionalString (cfg.configFile != null) ''
      cp ${cfg.configFile} "$TMPDIR/config/automationctl/config.toml"
    ''}
    ${lib.concatMapStringsSep "\n" (task: "ln -s ${task.path} \"$out/tasks/${task.file}\"") tasks}
    ${lib.optionalString (tasks != [ ]) ''
      XDG_CONFIG_HOME="$TMPDIR/config" automationctl lint "$out"/tasks/*.toml --backend ${backend}
    ''}
  '';
in
{
  options.programs.automationctl = {
    enable = lib.mkEnableOption "automationctl and its declared tasks";

    package = lib.mkOption {
      type = lib.types.package;
      description = "automationctl package to install and to run the scheduler commands with.";
    };

    configFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Machine policy installed as `config.toml` in automationctl's configuration directory.";
    };

    tasks = lib.mkOption {
      type = lib.types.attrsOf toml.type;
      default = { };
      example = lib.literalExpression ''
        {
          hourly-check = {
            schema_version = 2;
            description = "Record a simple health check";
            command = [ "/usr/bin/true" ];
            schedule = "every 1h";
            timeout = "2m";
          };
        }
      '';
      description = ''
        Task specs by task name. Each is installed on activation and removed
        again once it is no longer declared. The attribute name becomes the
        task's `name`, and the Home Manager profile is added to `path_prepend`.
        Inline `stdin` rather than using `stdin_file`.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    xdg.configFile."automationctl/config.toml" = lib.mkIf (cfg.configFile != null) {
      source = cfg.configFile;
    };

    home.activation.syncAutomations = lib.hm.dag.entryAfter [ "linkGeneration" "installPackages" ] ''
      run env \
        XDG_CONFIG_HOME=${lib.escapeShellArg config.xdg.configHome} \
        XDG_STATE_HOME=${lib.escapeShellArg config.xdg.stateHome} \
        AUTOMATIONCTL_EXECUTABLE=${cfg.package}/bin/automationctl \
        ${pkgs.python3}/bin/python3 ${./sync_tasks.py} \
        --tasks ${bundle}/tasks --backend ${backend} \
        --executable ${cfg.package}/bin/automationctl \
        --state ${lib.escapeShellArg "${config.xdg.stateHome}/automationctl/home-manager-tasks.json"}
    '';
  };
}
