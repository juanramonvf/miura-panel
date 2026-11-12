"""Keep systemctl --root confined to the isolated panel prefix."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-spanish-empire-panel.sh"
SYSTEMCTL = shutil.which("systemctl")
WANTS = (
    "multi-user.target.wants",
    "timers.target.wants",
)


@unittest.skipUnless(SYSTEMCTL, "systemctl es necesario para cubrir el modo --root")
class RootPrefixSystemdWantsConfinementTests(unittest.TestCase):
    def run_installer(self, prefix: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        return subprocess.run(
            ["bash", str(INSTALLER), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    @staticmethod
    def snapshot(directory: pathlib.Path) -> dict[str, tuple[str, str]]:
        result = {}
        for path in sorted(directory.rglob("*")):
            relative = str(path.relative_to(directory))
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", path.read_bytes().hex())
            elif path.is_dir():
                result[relative] = ("directory", "")
        return result

    def install_clean(self, prefix: pathlib.Path) -> None:
        result = self.run_installer(prefix, "--normal")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def make_external_wants(self, base: pathlib.Path, prefix: pathlib.Path) -> pathlib.Path:
        outside = base / "external-systemd"
        systemd_dir = prefix / "etc/systemd/system"
        outside.mkdir()
        systemd_dir.mkdir(parents=True, exist_ok=True)

        for name in WANTS:
            external = outside / name
            external.mkdir()
            (external / "sentinel.txt").write_text(
                f"preserve {name}\n", encoding="utf-8"
            )
        # Rollback contains a narrow cleanup for this exact unit-link name;
        # this sentinel proves it cannot escape through the wants-directory link.
        (outside / WANTS[0] / "server-dashboard-admin.service").symlink_to(
            "/etc/systemd/system/server-dashboard-admin.service"
        )

        for name in WANTS:
            wants = systemd_dir / name
            if wants.is_symlink():
                wants.unlink()
            elif wants.exists():
                shutil.rmtree(wants)
            wants.symlink_to(outside / name, target_is_directory=True)
        return outside

    def test_clean_install_refuses_external_wants_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="miura-wants-clean-") as temp:
            base = pathlib.Path(temp)
            prefix = base / "root"
            prefix.mkdir()
            outside = self.make_external_wants(base, prefix)
            before = self.snapshot(outside)

            result = self.run_installer(prefix, "--normal")

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("enlazada", result.stderr)
            self.assertEqual(self.snapshot(outside), before)

    def test_upgrade_refuses_external_wants_symlinks_without_touching_sentinels(self):
        with tempfile.TemporaryDirectory(prefix="miura-wants-upgrade-") as temp:
            base = pathlib.Path(temp)
            prefix = base / "root"
            self.install_clean(prefix)
            outside = self.make_external_wants(base, prefix)
            before = self.snapshot(outside)

            result = self.run_installer(prefix, "--upgrade")

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("enlazada", result.stderr)
            self.assertEqual(self.snapshot(outside), before)

    def test_rollback_refuses_external_wants_symlinks_without_deleting_sentinel(self):
        with tempfile.TemporaryDirectory(prefix="miura-wants-rollback-") as temp:
            base = pathlib.Path(temp)
            prefix = base / "root"
            self.install_clean(prefix)
            outside = self.make_external_wants(base, prefix)
            before = self.snapshot(outside)

            result = self.run_installer(prefix, "--rollback")

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("enlazada", result.stderr)
            self.assertEqual(self.snapshot(outside), before)


if __name__ == "__main__":
    unittest.main()
