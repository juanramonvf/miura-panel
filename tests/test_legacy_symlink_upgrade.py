"""Isolated compatibility tests for the inherited admin-setup alias."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
INSTALLER = ROOT / "dist" / f"install-spanish-empire-panel-v{VERSION}.sh"
V1_0_35_INSTALLER = ROOT / "dist" / "install-spanish-empire-panel-v1.0.35.sh"
LEGACY_ALIAS = "miura-panel-admin-setup"
LEGACY_TARGET = "spanish-empire-panel-admin-setup"
LEGACY_BODY = "#!/bin/sh\nprintf 'legacy admin setup target\\n'\n"


class LegacyAdminSetupSymlinkUpgradeTests(unittest.TestCase):
    def run_installer_file(self, installer, prefix, *args, ok=True):
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        result = subprocess.run(
            ["bash", str(installer), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if ok and result.returncode:
            self.fail(f"{args} failed: {result.stdout}\n{result.stderr}")
        if not ok and not result.returncode:
            self.fail(f"{args} unexpectedly succeeded")
        return result

    def run_installer(self, prefix, *args, ok=True):
        return self.run_installer_file(INSTALLER, prefix, *args, ok=ok)

    def installed_demo_prefix(self, parent):
        prefix = parent / "root"
        legacy_installer = V1_0_35_INSTALLER if V1_0_35_INSTALLER.is_file() else INSTALLER
        self.run_installer_file(legacy_installer, prefix, "--demo")
        web = prefix / "var/www/server-dashboard"
        self.assertEqual(json.loads((web / "version.json").read_text())["version"], "1.0.35")
        (web / "legacy-sentinel.txt").write_text("preserve before update\n", encoding="utf-8")
        conf = prefix / "etc/server-dashboard.conf"
        conf.write_text("DASHBOARD_MODE=demo\nDASHBOARD_TITLE=Legacy install\n", encoding="utf-8")
        return prefix

    def add_legacy_alias(self, prefix):
        bin_dir = prefix / "usr/local/sbin"
        alias = bin_dir / LEGACY_ALIAS
        alias.unlink()
        target = bin_dir / LEGACY_TARGET
        target.write_text(LEGACY_BODY, encoding="utf-8")
        target.chmod(0o755)
        alias.symlink_to(LEGACY_TARGET)
        return alias, target

    def test_exact_legacy_relative_link_upgrades_and_manual_rollback_remains_usable(self):
        with tempfile.TemporaryDirectory(prefix="miura-legacy-admin-link-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            alias, target = self.add_legacy_alias(prefix)
            self.assertEqual(os.readlink(alias), LEGACY_TARGET)
            self.assertTrue(target.is_file() and not target.is_symlink())
            self.assertEqual(target.stat().st_uid, os.geteuid())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

            result = self.run_installer(prefix, "--upgrade")
            self.assertIn("Miura Panel v" + VERSION, result.stdout)
            self.assertEqual(json.loads((prefix / "var/www/server-dashboard/version.json").read_text())["version"], VERSION)
            self.assertIn("DASHBOARD_MODE=demo", (prefix / "etc/server-dashboard.conf").read_text())
            # The inherited alias must remain intact and both names must resolve
            # to the current managed implementation after the upgrade.
            self.assertTrue(alias.is_symlink())
            self.assertEqual(os.readlink(alias), LEGACY_TARGET)
            self.assertEqual(target.read_bytes(), (ROOT / "admin-setup.py").read_bytes())

            # Rollback restores the previous command content under both names.
            # It may normalize the alias to a regular file for the safe archive format.
            self.run_installer(prefix, "--rollback")
            self.assertEqual(json.loads((prefix / "var/www/server-dashboard/version.json").read_text())["version"], "1.0.35")
            self.assertEqual((prefix / "var/www/server-dashboard/legacy-sentinel.txt").read_text(), "preserve before update\n")
            self.assertIn("DASHBOARD_MODE=demo", (prefix / "etc/server-dashboard.conf").read_text())
            self.assertTrue(alias.exists() and not alias.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), LEGACY_BODY)
            self.assertEqual(alias.read_text(encoding="utf-8"), LEGACY_BODY)
            for command in (alias, target):
                ran = subprocess.run([str(command)], capture_output=True, text=True, check=False)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertEqual(ran.stdout, "legacy admin setup target\n")

    def test_rollback_preserves_independent_spanish_command_without_legacy_alias(self):
        """Rollback must not delete an unrelated pre-existing Spanish command."""
        with tempfile.TemporaryDirectory(prefix="miura-independent-spanish-admin-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            bin_dir = prefix / "usr/local/sbin"
            alias = bin_dir / LEGACY_ALIAS
            target = bin_dir / LEGACY_TARGET
            self.assertTrue(alias.is_file() and not alias.is_symlink())

            independent_body = "#!/bin/sh\nprintf 'independent Spanish command\\n'\n"
            target.write_text(independent_body, encoding="utf-8")
            target.chmod(0o751)
            before = target.stat()
            before_mode = before.st_mode & 0o7777

            self.run_installer(prefix, "--upgrade")
            self.run_installer(prefix, "--rollback")

            self.assertTrue(target.is_file() and not target.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), independent_body)
            after = target.stat()
            self.assertEqual(after.st_mode & 0o7777, before_mode)
            self.assertEqual(after.st_uid, before.st_uid)
            ran = subprocess.run([str(target)], capture_output=True, text=True, check=False)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertEqual(ran.stdout, "independent Spanish command\n")

    def test_malicious_or_unrecognized_link_forms_are_rejected_before_update(self):
        cases = (
            ("absolute target", lambda alias, target, prefix: alias.symlink_to("/etc/passwd")),
            ("traversal target", lambda alias, target, prefix: alias.symlink_to("../../etc/passwd")),
            ("unrecognized relative target", lambda alias, target, prefix: alias.symlink_to("unknown-admin-setup")),
            ("missing referent", lambda alias, target, prefix: (target.unlink(), alias.symlink_to(LEGACY_TARGET))),
            ("symlink referent", lambda alias, target, prefix: (target.unlink(), target.symlink_to("/etc/passwd"), alias.symlink_to(LEGACY_TARGET))),
            ("writable referent", lambda alias, target, prefix: (target.write_text(LEGACY_BODY), target.chmod(0o777), alias.symlink_to(LEGACY_TARGET))),
            ("non-executable referent", lambda alias, target, prefix: (target.write_text(LEGACY_BODY), target.chmod(0o644), alias.symlink_to(LEGACY_TARGET))),
            ("multiply-linked referent", lambda alias, target, prefix: (os.link(target, target.parent / "second-link"), alias.symlink_to(LEGACY_TARGET))),
            ("directory referent", lambda alias, target, prefix: (target.unlink(), target.mkdir(), alias.symlink_to(LEGACY_TARGET))),
        )
        for name, create_link in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="miura-malicious-admin-link-") as tmp:
                prefix = self.installed_demo_prefix(pathlib.Path(tmp))
                bin_dir = prefix / "usr/local/sbin"
                alias = bin_dir / LEGACY_ALIAS
                alias.unlink()
                target = bin_dir / LEGACY_TARGET
                target.write_text(LEGACY_BODY, encoding="utf-8")
                target.chmod(0o755)
                create_link(alias, target, prefix)
                old_web = prefix / "var/www/server-dashboard"
                before_conf = (prefix / "etc/server-dashboard.conf").read_text(encoding="utf-8")

                result = self.run_installer(prefix, "--upgrade", ok=False)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(json.loads((old_web / "version.json").read_text())["version"], "1.0.35")
                self.assertEqual((old_web / "legacy-sentinel.txt").read_text(), "preserve before update\n")
                self.assertEqual((prefix / "etc/server-dashboard.conf").read_text(encoding="utf-8"), before_conf)

    def test_other_managed_alias_does_not_inherit_the_admin_link_exception(self):
        with tempfile.TemporaryDirectory(prefix="miura-other-legacy-link-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            bin_dir = prefix / "usr/local/sbin"
            other_alias = bin_dir / "miura-panel-fleet"
            other_alias.unlink()
            target = bin_dir / "spanish-empire-panel-fleet"
            target.write_text(LEGACY_BODY, encoding="utf-8")
            target.chmod(0o755)
            other_alias.symlink_to(target.name)
            result = self.run_installer(prefix, "--upgrade", ok=False)
            self.assertTrue(result.stderr.strip())
            self.assertEqual(json.loads((prefix / "var/www/server-dashboard/version.json").read_text())["version"], "1.0.35")

    def test_hardlinked_alias_and_symlinked_parent_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="miura-hardlink-admin-alias-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            bin_dir = prefix / "usr/local/sbin"
            alias = bin_dir / LEGACY_ALIAS
            alias.unlink()
            target = bin_dir / LEGACY_TARGET
            target.write_text(LEGACY_BODY, encoding="utf-8")
            target.chmod(0o755)
            os.link(target, alias)
            self.assertGreater(alias.stat().st_nlink, 1)
            result = self.run_installer(prefix, "--upgrade", ok=False)
            self.assertTrue(result.stderr.strip())

        with tempfile.TemporaryDirectory(prefix="miura-parent-symlink-admin-alias-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            real_bin = prefix / "usr/local/sbin"
            moved_bin = prefix / "attacker-controlled-sbin"
            real_bin.rename(moved_bin)
            (prefix / "usr/local").mkdir(parents=True, exist_ok=True)
            (prefix / "usr/local/sbin").symlink_to(moved_bin)
            result = self.run_installer(prefix, "--upgrade", ok=False)
            self.assertIn("Ruta de instalación enlazada", result.stderr)

    @unittest.skipUnless(os.geteuid() == 0, "cambiar propietario requiere UID 0")
    def test_foreign_owned_referent_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="miura-foreign-owner-admin-link-") as tmp:
            prefix = self.installed_demo_prefix(pathlib.Path(tmp))
            alias, target = self.add_legacy_alias(prefix)
            os.chown(target, 12345, -1)
            result = self.run_installer(prefix, "--upgrade", ok=False)
            self.assertIn("no pertenece al usuario esperado", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
