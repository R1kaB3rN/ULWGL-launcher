import os
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from http.client import HTTPException, HTTPResponse, HTTPSConnection
from stat import S_ISDIR, S_ISLNK, S_ISREG
from struct import pack

try:
    from importlib.resources.abc import Traversable
except ModuleNotFoundError:
    from importlib.abc import Traversable

from json import load
from pathlib import Path
from secrets import token_urlsafe
from shutil import move, rmtree
from subprocess import run
from tarfile import open as taropen
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any

from filelock import FileLock

from umu.umu_consts import CONFIG, UMU_CACHE, UMU_LOCAL
from umu.umu_log import log
from umu.umu_util import find_obsolete, https_connection, run_zenity

try:
    from tarfile import tar_filter

    has_data_filter: bool = True
except ImportError:
    has_data_filter: bool = False


def _install_umu(
    json: dict[str, Any],
    thread_pool: ThreadPoolExecutor,
    client_session: HTTPSConnection,
) -> None:
    resp: HTTPResponse
    tmp: Path = Path(mkdtemp())
    ret: int = 0  # Exit code from zenity
    # Codename for the runtime (e.g., 'sniper')
    codename: str = json["umu"]["versions"]["runtime_platform"]
    # Archive containing the runtime
    archive: str = f"SteamLinuxRuntime_{codename}.tar.xz"
    base_url: str = (
        f"https://repo.steampowered.com/steamrt-images-{codename}"
        "/snapshots/latest-container-runtime-public-beta"
    )
    token: str = f"?versions={token_urlsafe(16)}"

    log.debug("Codename: %s", codename)
    log.debug("URL: %s", base_url)

    # Download the runtime and optionally create a popup with zenity
    if os.environ.get("UMU_ZENITY") == "1":
        curl: str = "curl"
        opts: list[str] = [
            "-LJ",
            "--silent",
            "-O",
            f"{base_url}/{archive}",
            "--output-dir",
            str(tmp),
        ]
        msg: str = "Downloading umu runtime, please wait..."
        ret = run_zenity(curl, opts, msg)

    # Handle the exit code from zenity
    if ret:
        tmp.joinpath(archive).unlink(missing_ok=True)
        log.console("Retrying from Python...")

    if not os.environ.get("UMU_ZENITY") or ret:
        digest: str = ""
        endpoint: str = (
            f"/steamrt-images-{codename}"
            "/snapshots/latest-container-runtime-public-beta"
        )
        hashsum = sha256()

        # Get the digest for the runtime archive
        client_session.request("GET", f"{endpoint}/SHA256SUMS{token}")

        with client_session.getresponse() as resp:
            if resp.status != 200:
                err: str = (
                    f"repo.steampowered.com returned the status: {resp.status}"
                )
                raise HTTPException(err)

            # Parse SHA256SUMS
            for line in resp.read().decode("utf-8").splitlines():
                if line.endswith(archive):
                    digest = line.split(" ")[0]
                    break

        # Download the runtime
        log.console(f"Downloading latest steamrt {codename}, please wait...")
        client_session.request("GET", f"{endpoint}/{archive}{token}")

        with (
            client_session.getresponse() as resp,
            tmp.joinpath(archive).open(mode="ab+", buffering=0) as file,
        ):
            if resp.status != 200:
                err: str = (
                    f"repo.steampowered.com returned the status: {resp.status}"
                )
                raise HTTPException(err)

            chunk_size: int = 64 * 1024  # 64 KB
            buffer: bytearray = bytearray(chunk_size)
            view: memoryview = memoryview(buffer)
            while size := resp.readinto(buffer):
                file.write(view[:size])
                hashsum.update(view[:size])

            # Verify the runtime digest
            if hashsum.hexdigest() != digest:
                err: str = f"Digest mismatched: {archive}"
                raise ValueError(err)

        log.console(f"{archive}: SHA256 is OK")

    # Open the tar file and move the files
    log.debug("Opening: %s", tmp.joinpath(archive))

    UMU_CACHE.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=UMU_CACHE) as tmpcache:
        log.debug("Created: %s", tmpcache)
        log.debug("Moving: %s -> %s", tmp.joinpath(archive), tmpcache)
        move(tmp.joinpath(archive), tmpcache)

        with (
            taropen(f"{tmpcache}/{archive}", "r:xz") as tar,
        ):
            futures: list[Future] = []

            if has_data_filter:
                log.debug("Using filter for archive")
                tar.extraction_filter = tar_filter
            else:
                log.warning("Python: %s", sys.version)
                log.warning("Using no data filter for archive")
                log.warning("Archive will be extracted insecurely")

            # Ensure the target directory exists
            UMU_LOCAL.mkdir(parents=True, exist_ok=True)

            # Extract the entirety of the archive w/ or w/o the data filter
            log.debug(
                "Extracting: %s -> %s", f"{tmpcache}/{archive}", tmpcache
            )
            tar.extractall(path=tmpcache)  # noqa: S202

            # Move the files to the correct location
            source_dir: Path = Path(tmpcache, f"SteamLinuxRuntime_{codename}")
            var: Path = UMU_LOCAL.joinpath("var")
            log.debug("Source: %s", source_dir)
            log.debug("Destination: %s", UMU_LOCAL)

            # Move each file to the dest dir, overwriting if exists
            futures.extend(
                [
                    thread_pool.submit(_move, file, source_dir, UMU_LOCAL)
                    for file in source_dir.glob("*")
                ]
            )

            if var.is_dir():
                log.debug("Removing: %s", var)
                # Remove the variable directory to avoid Steam Linux Runtime
                # related errors when creating it. Supposedly, it only happens
                # when going from umu-launcher 0.1-RC4 -> 1.1.1+
                # See https://github.com/Open-Wine-Components/umu-launcher/issues/213#issue-2576708738
                thread_pool.submit(rmtree, str(var))

            for future in futures:
                future.result()

    # Rename _v2-entry-point
    log.debug("Renaming: _v2-entry-point -> umu")
    UMU_LOCAL.joinpath("_v2-entry-point").rename(UMU_LOCAL.joinpath("umu"))

    # Validate the runtime after moving the files
    ret = check_runtime(UMU_LOCAL, json)

    # Compute a digest of the metadata for future attestation
    if ret != 1:
        # At this point, the runtime is authenticated. For subsequent launches,
        # we'll check against our digest to ensure we're intact
        thread_pool.submit(
            UMU_LOCAL.joinpath("umu.hashsum").write_text,
            get_runtime_digest(UMU_LOCAL, thread_pool),
            "utf-8",
        )
        return

    log.warning("steamrt validation failed, skipping metadata checksum")


