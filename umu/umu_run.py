#!/usr/bin/env python3

import os
import sys
from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from pathlib import Path
from typing import Any
from re import match
from subprocess import run
from umu_dl_util import get_umu_proton
from umu_util import setup_umu
from umu_log import log, console_handler, CustomFormatter
from logging import INFO, WARNING, DEBUG
from errno import ENETUNREACH
from concurrent.futures import ThreadPoolExecutor, Future
from socket import AF_INET, SOCK_DGRAM, socket
from pwd import getpwuid
from umu_plugins import set_env_toml
from ctypes.util import find_library
from shutil import which
from umu_consts import (
    PROTON_VERBS,
    DEBUG_FORMAT,
    STEAM_COMPAT,
    UMU_LOCAL,
    FLATPAK_PATH,
    FLATPAK_ID,
)


def parse_args() -> Namespace | tuple[str, list[str]]:  # noqa: D103
    opt_args: set[str] = {"--help", "-h", "--config"}
    parser: ArgumentParser = ArgumentParser(
        description="Unified Linux Wine Game Launcher",
        epilog=(
            "See umu(1) for more info and examples, or visit\n"
            "https://github.com/Open-Wine-Components/umu-launcher"
        ),
        formatter_class=RawTextHelpFormatter,
    )
    parser.add_argument("--config", help="path to TOML file (requires Python 3.11+)")

    if not sys.argv[1:]:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if sys.argv[1:][0] in opt_args:
        return parser.parse_args(sys.argv[1:])

    if sys.argv[1] in PROTON_VERBS:
        if "PROTON_VERB" not in os.environ:
            os.environ["PROTON_VERB"] = sys.argv[1]
        sys.argv.pop(1)

    return sys.argv[1], sys.argv[2:]


def set_log() -> None:
    """Adjust the log level for the logger."""
    levels: set[str] = {"1", "warn", "debug"}

    if os.environ["UMU_LOG"] not in levels:
        return

    if os.environ["UMU_LOG"] == "1":
        # Show the envvars and command at this level
        log.setLevel(level=INFO)
    elif os.environ["UMU_LOG"] == "warn":
        log.setLevel(level=WARNING)
    elif os.environ["UMU_LOG"] == "debug":
        # Show all logs
        console_handler.setFormatter(CustomFormatter(DEBUG_FORMAT))
        log.addHandler(console_handler)
        log.setLevel(level=DEBUG)

    os.environ.pop("UMU_LOG")


def setup_pfx(path: str) -> None:
    """Create a symlink to the WINE prefix and tracked_files file."""
    pfx: Path = Path(path).joinpath("pfx").expanduser()
    steam: Path = Path(path).expanduser().joinpath("drive_c", "users", "steamuser")
    # Login name of the user as determined by the password database (pwd)
    user: str = getpwuid(os.getuid()).pw_name
    wineuser: Path = Path(path).expanduser().joinpath("drive_c", "users", user)

    if pfx.is_symlink():
        pfx.unlink()

    if not pfx.is_dir():
        pfx.symlink_to(Path(path).expanduser().resolve(strict=True))

    Path(path).joinpath("tracked_files").expanduser().touch()

    # Create a symlink of the current user to the steamuser dir or vice versa
    # Default for a new prefix is: unixuser -> steamuser
    if (
        not wineuser.is_dir()
        and not steam.is_dir()
        and not (wineuser.is_symlink() or steam.is_symlink())
    ):
        # For new prefixes with our Proton: user -> steamuser
        steam.mkdir(parents=True)
        wineuser.unlink(missing_ok=True)
        wineuser.symlink_to("steamuser")
    elif wineuser.is_dir() and not steam.is_dir() and not steam.is_symlink():
        # When there's a user dir: steamuser -> user
        steam.unlink(missing_ok=True)
        steam.symlink_to(user)
    elif not wineuser.exists() and not wineuser.is_symlink() and steam.is_dir():
        wineuser.unlink(missing_ok=True)
        wineuser.symlink_to("steamuser")
    else:
        log.debug("Skipping link creation for prefix")
        log.debug("User steamuser directory exists: %s", steam)
        log.debug("User home directory exists: %s", wineuser)


