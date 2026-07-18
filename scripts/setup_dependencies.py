"""Explicit, hash-verified downloader for optional Copperbrain runtime integrations.

Never runs automatically and never runs through MCP. The user invokes it directly:

    uv run python scripts/setup_dependencies.py

It fetches, from official sources only, over HTTPS, verifying a published checksum whenever
the source provides one:

  * a Java runtime (Eclipse Temurin JDK, via the Adoptium API) into
    ``<COPPERBRAIN_DATA_DIR>/integrations/java/``;
  * the latest official FreeRouting release JAR (via the GitHub Releases API) into
    ``<COPPERBRAIN_DATA_DIR>/integrations/freerouting/``, with an honest
    ``scoped_net_classes_cli=false`` capability record (see below);
  * the JLCImport and JLCPCB Tools KiCad plugins (via KiCad's own official PCM repository
    metadata at kicad.github.io) into the detected KiCad user ``3rdparty/plugins`` directory.

The JLC plugin step writes outside this repository, into the local KiCad installation's plugin
directory. Nothing here grants "scoped" (net-class-limited) FreeRouting routing: that requires a
JAR whose headless ``-inc`` behavior has been independently verified, which this script cannot
claim on your behalf. It always records ``scoped_net_classes_cli=false`` for what it downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
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
            with open(descriptor, "wb") as stream:
                while chunk := response.read(1 << 20):
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


def _safe_extract_tar(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    resolved_target = target.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if not (target / member.name).resolve().is_relative_to(resolved_target):
                raise RuntimeError(f"Refusing to extract an unsafe path: {member.name}")
        bundle.extractall(target)


def _extract(archive: Path, target: Path) -> None:
    if archive.suffix == ".zip":
        _safe_extract_zip(archive, target)
    elif archive.name.endswith((".tar.gz", ".tgz")):
        _safe_extract_tar(archive, target)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _adoptium_os() -> str:
    system = platform.system().lower()
    mapping = {"windows": "windows", "darwin": "mac", "linux": "linux"}
    if system not in mapping:
        raise RuntimeError(f"Unsupported operating system for Java download: {system}")
    return mapping[system]


def _adoptium_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "aarch64"
    raise RuntimeError(f"Unsupported CPU architecture for Java download: {machine}")


def setup_java(data_dir: Path) -> None:
    os_name = _adoptium_os()
    arch = _adoptium_arch()
    url = (
        "https://api.adoptium.net/v3/assets/latest/25/hotspot"
        f"?image_type=jdk&os={os_name}&architecture={arch}&vendor=eclipse"
    )
    assets = _get_json(url)
    if not assets:
        raise RuntimeError("Adoptium has no Java 25 build published for this platform yet")
    package = assets[0]["binary"]["package"]
    target_dir = data_dir / "integrations" / "java"
    archive_path = target_dir / package["name"]
    print(f"Downloading Java 25 ({os_name}/{arch}) from Adoptium...")
    _download(package["link"], archive_path, expected_sha256=package.get("checksum"))
    print(f"Extracting {archive_path.name}...")
    _extract(archive_path, target_dir)
    archive_path.unlink()
    print(f"Java installed under {target_dir}")


def setup_freerouting(data_dir: Path) -> None:
    release = _get_json("https://api.github.com/repos/freerouting/freerouting/releases/latest")
    jar_asset = next(
        (asset for asset in release.get("assets", ()) if asset["name"].lower().endswith(".jar")),
        None,
    )
    if jar_asset is None:
        raise RuntimeError("The latest FreeRouting GitHub release has no .jar asset")
    version = str(release.get("tag_name", "")).lstrip("v") or "unknown"
    name = (
        jar_asset["name"]
        if jar_asset["name"].lower().startswith("freerouting")
        else (f"freerouting-{version}.jar")
    )
    target_dir = data_dir / "integrations" / "freerouting"
    jar_path = target_dir / name
    digest_field = jar_asset.get("digest") or ""
    expected = digest_field.split(":", 1)[1] if digest_field.startswith("sha256:") else None
    print(f"Downloading FreeRouting {version} from {jar_asset['browser_download_url']}...")
    actual_hash = _download(jar_asset["browser_download_url"], jar_path, expected_sha256=expected)
    capability_path = jar_path.with_name(f"{jar_path.name}.capabilities.json")
    capability_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jar_sha256": actual_hash,
                "scoped_net_classes_cli": False,
                "description": (
                    "Unmodified upstream FreeRouting release fetched by "
                    "scripts/setup_dependencies.py. Headless -inc net-class exclusion is not "
                    "independently verified for this build, so only full-board routing runs. "
                    "Only flip this to true after verifying scoped exclusion on this exact JAR."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"FreeRouting installed at {jar_path}")
    print(f"  wrote {capability_path.name} (scoped_net_classes_cli=false, see its description)")


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
    parser.add_argument("--skip-java", action="store_true", help="Do not download a Java runtime")
    parser.add_argument(
        "--skip-freerouting", action="store_true", help="Do not download FreeRouting"
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
    if not args.skip_java:
        print("  - Java 25 (Eclipse Temurin JDK)      -> integrations/java/ (inside data dir)")
    if not args.skip_freerouting:
        print("  - FreeRouting (latest official JAR)  -> integrations/freerouting/ (data dir)")
    if not args.skip_jlc_plugins:
        print("  - JLCImport / JLCPCB Tools plugins   -> KiCad's 3rdparty/plugins/ (not in repo)")
    print()
    print("All downloads use HTTPS from official sources only (GitHub, Adoptium, kicad.github.io)")
    print("and are checksum-verified whenever the source publishes one.")
    print()
    if not _confirm("Proceed?", assume_yes=args.yes):
        print("Aborted.")
        return

    failures: list[str] = []

    if not args.skip_java:
        try:
            setup_java(data_dir)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            print(f"Java setup failed: {exc}", file=sys.stderr)
            failures.append("java")

    if not args.skip_freerouting:
        try:
            setup_freerouting(data_dir)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            print(f"FreeRouting setup failed: {exc}", file=sys.stderr)
            failures.append("freerouting")

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
    print("Copperbrain now sees. Scoped (net-class-limited) routing still requires a JAR with an")
    print("independently verified .capabilities.json; this script never claims that for you.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
