"""Explicit, hash-verified downloader for optional Copperbrain runtime integrations.

Never runs automatically and never runs through MCP. The user invokes it directly:

    uv run python scripts/setup_dependencies.py

It fetches, from official sources only, over HTTPS, verifying a published checksum whenever
the source provides one:

  * the pinned official KiCadRoutingTools PCM release, including its platform-specific Rust
    core, into ``<COPPERBRAIN_DATA_DIR>/integrations/kicad-routing-tools/``;
  * the JLCImport and JLCPCB Tools KiCad plugins (via KiCad's own official PCM repository
    metadata at kicad.github.io) into the detected KiCad user ``3rdparty/plugins`` directory.

The JLC plugin step writes outside this repository, into the local KiCad installation's plugin
directory. KiCadRoutingTools remains in Copperbrain's private integration directory and is never
installed over, or imported from, a customer project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from platformdirs import user_documents_path

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.config import Settings

_USER_AGENT = "Copperbrain-Setup/0.1"
_ALLOWED_HOST_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
    "adoptium.net",
    "kicad.github.io",
)
_REQUEST_TIMEOUT_SECONDS = 20
_DOWNLOAD_TIMEOUT_SECONDS = 300
_MAX_DOWNLOAD_BYTES = 250_000_000
_KICAD_ROUTING_TOOLS_VERSION = "0.18.2"
_KICAD_ROUTING_TOOLS_SHA256 = "fcdec9e9c4ff3c614407831eda0abab15f8a57942e65fa0ea1f8a6288611bd64"


def _allowed_host(url: str) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}") for suffix in _ALLOWED_HOST_SUFFIXES
    )


def _require_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not _allowed_host(url):
        raise RuntimeError(f"Refusing a non-allowlisted or non-HTTPS URL: {url}")


def _get_json(url: str) -> Any:
    _require_allowed(url)
    accept = (
        "application/vnd.github+json"
        if (urllib.parse.urlparse(url).hostname or "").endswith("github.com")
        else "application/json"
    )
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        _require_allowed(response.geturl())
        return json.loads(response.read().decode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, *, expected_sha256: str | None) -> str:
    """Download atomically over HTTPS, verifying a checksum when one is available."""
    _require_allowed(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            _require_allowed(response.geturl())
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None and int(declared_length) > _MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"Refusing download larger than {_MAX_DOWNLOAD_BYTES} bytes")
            with open(descriptor, "wb") as stream:
                downloaded = 0
                while chunk := response.read(1 << 20):
                    downloaded += len(chunk)
                    if downloaded > _MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(f"Download exceeded {_MAX_DOWNLOAD_BYTES} byte limit")
                    digest.update(chunk)
                    stream.write(chunk)
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual.lower() != expected_sha256.lower():
            raise RuntimeError(
                f"Checksum mismatch for {url}: expected {expected_sha256}, got {actual}"
            )
        temp_path.replace(destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    verified = (
        "verified" if expected_sha256 is not None else "unverified: source published no checksum"
    )
    print(f"  sha256 ({verified}): {actual}")
    return actual


def _safe_extract_zip(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not (target / member.filename).resolve().is_relative_to(resolved_target):
                raise RuntimeError(f"Refusing to extract an unsafe path: {member.filename}")
        bundle.extractall(target)


def setup_kicad_routing_tools(data_dir: Path) -> None:
    version = _KICAD_ROUTING_TOOLS_VERSION
    asset_name = f"KiCadRoutingTools-{version}.zip"
    download_url = (
        f"https://github.com/drandyhaas/KiCadRoutingTools/releases/download/v{version}/{asset_name}"
    )
    target = data_dir / "integrations" / "kicad-routing-tools" / version
    runtime_root = target / "plugins"
    if (runtime_root / "route.py").is_file() and (runtime_root / "VERSION").is_file():
        _validate_routing_runtime(runtime_root)
        print(f"KiCadRoutingTools {version} is already installed at {runtime_root}")
        return
    print(f"Downloading KiCadRoutingTools {version} from {download_url}...")
    with tempfile.TemporaryDirectory(prefix="copperbrain-kicad-routing-tools-") as workdir:
        archive = Path(workdir) / asset_name
        extracted = Path(workdir) / "extracted"
        _download(
            download_url,
            archive,
            expected_sha256=_KICAD_ROUTING_TOOLS_SHA256,
        )
        _safe_extract_zip(archive, extracted)
        staged_runtime = extracted / "plugins"
        if (
            not (staged_runtime / "route.py").is_file()
            or not (staged_runtime / "LICENSE").is_file()
        ):
            raise RuntimeError("KiCadRoutingTools PCM archive has an unexpected layout")
        rust_dir = staged_runtime / "rust_router"
        machine = platform.machine().lower()
        if sys.platform == "win32" and machine in ("amd64", "x86_64"):
            source = rust_dir / "grid_router-windows-x86_64.pyd"
            canonical = rust_dir / "grid_router.pyd"
        elif sys.platform.startswith("linux") and machine in ("amd64", "x86_64"):
            source = rust_dir / "grid_router-linux-x86_64.so"
            canonical = rust_dir / "grid_router.so"
        elif sys.platform == "darwin" and machine == "arm64":
            source = rust_dir / "grid_router-macos-arm64.so"
            canonical = rust_dir / "grid_router.so"
        elif sys.platform == "darwin" and machine in ("amd64", "x86_64"):
            source = rust_dir / "grid_router-macos-x86_64.so"
            canonical = rust_dir / "grid_router.so"
        else:
            raise RuntimeError(f"No prebuilt KiCadRoutingTools core for {sys.platform}/{machine}")
        if not source.is_file():
            raise RuntimeError(f"KiCadRoutingTools archive is missing {source.name}")
        shutil.copy2(source, canonical)
        _validate_routing_runtime(staged_runtime)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise RuntimeError(f"Refusing to replace incomplete runtime directory: {target}")
        shutil.move(str(extracted), target)
    print(f"KiCadRoutingTools {version} installed at {runtime_root}")


def _validate_routing_runtime(runtime_root: Path) -> None:
    """Prove Python dependencies and the platform Rust ABI before installation succeeds."""
    try:
        result = subprocess.run(
            [sys.executable, str(runtime_root / "startup_checks.py")],
            cwd=runtime_root,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"KiCadRoutingTools runtime validation failed: {exc}") from exc
    if result.returncode != 0:
        reason = (result.stderr or result.stdout)[-4000:]
        raise RuntimeError(f"KiCadRoutingTools runtime validation failed: {reason}")


def _kicad_plugin_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    detection = detect_kicad()
    if detection.user_data_directories:
        return detection.user_data_directories[0] / "3rdparty" / "plugins"
    documents_kicad = user_documents_path() / "KiCad"
    if documents_kicad.is_dir():
        versions = sorted(
            (path for path in documents_kicad.iterdir() if path.is_dir()), reverse=True
        )
        if versions:
            return versions[0] / "3rdparty" / "plugins"
    raise RuntimeError(
        "Could not locate a KiCad user data directory. Start KiCad 10 at least once, then "
        "re-run this script, or pass --kicad-plugin-dir explicitly."
    )


def setup_jlc_plugins(plugin_root: Path) -> None:
    repository = _get_json("https://kicad.github.io/addons/repository.json")
    try:
        packages_url = repository["resources"]["packages"]["url"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            "KiCad's official addon repository metadata has an unexpected shape; install "
            "JLCImport/JLCPCB Tools manually through KiCad's Plugin and Content Manager instead."
        ) from exc
    packages = _get_json(packages_url).get("packages", [])
    matches = [
        package
        for package in packages
        if "jlc" in str(package.get("name", "")).lower()
        or "jlc" in str(package.get("identifier", "")).lower()
    ]
    if not matches:
        print("No JLC-related plugin is currently listed in KiCad's official repository; skipping.")
        return
    plugin_root.mkdir(parents=True, exist_ok=True)
    for package in matches:
        versions = package.get("versions", [])
        if not versions:
            continue
        latest = versions[-1]
        download_url = latest.get("download_url")
        identifier = package.get("identifier")
        if not download_url or not identifier:
            print(f"  skipping {package.get('name', '<unnamed>')}: missing download metadata")
            continue
        install_dir = plugin_root / identifier
        print(f"Installing {package.get('name', identifier)} ({identifier}) into {install_dir}...")
        with tempfile.TemporaryDirectory(prefix="copperbrain-jlc-plugin-") as workdir:
            archive_path = Path(workdir) / f"{identifier}.zip"
            _download(download_url, archive_path, expected_sha256=latest.get("download_sha256"))
            if install_dir.exists():
                shutil.rmtree(install_dir)
            _safe_extract_zip(archive_path, install_dir)
        print(f"  installed {package.get('name', identifier)} {latest.get('version', '')}".rstrip())


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument(
        "--skip-routing", action="store_true", help="Do not download KiCadRoutingTools"
    )
    parser.add_argument(
        "--skip-jlc-plugins", action="store_true", help="Do not install JLC KiCad plugins"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None, help="Override COPPERBRAIN_DATA_DIR for this run"
    )
    parser.add_argument(
        "--kicad-plugin-dir",
        type=Path,
        default=None,
        help="Explicit KiCad 3rdparty/plugins directory (skips auto-detection)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir or Settings.from_environment().data_dir

    print("Copperbrain dependency setup")
    print(f"  Copperbrain data directory: {data_dir}")
    if not args.skip_routing:
        print(
            "  - KiCadRoutingTools + Rust core       -> "
            "integrations/kicad-routing-tools/ (data dir)"
        )
    if not args.skip_jlc_plugins:
        print("  - JLCImport / JLCPCB Tools plugins   -> KiCad's 3rdparty/plugins/ (not in repo)")
    print()
    print("All downloads use HTTPS from official sources only (GitHub and kicad.github.io)")
    print("and are checksum-verified whenever the source publishes one.")
    print()
    if not _confirm("Proceed?", assume_yes=args.yes):
        print("Aborted.")
        return

    failures: list[str] = []

    if not args.skip_routing:
        try:
            setup_kicad_routing_tools(data_dir)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            print(f"KiCadRoutingTools setup failed: {exc}", file=sys.stderr)
            failures.append("kicad-routing-tools")

    if not args.skip_jlc_plugins:
        try:
            plugin_root = _kicad_plugin_root(args.kicad_plugin_dir)
            setup_jlc_plugins(plugin_root)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            print(f"JLC plugin setup failed: {exc}", file=sys.stderr)
            print(
                "  install JLCImport/JLCPCB Tools manually via KiCad's Plugin and Content Manager"
            )
            failures.append("jlc-plugins")

    print()
    if failures:
        print(f"Finished with failures: {', '.join(failures)}")
    else:
        print("Finished.")
    print("Call the MCP tools `detect_kicad` and `get_routing_backend_status` to confirm what")
    print("Copperbrain now sees. The MCP routes only explicit, nonempty net batches.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
