import os
from pathlib import Path
from umu_log import log
from shutil import which
from umu_consts import FLATPAK_PATH


def flatpak_run_in_host(
    env: dict[str, str],
    local: Path,
    root: Path,
    command: list[str],
    opts: list[str] = None,
) -> list[str]:
    """Build a command to execute outside a Flatpak.

    Will execute the system umu-launcher instead the one in the Flatpak
    """
    verb: str = env["PROTON_VERB"]
    flatpak_bin: str = which("flatpak-spawn")

    if env.get("UMU_CONTAINER") != "0":
        if opts:
            command.extend(
                root.joinpath("reaper").as_posix(),
                f"UMU_ID={os.environ.get('UMU_ID')}",
                "--",
                local.joinpath("umu").as_posix(),
                "--verb",
                verb,
                "--",
                Path(os.environ.get("PROTONPATH")).joinpath("proton").as_posix(),
                verb,
                os.environ.get("EXE"),
                *opts,
            )
            return command
        command.extend(
            [
                root.joinpath("reaper").as_posix(),
                f"UMU_ID={os.environ.get('UMU_ID')}",
                "--",
                local.joinpath("umu").as_posix(),
                "--verb",
                verb,
                "--",
                Path(os.environ.get("PROTONPATH")).joinpath("proton").as_posix(),
                verb,
                os.environ.get("EXE"),
            ],
        )
        return command

    log.warning("Will execute flatpak-spawn")
    log.warning("Changing prefix: %s -> %s", root.parent, "/usr")
    log.warning("Assuming system umu-launcher is installed")
    root = Path("/usr/share/umu")

    if opts:
        command.extend(
            flatpak_bin,
            *[
                f"--env={var}={os.environ.get(var)}"
                for var in os.environ
                if var in env or var.startswith(("GAMESCOPE", "DISPLAY"))
            ],
            "--host",
            root.joinpath("reaper").as_posix(),
            f"UMU_ID={os.environ.get('UMU_ID')}",
            "--",
            local.joinpath("umu").as_posix(),
            "--verb",
            verb,
            "--",
            Path(os.environ.get("PROTONPATH")).joinpath("proton").as_posix(),
            verb,
            os.environ.get("EXE"),
            *opts,
        )
        return command
    command.extend(
        [
            flatpak_bin,
            *[
                f"--env={var}={os.environ.get(var)}"
                for var in os.environ
                if var in env or var.startswith(("GAMESCOPE", "DISPLAY"))
            ],
            "--host",
            root.joinpath("reaper").as_posix(),
            f"UMU_ID={os.environ.get('UMU_ID')}",
            "--",
            local.joinpath("umu").as_posix(),
            "--verb",
            verb,
            "--",
            Path(os.environ.get("PROTONPATH")).joinpath("proton").as_posix(),
            verb,
            os.environ.get("EXE"),
        ],
    )

    return command
