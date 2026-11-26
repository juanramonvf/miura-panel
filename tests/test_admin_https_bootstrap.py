"""Isolated bootstrap and persistence checks for the HTTPS admin service.

These tests use MIURA_ROOT_PREFIX; they never enable a service in the host's
system manager or write to host /etc.
"""

from __future__ import annotations

import json
import builtins
import http.client
import importlib.util
import errno
import grp
import os
import pathlib
import pwd
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import urllib.parse
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
INSTALLER = pathlib.Path(os.environ.get(
    "MIURA_INSTALLER_PATH",
    ROOT / "dist" / f"install-spanish-empire-panel-v{VERSION}.sh",
)).resolve()
V1038_INSTALLER = ROOT / "dist" / "install-spanish-empire-panel-v1.0.38.sh"
ADMIN_UNIT = pathlib.Path("etc/systemd/system/server-dashboard-admin.service")
ADMIN_ROOT = pathlib.Path("etc/spanish-empire-panel")
ADMIN_STATE = pathlib.Path("var/lib/spanish-empire-panel-admin")


class AdminHttpsBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not INSTALLER.is_file():
            raise AssertionError(f"Missing installer: {INSTALLER}")

    def install(self, prefix: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        result = subprocess.run(
            ["bash", str(INSTALLER), *args], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=90, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def load_setup(self, prefix: pathlib.Path):
        env = patch.dict(os.environ, {"MIURA_ROOT_PREFIX": str(prefix)})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(
            f"miura_admin_setup_{id(prefix)}", ROOT / "admin-setup.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def set_free_port_pair(self, prefix: pathlib.Path) -> int:
        for _ in range(30):
            with socket.socket() as first, socket.socket() as second:
                first.bind(("127.0.0.1", 0))
                port = first.getsockname()[1]
                if port >= 65535:
                    continue
                try:
                    second.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
                conf = prefix / "etc/server-dashboard.conf"
                text = conf.read_text(encoding="utf-8")
                lines = [line for line in text.splitlines()
                         if not line.startswith("DASHBOARD_PORT=")]
                lines.append(f"DASHBOARD_PORT={port}")
                conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return port + 1
        self.fail("could not reserve an adjacent dashboard/admin port pair")

    @staticmethod
    def add_minimal_boot_target(prefix: pathlib.Path) -> None:
        # A real Debian root contains this target in /usr/lib/systemd/system.
        # The isolated installer prefix intentionally does not copy base OS
        # units, so provide a harmless fixture for `systemctl --root enable`.
        target = prefix / "usr/lib/systemd/system/multi-user.target"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[Unit]\nDescription=Isolated boot target\n", encoding="utf-8")

    def credentials(self, module, email="owner@example.test", password="Miura-Test-Password-39!"):
        responses = iter([email, email, "1"])
        input_patch = patch.object(builtins, "input", side_effect=lambda _prompt="": next(responses))
        pass_patch = patch.object(module.getpass, "getpass", side_effect=[password, password])
        return input_patch, pass_patch, email, password

    def cleanup_transient_unit(self, unit_name):
        def load_state():
            status = subprocess.run(
                ["systemctl", "show", "--property=LoadState", "--value", unit_name],
                capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            return status.stdout.strip()

        def active_state():
            status = subprocess.run(
                ["systemctl", "show", "--property=ActiveState", "--value", unit_name],
                capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            return status.stdout.strip()

        if load_state() == "not-found":
            return
        stopped = subprocess.run(
            ["systemctl", "stop", unit_name], capture_output=True, text=True,
            timeout=15, check=False,
        )
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        state = load_state()
        if state == "not-found":
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = load_state()
            if state == "not-found":
                return
            if active_state() == "failed":
                reset = subprocess.run(
                    ["systemctl", "reset-failed", unit_name], capture_output=True, text=True,
                    timeout=15, check=False,
                )
                self.assertEqual(reset.returncode, 0, reset.stdout + reset.stderr)
            time.sleep(0.1)
        self.fail(f"La unidad temporal {unit_name} sigue cargada: {state}")

    def start_transient_unit(self, command, unit_name, timeout=20):
        started = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        return started

    def test_transient_https_unit_cleanup_on_startup_failure(self):
        for outcome in ("success", "nonzero", "timeout"):
            for stop_behavior in ("collects", "failed"):
                with self.subTest(outcome=outcome, stop_behavior=stop_behavior):
                    unit_name = f"miura-admin-start-failure-{uuid.uuid4().hex[:12]}.service"
                    unit_loaded = False
                    unit_failed = False
                    calls = []

                    def run(command, **kwargs):
                        nonlocal unit_loaded, unit_failed
                        calls.append(tuple(command))
                        if command[0] == "systemd-run":
                            unit_loaded = True
                            if outcome == "timeout":
                                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
                            if outcome == "nonzero":
                                return subprocess.CompletedProcess(command, 1, "", "injected startup failure")
                            return subprocess.CompletedProcess(command, 0, "", "")
                        if command[:3] == ["systemctl", "show", "--property=LoadState"]:
                            state = "loaded" if unit_loaded else "not-found"
                            return subprocess.CompletedProcess(command, 0, state + "\n", "")
                        if command[:3] == ["systemctl", "show", "--property=ActiveState"]:
                            state = "failed" if unit_failed else "inactive"
                            return subprocess.CompletedProcess(command, 0, state + "\n", "")
                        if command[:2] == ["systemctl", "stop"]:
                            if stop_behavior == "collects":
                                unit_loaded = False
                            else:
                                unit_failed = True
                        if command[:2] == ["systemctl", "reset-failed"]:
                            unit_loaded = False
                            unit_failed = False
                        return subprocess.CompletedProcess(command, 0, "", "")

                    with patch.object(subprocess, "run", side_effect=run):
                        try:
                            if outcome == "timeout":
                                with self.assertRaises(subprocess.TimeoutExpired):
                                    self.start_transient_unit(["systemd-run", f"--unit={unit_name}"], unit_name)
                            elif outcome == "nonzero":
                                with self.assertRaisesRegex(AssertionError, "injected startup failure"):
                                    self.start_transient_unit(["systemd-run", f"--unit={unit_name}"], unit_name)
                            else:
                                started = self.start_transient_unit(
                                    ["systemd-run", f"--unit={unit_name}"], unit_name)
                                self.assertEqual(started.returncode, 0)
                        finally:
                            self.cleanup_transient_unit(unit_name)

                    self.assertFalse(unit_loaded)
                    self.assertIn(("systemctl", "stop", unit_name), calls)
                    reset_calls = calls.count(("systemctl", "reset-failed", unit_name))
                    self.assertEqual(reset_calls, int(stop_behavior == "failed"))
                    self.assertEqual(calls[-1], (
                        "systemctl", "show", "--property=LoadState", "--value", unit_name,
                    ))

    @staticmethod
    def omv7_v1038_admin_unit():
        return '''[Unit]
Description=Miura Panel Configuración HTTPS autenticada
After=network-online.target server-dashboard-web.service
Wants=network-online.target
ConditionPathExists=/etc/spanish-empire-panel/admin-enabled

[Service]
Type=simple
User=sep-admin
Group=sep-admin
SupplementaryGroups=server-dashboard
WorkingDirectory=/var/www/server-dashboard
ExecStart=/usr/bin/python3 /usr/local/sbin/sep-admin-web.py
Restart=on-failure
RestartSec=3
UMask=0077
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=false
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=/var/lib/spanish-empire-panel-admin /var/lib/server-dashboard/preferences
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
'''

    @staticmethod
    def omv7_v1038_preferences_override():
        return b'''[Service]
SupplementaryGroups=server-dashboard-prefs
SupplementaryGroups=server-dashboard-read
ReadWritePaths=/var/lib/server-dashboard/preferences
'''

    def launch_https_service(self, prefix: pathlib.Path, module, bind: str, port: int):
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        log = tempfile.TemporaryFile(mode="w+t", prefix="miura-admin-service-")
        service = subprocess.Popen(
            [sys.executable, str(prefix / "usr/local/sbin/server-dashboard-admin.py")],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self.stop_service, service, log)
        return service, log

    @staticmethod
    def log_contents(log):
        log.flush()
        log.seek(0)
        return log.read()

    @staticmethod
    def stop_service(service, log):
        if service.poll() is None:
            service.terminate()
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=5)
        log.close()

    @staticmethod
    def systemd_manager_available() -> bool:
        if os.geteuid() != 0 or not shutil.which("systemd-run") or not pathlib.Path("/run/systemd/system").is_dir():
            return False
        try:
            pwd.getpwnam("sep-admin")
            grp.getgrnam("sep-admin")
            grp.getgrnam("www-data")
        except KeyError:
            return False
        return subprocess.run(
            ["systemctl", "show-environment"], capture_output=True, text=True, timeout=5, check=False
        ).returncode == 0

    def test_clean_install_stages_admin_disabled_until_credentials_and_tls_exist(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-bootstrap-clean-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")

            unit_path = prefix / ADMIN_UNIT
            self.assertTrue(unit_path.is_file(), "clean install must provide the admin unit")
            unit = unit_path.read_text(encoding="utf-8")
            self.assertIn("User=sep-admin\n", unit)
            self.assertIn("Group=sep-admin\n", unit)
            self.assertIn("ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py\n", unit)
            self.assertIn("ProtectSystem=strict\n", unit)
            self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n", unit)
            self.assertIn("ProtectHome=true\n", unit)
            self.assertIn("ProtectKernelTunables=true\n", unit)
            self.assertIn("ProtectKernelModules=true\n", unit)
            self.assertIn("ProtectControlGroups=true\n", unit)
            self.assertIn("NoNewPrivileges=true\n", unit)
            self.assertIn("RestrictSUIDSGID=true\n", unit)
            self.assertIn("UMask=0077\n", unit)
            self.assertIn("SupplementaryGroups=server-dashboard-read server-dashboard-prefs\n", unit)
            self.assertIn("StateDirectory=spanish-empire-panel-admin\n", unit)
            self.assertIn("StateDirectoryMode=0700\n", unit)
            write_paths = [line.split("=", 1)[1] for line in unit.splitlines()
                           if line.startswith("ReadWritePaths=")]
            self.assertEqual(write_paths, ["/var/lib/server-dashboard/preferences"])
            self.assertIn("ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.json\n", unit)
            self.assertIn("ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.crt\n", unit)
            self.assertIn("ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.key\n", unit)

            admin = prefix / ADMIN_ROOT
            self.assertFalse((admin / "admin-enabled").exists())
            self.assertFalse((admin / "admin.json").exists())
            self.assertFalse((admin / "admin.crt").exists())
            self.assertFalse((admin / "admin.key").exists())

            # The candidate root contains a unit, but no boot-enable link until
            # setup has supplied credentials and a verified certificate.
            self.assertFalse(
                (prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service").exists()
            )
            self.assertTrue((prefix / ADMIN_STATE).is_dir())
            self.assertFalse(any((prefix / ADMIN_STATE).iterdir()))
            passwd = (prefix / "etc/passwd").read_text(encoding="utf-8").splitlines()
            groups = (prefix / "etc/group").read_text(encoding="utf-8").splitlines()
            admin_users = [line.split(":") for line in passwd if line.split(":", 1)[0] == "sep-admin"]
            admin_groups = [line.split(":") for line in groups if line.split(":", 1)[0] == "sep-admin"]
            self.assertEqual(len(admin_users), 1)
            self.assertEqual(len(admin_groups), 1)
            self.assertTrue(admin_users[0][2].isdigit(), "service account must have a numeric UID")
            self.assertNotEqual(int(admin_users[0][2]), 0, "service account must not run as root")
            self.assertTrue(admin_users[0][3].isdigit(), "service account must have a numeric primary GID")
            self.assertIn(admin_users[0][6], ("/usr/sbin/nologin", "/bin/false"))
            self.assertEqual(
                admin_groups[0][2], admin_users[0][3],
                "service account must use its dedicated primary group",
            )
            self.assertEqual(admin_groups[0][3], "", "dedicated admin group must not contain human accounts")
            self.assertEqual((prefix / ADMIN_STATE).stat().st_mode & 0o777, 0o700)

            analyzer = shutil.which("systemd-analyze")
            if analyzer:
                checked = subprocess.run(
                    [analyzer, "verify", str(unit_path)],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_upgrade_from_v1038_preserves_configuration_and_installs_candidate_helpers(self):
        if not V1038_INSTALLER.is_file():
            self.skipTest("no está disponible el instalador v1.0.38")
        with tempfile.TemporaryDirectory(prefix="miura-upgrade-v1038-") as temp:
            prefix = pathlib.Path(temp) / "root"
            env = os.environ.copy()
            env["MIURA_ROOT_PREFIX"] = str(prefix)
            old = subprocess.run(["bash", str(V1038_INSTALLER), "--normal"], cwd=ROOT,
                env=env, capture_output=True, text=True, timeout=90, check=False)
            self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
            conf = prefix / "etc/server-dashboard.conf"
            conf.write_text(conf.read_text() + "CUSTOM_KEEP=value\n", encoding="utf-8")
            self.install(prefix, "--upgrade")
            self.assertIn("CUSTOM_KEEP=value", conf.read_text())
            self.assertEqual(json.loads((prefix / "var/www/server-dashboard/version.json").read_text())["version"], "1.0.42")
            self.assertTrue((prefix / ADMIN_UNIT).is_file())
            self.assertTrue((prefix / "usr/local/sbin/server-dashboard-admin.py").is_file())

    def test_vendor_admin_unit_is_resolved_without_creating_an_etc_override(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-vendor-unit-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            override = prefix / ADMIN_UNIT
            vendor = prefix / "usr/lib/systemd/system/server-dashboard-admin.service"
            vendor.parent.mkdir(parents=True, exist_ok=True)
            override.rename(vendor)
            module = self.load_setup(prefix)
            safe_unit = vendor.read_text(encoding="utf-8")
            module.ensure_prerequisites()
            self.assertTrue(vendor.is_file())
            self.assertFalse(override.exists())
            vendor.write_text(safe_unit + "\n[Service]\nUser=root\nReadWritePaths=/etc\n",
                              encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "perfil seguro requerido"):
                module.ensure_prerequisites()
            self.assertFalse((prefix / ADMIN_ROOT / "admin.json").exists())
            vendor.write_text(safe_unit, encoding="utf-8")
            dropin = prefix / "etc/systemd/system/server-dashboard-admin.service.d"
            dropin.mkdir(parents=True)
            (dropin / "10-unsafe.conf").write_text("[Service]\nUser=root\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "overrides de la unidad"):
                module.ensure_prerequisites()
            self.assertFalse((prefix / ADMIN_ROOT / "admin.json").exists())

    def test_legacy_systemd_profile_migrates_with_copy_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-unit-migration-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            module = self.load_setup(prefix)
            legacy_unit = self.omv7_v1038_admin_unit()
            vendor = prefix / "usr/lib/systemd/system/server-dashboard-admin.service"
            vendor.parent.mkdir(parents=True, exist_ok=True)
            for directory in (prefix / "usr", prefix / "usr/lib", prefix / "usr/lib/systemd", vendor.parent):
                directory.chmod(0o755)
            (prefix / ADMIN_UNIT).replace(vendor)
            vendor.write_text(legacy_unit, encoding="utf-8")
            vendor.chmod(0o644)
            override = prefix / "etc/systemd/system/server-dashboard-admin.service.d/miura-preferences.conf"
            override.parent.mkdir(parents=True)
            override.parent.chmod(0o750)
            override.write_bytes(self.omv7_v1038_preferences_override())
            override.chmod(0o644)
            (prefix / "lib").symlink_to("usr/lib", target_is_directory=True)
            vendor_override = prefix / "usr/lib/systemd/system/server-dashboard-admin.service.d/10-legacy.conf"
            vendor_override.parent.mkdir(parents=True)
            vendor_override.parent.chmod(0o755)
            vendor_override.write_bytes(override.read_bytes())
            vendor_override.chmod(0o644)
            original_vendor = vendor.read_bytes()
            original_override = override.read_bytes()
            original_vendor_override = vendor_override.read_bytes()

            backup = module.migrate_admin_unit()
            self.assertIsNotNone(backup)
            self.assertTrue((prefix / ADMIN_UNIT).is_file())
            self.assertNotIn("ConditionPathExists", (prefix / ADMIN_UNIT).read_text())
            self.assertFalse(override.exists())
            self.assertFalse(vendor_override.exists())
            self.assertTrue((pathlib.Path(backup) / "manifest.json").is_file())
            module.ensure_prerequisites()

            module.restore_admin_unit_migration(backup)
            self.assertFalse((prefix / ADMIN_UNIT).exists())
            self.assertEqual(vendor.read_bytes(), original_vendor)
            self.assertEqual(override.read_bytes(), original_override)
            self.assertEqual(vendor_override.read_bytes(), original_vendor_override)
            self.assertEqual(override.parent.stat().st_mode & 0o777, 0o750)

    def test_legacy_migration_failure_restores_unit_and_miura_override_exactly(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-unit-failed-migration-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            module = self.load_setup(prefix)
            unit = prefix / ADMIN_UNIT
            original_unit = self.omv7_v1038_admin_unit().encode()
            unit.write_bytes(original_unit)
            unit.chmod(0o640)
            dropin = prefix / "etc/systemd/system/server-dashboard-admin.service.d/miura-preferences.conf"
            dropin.parent.mkdir(parents=True)
            dropin.parent.chmod(0o755)
            original_dropin = self.omv7_v1038_preferences_override()
            dropin.write_bytes(original_dropin)
            dropin.chmod(0o640)
            vendor_dropin = prefix / "usr/lib/systemd/system/server-dashboard-admin.service.d/miura-preferences.conf"
            vendor_dropin.parent.mkdir(parents=True)
            for directory in (prefix / "usr", prefix / "usr/lib", prefix / "usr/lib/systemd",
                              prefix / "usr/lib/systemd/system", vendor_dropin.parent):
                directory.chmod(0o755)
            original_vendor_dropin = (
                b"[Service]\nSupplementaryGroups=server-dashboard-prefs\n"
                b"ReadWritePaths=/var/lib/server-dashboard/preferences\n")
            vendor_dropin.write_bytes(original_vendor_dropin)
            vendor_dropin.chmod(0o640)
            original_unlink = pathlib.Path.unlink

            def fail_dropin_unlink(path, *args, **kwargs):
                if path == vendor_dropin:
                    raise OSError("injected failure after canonical unit replacement")
                return original_unlink(path, *args, **kwargs)

            with patch.object(pathlib.Path, "unlink", fail_dropin_unlink):
                with self.assertRaisesRegex(OSError, "injected failure"):
                    module.migrate_admin_unit()
            self.assertEqual(unit.read_bytes(), original_unit)
            self.assertEqual(unit.stat().st_mode & 0o777, 0o640)
            self.assertEqual(dropin.read_bytes(), original_dropin)
            self.assertEqual(dropin.stat().st_mode & 0o777, 0o640)
            self.assertEqual(vendor_dropin.read_bytes(), original_vendor_dropin)
            self.assertEqual(vendor_dropin.stat().st_mode & 0o777, 0o640)

    def test_v1039_unit_and_preferences_override_remain_compatible(self):
        with tempfile.TemporaryDirectory(prefix="miura-v1039-unit-compatible-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            module = self.load_setup(prefix)
            unit = prefix / ADMIN_UNIT
            # Literal unit shipped by the v1.0.39 installer, independently
            # transcribed from that tagged installer.
            current_unit = module.canonical_admin_unit()
            v1039_unit = '''[Unit]
Description=Miura Panel authenticated HTTPS administration
After=network.target

[Service]
Type=simple
User=sep-admin
Group=sep-admin
SupplementaryGroups=server-dashboard-read server-dashboard-prefs
WorkingDirectory=/var/www/server-dashboard
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.json
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.crt
ExecStartPre=/usr/bin/test -s /etc/spanish-empire-panel/admin.key
ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
UnsetEnvironment=MIURA_ROOT_PREFIX
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
StateDirectory=spanish-empire-panel-admin
StateDirectoryMode=0700
ReadWritePaths=/var/lib/server-dashboard/preferences

[Install]
WantedBy=multi-user.target
'''
            unit.write_text(v1039_unit, encoding="utf-8")
            unit.chmod(0o644)
            dropin = prefix / "etc/systemd/system/server-dashboard-admin.service.d/miura-preferences.conf"
            dropin.parent.mkdir(parents=True)
            dropin.parent.chmod(0o755)
            dropin.write_text(
                "[Service]\nSupplementaryGroups=server-dashboard-prefs\n"
                "ReadWritePaths=/var/lib/server-dashboard/preferences\n", encoding="utf-8")
            dropin.chmod(0o644)
            backup = module.migrate_admin_unit()
            self.assertIsNotNone(backup)
            self.assertEqual(unit.read_text(encoding="utf-8"), current_unit)
            self.assertFalse(dropin.exists())
            module.ensure_prerequisites()

    def test_upgrade_v1038_omv7_profile_then_first_https_activation(self):
        if not V1038_INSTALLER.is_file():
            self.skipTest("no está disponible el instalador v1.0.38")
        with tempfile.TemporaryDirectory(prefix="miura-v1038-omv7-first-https-") as temp:
            prefix = pathlib.Path(temp) / "root"
            env = os.environ.copy()
            env["MIURA_ROOT_PREFIX"] = str(prefix)
            old = subprocess.run(["bash", str(V1038_INSTALLER), "--normal"], cwd=ROOT,
                env=env, capture_output=True, text=True, timeout=90, check=False)
            self.assertEqual(old.returncode, 0, old.stdout + old.stderr)

            module = self.load_setup(prefix)
            vendor = prefix / "usr/lib/systemd/system/server-dashboard-admin.service"
            vendor.parent.mkdir(parents=True, exist_ok=True)
            for directory in (prefix / "usr", prefix / "usr/lib", prefix / "usr/lib/systemd", vendor.parent):
                directory.chmod(0o755)
            vendor.write_text(self.omv7_v1038_admin_unit(), encoding="utf-8")
            vendor.chmod(0o644)
            inherited_dropin = prefix / "etc/systemd/system/server-dashboard-admin.service.d/miura-preferences.conf"
            inherited_dropin.parent.mkdir(parents=True, exist_ok=True)
            inherited_dropin.parent.chmod(0o755)
            inherited_dropin.write_bytes(self.omv7_v1038_preferences_override())
            inherited_dropin.chmod(0o644)
            old_unit_bytes = vendor.read_bytes()

            self.install(prefix, "--upgrade")
            self.assertEqual(vendor.read_bytes(), self.omv7_v1038_admin_unit().encode("utf-8"))
            self.assertEqual(inherited_dropin.read_bytes(), self.omv7_v1038_preferences_override())
            self.add_minimal_boot_target(prefix)
            port = self.set_free_port_pair(prefix)
            input_patch, pass_patch, _email, _password = self.credentials(module)
            with input_patch, pass_patch:
                module.main()

            unit = prefix / ADMIN_UNIT
            self.assertTrue(unit.is_file())
            normalized = unit.read_text(encoding="utf-8")
            self.assertEqual(normalized, module.canonical_admin_unit())
            self.assertIn("ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py\n", normalized)
            self.assertIn("SupplementaryGroups=server-dashboard-read server-dashboard-prefs\n", normalized)
            self.assertIn("NoNewPrivileges=true\n", normalized)
            self.assertIn("RestrictSUIDSGID=true\n", normalized)
            self.assertNotIn("ConditionPathExists", normalized)
            self.assertFalse(inherited_dropin.exists())
            self.assertEqual(vendor.read_bytes(), old_unit_bytes)
            self.assertFalse((prefix / ADMIN_ROOT / "admin-enabled").exists())

            service, log = self.launch_https_service(prefix, module, "127.0.0.1", port)
            try:
                self.assertIsNone(service.poll(), self.log_contents(log))
                module.activate_admin("127.0.0.1", port)
                self.assertFalse((prefix / ADMIN_ROOT / "admin-enabled").exists())
                module.complete_activation()
                self.assertTrue((prefix / ADMIN_ROOT / "admin-enabled").is_file())
            finally:
                self.stop_service(service, log)

    def test_legacy_unit_unknown_directive_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-unit-unknown-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            module = self.load_setup(prefix)
            unit = prefix / ADMIN_UNIT
            original = self.omv7_v1038_admin_unit().replace(
                "NoNewPrivileges=false\n", "NoNewPrivileges=false\nEnvironment=UNREVIEWED=1\n")
            unit.write_text(original, encoding="utf-8")
            unit.chmod(0o644)
            with self.assertRaisesRegex(SystemExit, "configuración desconocida"):
                module.migrate_admin_unit()
            self.assertEqual(unit.read_text(encoding="utf-8"), original)

            original = self.omv7_v1038_admin_unit().encode("utf-8")
            unit.write_bytes(original)
            dropin = prefix / ADMIN_UNIT.parent / (ADMIN_UNIT.name + ".d") / "miura-preferences.conf"
            dropin.parent.mkdir(mode=0o750)
            unknown = self.omv7_v1038_preferences_override() + b"Environment=UNREVIEWED=1\n"
            dropin.write_bytes(unknown)
            dropin.chmod(0o640)
            with self.assertRaisesRegex(SystemExit, "Override administrativo desconocido"):
                module.migrate_admin_unit()
            self.assertEqual(unit.read_bytes(), original)
            self.assertEqual(dropin.read_bytes(), unknown)

    def test_setup_verifies_live_tls_before_enable_then_real_password_login(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-bootstrap-login-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            self.add_minimal_boot_target(prefix)
            port = self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)
            unit_path = prefix / ADMIN_UNIT
            unit_path.write_text(unit_path.read_text().replace(
                "After=network.target", "After=network.target\nConditionPathExists=/etc/spanish-empire-panel/admin-enabled"
            ), encoding="utf-8")
            inherited_dropin = prefix / "etc/systemd/system/server-dashboard-admin.service.d/10-legacy-prefs.conf"
            inherited_dropin.parent.mkdir(parents=True)
            inherited_dropin.parent.chmod(0o755)
            inherited_dropin.write_bytes(self.omv7_v1038_preferences_override())
            inherited_dropin.chmod(0o644)
            input_patch, pass_patch, email, password = self.credentials(module)
            service = log = None
            try:
                with input_patch, pass_patch:
                    module.main()
                self.assertNotIn("ConditionPathExists", unit_path.read_text(encoding="utf-8"))
                self.assertFalse(inherited_dropin.exists())
                self.assertTrue((prefix / ADMIN_ROOT / "admin.json").is_file())
                self.assertTrue((prefix / ADMIN_ROOT / "admin.crt").is_file())
                self.assertFalse((prefix / ADMIN_ROOT / "admin-enabled").exists())
                enabled_link = prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service"
                self.assertFalse(enabled_link.exists())
                service, log = self.launch_https_service(prefix, module, "127.0.0.1", port)
                self.assertIsNone(service.poll(), self.log_contents(log))
                module.activate_admin("127.0.0.1", port)
                self.assertTrue(enabled_link.is_symlink())
                self.assertFalse((prefix / ADMIN_ROOT / "admin-enabled").exists())
                module.complete_activation()
                self.assertTrue((prefix / ADMIN_ROOT / "admin-enabled").is_file())
                self.assertTrue((prefix / "var/www/server-dashboard/admin-status.json").is_file())
                self.assertEqual(module.STATUS.stat().st_mode & 0o777, 0o640)
                self.assertEqual((prefix / ADMIN_ROOT / "admin.json").stat().st_mode & 0o777, 0o400)
                self.assertEqual((prefix / ADMIN_ROOT / "admin.crt").stat().st_mode & 0o777, 0o400)
                self.assertEqual((prefix / ADMIN_ROOT / "admin.key").stat().st_mode & 0o777, 0o400)
                self.assertEqual((prefix / ADMIN_STATE / "account.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual((prefix / ADMIN_STATE).stat().st_mode & 0o777, 0o700)

                context = ssl._create_unverified_context()
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/login")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Administración HTTPS", response.read().decode("utf-8"))
                connection.close()

                body = json.dumps({"email": email, "password": password, "next": "/configuracion"}).encode("utf-8")
                headers = {"Content-Type": "application/json", "Origin": f"https://127.0.0.1:{port}"}
                bad_body = json.dumps({"email": email, "password": "incorrect-password"}).encode("utf-8")
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/login", body=bad_body, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 403, response.read().decode("utf-8"))
                self.assertFalse(
                    any(key.lower() == "set-cookie" and value.startswith("sep_adm=")
                        for key, value in response.getheaders()),
                    "failed login must not issue an authentication cookie",
                )
                response.read()
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/login", body=body, headers=headers)
                response = connection.getresponse()
                login_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(login_result["next"], "/configuracion")
                cookies = response.getheaders()
                cookie = next(value.split(";", 1)[0] for key, value in cookies if key.lower() == "set-cookie" and value.startswith("sep_adm="))
                self.assertTrue(any(key.lower() == "set-cookie" and "Secure" in value and "HttpOnly" in value and "SameSite=Strict" in value for key, value in cookies))
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/admin-status.json", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"enabled": True})
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/login", body=json.dumps({
                    "email": email, "password": password, "next": "https://evil.example/"
                }), headers=headers)
                response = connection.getresponse()
                unsafe_redirect_result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(unsafe_redirect_result["next"], "/")
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/admin", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                admin_html = response.read().decode("utf-8")
                self.assertIn("Configuración", admin_html)
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                for protected_path in ("/configuracion", "/admin"):
                    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                    connection.request("GET", protected_path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 303)
                    self.assertEqual(response.getheader("Location"), "/login?next="+urllib.parse.quote(protected_path,safe=''))
                    response.read()
                    connection.close()

                dashboard_html = (prefix / "var/www/server-dashboard/index.html").read_text()
                self.assertIn('id="sepAccountBar"', dashboard_html)
                self.assertIn('id="sepAccountSettings"', dashboard_html)
                self.assertIn('id="sepAccountLogout"', dashboard_html)
                self.assertIn("Sesión: ", dashboard_html)

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/api/admin/state", headers={"Cookie": cookie})
                response = connection.getresponse()
                state = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                connection.close()
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/logout", body="{}", headers={
                    "Cookie": cookie, "Origin": f"https://127.0.0.1:{port}",
                    "Content-Type": "application/json", "X-SEP-CSRF": state["csrf"]})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(any(key.lower() == "set-cookie" and value.startswith("sep_adm=")
                                    and "Max-Age=0" in value for key, value in response.getheaders()))
                response.read()
                connection.close()

                with self.assertRaises(OSError):
                    plain = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    plain.request("GET", "/login")
                    plain.getresponse()

                preserved = [
                    prefix / ADMIN_ROOT / name
                    for name in ("admin.json", "admin.crt", "admin.key", "admin-enabled")
                ] + [prefix / ADMIN_STATE / "account.json"]
                snapshots = {path: path.read_bytes() for path in preserved}
                self.install(prefix, "--upgrade")
                self.assertTrue(enabled_link.is_symlink())
                for path, content in snapshots.items():
                    self.assertEqual(path.read_bytes(), content, str(path))
                self.install(prefix, "--rollback")
                self.assertTrue(enabled_link.is_symlink())
                for path, content in snapshots.items():
                    self.assertEqual(path.read_bytes(), content, str(path))
            finally:
                if service is not None:
                    self.stop_service(service, log)

    @unittest.skipUnless(os.geteuid() == 0, "requiere UID 0 para verificar el acceso con dos identidades")
    def test_admin_status_permissions_on_upgrade_creation_and_restore(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-status-") as temp:
            # All paths remain in the temporary tree, including when the
            # setup helper runs its privileged file ownership branch.
            top = pathlib.Path(temp)
            top.chmod(0o711)
            prefix = top / "root"
            self.install(prefix, "--normal")
            # A real Debian installation already has searchable OS parents.
            for parent in (prefix, prefix / "var", prefix / "var/www"):
                parent.chmod(0o755)
            module = self.load_setup(prefix)
            web = pwd.getpwnam("www-data")
            try:
                admin = pwd.getpwnam("sep-admin")
            except KeyError:
                admin = pwd.getpwnam("nobody")
            try:
                read_gid = grp.getgrnam("server-dashboard-read").gr_gid
            except KeyError:
                read_gid = web.pw_gid  # Same numeric fallback as the isolated installer.
            enabled = b'{"enabled":true}\n'
            module.STATUS.write_bytes(enabled)
            os.chown(module.STATUS, web.pw_uid, web.pw_gid)
            module.STATUS.chmod(0o400)

            def as_service(account, script, *paths):
                return subprocess.run(
                    [sys.executable, "-c", script, *map(str, paths)], cwd=top,
                    user=account.pw_uid, group=account.pw_gid, extra_groups=[read_gid],
                    capture_output=True, text=True, check=False,
                )

            read = "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text().strip())"
            legacy = as_service(admin, read, module.STATUS)
            self.assertNotEqual(legacy.returncode, 0)
            self.assertIn("PermissionError", legacy.stderr)

            def check_status():
                st = module.STATUS.stat()
                self.assertEqual((st.st_uid, st.st_gid, st.st_mode & 0o777), (0, read_gid, 0o640))
                for account in (web, admin):
                    result = as_service(account, read, module.STATUS)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"enabled": True})
                    denied = as_service(account, "import sys; open(sys.argv[1], 'ab')", module.STATUS)
                    self.assertIn("PermissionError", denied.stderr)

            self.install(prefix, "--upgrade")
            check_status()
            module.ROOT.mkdir(mode=0o750, exist_ok=True)
            module.STATE.mkdir(mode=0o700, exist_ok=True)
            private = (module.ADMIN, module.KEY, module.ACCOUNT_STATE)
            for path in private:
                path.write_bytes(b"private fixture\n")
                path.chmod(0o600)
            with patch.object(module, "ROOT_PREFIX", None), patch.object(
                module, "status_read_gid", return_value=read_gid
            ):
                os.chown(module.STATUS, web.pw_uid, web.pw_gid)
                module.STATUS.chmod(0o400)
                module.normalize_managed_file_permissions(admin.pw_uid, admin.pw_gid)
                check_status()
                module.STATUS.unlink()
                module.complete_activation()
                check_status()
                status_snapshot = module.snapshot(module.STATUS)
                module.STATUS.unlink()
                module.restore_snapshot(module.STATUS, status_snapshot)
                check_status()
            for path, mode in zip(private, (0o400, 0o400, 0o600)):
                self.assertEqual(path.stat().st_mode & 0o777, mode)
                self.assertIn("PermissionError", as_service(web, read, path).stderr)

    def test_failed_https_health_check_never_enables_admin(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-bootstrap-failure-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            port = self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)
            input_patch, pass_patch, _email, _password = self.credentials(module)
            with input_patch, pass_patch:
                module.main()
            with patch.object(
                module, "check_https_ready", side_effect=RuntimeError("No se pudo validar HTTPS")
            ):
                with self.assertRaisesRegex(RuntimeError, "No se pudo validar HTTPS"):
                    module.activate_admin("127.0.0.1", port)
            for name in ("admin.json", "admin.crt", "admin.key"):
                self.assertTrue((prefix / ADMIN_ROOT / name).is_file(), name)
            self.assertFalse((prefix / ADMIN_ROOT / "admin-enabled").exists())
            self.assertTrue((prefix / ADMIN_STATE / "account.json").is_file())
            self.assertFalse((prefix / "var/www/server-dashboard/admin-status.json").exists())
            self.assertFalse(
                (prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service").exists()
            )

    def test_setup_rolls_back_credentials_when_production_health_check_fails(self):
        """Exercise the production failure path with every filesystem path pinned to temp."""
        with tempfile.TemporaryDirectory(prefix="miura-admin-bootstrap-transaction-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)
            pinned = (
                module.ROOT, module.STATE, module.CONF, module.MARK, module.ADMIN,
                module.CERT, module.KEY, module.WEB, module.STATUS,
                module.ADMIN_UNIT, module.ACCOUNT_STATE,
            )
            self.assertTrue(all(path.is_relative_to(prefix) for path in pinned))

            # Keep all paths temporary while exercising the production start /
            # probe / cleanup branch. The systemctl runner is a stub, not the
            # host manager; openssl still runs to create a real fixture cert.
            module.ROOT_PREFIX = None
            module.ensure_prerequisites = lambda **_kwargs: (0, 0)
            module.migrate_admin_unit = lambda: None
            calls = []
            real_subprocess = subprocess

            class IsolatedSubprocess:
                DEVNULL = real_subprocess.DEVNULL
                CalledProcessError = real_subprocess.CalledProcessError
                TimeoutExpired = real_subprocess.TimeoutExpired
                check_output = staticmethod(real_subprocess.check_output)

                @staticmethod
                def run(command, **kwargs):
                    if command and command[0] == "/usr/bin/systemctl":
                        calls.append(tuple(command))
                        return real_subprocess.CompletedProcess(command, 0)
                    return real_subprocess.run(command, **kwargs)

            module.subprocess = IsolatedSubprocess
            original_write = module.atomic_write
            module.status_read_gid = os.getgid
            module.atomic_write = lambda path, data, uid=None, gid=None, read_only=False, **options: original_write(
                path, data, None, None, read_only, **options
            )
            module.check_https_ready = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("TLS health check failed")
            )
            input_patch, pass_patch, _email, _password = self.credentials(module)
            with patch("os.geteuid", return_value=0), patch("os.chown"), input_patch, pass_patch:
                with self.assertRaisesRegex(RuntimeError, "TLS health check failed"):
                    module.main()

            self.assertIn(("/usr/bin/systemctl", "daemon-reload"), calls)
            self.assertIn(("/usr/bin/systemctl", "start", "server-dashboard-admin.service"), calls)
            self.assertIn(("/usr/bin/systemctl", "disable", "--now", "server-dashboard-admin.service"), calls)
            for name in ("admin.json", "admin.crt", "admin.key", "admin-enabled"):
                self.assertFalse((prefix / ADMIN_ROOT / name).exists(), name)
            self.assertFalse((prefix / ADMIN_STATE / "account.json").exists())
            self.assertFalse((prefix / "var/www/server-dashboard/admin-status.json").exists())
            self.assertFalse(
                (prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service").exists()
            )

    @unittest.skipUnless(systemd_manager_available(),
                         "requires isolated transient systemd manager and local sep-admin account")
    def test_admin_identity_can_read_runtime_files_and_only_write_allowlisted_paths(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-systemd-sandbox-") as temp:
            temp_path = pathlib.Path(temp)
            prefix = temp_path / "root"
            self.install(prefix, "--normal")
            port = self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)
            input_patch, pass_patch, _email, _password = self.credentials(module)
            with input_patch, pass_patch:
                module.main()

            # Root-prefix keeps host account/group databases untouched. Prepare
            # only the test tree with the same ownership/modes as production.
            sep_admin = pwd.getpwnam("sep-admin")
            read_gid = grp.getgrnam("www-data").gr_gid
            # Root-prefix deliberately does not mutate host accounts. Model
            # the installer-created, empty preferences group inside the
            # fixture so systemd-run can exercise the real supplementary GID.
            groups_file = prefix / "etc/group"
            group_lines = groups_file.read_text(encoding="utf-8").splitlines()
            # The group fixture lets systemd resolve the unit's supplementary
            # group, but each file below is readable only by the sep-admin
            # identity that consumes it.
            prefs_gid = read_gid
            group_lines.append(f"server-dashboard-prefs:x:{prefs_gid}:")
            groups_file.write_text("\n".join(group_lines) + "\n", encoding="utf-8")
            for path in (temp_path, prefix, prefix / "etc", prefix / "usr", prefix / "usr/local",
                         prefix / "usr/local/sbin", prefix / "var", prefix / "var/www",
                         prefix / "var/lib"):
                os.chown(path, sep_admin.pw_uid, sep_admin.pw_gid)
                os.chmod(path, 0o700)
            admin_dir = prefix / ADMIN_ROOT
            os.chown(admin_dir, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(admin_dir, 0o700)
            for name in ("admin.json", "admin.crt", "admin.key"):
                path = admin_dir / name
                os.chown(path, sep_admin.pw_uid, sep_admin.pw_gid)
                os.chmod(path, 0o400)
            state = prefix / ADMIN_STATE
            os.chown(state, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(state, 0o700)
            account_file = state / "account.json"
            os.chown(account_file, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(account_file, 0o600)

            config = prefix / "etc/server-dashboard.conf"
            os.chown(config, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(config, 0o400)
            web = prefix / "var/www/server-dashboard"
            os.chown(web, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(web, 0o700)
            for path in web.rglob("*"):
                if path.is_dir():
                    os.chown(path, sep_admin.pw_uid, sep_admin.pw_gid)
                    os.chmod(path, 0o700)
                else:
                    os.chown(path, sep_admin.pw_uid, sep_admin.pw_gid)
                    os.chmod(path, 0o400)
            sbin = prefix / "usr/local/sbin"
            helpers = [sbin / "server-dashboard-admin.py", sbin / "server-dashboard-live.py",
                       sbin / "sep-admin-fleet.py"]
            for helper in helpers:
                os.chown(helper, sep_admin.pw_uid, sep_admin.pw_gid)
                os.chmod(helper, 0o500)

            preferences = prefix / "var/lib/server-dashboard/preferences"
            os.chown(prefix / "var/lib/server-dashboard", sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(prefix / "var/lib/server-dashboard", 0o700)
            os.chown(preferences, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(preferences, 0o700)
            paths_to_read = [admin_dir / "admin.json", admin_dir / "admin.crt", admin_dir / "admin.key",
                             config, web / "index.html", *helpers]
            forbidden = prefix / "etc/miura-admin-sandbox-write-denied"
            python = (
                "from pathlib import Path; import errno,os,sys; "
                f"assert {read_gid} in os.getgroups(), 'systemd did not apply the supplementary read group'; "
                "[p.read_bytes() for p in map(Path,sys.argv[1:-2])]; "
                "prefs=Path(sys.argv[-2]); tmp=prefs/'.ui.pending'; tmp.write_text('saved'); "
                "os.replace(tmp,prefs/'ui.json'); "
                "blocked=Path(sys.argv[-1])\n"
                "try: blocked.write_text('forbidden')\n"
                "except OSError as exc:\n"
                "  assert exc.errno in (errno.EROFS,errno.EACCES), exc\n"
                "else: raise SystemExit('ProtectSystem=strict allowed an unlisted write')"
            )
            audit_unit = f"miura-admin-audit-{uuid.uuid4().hex[:12]}.service"
            try:
                run = subprocess.run(
                    ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                     f"--unit={audit_unit}",
                     f"--property=User={sep_admin.pw_uid}", f"--property=Group={sep_admin.pw_gid}",
                     f"--property=SupplementaryGroups={read_gid}", "--property=NoNewPrivileges=true",
                     "--property=ProtectSystem=strict", "--property=ProtectHome=true",
                     f"--property=ReadWritePaths={preferences} {state}",
                     "/usr/bin/python3", "-c", python,
                     *map(str, paths_to_read), str(preferences), str(forbidden)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            finally:
                self.cleanup_transient_unit(audit_unit)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual((preferences / "ui.json").read_text(encoding="utf-8"), "saved")
            self.assertFalse(forbidden.exists())

            # Exercise the actual HTTPS application under the unprivileged
            # service identity and the unit's filesystem sandbox. This is a
            # transient unit with paths redirected to this temporary prefix.
            module.complete_activation()
            unit_name = f"miura-admin-https-{uuid.uuid4().hex[:12]}.service"
            service_command = [
                "systemd-run", "--quiet", "--collect", f"--unit={unit_name}",
                f"--property=User={sep_admin.pw_uid}",
                f"--property=Group={sep_admin.pw_gid}",
                f"--property=SupplementaryGroups={read_gid}",
                "--property=NoNewPrivileges=true", "--property=ProtectSystem=strict",
                "--property=ProtectHome=true", "--property=ProtectKernelTunables=true",
                "--property=ProtectKernelModules=true", "--property=ProtectControlGroups=true",
                "--property=UMask=0077", f"--property=ReadWritePaths={preferences} {state}",
                f"--property=WorkingDirectory={web}",
                f"--property=Environment=MIURA_ROOT_PREFIX={prefix}",
                "/usr/bin/python3", str(prefix / "usr/local/sbin/server-dashboard-admin.py"),
            ]
            try:
                started = self.start_transient_unit(service_command, unit_name)
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                context = ssl._create_unverified_context()
                response = None
                for _ in range(40):
                    try:
                        connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=1)
                        connection.request("GET", "/login")
                        response = connection.getresponse()
                        payload = response.read()
                        connection.close()
                        if response.status == 200:
                            self.assertIn("Administración HTTPS", payload.decode("utf-8"))
                            break
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.25)
                self.assertIsNotNone(response, "transient admin service never accepted HTTPS")
                self.assertEqual(response.status, 200)

                headers = {
                    "Content-Type": "application/json",
                    "Origin": f"https://127.0.0.1:{port}",
                }
                invalid = json.dumps({
                    "email": "owner@example.test", "password": "invalid-password",
                }).encode("utf-8")
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/login", body=invalid, headers=headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertFalse(any(key.lower() == "set-cookie" and value.startswith("sep_adm=")
                                     for key, value in response.getheaders()))
                connection.close()

                valid = json.dumps({
                    "email": "owner@example.test", "password": "Miura-Test-Password-39!",
                }).encode("utf-8")
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("POST", "/api/admin/login", body=valid, headers=headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                cookie = next(value.split(";", 1)[0] for key, value in response.getheaders()
                              if key.lower() == "set-cookie" and value.startswith("sep_adm="))
                self.assertTrue(any(key.lower() == "set-cookie" and "Secure" in value and "HttpOnly" in value
                                    and "SameSite=Strict" in value for key, value in response.getheaders()))
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/admin", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                self.cleanup_transient_unit(unit_name)

    def test_admin_json_and_private_key_symlinks_are_rejected_without_touching_targets(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-symlink-guard-") as temp:
            temp_path = pathlib.Path(temp)
            prefix = temp_path / "root"
            self.install(prefix, "--normal")
            module = self.load_setup(prefix)
            admin_root = prefix / ADMIN_ROOT
            admin_root.mkdir(parents=True, exist_ok=True)
            outside_auth = temp_path / "outside-admin.json"
            outside_auth.write_text('{"username":"outside@example.test"}\n', encoding="utf-8")
            auth_link = admin_root / "admin.json"
            auth_link.symlink_to(outside_auth)

            with patch.object(builtins, "input", side_effect=AssertionError("must reject before prompts")):
                with self.assertRaises(SystemExit):
                    module.main()
            self.assertTrue(auth_link.is_symlink())
            self.assertEqual(outside_auth.read_text(encoding="utf-8"), '{"username":"outside@example.test"}\n')

            auth_link.unlink()
            (admin_root / "admin.json").write_text(
                '{"username":"owner@example.test","hash":"x","salt":"00","port":8767}\n',
                encoding="utf-8",
            )
            outside_key = temp_path / "outside-admin.key"
            outside_key.write_text("private-test-sentinel\n", encoding="utf-8")
            key_link = admin_root / "admin.key"
            key_link.symlink_to(outside_key)
            with patch.object(builtins, "input", side_effect=AssertionError("must reject before prompts")):
                with self.assertRaises(SystemExit):
                    module.main()
            self.assertTrue(key_link.is_symlink())
            self.assertEqual(outside_key.read_text(encoding="utf-8"), "private-test-sentinel\n")

    def test_failed_reconfigure_restores_active_credentials_then_restarts_service(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-reconfigure-rollback-") as temp:
            temp_path = pathlib.Path(temp)
            prefix = temp_path / "root"
            self.install(prefix, "--normal")
            self.add_minimal_boot_target(prefix)
            port = self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)

            # Seed a valid active installation wholly within this prefix.
            first_input, first_password, _email, _password = self.credentials(module)
            with first_input, first_password:
                module.main()
            subprocess.run(
                ["systemctl", f"--root={prefix}", "enable", module.SERVICE],
                capture_output=True, text=True, timeout=10, check=True,
            )
            module.complete_activation()
            tracked = [module.ADMIN, module.CERT, module.KEY, module.MARK,
                      module.STATUS, module.ACCOUNT_STATE]
            before = {path: path.read_bytes() for path in tracked}
            enabled_link = prefix / "etc/systemd/system/multi-user.target.wants/server-dashboard-admin.service"
            self.assertTrue(enabled_link.is_symlink())

            # All module constants remain pinned to the isolated tree. Switching
            # the control-flow flag exercises the production state/restart path;
            # every systemctl subprocess is intercepted below.
            pinned = (module.ROOT, module.STATE, module.CONF, module.MARK, module.ADMIN,
                      module.CERT, module.KEY, module.WEB, module.STATUS,
                      module.ADMIN_UNIT, module.ACCOUNT_STATE)
            self.assertTrue(all(path.is_relative_to(prefix) for path in pinned))
            module.ROOT_PREFIX = None
            module.ensure_prerequisites = lambda **_kwargs: (0, 0)
            module.migrate_admin_unit = lambda: None
            calls = []
            restoring = {"started": False}
            real_subprocess = subprocess

            class IsolatedSubprocess:
                DEVNULL = real_subprocess.DEVNULL
                CalledProcessError = real_subprocess.CalledProcessError
                TimeoutExpired = real_subprocess.TimeoutExpired
                check_output = staticmethod(real_subprocess.check_output)

                @staticmethod
                def run(command, **kwargs):
                    if command and command[0] == "/usr/bin/systemctl":
                        calls.append(tuple(command))
                        if command[1:3] == ("disable", "--now"):
                            restoring["started"] = True
                        if "restart" in command and restoring["started"]:
                            for path, content in before.items():
                                if path.read_bytes() != content:
                                    raise AssertionError(f"restart happened before restore: {path}")
                        stdout = ""
                        if "--property=ActiveState" in command:
                            stdout = "active\n"
                        elif "--property=UnitFileState" in command:
                            stdout = "enabled\n"
                        return real_subprocess.CompletedProcess(
                            command, 0, stdout=stdout, stderr=""
                        )
                    return real_subprocess.run(command, **kwargs)

            module.subprocess = IsolatedSubprocess
            real_atomic_write = module.atomic_write
            module.status_read_gid = os.getgid
            module.atomic_write = lambda path, data, uid=None, gid=None, read_only=False, **options: real_atomic_write(
                path, data, None, None, read_only, **options
            )
            real_run = module.run

            def isolated_run(*command, timeout=90):
                if command and command[0] == "/usr/bin/systemctl":
                    return module.subprocess.run(command, timeout=timeout)
                return real_run(*command, timeout=timeout)

            module.run = isolated_run
            module.check_https_ready = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("TLS health check failed during reconfigure")
            )
            responses = iter(["s", "s", "owner@example.test", "owner@example.test", "n", "1"])
            input_patch = patch.object(builtins, "input", side_effect=lambda _prompt="": next(responses))
            pass_patch = patch.object(module.getpass, "getpass",
                                      side_effect=["Miura-Reconfigure-Password-39!", "Miura-Reconfigure-Password-39!"])
            with patch("os.geteuid", return_value=0), patch("os.chown"), input_patch, pass_patch:
                with self.assertRaisesRegex(RuntimeError, "TLS health check failed"):
                    module.main()

            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content, str(path))
            self.assertTrue(enabled_link.is_symlink())
            restarts = [call for call in calls if call[:2] == ("/usr/bin/systemctl", "restart")]
            self.assertTrue(restarts, f"recovered active service was not restarted: {calls}")


if __name__ == "__main__":
    unittest.main()
