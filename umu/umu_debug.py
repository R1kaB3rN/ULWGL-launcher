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
    log.warning("Will execute flatpak-spawn")
    log.warning("Changing prefix: %s -> %s", root.parent, "/usr")
    log.warning("Assuming system umu-launcher is installed")

    root: Path = Path("/usr/share/umu")
    verb: str = env["PROTON_VERB"]
    flatpak_bin: str = which("flatpak-spawn")

    if not FLATPAK_PATH or env.get("UMU_CONTAINER") != "0" or not flatpak_bin:
        log.warning("Will not execute flatpakk-spawn")
        log.warning("FLATPAK_PATH: %s", FLATPAK_PATH)
        log.warning("UMU_CONTAINER: %s", env.get("UMU_CONTAINER"))
        log.warning("flatpak-spawn: %s", flatpak_bin)
        log.warning("Reverting prefix: %s -> %s", root.parent, "/app")
        root = Path("/app/share/umu")
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

    if opts:
        command.extend(
            flatpak_bin,
            *[
                f"--env={var}={os.environ.get(var)}"
                for var in os.environ
                if var in env or var.startswith("GAMESCOPE", "DISPLAY")
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
                if var in env or var.startswith("GAMESCOPE", "DISPLAY")
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