def setup_umu(
    root: Traversable, local: Path, thread_pool: ThreadPoolExecutor
) -> None:
    """Install or update the runtime for the current user."""
    log.debug("Root: %s", root)
    log.debug("Local: %s", local)
    json: dict[str, Any] = _get_json(root, CONFIG)
    host: str = "repo.steampowered.com"

    # New install or umu dir is empty
    if not local.exists() or not any(local.iterdir()):
        log.debug("New install detected")
        log.console(
            "Setting up Unified Launcher for Windows Games on Linux..."
        )
        local.mkdir(parents=True, exist_ok=True)
        with https_connection(host) as client_session:
            _restore_umu(
                json,
                thread_pool,
                lambda: local.joinpath("umu").is_file(),
                client_session,
            )
        return

    if os.environ.get("UMU_RUNTIME_UPDATE") == "0":
        log.debug("Runtime Platform updates disabled")
        return

    find_obsolete()

    with https_connection(host) as client_session:
        _update_umu(local, json, thread_pool, client_session)


def _update_umu(
    local: Path,
    json: dict[str, Any],
    thread_pool: ThreadPoolExecutor,
    client_session: HTTPSConnection,
) -> None:
    """For existing installations, check for updates to the runtime.

    The runtime platform will be updated to the latest public beta by comparing
    the local VERSIONS.txt against the remote one.
    """
    runtime: Path
    resp: HTTPResponse
    codename: str = json["umu"]["versions"]["runtime_platform"]
    endpoint: str = (
        f"/steamrt-images-{codename}"
        "/snapshots/latest-container-runtime-public-beta"
    )
    token: str = f"?version={token_urlsafe(16)}"
    checksum: Path = local.joinpath("umu.hashsum")
    enabled_integrity: bool = os.environ.get("UMU_RUNTIME_INTEGRITY") == "1"
    log.debug("Existing install detected")
    log.debug("Sending request to '%s'...", client_session.host)

    # When integrity is enabled, restore our runtime if our digest is missing
    if enabled_integrity and not checksum.is_file():
        lock: FileLock = FileLock(f"{local}/umu.lock")
        log.warning("File '%s' is missing", checksum)
        log.console("Restoring Runtime Platform...")
        log.debug("Acquiring file lock '%s'...", lock.lock_file)
        with lock:
            log.debug("Acquired file lock '%s'", lock.lock_file)
            for file in local.glob("*"):
                if file.is_dir():
                    rmtree(str(file))
                if file.is_file() and not file.name.endswith(".lock"):
                    file.unlink()
        _restore_umu(
            json,
            thread_pool,
            lambda: local.joinpath("umu").is_file(),
            client_session,
        )
        return

    # Check if our runtime directory is intact and restore if not
    if enabled_integrity:
        digest_ret: str = get_runtime_digest(local, thread_pool)
        digest_local_ret: str = local.joinpath("umu.hashsum").read_text(
            encoding="utf-8"
        )

        log.debug("Runtime Platform integrity enabled")
        log.debug("Source: %s", local)
        log.debug("Digest: %s", digest_ret)
        log.debug("Source: %s", local / "umu.hashsum")
        log.debug("Digest: %s", digest_local_ret)

        if digest_ret != digest_local_ret:
            lock: FileLock = FileLock(f"{local}/umu.lock")
            log.warning("Runtime Platform corrupt")
            log.console("Restoring Runtime Platform...")
            log.debug("Acquiring file lock '%s'...", lock.lock_file)
            with lock:
                log.debug("Acquired file lock '%s'", lock.lock_file)
                for file in local.glob("*"):
                    if file.is_dir():
                        rmtree(str(file))
                    if file.is_file() and not file.name.endswith(".lock"):
                        file.unlink()
            _restore_umu(
                json,
                thread_pool,
                lambda: local.joinpath("umu").is_file(),
                client_session,
            )
            return

    # Find the runtime directory (e.g., sniper_platform_0.20240530.90143)
    # Assume the directory begins with the alias. At this point, our runtime
    # may or may not be intact. The client is responsible for restoring it by
    # force an update or enabling integrity
    runtime = max(file for file in local.glob(f"{codename}*") if file.is_dir())

    log.debug("Runtime: %s", runtime.name)
    log.debug("Codename: %s", codename)

    # Update the runtime if necessary by comparing VERSIONS.txt to the remote
    # repo.steampowered currently sits behind a Cloudflare proxy, which may
    # respond with cf-cache-status: HIT in the header for subsequent requests
    # indicating the response was found in the cache and was returned. Valve
    # has control over the CDN's cache control behavior, so we must not assume
    # all of the cache will be purged after new files are uploaded. Therefore,
    # always avoid the cache by appending a unique query to the URI
    url: str = f"{endpoint}/SteamLinuxRuntime_{codename}.VERSIONS.txt{token}"
    client_session.request("GET", url)

    # Attempt to compare the digests
    with client_session.getresponse() as resp:
        if resp.status != 200:
            log.warning(
                "repo.steampowered.com returned the status: %s", resp.status
            )
            return

        steamrt_latest_digest: bytes = sha256(resp.read()).digest()
        steamrt_local_digest: bytes = sha256(
            local.joinpath("VERSIONS.txt").read_bytes()
        ).digest()
        steamrt_versions: Path = local.joinpath("VERSIONS.txt")

        log.debug("Source: %s", url)
        log.debug("Digest: %s", steamrt_latest_digest)
        log.debug("Source: %s", steamrt_versions)
        log.debug("Digest: %s", steamrt_local_digest)

        if steamrt_latest_digest != steamrt_local_digest:
            lock: FileLock = FileLock(f"{local}/umu.lock")
            log.console("Updating steamrt to latest...")
            log.debug("Acquiring file lock '%s'...", lock.lock_file)

            with lock:
                log.debug("Acquired file lock '%s'", lock.lock_file)
                # Once another process acquires the lock, check if the latest
                # runtime has already been downloaded
                if (
                    steamrt_latest_digest
                    == sha256(steamrt_versions.read_bytes()).digest()
                ):
                    log.debug("Released file lock '%s'", lock.lock_file)
                    return
                _install_umu(json, thread_pool, client_session)
                log.debug("Removing: %s", runtime)
                rmtree(str(runtime))
                log.debug("Released file lock '%s'", lock.lock_file)

    log.console("steamrt is up to date")


