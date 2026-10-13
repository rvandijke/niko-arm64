#!/usr/bin/env python3
"""Build a native Apple Silicon (arm64) copy of the Intel-only Niko Home Control programming software.

The app is Electron (UI) + a self-contained .NET backend. Niko's own code is CPU-neutral
(JavaScript and .NET IL); only the third-party runtimes are Intel builds. This script swaps
those for the official arm64 builds of the same versions, and then:

  - precompiles all .NET assemblies for arm64 (ReadyToRun), so nothing is JIT-compiled at startup
  - fixes the backend lingering after quit (it ignores SIGINT/SIGTERM; we SIGKILL it on exit)

By default it finds the Niko app in /Applications and writes
"/Applications/<same name> (Apple Silicon).app" next to it. The source app is never modified.
Run with --help for options.

Requirements: macOS, Python 3.9+ (the one that comes with the Xcode Command Line Tools is fine),
internet access for the downloads (~170 MB, cached), about 1.5 GB free disk space.
Run it on an Apple Silicon Mac: on an Intel Mac the build works but can't be smoke-tested.

Examples:
    ./build-niko-arm64.py
    ./build-niko-arm64.py --source ~/Downloads/"Niko Home Control programming software.app" --output ~/Applications/Niko.app
    ./build-niko-arm64.py --sign-identity "Developer ID Application: Jane Doe (ABCDE12345)"

Not affiliated with Niko. The result is for personal use on your own Mac; don't redistribute it.
"""
import argparse
import concurrent.futures
import json
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

APPLICATIONS = Path("/Applications")
NIKO_BUNDLE_ID = "be.niko.smart.configtool"
OUTPUT_SUFFIX = " (Apple Silicon)"
DEFAULT_CACHE = Path.home() / "Library/Caches/niko-arm64"
AD_HOC = "-"
BACKEND_DIR = Path("Contents/Resources/app/backend")
CONFIG_TOOL_MANAGER = Path("Contents/Resources/app/configToolManager.js")

# PE machine types (.NET assemblies use the PE file format, also on macOS)
MACHINE_X64 = 0x8664
MACHINE_ARM64 = 0xAA64
MACHINE_OS_XOR_OSX = 0x4644  # ReadyToRun images XOR the machine type with an OS-specific value
COR_FLAG_IL_ONLY = 0x1

# The .NET apphost launcher contains this placeholder; the SDK overwrites it with the app's DLL name.
APPHOST_PLACEHOLDER = b"c3ab8ff13720e8ad9047dd39466b3c8974e592c2fa383d4a3960714caef0c4f2"
# Electron's fuse settings live right after this marker in the framework binary.
ELECTRON_FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"

# Only used with a real signing identity (hardened runtime). Ad-hoc signatures don't need them.
# Electron and .NET both generate machine code at runtime (JIT), and the app loads ad-hoc-signed libraries.
HARDENED_RUNTIME_ENTITLEMENTS = {
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.disable-library-validation": True,
}

QUIT_FIX_ORIGINAL ="            _childProcessInfo.kill();\n"
QUIT_FIX_PATCHED = (
    "            // backend ignores SIGINT/SIGTERM and Electron exits without waiting, so force it\n"
    "            _childProcessInfo.kill('SIGKILL');\n"
)


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------------------------
# Reading the source app
# ---------------------------------------------------------------------------------------------

@dataclass
class SourceApp:
    path: Path
    executable: str       # CFBundleExecutable, also the prefix of the Electron helper apps
    version: str
    electron: str         # Electron version, e.g. "34.1.1"
    dotnet: str           # .NET runtime version, e.g. "10.0.9"
    sqlite: str           # SQLitePCLRaw.lib.e_sqlite3 package version
    backend_name: str     # e.g. "Niko.Config.Host.WebSockets"

    @property
    def electron_framework_binary(self) -> Path:
        return self.path / "Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework"