def check_env(env: set[str, str]) -> dict[str, str] | dict[str, Any]:
    """Before executing a game, check for environment variables and set them.

    GAMEID is strictly required
    """
    if not os.environ.get("GAMEID"):
        err: str = "Environment variable not set or is empty: GAMEID"
        raise ValueError(err)
    env["GAMEID"] = os.environ["GAMEID"]

    if os.environ.get("WINEPREFIX") == "":
        err: str = "Environment variable is empty: WINEPREFIX"
        raise ValueError(err)
    if "WINEPREFIX" not in os.environ:
        id: str = env["GAMEID"]
        pfx: Path = Path.home().joinpath("Games", "umu", f"{id}")
        pfx.mkdir(parents=True, exist_ok=True)
        os.environ["WINEPREFIX"] = pfx.as_posix()
    if not Path(os.environ["WINEPREFIX"]).expanduser().is_dir():
        pfx: Path = Path(os.environ["WINEPREFIX"])
        pfx.mkdir(parents=True, exist_ok=True)
        os.environ["WINEPREFIX"] = pfx.as_posix()
    env["WINEPREFIX"] = os.environ["WINEPREFIX"]

    # Proton Version
    # Ensure a string is passed instead of a path
    # Since shells auto expand paths, pathlib will destroy the STEAM_COMPAT
    # stem when it encounters a separator
    if (
        os.environ.get("PROTONPATH")
        and Path(f"{STEAM_COMPAT}/" + os.environ.get("PROTONPATH")).is_dir()
    ):
        log.debug("Proton version selected")
        os.environ["PROTONPATH"] = STEAM_COMPAT.joinpath(
            os.environ["PROTONPATH"]
        ).as_posix()

    # GE-Proton
    if os.environ.get("PROTONPATH") == "GE-Proton":
        log.debug("GE-Proton selected")
        get_umu_proton(env)

    if "PROTONPATH" not in os.environ:
        os.environ["PROTONPATH"] = ""
        get_umu_proton(env)

    env["PROTONPATH"] = os.environ["PROTONPATH"]

    # If download fails/doesn't exist in the system, raise an error
    if not os.environ["PROTONPATH"]:
        err: str = (
            "Download failed\n"
            "UMU-Proton could not be found in compatibilitytools.d\n"
            "Please set $PROTONPATH or visit https://github.com/Open-Wine-Components/umu-proton/releases"
        )
        raise FileNotFoundError(err)

    return env


def set_env(
    env: dict[str, str], args: Namespace | tuple[str, list[str]]
) -> dict[str, str]:
    """Set various environment variables for the Steam RT.

    Filesystem paths will be formatted and expanded as POSIX
    """
    # PROTON_VERB
    # For invalid Proton verbs, just assign the waitforexitandrun
    if os.environ.get("PROTON_VERB") in PROTON_VERBS:
        env["PROTON_VERB"] = os.environ["PROTON_VERB"]
    else:
        env["PROTON_VERB"] = "waitforexitandrun"

    # EXE
    # Empty string for EXE will be used to create a prefix
    if isinstance(args, tuple) and isinstance(args[0], str) and not args[0]:
        env["EXE"] = ""
        env["STEAM_COMPAT_INSTALL_PATH"] = ""
        env["PROTON_VERB"] = "waitforexitandrun"
    elif isinstance(args, tuple):
        try:
            env["EXE"] = Path(args[0]).expanduser().resolve(strict=True).as_posix()
            env["STEAM_COMPAT_INSTALL_PATH"] = Path(env["EXE"]).parent.as_posix()
        except FileNotFoundError:
            # Assume that the executable will be inside the wine prefix or container
            env["EXE"] = Path(args[0]).as_posix()
            env["STEAM_COMPAT_INSTALL_PATH"] = ""
            log.warning("Executable not found: %s", env["EXE"])
    else:
        # Config branch
        env["EXE"] = Path(env["EXE"]).expanduser().as_posix()
        env["STEAM_COMPAT_INSTALL_PATH"] = Path(env["EXE"]).parent.as_posix()

    if "STORE" in os.environ:
        env["STORE"] = os.environ["STORE"]

    # UMU_ID
    env["UMU_ID"] = env["GAMEID"]
    env["ULWGL_ID"] = env["UMU_ID"]  # Set ULWGL_ID for compatibility
    env["STEAM_COMPAT_APP_ID"] = "0"

    if match(r"^umu-[\d\w]+$", env["UMU_ID"]):
        env["STEAM_COMPAT_APP_ID"] = env["UMU_ID"][env["UMU_ID"].find("-") + 1 :]
    env["SteamAppId"] = env["STEAM_COMPAT_APP_ID"]
    env["SteamGameId"] = env["SteamAppId"]

    # PATHS
    env["WINEPREFIX"] = (
        Path(env["WINEPREFIX"]).expanduser().resolve(strict=True).as_posix()
    )
    env["PROTONPATH"] = (
        Path(env["PROTONPATH"]).expanduser().resolve(strict=True).as_posix()
    )
    env["STEAM_COMPAT_DATA_PATH"] = env["WINEPREFIX"]
    env["STEAM_COMPAT_SHADER_PATH"] = env["STEAM_COMPAT_DATA_PATH"] + "/shadercache"
    env["STEAM_COMPAT_TOOL_PATHS"] = env["PROTONPATH"] + ":" + UMU_LOCAL.as_posix()
    env["STEAM_COMPAT_MOUNTS"] = env["STEAM_COMPAT_TOOL_PATHS"]

    # Game drive
    enable_steam_game_drive(env)

    return env


