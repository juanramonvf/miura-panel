"""Reject unsafe root-prefix account databases before opening or writing them."""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-spanish-empire-panel.sh"
V1038_INSTALLER = ROOT / "dist/install-spanish-empire-panel-v1.0.38.sh"


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

    def test_legacy_admin_group_membership_is_removed_without_other_members(self):
        with tempfile.TemporaryDirectory(prefix="miura-root-prefix-legacy-group-") as temp:
            prefix = pathlib.Path(temp) / "root"
            etc = prefix / "etc"
            env = {**os.environ, "MIURA_ROOT_PREFIX": str(prefix)}
            old = subprocess.run(["bash", str(V1038_INSTALLER), "--normal"], cwd=ROOT,
                env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
            group_file = etc / "group"
            group_file.write_text("server-dashboard:x:2500:sep-admin,monitor\n", encoding="utf-8")
            result = subprocess.run(["bash", str(INSTALLER), "--upgrade"], cwd=ROOT,
                env=env, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            prefix.chmod(0o755)
            (prefix / "etc").chmod(0o755)
            (prefix / "etc/systemd").chmod(0o755)
            (prefix / "etc/systemd/system").chmod(0o755)
            rows = [line.split(":") for line in group_file.read_text().splitlines()]
            legacy = [row for row in rows if row[0] == "server-dashboard"]
            self.assertEqual(len(legacy), 1)
            self.assertEqual(legacy[0][3], "monitor")
            admin = [row for row in rows if row[0] == "sep-admin"]
            self.assertEqual(len(admin), 1)
            self.assertEqual(admin[0][3], "")
            rollback = subprocess.run(
                ["bash", str(INSTALLER), "--rollback"], cwd=ROOT, env=env,
                capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            restored = [line.split(":") for line in group_file.read_text().splitlines()
                        if line.split(":", 1)[0] == "server-dashboard"]
            self.assertEqual(restored[0][3], "monitor,sep-admin")

    def test_root_prefix_group_and_permissions_restore_after_failures_at_each_phase(self):
        for phase in ("after-account-db", "after-backup-before-file-install", "after-trap"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                    prefix=f"miura-root-prefix-group-rollback-{phase}-") as temp:
                prefix = pathlib.Path(temp) / "root"
                etc = prefix / "etc"
                env = {**os.environ, "MIURA_ROOT_PREFIX": str(prefix)}
                old = subprocess.run(["bash", str(V1038_INSTALLER), "--normal"], cwd=ROOT,
                    env=env, capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(old.returncode, 0, old.stdout + old.stderr)

                group_file = etc / "group"
                original_group = b"server-dashboard:x:2500:sep-admin,monitor\noperators:x:2600:alice\n"
                group_file.write_bytes(original_group)
                group_file.chmod(0o640)
                original_group_stat = group_file.stat()
                passwd_file = etc / "passwd"
                self.assertFalse(passwd_file.exists())

                test_env = dict(env)
                if phase == "after-account-db":
                    test_env["MIURA_TEST_FAIL_AFTER_ACCOUNT_DB"] = "1"
                elif phase == "after-trap":
                    test_env["MIURA_TEST_FAIL_AFTER_ROLLBACK_TRAP"] = "1"
                else:
                    conf = etc / "server-dashboard.conf"
                    conf.write_text(conf.read_text().replace(
                        "DASHBOARD_MODE=normal", "DASHBOARD_MODE=invalid"), encoding="utf-8")

                failed = subprocess.run(["bash", str(INSTALLER), "--upgrade"], cwd=ROOT,
                    env=test_env, capture_output=True, text=True, timeout=60, check=False)
                self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
                self.assertEqual(group_file.read_bytes(), original_group)
                restored_stat = group_file.stat()
                self.assertEqual(stat.S_IMODE(restored_stat.st_mode), stat.S_IMODE(original_group_stat.st_mode))
                self.assertEqual((restored_stat.st_uid, restored_stat.st_gid),
                                 (original_group_stat.st_uid, original_group_stat.st_gid))
                self.assertFalse(passwd_file.exists(), "rollback must remove passwd created by the failed fixture")


if __name__ == "__main__":
    unittest.main()
