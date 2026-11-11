"""Build the single-file portable Windows app and a source-complete release ZIP."""
from importlib import metadata
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
validation = root / "validation"
validation.mkdir(exist_ok=True)
output = root / "dist"
output.mkdir(exist_ok=True)
staging = root / "build/release"
staging.mkdir(parents=True, exist_ok=True)
license_dir = staging / "licenses"
if license_dir.exists():
    if not license_dir.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Release license directory resolves outside the project")
    shutil.rmtree(license_dir)
license_dir.mkdir()
# Also embed notices so they travel with a standalone copy of the EXE.
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name", "")
    if not (name.lower() in ("bleak", "typing_extensions", "pyinstaller") or name.startswith("winrt-")):
        continue
    for relative in distribution.files or []:
        if "license" in str(relative).lower() and not str(relative).endswith(".py"):
            source = distribution.locate_file(relative)
            if source.is_file():
                destination = license_dir / name / Path(relative).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
for source, filename in ((Path(sys.base_prefix) / "LICENSE.txt", "Python.txt"),
                         (Path(sys.base_prefix) / "tcl/tcl8.6/license.terms", "Tcl.txt"),
                         (Path(sys.base_prefix) / "tcl/tk8.6/license.terms", "Tk.txt")):
    if source.is_file():
        shutil.copy2(source, license_dir / filename)
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--onefile", "--name", "NC300Control", "--collect-submodules", "bleak.backends.winrt",
           "--collect-submodules", "winrt", "--add-data", "LICENSE;.",
           "--add-data", "THIRD_PARTY.md;.", "--add-data", "nc300/assets;nc300/assets",
           "--add-data", "build/release/licenses;licenses",
           "--icon", "nc300/assets/app.ico", "run_nc300.py"]
print("Building NC300 Control portable EXE. Output: validation/build.log", flush=True)
result = subprocess.run(command, cwd=root, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, errors="replace")
(validation / "build.log").write_text(result.stdout, encoding="utf-8")
if result.returncode:
    print(result.stdout[-12000:])
    raise SystemExit(result.returncode)

source_archive = output / "NC300Control-source.zip"
with zipfile.ZipFile(source_archive, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("README.md", "LICENSE", "THIRD_PARTY.md", "requirements.txt", "requirements-dev.txt",
                 "Start-NC300.cmd", "Build.ps1", "run_nc300.py", ".gitignore", ".gitattributes"):
        archive.write(root / name, "NC300Control/" + name)
    for directory in ("nc300", "tests", "tools"):
        for source in sorted((root / directory).rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts and source.suffix in (".py", ".ico", ".png"):
                archive.write(source, "NC300Control/" + source.relative_to(root).as_posix())

executable = output / "NC300Control.exe"
release = output / "NC300Control-portable.zip"
with zipfile.ZipFile(release, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in (executable, source_archive, root / "README.md", root / "LICENSE", root / "THIRD_PARTY.md"):
        archive.write(path, path.name)
    for path in sorted(license_dir.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(staging).as_posix())
checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                    for path in (executable, source_archive, release))
(output / "SHA256SUMS.txt").write_text(checksums, encoding="ascii")
print("Build complete: " + str(executable))
print("GitHub release bundle: " + str(release))