def enable_steam_game_drive(env: dict[str, str]) -> dict[str, str]:
    """Enable Steam Game Drive functionality.

    Expects STEAM_COMPAT_INSTALL_PATH to be set
    STEAM_RUNTIME_LIBRARY_PATH will not be set if the exe directory does not exist
    """
    paths: set[str] = set()
    root: Path = Path("/")
    libc: str = find_library("c")
    # All library paths that are currently supported by the container runtime framework
    # See https://gitlab.steamos.cloud/steamrt/steam-runtime-tools/-/blob/main/docs/distro-assumptions.md
    # Non-FHS filesystems should run in a FHS chroot to comply
    steamrt_paths: list[str] = [
        "/usr/lib64",
        "/usr/lib32",
        "/usr/lib",
        "/usr/lib/x86_64-linux-gnu",
        "/usr/lib/i386-linux-gnu",
    ]

    # Check for mount points going up toward the root
    # NOTE: Subvolumes can be mount points
    for path in Path(env["STEAM_COMPAT_INSTALL_PATH"]).parents:
        if path.is_mount() and path != root:
            if os.environ.get("STEAM_COMPAT_LIBRARY_PATHS"):
                env["STEAM_COMPAT_LIBRARY_PATHS"] = (
                    os.environ["STEAM_COMPAT_LIBRARY_PATHS"] + ":" + path.as_posix()
                )
            else:
                env["STEAM_COMPAT_LIBRARY_PATHS"] = path.as_posix()
            break

    if os.environ.get("LD_LIBRARY_PATH"):
        paths = {path for path in os.environ["LD_LIBRARY_PATH"].split(":")}

    if env["STEAM_COMPAT_INSTALL_PATH"]:
        paths.add(env["STEAM_COMPAT_INSTALL_PATH"])

    for path in steamrt_paths:
        if not Path(path).is_symlink() and Path(path, libc).is_file():
            paths.add(path)
    env["STEAM_RUNTIME_LIBRARY_PATH"] = ":".join(list(paths))

    return env


