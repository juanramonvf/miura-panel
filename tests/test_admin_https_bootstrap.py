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
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
INSTALLER = pathlib.Path(os.environ.get(
    "MIURA_INSTALLER_PATH",
    ROOT / "dist" / f"install-spanish-empire-panel-v{VERSION}.sh",
)).resolve()
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
            self.assertIn("ProtectHome=true\n", unit)
            self.assertIn("ProtectKernelTunables=true\n", unit)
            self.assertIn("ProtectKernelModules=true\n", unit)
            self.assertIn("ProtectControlGroups=true\n", unit)
            self.assertIn("NoNewPrivileges=true\n", unit)
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
            self.assertEqual(admin_users[0][2], admin_users[0][3], "service account must use its dedicated primary group")
            self.assertIn(admin_users[0][6], ("/usr/sbin/nologin", "/bin/false"))
            self.assertEqual(admin_groups[0][2], admin_users[0][3])
            self.assertEqual(admin_groups[0][3], "", "dedicated admin group must not contain human accounts")
            self.assertEqual((prefix / ADMIN_STATE).stat().st_mode & 0o777, 0o700)

            analyzer = shutil.which("systemd-analyze")
            if analyzer:
                checked = subprocess.run(
                    [analyzer, "verify", str(unit_path)],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

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

    def test_setup_verifies_live_tls_before_enable_then_real_password_login(self):
        with tempfile.TemporaryDirectory(prefix="miura-admin-bootstrap-login-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.install(prefix, "--normal")
            self.add_minimal_boot_target(prefix)
            port = self.set_free_port_pair(prefix)
            module = self.load_setup(prefix)
            input_patch, pass_patch, email, password = self.credentials(module)
            service = log = None
            try:
                with input_patch, pass_patch:
                    module.main()
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
                self.assertEqual((prefix / ADMIN_ROOT / "admin.json").stat().st_mode & 0o777, 0o640)
                self.assertEqual((prefix / ADMIN_ROOT / "admin.crt").stat().st_mode & 0o777, 0o640)
                self.assertEqual((prefix / ADMIN_ROOT / "admin.key").stat().st_mode & 0o777, 0o640)
                self.assertEqual((prefix / ADMIN_STATE / "account.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual((prefix / ADMIN_STATE).stat().st_mode & 0o777, 0o700)

                context = ssl._create_unverified_context()
                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/login")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Administración HTTPS", response.read().decode("utf-8"))
                connection.close()

                body = json.dumps({"email": email, "password": password}).encode("utf-8")
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
                self.assertEqual(response.status, 200, response.read().decode("utf-8"))
                cookies = response.getheaders()
                cookie = next(value.split(";", 1)[0] for key, value in cookies if key.lower() == "set-cookie" and value.startswith("sep_adm="))
                self.assertTrue(any(key.lower() == "set-cookie" and "Secure" in value and "HttpOnly" in value and "SameSite=Strict" in value for key, value in cookies))
                connection.close()

                connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
                connection.request("GET", "/admin", headers={"Cookie": cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn("Configuración", response.read().decode("utf-8"))
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
            module.ensure_prerequisites = lambda: (0, 0)
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
            module.atomic_write = lambda path, data, mode, uid=None, gid=None: original_write(
                path, data, mode, None, None
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
            sep_group = grp.getgrnam("sep-admin")
            read_gid = grp.getgrnam("www-data").gr_gid
            # Root-prefix deliberately does not mutate host accounts. Model
            # the installer-created, empty preferences group inside the
            # fixture so systemd-run can exercise the real supplementary GID.
            groups_file = prefix / "etc/group"
            group_lines = groups_file.read_text(encoding="utf-8").splitlines()
            # Use the existing www-data numeric GID for this fixture entry;
            # the host's systemd resolves supplementary groups against its
            # own group database, while the production unit is checked above
            # for the dedicated server-dashboard-prefs name.
            prefs_gid = read_gid
            group_lines.append(f"server-dashboard-prefs:x:{prefs_gid}:")
            groups_file.write_text("\n".join(group_lines) + "\n", encoding="utf-8")
            for path in (temp_path, prefix, prefix / "etc", prefix / "usr", prefix / "usr/local",
                         prefix / "usr/local/sbin", prefix / "var", prefix / "var/www",
                         prefix / "var/lib"):
                os.chmod(path, 0o755)
            admin_dir = prefix / ADMIN_ROOT
            os.chown(admin_dir, 0, sep_group.gr_gid)
            os.chmod(admin_dir, 0o750)
            for name in ("admin.json", "admin.crt", "admin.key"):
                path = admin_dir / name
                os.chown(path, 0, sep_group.gr_gid)
                os.chmod(path, 0o640)
            state = prefix / ADMIN_STATE
            os.chown(state, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(state, 0o700)
            account_file = state / "account.json"
            os.chown(account_file, sep_admin.pw_uid, sep_admin.pw_gid)
            os.chmod(account_file, 0o600)

            config = prefix / "etc/server-dashboard.conf"
            os.chown(config, 0, read_gid)
            os.chmod(config, 0o640)
            web = prefix / "var/www/server-dashboard"
            os.chown(web, 0, read_gid)
            os.chmod(web, 0o750)
            for path in web.rglob("*"):
                if path.is_dir():
                    os.chown(path, 0, read_gid)
                    os.chmod(path, 0o750)
                else:
                    os.chown(path, 0, read_gid)
                    os.chmod(path, 0o640)
            sbin = prefix / "usr/local/sbin"
            helpers = [sbin / "server-dashboard-admin.py", sbin / "server-dashboard-live.py",
                       sbin / "sep-admin-fleet.py"]
            for helper in helpers:
                os.chown(helper, 0, read_gid)
                os.chmod(helper, 0o750)

            preferences = prefix / "var/lib/server-dashboard/preferences"
            os.chown(prefix / "var/lib/server-dashboard", 0, read_gid)
            os.chmod(prefix / "var/lib/server-dashboard", 0o710)
            os.chown(preferences, 0, prefs_gid)
            os.chmod(preferences, 0o2770)
            paths_to_read = [admin_dir / "admin.json", admin_dir / "admin.crt", admin_dir / "admin.key",
                             config, web / "index.html", *helpers]
            forbidden = prefix / "etc/miura-admin-sandbox-write-denied"
            python = (
                "from pathlib import Path; import errno,os,sys; "
                "[p.read_bytes() for p in map(Path,sys.argv[1:-2])]; "
                "prefs=Path(sys.argv[-2]); tmp=prefs/'.ui.pending'; tmp.write_text('saved'); "
                "os.replace(tmp,prefs/'ui.json'); "
                "blocked=Path(sys.argv[-1])\n"
                "try: blocked.write_text('forbidden')\n"
                "except OSError as exc:\n"
                "  assert exc.errno in (errno.EROFS,errno.EACCES), exc\n"
                "else: raise SystemExit('ProtectSystem=strict allowed an unlisted write')"
            )
            run = subprocess.run(
                ["systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                 f"--unit=miura-admin-audit-{uuid.uuid4().hex[:12]}",
                 f"--property=User={sep_admin.pw_uid}", f"--property=Group={sep_admin.pw_gid}",
                 f"--property=SupplementaryGroups={read_gid}", "--property=NoNewPrivileges=true",
                 "--property=ProtectSystem=strict", "--property=ProtectHome=true",
                 f"--property=ReadWritePaths={preferences} {state}",
                 "/usr/bin/python3", "-c", python,
                 *map(str, paths_to_read), str(preferences), str(forbidden)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual((preferences / "ui.json").read_text(encoding="utf-8"), "saved")
            self.assertFalse(forbidden.exists())

            # Exercise the actual HTTPS application under the unprivileged
            # service identity and the unit's filesystem sandbox. This is a
            # transient unit with paths redirected to this temporary prefix.
            module.complete_activation()
            unit_name = f"miura-admin-https-{uuid.uuid4().hex[:12]}.service"
            service_command = [
                "systemd-run", "--quiet", f"--unit={unit_name}",
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
            started = subprocess.run(
                service_command, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
            context = ssl._create_unverified_context()
            try:
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
                subprocess.run(["systemctl", "stop", unit_name], capture_output=True,
                               text=True, timeout=15, check=False)

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
            module.ensure_prerequisites = lambda: (0, 0)
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
            module.atomic_write = lambda path, data, mode, uid=None, gid=None: real_atomic_write(
                path, data, mode, None, None
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
