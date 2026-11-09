"""Validate PyInstaller's onedir output and create the portable ZIP, on any OS."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Optional
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))
import ddi_support

ARCHIVE_NAME = "Chill-Windows-Portable.zip"


def validate_distribution(folder: Path, source: Path = ROOT) -> list[Path]:
    folder, source = Path(folder), Path(source)
    executable = folder / "Chill.exe"
    if not executable.is_file():
        raise ValueError("Missing Chill.exe; build the complete onedir distribution first")
    with executable.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("Chill.exe is not a Windows PE executable")
    # Compare every shipped UI asset, including the offline Leaflet fallback.
    assets = sorted(path for path in (source / "ui").rglob("*") if path.is_file())
    if not assets or not (source / "ui/index.html").is_file():
        raise ValueError("Source UI is missing")
    for asset in assets:
        bundled = folder / "_internal" / asset.relative_to(source)
        if not bundled.is_file() or bundled.read_bytes() != asset.read_bytes():
            raise ValueError("Missing or stale bundled resource: " + str(asset.relative_to(source)))
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise ValueError("Portable Windows distributions must not contain symbolic links")
    for variant in ddi_support.DDI_ASSETS:
        ddi_support.image_files(folder / "_internal" / ddi_support.DDI_ROOT / variant, variant)
    return assets


def package_distribution(folder: Path, output: Path, *, source: Path = ROOT,
                         dependencies: Optional[Path] = None) -> Path:
    folder, output, source = Path(folder).resolve(), Path(output).resolve(), Path(source).resolve()
    if output == folder or folder in output.parents:
        raise ValueError("The ZIP must be outside the distribution folder")
    validate_distribution(folder, source)
    for relative in ("README_FOR_HER.md", "README.md", "ENGINE.md", "artifacts/piggy-desktop-zh.jpg"):
        origin, destination = source / relative, folder / relative
        if not origin.is_file():
            raise ValueError("Missing documentation resource: " + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
    if dependencies is not None:
        shutil.copy2(dependencies, folder / "dependencies-windows.txt")
    manifest = {
        "application": "Chill", "executable": "Chill.exe", "layout": "onedir",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "commit": os.environ.get("GITHUB_SHA"), "build_python": sys.version.split()[0],
        "files": {},
    }
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path != folder / "BUILD_INFO.json":
            with path.open("rb") as handle:
                digest = hashlib.sha256()
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            manifest["files"][path.relative_to(folder).as_posix()] = digest.hexdigest()
    (folder / "BUILD_INFO.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    # The previous good archive remains intact if compression or validation fails.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".pace-", suffix=".zip", delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(folder).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            corrupt = archive.testzip()
            if corrupt:
                raise ValueError("ZIP CRC check failed: " + corrupt)
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=ROOT / "dist/Chill")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ARCHIVE_NAME)
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument("--prepare-ddi", action="store_true", help="stage verified offline images before PyInstaller")
    args = parser.parse_args()
    try:
        if args.prepare_ddi:
            for variant in ddi_support.DDI_ASSETS:
                ddi_support.prepare_image(ROOT / ddi_support.DDI_ROOT / variant, variant)
                print(f"Verified DDI assets: {variant} ({ddi_support.DDI_REVISION})")
            return 0
        output = package_distribution(args.folder, args.output, dependencies=args.dependencies)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, "Packaging failed: " + str(error) + "\n")
    print("Validated portable archive: " + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
