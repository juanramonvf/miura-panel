"""Safety tests for package_release.py's version/output preconditions."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_SCRIPT = ROOT / "scripts" / "release" / "package_release.py"


class PackageReleasePreconditionTests(unittest.TestCase):
    def test_version_mismatch_fails_before_creating_output_directory(self):
        with tempfile.TemporaryDirectory(prefix="miura-package-wrong-version-") as temp:
            output = pathlib.Path(temp) / "must-not-be-created"
            result = subprocess.run(
                [sys.executable, str(PACKAGE_SCRIPT), "9.9.9", str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no coincide", result.stderr)
            self.assertFalse(output.exists())

    def test_nonempty_output_directory_is_rejected_without_changes(self):
        version = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
        with tempfile.TemporaryDirectory(prefix="miura-package-nonempty-") as temp:
            output = pathlib.Path(temp) / "output"
            output.mkdir()
            sentinel = output / "do-not-overwrite.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(PACKAGE_SCRIPT), version, str(output)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no está vacío", result.stderr)
            self.assertEqual(list(output.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main()