def _get_json(path: Traversable, config: str) -> dict[str, Any]:
    """Validate the state of the configuration file umu_version.json in a path.

    The configuration file will be used to update the runtime and it reflects
    the tools currently used by launcher. The key/value pairs umu and versions
    must exist.
    """
    json: dict[str, Any]
    # Steam Runtime platform values
    # See https://gitlab.steamos.cloud/steamrt/steamrt/-/wikis/home
    steamrts: set[str] = {
        "soldier",
        "sniper",
        "medic",
        "steamrt5",
    }

    # umu_version.json in the system path should always exist
    if not path.joinpath(config).is_file():
        err: str = (
            f"File not found: {config}\n"
            "Please reinstall the package to recover configuration file"
        )
        raise FileNotFoundError(err)

    with path.joinpath(config).open(mode="r", encoding="utf-8") as file:
        json = load(file)

    # Raise an error if "umu" and "versions" doesn't exist
    if not json or "umu" not in json or "versions" not in json["umu"]:
        err: str = (
            f"Failed to load {config} or 'umu' or 'versions' not in: {config}"
        )
        raise ValueError(err)

    # The launcher will use the value runtime_platform to glob files. Attempt
    # to guard against directory removal attacks for non-system wide installs
    if json["umu"]["versions"]["runtime_platform"] not in steamrts:
        err: str = "Value for 'runtime_platform' is not a steamrt"
        raise ValueError(err)

    return json