def read_source_app(path: Path) -> SourceApp:
    info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
    backend = path / BACKEND_DIR
    runtime_config_file = next(backend.glob("*.runtimeconfig.json"))
    backend_name = runtime_config_file.name.removesuffix(".runtimeconfig.json")

    framework_binary = path / "Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework"
    electron = re.search(rb"Electron/(\d+\.\d+\.\d+)", framework_binary.read_bytes()).group(1).decode()

    runtime_config = json.loads(runtime_config_file.read_text())["runtimeOptions"]
    dotnet = next(f["version"] for f in runtime_config["includedFrameworks"] if f["name"] == "Microsoft.NETCore.App")

    deps = (backend / f"{backend_name}.deps.json").read_text()
    sqlite = re.search(r'"SQLitePCLRaw\.lib\.e_sqlite3/([\d.]+)"', deps).group(1)

    return SourceApp(path, info["CFBundleExecutable"], info["CFBundleShortVersionString"],
                     electron, dotnet, sqlite, backend_name)


def find_intel_niko_app(folder: Path) -> Path:
    """The Niko app in `folder`, recognised by bundle ID. Skips builds this script made (already arm64)."""
    candidates = []
    for app in sorted(folder.glob("*.app")):
        try:
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        if info.get("CFBundleIdentifier") != NIKO_BUNDLE_ID:
            continue
        executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
        if "x86_64" in run("lipo", "-archs", executable).split():
            candidates.append(app)

    if not candidates:
        raise SystemExit(f"No Intel Niko Home Control app found in {folder}. Install it from niko.eu, or pass --source.")
    if len(candidates) > 1:
        raise SystemExit("Several Niko apps found; pick one with --source:\n  " + "\n  ".join(map(str, candidates)))
    return candidates[0]


def default_output_path(source: Path) -> Path:
    return APPLICATIONS / f"{source.stem}{OUTPUT_SUFFIX}.app"


# ---------------------------------------------------------------------------------------------
# Downloading the arm64 components (cached)
# ---------------------------------------------------------------------------------------------

@dataclass
class Components:
    electron_app: Path    # Electron.app (arm64)
    dotnet_runtime: Path  # extracted aspnetcore-runtime tarball (contains the .NET runtime too)
    apphost: Path         # arm64 .NET launcher template
    crossgen2: Path       # ReadyToRun compiler
    libe_sqlite3: Path


def download(url: str, target: Path) -> Path:
    if not target.exists():
        log(f"  downloading {target.name}")
        partial = target.with_suffix(".part")
        run("curl", "--fail", "--location", "--silent", "--show-error", "--output", partial, url)
        partial.rename(target)
    return target


def extract_once(archive: Path, extract) -> Path:
    """Extract next to the archive (once); `extract(destination)` does the actual work."""
    target = archive.with_suffix("")
    if not target.exists():
        partial = archive.with_suffix(".extracting")
        shutil.rmtree(partial, ignore_errors=True)
        extract(partial)
        partial.rename(target)
    return target


def nuget_package(package_id: str, version: str, cache: Path) -> Path:
    url = f"https://api.nuget.org/v3-flatcontainer/{package_id}/{version}/{package_id}.{version}.nupkg"
    archive = download(url, cache / f"{package_id}.{version}.nupkg")
    return extract_once(archive, lambda dest: zipfile.ZipFile(archive).extractall(dest))


def download_components(app: SourceApp, cache: Path) -> Components:
    cache.mkdir(parents=True, exist_ok=True)

    electron_name = f"electron-v{app.electron}-darwin-arm64.zip"
    electron_zip = download(
        f"https://github.com/electron/electron/releases/download/v{app.electron}/{electron_name}", cache / electron_name)
    # ditto keeps the framework symlinks intact; zipfile would not
    electron = extract_once(electron_zip, lambda dest: run("ditto", "-xk", electron_zip, dest))

    runtime_name = f"aspnetcore-runtime-{app.dotnet}-osx-arm64.tar.gz"
    runtime_tarball = download(
        f"https://builds.dotnet.microsoft.com/dotnet/aspnetcore/Runtime/{app.dotnet}/{runtime_name}", cache / runtime_name)
    runtime = extract_once(runtime_tarball, lambda dest: tarfile.open(runtime_tarball).extractall(dest))

    apphost_package = nuget_package("microsoft.netcore.app.host.osx-arm64", app.dotnet, cache)
    crossgen2_package = nuget_package("microsoft.netcore.app.crossgen2.osx-arm64", app.dotnet, cache)
    sqlite_package = nuget_package("sqlitepclraw.lib.e_sqlite3", app.sqlite, cache)

    crossgen2 = crossgen2_package / "tools/crossgen2"
    crossgen2.chmod(0o755)  # zip extraction drops the executable bit

    return Components(
        electron_app=electron / "Electron.app",
        dotnet_runtime=runtime,
        apphost=apphost_package / "runtimes/osx-arm64/native/apphost",
        crossgen2=crossgen2,
        libe_sqlite3=sqlite_package / "runtimes/osx-arm64/native/libe_sqlite3.dylib",
    )