def build_command(
    env: dict[str, str],
    local: Path,
    root: Path,
    command: list[str],
    opts: list[str] = None,
) -> list[str]:
    """Build the command to be executed."""
    verb: str = env["PROTON_VERB"]
    flatpak_bin: str = which("flatpak-spawn")

    # Raise an error if the _v2-entry-point cannot be found
    if not local.joinpath("umu").is_file():
        err: str = (
            "Path to _v2-entry-point cannot be found in: "
            f"{local}\n"
            "Please install a Steam Runtime platform"
        )
        raise FileNotFoundError(err)

    if not Path(env.get("PROTONPATH")).joinpath("proton").is_file():
        err: str = "The following file was not found in PROTONPATH: proton"
        raise FileNotFoundError(err)

    # Flatpak
    # When running inside a Flatpak, breakout of it
    if FLATPAK_ID and flatpak_bin:
        log.debug("Will execute flatpaks-spawn for command")
        if opts:
            command.extend(
                flatpak_bin,
                *[f"--env={var}={os.environ.get(var)}" for var in os.environ],
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
                *[f"--env={var}={os.environ.get(var)}" for var in os.environ],
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

    # System package
    if opts:
        command.extend(
            [
                root.joinpath("reaper").as_posix(),
                f"UMU_ID={env.get('UMU_ID')}",
                "--",
                local.joinpath("umu").as_posix(),
                "--verb",
                verb,
                "--",
                Path(env.get("PROTONPATH")).joinpath("proton").as_posix(),
                verb,
                env.get("EXE"),
                *opts,
            ],
        )
        return command
    command.extend(
        [
            root.joinpath("reaper").as_posix(),
            f"UMU_ID={env.get('UMU_ID')}",
            "--",
            local.joinpath("umu").as_posix(),
            "--verb",
            verb,
            "--",
            Path(env.get("PROTONPATH")).joinpath("proton").as_posix(),
            verb,
            env.get("EXE"),
        ],
    )

    return command


def main() -> int:  # noqa: D103
    env: list[str, str] = {
        "WINEPREFIX": "",
        "GAMEID": "",
        "PROTON_CRASH_REPORT_DIR": "/tmp/umu_crashreports",
        "PROTONPATH": "",
        "STEAM_COMPAT_APP_ID": "",
        "STEAM_COMPAT_TOOL_PATHS": "",
        "STEAM_COMPAT_LIBRARY_PATHS": "",
        "STEAM_COMPAT_MOUNTS": "",
        "STEAM_COMPAT_INSTALL_PATH": "",
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": "",
        "STEAM_COMPAT_DATA_PATH": "",
        "STEAM_COMPAT_SHADER_PATH": "",
        "FONTCONFIG_PATH": "",
        "EXE": "",
        "SteamAppId": "",
        "SteamGameId": "",
        "STEAM_RUNTIME_LIBRARY_PATH": "",
        "STORE": "",
        "PROTON_VERB": "",
        "UMU_ID": "",
        "ULWGL_ID": "",
        "UMU_ZENITY": "",
    }
    command: list[str] = []
    opts: list[str] = None
    root: Path = Path(__file__).resolve(strict=True).parent
    executor: ThreadPoolExecutor = ThreadPoolExecutor()
    future: Future = None
    args: Namespace | tuple[str, list[str]] = parse_args()

    if os.geteuid() == 0:
        err: str = "This script should never be run as the root user"
        log.error(err)
        sys.exit(1)

    if "musl" in os.environ.get("LD_LIBRARY_PATH", ""):
        err: str = "This script is not designed to run on musl-based systems"
        log.error(err)
        sys.exit(1)

    if "UMU_LOG" in os.environ:
        set_log()

    log.debug("Arguments: %s", args)

    if FLATPAK_PATH and root == Path("/app/share/umu"):
        log.debug("Flatpak environment detected")
        log.debug("FLATPAK_ID: %s", FLATPAK_ID)
        log.debug("Persisting the runtime at: %s", FLATPAK_PATH)

    # Setup the launcher and runtime files
    # An internet connection is required for new setups
    try:
        with socket(AF_INET, SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.connect(("1.1.1.1", 53))
        future = executor.submit(setup_umu, root, UMU_LOCAL)
    except TimeoutError:  # Request to a server timed out
        if not UMU_LOCAL.exists() or not any(UMU_LOCAL.iterdir()):
            err: str = (
                "umu has not been setup for the user\n"
                "An internet connection is required to setup umu"
            )
            raise RuntimeError(err)
        log.debug("Request timed out")
    except OSError as e:  # No internet
        if (
            e.errno == ENETUNREACH
            and not UMU_LOCAL.exists()
            or not any(UMU_LOCAL.iterdir())
        ):
            err: str = (
                "umu has not been setup for the user\n"
                "An internet connection is required to setup umu"
            )
            raise RuntimeError(err)
        if e.errno != ENETUNREACH:
            raise
        log.debug("Network is unreachable")

    # Check environment
    if isinstance(args, Namespace) and getattr(args, "config", None):
        env, opts = set_env_toml(env, args)
    else:
        opts = args[1]  # Reference the executable options
        check_env(env)

    # Prepare the prefix
    setup_pfx(env["WINEPREFIX"])

    # Configure the environment
    set_env(env, args)

    # Set all environment variables
    # NOTE: `env` after this block should be read only
    for key, val in env.items():
        log.info("%s=%s", key, val)
        os.environ[key] = val

    if future:
        future.result()
    executor.shutdown()

    # Run
    build_command(env, UMU_LOCAL, root, command, opts)
    log.debug("%s", command)

    return run(command, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("Keyboard Interrupt")
    except SystemExit as e:
        if e.code:
            log.debug("subprocess exited with the status code: %s", e.code)
            sys.exit(e.code)
    except BaseException:
        log.exception("BaseException")
    finally:
        UMU_LOCAL.joinpath(".ref").unlink(
            missing_ok=True
        )  # Cleanup .ref file on every exit
