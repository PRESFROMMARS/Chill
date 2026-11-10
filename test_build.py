"""Packaging contracts and real ZIP validation, without building a Windows EXE."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ddi_support
from scripts import package_windows as package
from scripts import smoke_windows as smoke

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent


class WorkflowTests(unittest.TestCase):
    @unittest.skipIf(yaml is None, "install requirements-build.txt for YAML validation")
    def test_workflow_triggers_build_flags_and_artifact_contract(self):
        # BaseLoader keeps YAML 1.1 from interpreting the Actions key 'on' as True.
        data = yaml.load((ROOT / ".github/workflows/build_windows.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(data["on"]), {"push", "workflow_dispatch"})
        job = data["jobs"]["build"]
        self.assertEqual(job["runs-on"], "windows-latest")
        steps = job["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        for flag in ("--onedir", "--noconsole", "--uac-admin", "--noupx", "--collect-all pymobiledevice3",
                     "--recursive-copy-metadata pymobiledevice3", '--add-data "ui:ui"', '--add-data "assets/ddi:assets/ddi"',
                     '--icon "ui/assets/chill.ico"', "--name Chill"):
            self.assertIn(flag, commands)
        self.assertLess(commands.index("--prepare-ddi"), commands.index("python -m PyInstaller"))
        self.assertNotIn("--onefile", commands)
        self.assertIn("scripts/smoke_windows.py", commands)
        self.assertIn("scripts/package_windows.py", commands)
        self.assertIn("test_hardening", commands)
        mac = data["jobs"]["macos-tests"]
        self.assertEqual(mac["runs-on"], "macos-latest")
        mac_commands = "\n".join(step.get("run", "") for step in mac["steps"])
        for suite in ("test_engine", "test_main", "test_build", "test_precision", "test_audit",
                      "test_recovery", "test_scheduling", "test_boundaries", "test_hardening"):
            self.assertIn(suite, mac_commands)
        self.assertIn("pip install PyYAML", mac_commands)
        upload = next(step["with"] for step in steps
                      if step.get("with", {}).get("name") == "Chill-Windows-Portable")
        self.assertEqual(upload["path"], "dist/" + package.ARCHIVE_NAME)
        self.assertEqual(upload["if-no-files-found"], "error")
        self.assertEqual(upload["archive"], "false")

    @unittest.skipIf(yaml is None, "install requirements-build.txt for YAML validation")
    def test_release_waits_for_both_platforms_and_ships_complete_installer(self):
        data = yaml.load((ROOT / ".github/workflows/build_windows.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertIn("v*", data["on"]["push"]["tags"])
        release = data["jobs"]["release"]
        self.assertEqual(set(release["needs"]), {"build", "macos-tests"})
        self.assertEqual(release["permissions"]["contents"], "write")
        self.assertIn("refs/tags/v", release["if"])
        commands = "\n".join(step.get("run", "") for step in release["steps"])
        self.assertIn("gh release create", commands)
        self.assertIn("--verify-tag", commands)
        self.assertIn("release/Chill-Setup.exe", commands)
        installer = (ROOT / "scripts/installer_windows.iss").read_text()
        self.assertIn('Source: "..\\dist\\Chill\\*"', installer)
        self.assertIn("recursesubdirs", installer)
        self.assertIn("OutputBaseFilename=Chill-Setup", installer)

    def test_runtime_requirements_include_all_requested_libraries(self):
        text = (ROOT / "requirements.txt").read_text()
        for library in ("pywebview", "pymobiledevice3", "requests"):
            self.assertRegex(text, r"(?m)^" + library + r"[>=]")


class SmokeTests(unittest.TestCase):
    def test_windowed_helper_has_explicit_pipes_and_reports_failure(self):
        result = subprocess.CompletedProcess([], 2, stdout="missing packaged library")
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder)/"Chill.exe"
            executable.write_bytes(b"MZfixture")
            with patch.object(smoke.subprocess, "run", return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError, "missing packaged library"):
                    smoke.smoke(executable)
                self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(run.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_windowed_helper_timeout_keeps_the_diagnostic_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder)/"Chill.exe"
            executable.write_bytes(b"MZfixture")
            error = subprocess.TimeoutExpired([], 60, output=b"import stalled")
            with patch.object(smoke.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "import stalled"):
                    smoke.smoke(executable)

    def test_smoke_checks_the_direct_driver_imports_after_the_tunnel_cli(self):
        ok = subprocess.CompletedProcess([], 0, stdout="")
        broken = subprocess.CompletedProcess([], 1, stdout="missing Cryptex API")
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder)/"Chill.exe"
            executable.write_bytes(b"MZfixture")
            with patch.object(smoke.subprocess, "run", side_effect=[ok, broken]) as run:
                with self.assertRaisesRegex(RuntimeError, "missing Cryptex API"):
                    smoke.smoke(executable)
                self.assertEqual(run.call_args.args[0], [str(executable.resolve()), "--piggy-check-device-driver"])


class DeveloperImageDownloadTests(unittest.TestCase):
    def test_raw_timeout_and_bad_mirror_fall_back_to_verified_bytes(self):
        data = b"expected image"
        digest = (len(data), hashlib.sha256(data).hexdigest())
        with patch.object(ddi_support.urllib.request, "build_opener") as builder:
            opener = builder.return_value
            opener.open.side_effect = [TimeoutError("raw unavailable"), io.BytesIO(b"mirror error"),
                                       OSError("second mirror unavailable"), io.BytesIO(data)]
            self.assertEqual(ddi_support._download("Cryptex", "Image.dmg", digest), data)
            requests = opener.open.call_args_list
        self.assertEqual([request.kwargs["timeout"] for request in requests], [3, 3, 3, 3])
        self.assertEqual([request.args[0].split("/")[2] for request in requests],
                         ["raw.githubusercontent.com", "fastly.jsdelivr.net", "testingcf.jsdelivr.net",
                          "cdn.jsdelivr.net"])
        self.assertTrue(all(ddi_support.DDI_REVISION in request.args[0] for request in requests))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="piggy-package-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "Chill"
        self.folder.mkdir()
        (self.folder / "Chill.exe").write_bytes(b"MZtest-fixture")
        shutil.copytree(ROOT / "ui", self.folder / "_internal/ui")
        fixtures = {}
        for variant, files in ddi_support.DDI_ASSETS.items():
            folder = self.folder / "_internal" / ddi_support.DDI_ROOT / variant
            folder.mkdir(parents=True)
            fixtures[variant] = {}
            for name in files:
                data = f"{variant}/{name}".encode()
                (folder/name).write_bytes(data)
                fixtures[variant][name] = (len(data), hashlib.sha256(data).hexdigest())
        patcher = patch.object(ddi_support, "DDI_ASSETS", fixtures)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = self.folder.parent / package.ARCHIVE_NAME

    def test_complete_folder_archived_with_docs_hidden_files_and_verified_hashes(self):
        (self.folder / "_internal/.needed").write_bytes(b"hidden resource")
        package.package_distribution(self.folder, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            for variant, files in ddi_support.DDI_ASSETS.items():
                for name in files:
                    self.assertIn(f"_internal/assets/ddi/{variant}/{name}", names)
            for name in ("Chill.exe", "_internal/ui/index.html", "_internal/ui/vendor/leaflet.js",
                         "_internal/ui/vendor/leaflet.css", "_internal/ui/vendor/LEAFLET-LICENSE.txt",
                         "README_FOR_HER.md", "artifacts/piggy-desktop-zh.jpg", "_internal/.needed",
                         "_internal/ui/assets/chill.ico", "_internal/ui/assets/chill-192.png"):
                self.assertIn(name, names)
            self.assertFalse(any(name.startswith("Chill/") or ".." in Path(name).parts for name in names))
            manifest = json.loads(archive.read("BUILD_INFO.json"))
            self.assertEqual(manifest["application"], "Chill")
            self.assertEqual(manifest["executable"], "Chill.exe")
            self.assertEqual(set(manifest["files"]), names - {"BUILD_INFO.json"})
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)

    def test_missing_or_stale_ui_aborts_before_creating_archive(self):
        bundled = self.folder / "_internal/ui/index.html"
        bundled.write_text("stale", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            package.package_distribution(self.folder, self.output)
        self.assertFalse(self.output.exists())

    def test_packaging_rejects_a_missing_cryptex_payload(self):
        (self.folder / "_internal/assets/ddi/Cryptex/Image.dmg.root_hash").unlink()
        with self.assertRaisesRegex(ValueError, "Cryptex/Image.dmg.root_hash"):
            package.package_distribution(self.folder, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_executable_and_non_pe_files_rejected(self):
        executable = self.folder / "Chill.exe"
        executable.unlink()
        with self.assertRaisesRegex(ValueError, "Missing Chill.exe"):
            package.validate_distribution(self.folder)
        executable.write_text("not executable")
        with self.assertRaisesRegex(ValueError, "PE executable"):
            package.validate_distribution(self.folder)

    def test_cannot_zip_inside_distribution_and_does_not_accept_symlinks(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            package.package_distribution(self.folder, self.folder / "nested.zip")
        linked = self.folder / "linked"
        try:
            linked.symlink_to(self.folder / "Chill.exe")
        except OSError as error:
            self.skipTest("This Windows account cannot create symlinks: " + str(error))
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            package.validate_distribution(self.folder)

    def test_failed_archive_preserves_last_good_zip_and_cleans_temporary_file(self):
        self.output.write_bytes(b"previous good archive")
        with patch.object(package.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                package.package_distribution(self.folder, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous good archive")
        self.assertEqual(list(self.folder.parent.glob(".pace-*.zip")), [])


if __name__ == "__main__":
    unittest.main()
