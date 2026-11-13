"""Reject unsafe root-prefix account databases before opening or writing them."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-spanish-empire-panel.sh"


class RootPrefixAccountDatabasePathTests(unittest.TestCase):
    def run_installer(self, prefix: pathlib.Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        return subprocess.run(
            ["bash", str(INSTALLER), "--normal"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_symlink_databases_to_external_sentinels_are_rejected_without_writes(self):
        for name in ("passwd", "group"):
            with self.subTest(database=name), tempfile.TemporaryDirectory(
                prefix="miura-root-prefix-db-symlink-"
            ) as temp:
                base = pathlib.Path(temp)
                prefix = base / "root"
                etc = prefix / "etc"
                etc.mkdir(parents=True)
                sentinel = base / f"external-{name}-sentinel"
                original = f"unchanged {name} sentinel\n"
                sentinel.write_text(original, encoding="utf-8")
                (etc / name).symlink_to(sentinel)

                result = self.run_installer(prefix)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"etc/{name}", result.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), original)
                self.assertEqual(sentinel.stat().st_nlink, 1)
                # Validate both databases before writing either one: a bad
                # group path must not leave a newly fabricated passwd file.
                if name == "group":
                    self.assertFalse((etc / "passwd").exists())

    def test_non_regular_databases_are_rejected(self):
        for name in ("passwd", "group"):
            with self.subTest(database=name), tempfile.TemporaryDirectory(
                prefix="miura-root-prefix-db-special-"
            ) as temp:
                prefix = pathlib.Path(temp) / "root"
                path = prefix / "etc" / name
                path.mkdir(parents=True)

                result = self.run_installer(prefix)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"etc/{name}", result.stderr)
                self.assertTrue(path.is_dir())
                if name == "group":
                    self.assertFalse((prefix / "etc/passwd").exists())

    def test_symlinked_etc_directory_cannot_escape_the_root_prefix(self):
        with tempfile.TemporaryDirectory(prefix="miura-root-prefix-etc-escape-") as temp:
            base = pathlib.Path(temp)
            prefix = base / "root"
            prefix.mkdir()
            external_etc = base / "external-etc"
            external_etc.mkdir()
            sentinel = external_etc / "passwd"
            original = "external account database sentinel\n"
            sentinel.write_text(original, encoding="utf-8")
            (prefix / "etc").symlink_to(external_etc, target_is_directory=True)

            result = self.run_installer(prefix)

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("etc", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), original)
            self.assertEqual(sentinel.stat().st_nlink, 1)

    def test_hardlinked_databases_are_rejected_and_external_sentinel_stays_intact(self):
        for name in ("passwd", "group"):
            with self.subTest(database=name), tempfile.TemporaryDirectory(
                prefix="miura-root-prefix-db-hardlink-"
            ) as temp:
                base = pathlib.Path(temp)
                prefix = base / "root"
                etc = prefix / "etc"
                etc.mkdir(parents=True)
                sentinel = base / f"external-{name}-sentinel"
                original = f"unchanged hardlink {name} sentinel\n"
                sentinel.write_text(original, encoding="utf-8")
                os.link(sentinel, etc / name)

                result = self.run_installer(prefix)

                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"etc/{name}", result.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), original)
                self.assertEqual(sentinel.stat().st_nlink, 2)
                if name == "group":
                    self.assertFalse((etc / "passwd").exists())


if __name__ == "__main__":
    unittest.main()