# ---------------------------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------------------------

def run(*command) -> str:
    return subprocess.run([str(c) for c in command], check=True, capture_output=True, text=True).stdout


def replace_file(source: Path, target: Path) -> None:
    """Replace target with a copy of source.

    Deletes first: overwriting a signed binary in place makes the kernel kill it later,
    because it caches the old code signature per file.
    """
    target.unlink()
    shutil.copy2(source, target)


def write_bytes_fresh(target: Path, data: bytes) -> None:
    target.unlink()
    target.write_bytes(data)


def is_macho(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")  # thin 64-bit, universal


# ---------------------------------------------------------------------------------------------
# Step 1: Electron shell
# ---------------------------------------------------------------------------------------------

def swap_electron(app: SourceApp, stage: Path, electron_app: Path) -> None:
    """Replace the Intel Electron binaries with arm64 ones. Niko's JavaScript is left alone."""
    ours, theirs = stage / "Contents", electron_app / "Contents"

    for framework in (ours / "Frameworks").glob("*.framework"):
        replacement = theirs / "Frameworks" / framework.name
        if replacement.exists():
            shutil.rmtree(framework)
            run("ditto", replacement, framework)

    # Helper apps are renamed copies: "<executable> Helper (GPU).app" <- "Electron Helper (GPU).app"
    for helper in (ours / "Frameworks").glob(f"{app.executable} Helper*.app"):
        suffix = helper.stem.removeprefix(app.executable)  # " Helper (GPU)"
        replace_file(theirs / f"Frameworks/Electron{suffix}.app/Contents/MacOS/Electron{suffix}",
                     helper / f"Contents/MacOS/{app.executable}{suffix}")

    replace_file(theirs / "MacOS/Electron", ours / "MacOS" / app.executable)


def copy_electron_fuses(app: SourceApp, stage: Path) -> None:
    """Give the arm64 framework the same fuse settings (security switches) as the original."""
    def fuse_wire(data: bytes) -> tuple[int, bytes]:
        start = data.index(ELECTRON_FUSE_SENTINEL) + len(ELECTRON_FUSE_SENTINEL)
        length = 2 + data[start + 1]  # version byte, count byte, one byte per fuse
        return start, data[start:start + length]

    target = stage / app.electron_framework_binary.relative_to(app.path)
    _, original = fuse_wire(app.electron_framework_binary.read_bytes())
    data = bytearray(target.read_bytes())
    start, current = fuse_wire(data)

    if current == original:
        return
    if len(current) != len(original):
        raise SystemExit("Electron fuse layout differs between builds; check the fuses manually.")
    data[start:start + len(original)] = original
    write_bytes_fresh(target, bytes(data))
    log(f"  applied original Electron fuses {original!r}")


# ---------------------------------------------------------------------------------------------
# Step 2: .NET runtime, launcher and SQLite
# ---------------------------------------------------------------------------------------------

def swap_dotnet_runtime(app: SourceApp, backend: Path, components: Components) -> int:
    """Replace every bundled runtime file with its arm64 counterpart. Returns how many were swapped."""
    runtime = components.dotnet_runtime
    search_dirs = [
        runtime / "shared/Microsoft.NETCore.App" / app.dotnet,
        runtime / "shared/Microsoft.AspNetCore.App" / app.dotnet,
        runtime / "host/fxr" / app.dotnet,
    ]
    swapped = 0
    for file in backend.iterdir():
        replacement = next((d / file.name for d in search_dirs if (d / file.name).is_file()), None)
        if replacement:
            replace_file(replacement, file)
            swapped += 1

    replace_file(components.libe_sqlite3, backend / "libe_sqlite3.dylib")
    return swapped


def write_apphost(app: SourceApp, backend: Path, template: Path) -> None:
    """The launcher binary: a generic template with the backend DLL's name patched in."""
    data = template.read_bytes()
    if data.count(APPHOST_PLACEHOLDER) != 1:
        raise SystemExit("Unexpected apphost template: placeholder not found exactly once.")
    dll_name = f"{app.backend_name}.dll".encode()
    patched = data.replace(APPHOST_PLACEHOLDER, dll_name.ljust(len(APPHOST_PLACEHOLDER), b"\0"))

    launcher = backend / app.backend_name
    write_bytes_fresh(launcher, patched)
    launcher.chmod(0o755)


def retarget_deps_file(app: SourceApp, backend: Path) -> None:
    deps_file = backend / f"{app.backend_name}.deps.json"
    content = deps_file.read_text()
    deps_file.write_text(content.replace("osx-x64", "osx-arm64"))


# ---------------------------------------------------------------------------------------------
# Steps 3 + 4: .NET assemblies
# ---------------------------------------------------------------------------------------------

class AssemblyHeader(NamedTuple):
    machine: int
    il_only: bool
    precompiled: bool        # has ReadyToRun native code
    machine_offset: int      # where the machine field sits in the file


def read_assembly_header(path: Path) -> Optional[AssemblyHeader]:
    """Parse just enough of a PE file to classify a .NET assembly. None if it isn't one."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return None

    machine, section_count = struct.unpack_from("<HH", data, pe + 4)
    optional_header_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional_header = pe + 24
    is_pe32 = struct.unpack_from("<H", data, optional_header)[0] == 0x10B
    data_directories = optional_header + (96 if is_pe32 else 112)
    cli_header_rva = struct.unpack_from("<I", data, data_directories + 14 * 8)[0]  # entry 14 = CLR header
    if not cli_header_rva:
        return None

    section_table = optional_header + optional_header_size
    for i in range(section_count):
        virtual_size, virtual_address, _, raw_offset = struct.unpack_from("<IIII", data, section_table + i * 40 + 8)
        if virtual_address <= cli_header_rva < virtual_address + max(virtual_size, 1):
            cli_header = cli_header_rva - virtual_address + raw_offset
            flags = struct.unpack_from("<I", data, cli_header + 16)[0]
            native_header_rva = struct.unpack_from("<I", data, cli_header + 64)[0]
            return AssemblyHeader(machine, bool(flags & COR_FLAG_IL_ONLY), native_header_rva != 0, pe + 4)
    return None


def find_il_assemblies(backend: Path) -> list[Path]:
    """All assemblies that still need precompiling. Fails on anything that can't run on arm64."""
    il_assemblies = []
    for dll in sorted(backend.glob("*.dll")):
        header = read_assembly_header(dll)
        if header is None:
            continue
        if header.precompiled:
            if header.machine != MACHINE_ARM64 ^ MACHINE_OS_XOR_OSX:
                raise SystemExit(f"{dll.name} is precompiled for another CPU and has no arm64 replacement.")
            continue
        if not header.il_only:
            raise SystemExit(f"{dll.name} contains native code and can't be converted.")
        il_assemblies.append(dll)
    return il_assemblies


def retarget_x64_assemblies(assemblies: list[Path]) -> None:
    """IL-only assemblies built with PlatformTarget=x64 refuse to load on arm64.
    The code inside is CPU-neutral, so flipping the machine field is enough."""
    for dll in assemblies:
        header = read_assembly_header(dll)
        if header.machine == MACHINE_X64:
            data = bytearray(dll.read_bytes())
            struct.pack_into("<H", data, header.machine_offset, MACHINE_ARM64)
            write_bytes_fresh(dll, bytes(data))
            log(f"  marked {dll.name} as arm64")


def precompile_assemblies(assemblies: list[Path], backend: Path, crossgen2: Path, work: Path) -> None:
    """ReadyToRun-compile assemblies to arm64 code, so the JIT doesn't compile them at every launch."""
    output_dir = work / "readytorun"
    output_dir.mkdir()

    def compile_one(dll: Path) -> Path:
        output = output_dir / dll.name
        run(crossgen2, dll, "-o", output, "-r", f"{backend}/*.dll",
            "--targetos:osx", "--targetarch:arm64", "-O")
        return output

    with concurrent.futures.ThreadPoolExecutor(os.cpu_count()) as pool:
        compiled = list(pool.map(compile_one, assemblies))
    # Replace only after all compiled: crossgen2 reads the originals as references
    for dll, output in zip(assemblies, compiled):
        replace_file(output, dll)


# ---------------------------------------------------------------------------------------------
# Step 5: quit fix
# ---------------------------------------------------------------------------------------------

def apply_quit_fix(stage: Path) -> bool:
    """On quit, Electron sends the backend SIGINT and exits ~5 ms later; the backend ignores it
    and lingers. Make the synchronous exit handler SIGKILL it. Returns False if the code changed."""
    script = stage / CONFIG_TOOL_MANAGER
    if not script.exists():
        return False
    content = script.read_text()
    if QUIT_FIX_PATCHED in content:
        return True
    if content.count(QUIT_FIX_ORIGINAL) != 1:
        return False
    script.write_text(content.replace(QUIT_FIX_ORIGINAL, QUIT_FIX_PATCHED))
    return True


# ---------------------------------------------------------------------------------------------
# Step 6: sign and verify
# ---------------------------------------------------------------------------------------------

def find_macho_files(bundle: Path) -> list[Path]:
    return [p for p in bundle.rglob("*") if p.is_file() and not p.is_symlink() and is_macho(p)]


def sign_bundle(bundle: Path, identity: str, work: Path) -> None:
    """Sign inside-out: loose binaries, then nested bundles, then the app itself.

    Ad-hoc ("-") is enough to run the app on the Mac that built it. A real identity also enables
    the hardened runtime, which needs entitlements for the JIT compilers.
    """
    command = ["codesign", "--force", "--sign", identity]
    if identity != AD_HOC:
        entitlements = work / "entitlements.plist"
        entitlements.write_bytes(plistlib.dumps(HARDENED_RUNTIME_ENTITLEMENTS))
        command += ["--options", "runtime", "--timestamp", "--entitlements", entitlements]

    for binary in find_macho_files(bundle):
        run(*command, binary)
    frameworks = bundle / "Contents/Frameworks"
    for nested in [*frameworks.glob("*.framework"), *frameworks.glob("*.app")]:
        run(*command, nested)
    run(*command, bundle)


def verify_build(app: SourceApp, bundle: Path, precompiled: bool) -> None:
    not_arm64 = [p for p in find_macho_files(bundle) if "arm64" not in run("lipo", "-archs", p)]
    if not_arm64:
        raise SystemExit("Still not arm64:\n  " + "\n  ".join(map(str, not_arm64)))

    backend = bundle / BACKEND_DIR
    if precompiled and find_il_assemblies(backend):
        raise SystemExit("Some assemblies were not precompiled.")

    deps = json.loads((backend / f"{app.backend_name}.deps.json").read_text())
    if not deps["runtimeTarget"]["name"].endswith("osx-arm64"):
        raise SystemExit("deps.json was not retargeted to osx-arm64.")

    run("codesign", "--verify", "--deep", "--strict", bundle)

    if platform.machine() != "arm64":
        log("  not on Apple Silicon: skipping the backend smoke test")
        return
    # Starting the backend with --help loads the runtime and Niko's assemblies, then exits.
    result = subprocess.run([backend / app.backend_name, "--help"], capture_output=True, text=True, timeout=60)
    if app.backend_name.split(".")[0] not in result.stdout + result.stderr:  # help text goes to stderr
        raise SystemExit(f"Backend smoke test failed:\n{result.stdout}{result.stderr}")


# ---------------------------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------------------------

def check_can_install(destination: Path) -> None:
    """Fail before the build, not after: the destination must be writable and the old build closed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(destination.parent, os.W_OK):
        raise SystemExit(f"Can't write to {destination.parent}; pick another location with --output.")
    still_running = subprocess.run(["pgrep", "-f", f"{destination}/Contents/MacOS"], capture_output=True)
    if still_running.returncode == 0:
        raise SystemExit(f"{destination.name} is running; quit it and run this script again.")


def install(built: Path, destination: Path, previous_dir: Path) -> Path:
    """Move the build to destination. An existing build there is kept in previous_dir as a rollback."""
    check_can_install(destination)
    if destination.exists():
        previous = previous_dir / destination.name
        previous_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(previous, ignore_errors=True)
        destination.rename(previous)
        log(f"previous build kept in {previous_dir}/")
    shutil.move(built, destination)
    return destination


# ---------------------------------------------------------------------------------------------

@dataclass
class Options:
    source: Path
    output: Path          # the .app to write
    cache_dir: Path
    sign_identity: str
    precompile: bool
    quit_fix: bool


def build(options: Options) -> Path:
    source = options.source
    if not (source / "Contents/Info.plist").exists():
        raise SystemExit(f"Not an app bundle: {source}")
    check_can_install(options.output)
    app = read_source_app(source)
    log(f"{source.name} {app.version}: Electron {app.electron}, .NET {app.dotnet}, e_sqlite3 {app.sqlite}")
    components = download_components(app, options.cache_dir)

    # Build in a temp folder: macOS App Management blocks creating files inside an existing app bundle.
    work = Path(tempfile.mkdtemp(prefix="niko-arm64-"))
    try:
        return build_in(work, app, components, options)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_in(work: Path, app: SourceApp, components: Components, options: Options) -> Path:
    source = options.source
    stage = work / "stage"  # renamed to .app only after all edits are done
    backend = stage / BACKEND_DIR
    log("copying app")
    run("ditto", source, stage)

    log("1/6 Electron shell")
    swap_electron(app, stage, components.electron_app)
    copy_electron_fuses(app, stage)

    log("2/6 .NET runtime")
    swapped = swap_dotnet_runtime(app, backend, components)
    write_apphost(app, backend, components.apphost)
    retarget_deps_file(app, backend)
    log(f"  {swapped} runtime files")

    log("3/6 assemblies flagged x64")
    assemblies = find_il_assemblies(backend)
    retarget_x64_assemblies(assemblies)

    if options.precompile:
        log(f"4/6 precompiling {len(assemblies)} assemblies")
        precompile_assemblies(assemblies, backend, components.crossgen2, work)
    else:
        log("4/6 precompiling: skipped")

    if options.quit_fix:
        log("5/6 quit fix")
        if not apply_quit_fix(stage):
            log("  WARNING: shutdown code changed upstream; quit fix NOT applied. Check for leftover backends after quitting.")
    else:
        log("5/6 quit fix: skipped")

    log("6/6 signing and verifying")
    built = work / source.name
    stage.rename(built)
    sign_bundle(built, options.sign_identity, work)
    verify_build(app, built, options.precompile)

    return install(built, options.output, previous_dir=options.cache_dir / "previous")


def parse_options() -> Options:
    parser = argparse.ArgumentParser(
        description="Build a native Apple Silicon copy of the Niko Home Control programming software.")
    parser.add_argument("--source", type=Path,
                        help=f"the Intel app to convert (default: the Niko app found in {APPLICATIONS})")
    parser.add_argument("--output", type=Path,
                        help=f'the arm64 app to write (default: "{APPLICATIONS}/<source name>{OUTPUT_SUFFIX}.app"); '
                             'an existing build there is kept in <cache-dir>/previous/')
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                        help=f"where downloads are kept between runs (default: {DEFAULT_CACHE})")
    parser.add_argument("--sign-identity", default=AD_HOC,
                        help='codesign identity, e.g. "Developer ID Application: Name (TEAMID)" '
                             '(default: ad-hoc, which runs on the Mac that built it)')
    parser.add_argument("--no-precompile", dest="precompile", action="store_false",
                        help="skip ReadyToRun precompilation (faster build, slower first use of each screen)")
    parser.add_argument("--no-quit-fix", dest="quit_fix", action="store_false",
                        help="don't patch the app to stop the backend on quit")
    args = parser.parse_args()

    source = args.source.expanduser().resolve() if args.source else find_intel_niko_app(APPLICATIONS)
    output = args.output.expanduser().resolve() if args.output else default_output_path(source)
    if output.suffix != ".app":
        raise SystemExit("--output must end in .app")
    if output == source:
        raise SystemExit("--output can't be the source app.")
    return Options(source, output, args.cache_dir.expanduser().resolve(),
                   args.sign_identity, args.precompile, args.quit_fix)


def main() -> None:
    log(f"done: {build(parse_options())}")


if __name__ == "__main__":
    main()