def _move(file: Path, src: Path, dst: Path) -> None:
    """Move a file or directory to a destination.

    In order for the source and destination directory to be identical, when
    moving a directory, the contents of that same directory at the destination
    will be removed.
    """
    src_file: Path = src.joinpath(file.name)
    dest_file: Path = dst.joinpath(file.name)

    if dest_file.is_dir():
        log.debug("Removing directory: %s", dest_file)
        rmtree(str(dest_file))

    if src.is_file() or src.is_dir():
        log.debug("Moving: %s -> %s", src_file, dest_file)
        move(src_file, dest_file)


def check_runtime(src: Path, json: dict[str, Any]) -> int:
    """Validate the file hierarchy of the runtime platform.

    The mtree file included in the Steam runtime platform will be used to
    validate the integrity of the runtime's metadata after its moved to the
    home directory and used to run games.
    """
    runtime: Path
    codename: str = json["umu"]["versions"]["runtime_platform"]
    pv_verify: Path = src.joinpath("pressure-vessel", "bin", "pv-verify")
    ret: int = 1

    # Find the runtime directory
    try:
        runtime = max(
            file for file in src.glob(f"{codename}*") if file.is_dir()
        )
    except ValueError:
        log.warning("steamrt validation failed")
        log.warning("Could not find runtime in '%s'", src)
        return ret

    if not pv_verify.is_file():
        log.warning("steamrt validation failed")
        log.warning("File does not exist: '%s'", pv_verify)
        return ret

    log.console(f"Verifying integrity of {runtime.name}...")
    ret = run(
        [
            pv_verify,
            "--quiet",
            "--minimized-runtime",
            runtime.joinpath("files"),
        ],
        check=False,
    ).returncode

    if ret:
        log.warning("steamrt validation failed")
        log.debug("%s exited with the status code: %s", pv_verify.name, ret)
        return ret
    log.console(f"{runtime.name}: mtree is OK")

    return ret


def get_runtime_digest(path: Path, thread_pool: ThreadPoolExecutor) -> str:
    """Generate a digest for all runtime files within a directory."""
    hashsum = sha256()
    # Ignore any lock files, the variable dir and our checksum file
    # when computing the digest
    whitelist: tuple[str, ...] = (".lock", ".ref", "var", "umu.hashsum")
    futures: list[Future] = []

    # Find all runtime files and compute its hash in parallel
    for file in (
        file_toplvl
        for file_toplvl in path.glob("*")
        if not file_toplvl.name.endswith(whitelist)
    ):
        stat_ret: os.stat_result = file.stat()

        # Get all normal files within directories
        if S_ISDIR(stat_ret.st_mode):
            for subfile in (
                file_subdir
                for file_subdir in file.glob("*")
                if file_subdir.is_file() and not file_subdir.is_symlink()
            ):
                futures.append(
                    thread_pool.submit(_compute_digest, subfile.stat())
                )
            continue

        # File is in the top-level and is a normal file
        if S_ISREG(stat_ret.st_mode) and not S_ISLNK(stat_ret.st_mode):
            futures.append(thread_pool.submit(_compute_digest, stat_ret))

    for future in futures:
        future_ret = future.result()
        hashsum.update(future_ret.digest())

    return hashsum.hexdigest()


def _compute_digest(stat: os.stat_result):  # noqa: ANN202
    hashsum = sha256()
    fmt: str = "fiiiif"

    hashsum.update(
        pack(
            fmt,
            stat.st_mtime,  # Modification time
            stat.st_mode,  # Permissions
            stat.st_size,  # Size
            stat.st_uid,  # User
            stat.st_gid,  # Group
            stat.st_ctime,  # Creation time
        )
        + bytes(2048)  # Pad enough bytes to release the GIL
    )

    return hashsum


def _restore_umu(
    json: dict[str, Any],
    thread_pool: ThreadPoolExecutor,
    callback_fn: Callable[[], bool],
    client_session: HTTPSConnection,
) -> None:
    lock: FileLock = FileLock(f"{UMU_LOCAL}/umu.lock")
    log.debug("Acquiring file lock '%s'...", lock.lock_file)
    with lock:
        log.debug("Acquired file lock '%s'", lock.lock_file)
        if callback_fn():
            log.debug("Released file lock '%s'", lock.lock_file)
            log.console("steamrt was restored")
            return
        _install_umu(json, thread_pool, client_session)
        log.debug("Released file lock '%s'", lock.lock_file)
