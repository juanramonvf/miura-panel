"""Isolated permission and HTTP behavior checks for the web service.

The end-to-end checks run only inside MIURA_ROOT_PREFIX fixtures. The final
runtime test drops to the host's www-data uid/gid while serving only loopback.
"""

import json
import os
import pathlib
import pwd
import grp
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
INSTALLER = pathlib.Path(os.environ.get(
    "MIURA_INSTALLER_PATH",
    DIST / f"install-spanish-empire-panel-v{VERSION}.sh",
)).resolve()
OLD_INSTALLER = DIST / "install-spanish-empire-panel-v1.0.36.sh"
PREF_REL = pathlib.Path("var/lib/server-dashboard/preferences")
STATE_REL = pathlib.Path("var/lib/server-dashboard")
LIVE_REL = pathlib.Path("usr/local/sbin/server-dashboard-live.py")
WEB_REL = pathlib.Path("var/www/server-dashboard")
CONF_REL = pathlib.Path("etc/server-dashboard.conf")
UNIT_REL = pathlib.Path("etc/systemd/system/server-dashboard-web.service")
COLLECTOR_UNIT_REL = pathlib.Path("etc/systemd/system/server-dashboard-collector.service")
ADMIN_UNIT_REL = pathlib.Path("etc/systemd/system/server-dashboard-admin.service")
PREFS_GROUP = "server-dashboard-prefs"
READ_GROUP = "server-dashboard-read"


def setpriv_available():
    return shutil.which("setpriv") is not None


def service_groups_available():
    try:
        pwd.getpwnam("www-data")
        grp.getgrnam("www-data")
    except KeyError:
        return False
    return True


ROOT_PERMISSION_TESTS = os.geteuid() == 0 and service_groups_available()


def named_prefs_gid():
    try:
        return grp.getgrnam(PREFS_GROUP).gr_gid
    except KeyError:
        return None


def read_group_gid():
    try:
        return grp.getgrnam(READ_GROUP).gr_gid
    except KeyError:
        return grp.getgrnam("www-data").gr_gid


def legacy_group_gid():
    try:
        return grp.getgrnam("server-dashboard").gr_gid
    except KeyError:
        return 0


class WebServicePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not INSTALLER.is_file():
            raise AssertionError(f"Falta el instalador candidato: {INSTALLER}")

    def run_installer(self, installer, prefix, *args, extra_env=None):
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = str(prefix)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ["bash", str(installer), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode:
            self.fail(f"{installer.name} {args} falló:\n{result.stdout}\n{result.stderr}")
        return result

    def assert_service_contract(self, prefix):
        live = prefix / LIVE_REL
        self.assertTrue(live.is_file() and not live.is_symlink())
        self.assertEqual(live.stat().st_mode & 0o777, 0o750)
        self.assertEqual(live.stat().st_uid, 0)
        self.assertEqual(live.stat().st_gid, read_group_gid())
        for helper in ("server-dashboard-admin.py", "sep-admin-fleet.py"):
            path = prefix / "usr/local/sbin" / helper
            self.assertEqual(path.stat().st_uid, 0)
            self.assertEqual(path.stat().st_gid, read_group_gid())
            self.assertEqual(path.stat().st_mode & 0o777, 0o750)
        conf = (prefix / CONF_REL).stat()
        self.assertEqual(conf.st_uid, 0)
        self.assertEqual(conf.st_gid, read_group_gid())
        self.assertEqual(conf.st_mode & 0o777, 0o640)
        web = (prefix / WEB_REL).stat()
        self.assertEqual(web.st_uid, 0)
        self.assertEqual(web.st_gid, read_group_gid())
        self.assertEqual(web.st_mode & 0o777, 0o750)
        for path in (prefix / WEB_REL).rglob("*"):
            st = path.lstat()
            self.assertFalse(path.is_symlink(), path)
            self.assertEqual(st.st_gid, read_group_gid(), path)
            self.assertFalse(st.st_mode & 0o007, path)
            if path.is_dir():
                self.assertEqual(st.st_mode & 0o777, 0o750, path)
            else:
                self.assertEqual(st.st_mode & 0o777, 0o640, path)
        unit = (prefix / UNIT_REL).read_text(encoding="utf-8")
        self.assertIn("User=www-data\n", unit)
        self.assertIn("Group=www-data\n", unit)
        self.assertIn(f"SupplementaryGroups={PREFS_GROUP} {READ_GROUP}\n", unit)
        self.assertIn("ProtectSystem=strict\n", unit)
        writes = [line.split("=", 1)[1] for line in unit.splitlines()
                  if line.startswith("ReadWritePaths=")]
        self.assertEqual(writes, ["/var/lib/server-dashboard/preferences"])
        self.assertNotIn("ReadWritePaths=/var/lib/server-dashboard\n", unit)
        self.assertNotIn("ReadWritePaths=/var\n", unit)

        prefs = prefix / PREF_REL
        self.assertTrue(prefs.is_dir() and not prefs.is_symlink())
        st = prefs.stat()
        self.assertEqual(st.st_uid, 0)
        if named_prefs_gid() is not None:
            self.assertEqual(st.st_gid, named_prefs_gid())
        self.assertEqual(st.st_mode & 0o2777, 0o2770)
        state = (prefix / STATE_REL).stat()
        self.assertEqual(state.st_uid, 0)
        if named_prefs_gid() is not None:
            self.assertEqual(state.st_gid, named_prefs_gid())
        self.assertEqual(state.st_mode & 0o777, 0o710)
        collector = (prefix / COLLECTOR_UNIT_REL).read_text(encoding="utf-8")
        self.assertIn("ProtectSystem=strict\n", collector)
        collector_writes = [line.split("=", 1)[1] for line in collector.splitlines()
                            if line.startswith("ReadWritePaths=")]
        self.assertEqual(collector_writes,
                         ["/var/lib/server-dashboard /var/www/server-dashboard"])

    def assert_admin_preferences_dropin(self, prefix, expected):
        dropin_dir = (prefix / ADMIN_UNIT_REL).parent / (ADMIN_UNIT_REL.name + ".d")
        dropins = sorted(dropin_dir.glob("*.conf")) if dropin_dir.exists() else []
        if not expected:
            self.assertEqual(dropins, [])
            return
        self.assertEqual(len(dropins), 1)
        content = dropins[0].read_text(encoding="utf-8")
        self.assertIn("[Service]\n", content)
        self.assertIn("SupplementaryGroups=server-dashboard-prefs\n", content)
        self.assertIn(f"SupplementaryGroups={READ_GROUP}\n", content)
        analyzer = shutil.which("systemd-analyze")
        if analyzer:
            service = (
                "[Unit]\nDescription=Miura admin drop-in syntax test\n\n"
                "[Service]\nUser=root\nExecStart=/usr/bin/true\n"
                + "".join(line + "\n" for line in content.splitlines()[1:])
            )
            with tempfile.NamedTemporaryFile("w", suffix=".service", encoding="utf-8") as check:
                check.write(service)
                check.flush()
                result = subprocess.run(
                    [analyzer, "verify", check.name], capture_output=True,
                    text=True, timeout=10, check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Unknown key", result.stderr)
        writes = [line.split("=", 1)[1] for line in content.splitlines()
                  if line.startswith("ReadWritePaths=")]
        self.assertEqual(writes, ["/var/lib/server-dashboard/preferences"])

    @unittest.skipUnless(ROOT_PERMISSION_TESTS,
                         "requiere UID 0 y los grupos del servicio para revisar propietario/permisos")
    def test_clean_install_permissions_and_narrow_systemd_write_allowance(self):
        with tempfile.TemporaryDirectory(prefix="miura-web-clean-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.run_installer(INSTALLER, prefix, "--demo")
            self.assert_service_contract(prefix)
            self.assert_admin_preferences_dropin(prefix, expected=False)

    @unittest.skipUnless(ROOT_PERMISSION_TESTS and setpriv_available(),
                         "requiere root y setpriv para probar permisos efectivos con otro UID")
    def test_admin_runtime_group_can_read_all_required_paths_without_other_access(self):
        nobody = pwd.getpwnam("nobody")
        nogroup = grp.getgrnam("nogroup")
        read_gid = read_group_gid()
        with tempfile.TemporaryDirectory(prefix="miura-admin-read-access-") as temp:
            os.chown(temp, nobody.pw_uid, read_gid)
            os.chmod(temp, 0o710)
            prefix = pathlib.Path(temp) / "root"
            admin_unit = prefix / ADMIN_UNIT_REL
            admin_unit.parent.mkdir(parents=True, exist_ok=True)
            admin_unit.write_text(
                "[Unit]\nDescription=Isolated sep-admin fixture\n\n"
                "[Service]\nUser=sep-admin\nGroup=sep-admin\n"
                "ExecStart=/usr/bin/python3 /usr/local/sbin/server-dashboard-admin.py\n",
                encoding="utf-8",
            )
            self.run_installer(INSTALLER, prefix, "--demo")
            self.assert_service_contract(prefix)
            self.assert_admin_preferences_dropin(prefix, expected=True)
            os.chown(prefix, 0, read_gid)
            prefix.chmod(0o710)
            # These are standard searchable ancestors on the target host; the
            # installer fixture creates them privately by default.
            for rel in ("var", "var/www", "usr", "usr/local", "usr/local/sbin"):
                parent = prefix / rel
                if parent.exists():
                    parent.chmod(0o755)
            paths = [
                prefix / CONF_REL,
                prefix / WEB_REL / "index.html",
                prefix / "usr/local/sbin/server-dashboard-live.py",
                prefix / "usr/local/sbin/server-dashboard-admin.py",
                prefix / "usr/local/sbin/sep-admin-fleet.py",
            ]
            read_script = (
                "from pathlib import Path; import sys; "
                "[Path(p).read_bytes() for p in sys.argv[1:]]"
            )
            result = subprocess.run(
                [shutil.which("setpriv"), f"--reuid={nobody.pw_uid}",
                 f"--regid={nogroup.gr_gid}", f"--groups={read_gid}",
                 sys.executable, "-c", read_script, *map(str, paths)],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode:
                details = "\n".join(
                    f"{path}: {path.stat().st_uid}:{path.stat().st_gid} "
                    f"{path.stat().st_mode & 0o777:o}"
                    for target in paths for path in [target, *target.parents]
                    if path.exists()
                )
                identity = subprocess.run(
                    [shutil.which("setpriv"), f"--reuid={nobody.pw_uid}",
                     f"--regid={nogroup.gr_gid}", f"--groups={read_gid}", "id"],
                    capture_output=True, text=True, check=False,
                )
                self.fail(f"{result.stderr}\n{identity.stdout}\n{details}")

    @unittest.skipUnless(ROOT_PERMISSION_TESTS,
                         "requiere root para validar creación de ancestros web aislados")
    def test_missing_web_parent_is_created_searchable_and_private_parent_fails_closed(self):
        read_gid = read_group_gid()
        for restrictive in (False, True):
            with self.subTest(restrictive_parent=restrictive), tempfile.TemporaryDirectory(
                    prefix="miura-web-parent-check-") as temp:
                os.chown(temp, pwd.getpwnam("nobody").pw_uid, read_gid)
                os.chmod(temp, 0o710)
                prefix = pathlib.Path(temp) / "root"
                prefix.mkdir()
                os.chown(prefix, 0, read_gid)
                prefix.chmod(0o710)
                if restrictive:
                    (prefix / "var").mkdir(mode=0o750)
                result = subprocess.run(
                    ["bash", str(INSTALLER), "--demo"], cwd=ROOT,
                    env={**os.environ, "MIURA_ROOT_PREFIX": str(prefix),
                         "MIURA_TEST_WEB_PARENT_CHECK": "1"},
                    capture_output=True, text=True, timeout=90, check=False,
                )
                if restrictive:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("no pueden atravesar", result.stderr)
                    self.assertFalse((prefix / WEB_REL / "version.json").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for rel in ("var", "var/www"):
                        parent = (prefix / rel).stat()
                        self.assertEqual(parent.st_mode & 0o777, 0o755)
                        self.assertEqual(parent.st_uid, 0)

    @unittest.skipUnless(ROOT_PERMISSION_TESTS,
                         "requiere UID 0 para ejecutar instaladores en el prefijo temporal")
    def test_admin_unit_gets_only_preferences_dropin_and_it_survives_rollback(self):
        if not OLD_INSTALLER.is_file():
            self.skipTest("no está disponible el instalador previo v1.0.36")
        with tempfile.TemporaryDirectory(prefix="miura-admin-unit-dropin-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.run_installer(OLD_INSTALLER, prefix, "--demo")
            admin_unit = prefix / ADMIN_UNIT_REL
            admin_unit.parent.mkdir(parents=True, exist_ok=True)
            original = (
                "[Unit]\nDescription=Isolated admin service fixture\n\n"
                "[Service]\nUser=www-data\nGroup=www-data\n"
                "ExecStart=/usr/bin/true\nProtectSystem=strict\n"
            )
            admin_unit.write_text(original, encoding="utf-8")
            admin_unit.chmod(0o644)

            self.run_installer(INSTALLER, prefix, "--upgrade")
            self.assertEqual(admin_unit.read_text(encoding="utf-8"), original)
            self.assert_admin_preferences_dropin(prefix, expected=True)

            self.run_installer(INSTALLER, prefix, "--rollback")
            self.assertEqual(admin_unit.read_text(encoding="utf-8"), original)
            self.assert_admin_preferences_dropin(prefix, expected=True)
            self.assert_service_contract(prefix)

    @unittest.skipUnless(ROOT_PERMISSION_TESTS,
                         "requiere UID 0 para probar rutas gestionadas en un prefijo aislado")
    def test_admin_dropin_conflicts_and_unsafe_paths_fail_before_upgrade(self):
        cases = ("conflicting file", "symlinked directory", "writable directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                    prefix="miura-admin-dropin-reject-") as temp:
                prefix = pathlib.Path(temp) / "root"
                self.run_installer(OLD_INSTALLER, prefix, "--demo")
                admin_unit = prefix / ADMIN_UNIT_REL
                admin_unit.parent.mkdir(parents=True, exist_ok=True)
                admin_unit.write_text(
                    "[Unit]\nDescription=Isolated admin service fixture\n\n"
                    "[Service]\nUser=www-data\nGroup=www-data\n"
                    "ExecStart=/usr/bin/true\nProtectSystem=strict\n",
                    encoding="utf-8",
                )
                dropin_dir = (prefix / ADMIN_UNIT_REL).parent / (ADMIN_UNIT_REL.name + ".d")
                external_dir = pathlib.Path(temp) / "external"
                if case == "conflicting file":
                    dropin_dir.mkdir(parents=True)
                    (dropin_dir / "miura-preferences.conf").write_text(
                        "[Service]\nSupplementaryGroups=other\n", encoding="utf-8")
                elif case == "symlinked directory":
                    external_dir.mkdir()
                    dropin_dir.symlink_to(external_dir, target_is_directory=True)
                else:
                    dropin_dir.mkdir(parents=True)
                    dropin_dir.chmod(0o770)

                before_config = (prefix / CONF_REL).read_bytes()
                before_version = (prefix / WEB_REL / "version.json").read_bytes()
                env = {**os.environ, "MIURA_ROOT_PREFIX": str(prefix)}
                result = subprocess.run(
                    ["bash", str(INSTALLER), "--upgrade"], cwd=ROOT, env=env,
                    capture_output=True, text=True, timeout=90, check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((prefix / CONF_REL).read_bytes(), before_config)
                self.assertEqual((prefix / WEB_REL / "version.json").read_bytes(), before_version)

    @unittest.skipUnless(ROOT_PERMISSION_TESTS and setpriv_available(),
                         "requiere UID 0 y grupos del servicio para reproducir permisos heredados")
    def test_upgrade_from_v1034_and_v1036_repairs_modes_and_preserves_prefs(self):
        if not OLD_INSTALLER.is_file():
            self.skipTest("no está disponible el instalador previo v1.0.36")
        server_gid = legacy_group_gid()
        www = pwd.getpwnam("www-data")
        prefs_gid = named_prefs_gid() or grp.getgrnam("www-data").gr_gid
        for old_version in ("1.0.34", "1.0.36"):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory(
                    prefix=f"miura-web-upgrade-{old_version}-") as temp:
                os.chown(temp, www.pw_uid, www.pw_gid)
                os.chmod(temp, 0o100)
                prefix = pathlib.Path(temp) / "root"
                self.run_installer(OLD_INSTALLER, prefix, "--demo")
                (prefix / WEB_REL / "version.json").write_text(
                    json.dumps({"version": old_version}) + "\n", encoding="utf-8")
                conf = prefix / CONF_REL
                conf.write_text(
                    "DASHBOARD_MODE=demo\n"
                    "DASHBOARD_BIND_IP=127.0.0.1\n"
                    "DASHBOARD_PORT=8891\n"
                    "DASHBOARD_TITLE=Persist this title\n",
                    encoding="utf-8",
                )
                live = prefix / LIVE_REL
                live.chmod(0o750)  # reproduce the v1.0.36 root:root regression
                prefs = prefix / PREF_REL
                prefs.mkdir(parents=True, exist_ok=True)
                prefs.chmod(0o770)
                os.chown(prefs, 0, server_gid)
                old_ui = prefs / "ui.json"
                old_ui.write_text(
                    '{"servers":[{"host":"192.0.2.8","port":8766,'
                    '"label":"Stored before upgrade","secure":false,"primary":false}],'
                    '"disk_aliases":{}}\n',
                    encoding="utf-8",
                )
                old_ui.chmod(0o660)
                os.chown(old_ui, 0, server_gid)

                self.run_installer(INSTALLER, prefix, "--upgrade")
                self.assert_service_contract(prefix)
                if named_prefs_gid() is None:
                    # Root-prefix mode must not add a host group. Simulate the
                    # resolved supplemental GID solely inside this fixture.
                    os.chown(prefix / STATE_REL, 0, prefs_gid)
                    os.chown(prefs, 0, prefs_gid)
                    os.chown(old_ui, 0, prefs_gid)
                for parent in (prefix, prefix / "var", prefix / "var/lib"):
                    if parent.exists() and parent.is_dir():
                        parent.chmod(0o755)
                self.assertIn("DASHBOARD_MODE=demo", conf.read_text(encoding="utf-8"))
                self.assertIn("DASHBOARD_TITLE=Persist this title", conf.read_text(encoding="utf-8"))
                self.assertEqual(json.loads((prefix / WEB_REL / "version.json").read_text())["version"], VERSION)
                self.assertEqual(json.loads(old_ui.read_text())["servers"][0]["label"], "Stored before upgrade")
                read_as_service = subprocess.run(
                    [shutil.which("setpriv"),
                     f"--reuid={pwd.getpwnam('www-data').pw_uid}",
                     f"--regid={grp.getgrnam('www-data').gr_gid}",
                     f"--groups={prefs_gid},{read_group_gid()}",
                     sys.executable, "-c",
                     "from pathlib import Path; print(Path(__import__('sys').argv[1]).read_text())",
                     str(old_ui)],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertEqual(read_as_service.returncode, 0, read_as_service.stderr)
                self.assertIn("Stored before upgrade", read_as_service.stdout)
                self.run_installer(INSTALLER, prefix, "--rollback")
                self.assert_service_contract(prefix)
                self.assertEqual(json.loads((prefix / WEB_REL / "version.json").read_text())["version"], old_version)
                os.chown(temp, pwd.getpwnam("nobody").pw_uid, read_group_gid())
                os.chmod(temp, 0o710)
                os.chown(prefix, 0, read_group_gid())
                prefix.chmod(0o710)
                for rel in ("etc", "var", "var/www", "usr", "usr/local", "usr/local/sbin"):
                    parent = prefix / rel
                    if parent.exists(): parent.chmod(0o755)
                readable_after_rollback = [
                    prefix / CONF_REL, prefix / WEB_REL / "index.html",
                    prefix / LIVE_REL,
                    prefix / "usr/local/sbin/server-dashboard-admin.py",
                    prefix / "usr/local/sbin/sep-admin-fleet.py",
                ]
                nobody = pwd.getpwnam("nobody")
                nogroup = grp.getgrnam("nogroup")
                read_after_rollback = subprocess.run(
                    [shutil.which("setpriv"), f"--reuid={nobody.pw_uid}",
                     f"--regid={nogroup.gr_gid}",
                     f"--groups={read_group_gid()}", sys.executable, "-c",
                     "from pathlib import Path; import sys; [Path(p).read_bytes() for p in sys.argv[1:]]",
                     *map(str, readable_after_rollback)],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(read_after_rollback.returncode, 0,
                                 read_after_rollback.stderr)

    @unittest.skipUnless(ROOT_PERMISSION_TESTS and setpriv_available(),
                         "requiere UID 0, setpriv y grupos del servicio para ejecutar como www-data")
    def test_www_data_executes_service_and_saves_only_visual_preferences_over_http(self):
        www = pwd.getpwnam("www-data")
        www_gid = grp.getgrnam("www-data").gr_gid
        prefs_gid = named_prefs_gid() or www_gid
        with tempfile.TemporaryDirectory(prefix="miura-web-www-data-") as temp:
            os.chown(temp, www.pw_uid, www_gid)
            os.chmod(temp, 0o100)
            prefix = pathlib.Path(temp) / "root"
            self.run_installer(INSTALLER, prefix, "--demo")
            self.assert_service_contract(prefix)
            if named_prefs_gid() is None:
                os.chown(prefix / STATE_REL, 0, prefs_gid)
                os.chown(prefix / PREF_REL, 0, prefs_gid)

            # Make the fixture normal mode and use a port selected for this test.
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            conf = prefix / CONF_REL
            conf.write_text(
                "DASHBOARD_MODE=normal\n"
                "DASHBOARD_BIND_IP=127.0.0.1\n"
                f"DASHBOARD_PORT={port}\n",
                encoding="utf-8",
            )

            # The root-prefix fixture does not chown the host web tree as a real
            # installation would. Give only this read-only fixture to www-data.
            web = prefix / WEB_REL
            for path in sorted(web.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_dir():
                    os.chown(path, 0, www_gid)
                    path.chmod(0o750)
                elif path.is_file():
                    os.chown(path, 0, www_gid)
                    path.chmod(0o640)
            os.chown(web, 0, www_gid)
            web.chmod(0o750)

            original_live = (prefix / LIVE_REL).read_text(encoding="utf-8")
            runtime = prefix / LIVE_REL
            replacements = (
                ('CONFIG_FILE = Path("/etc/server-dashboard.conf")',
                 f"CONFIG_FILE = Path({str(conf)!r})"),
                ('WEB_DIR = Path("/var/www/server-dashboard")',
                 f"WEB_DIR = Path({str(web)!r})"),
                ('PREF_DIR = Path("/var/lib/server-dashboard/preferences")',
                 f"PREF_DIR = Path({str(prefix / PREF_REL)!r})"),
                ('grp.getgrnam("server-dashboard-prefs").gr_gid', str(prefs_gid)),
            )
            patched = original_live
            for old, new in replacements:
                self.assertEqual(patched.count(old), 1, f"No se encontró constante: {old}")
                patched = patched.replace(old, new, 1)
            runtime.write_text(patched, encoding="utf-8")
            os.chown(runtime, 0, www_gid)
            runtime.chmod(0o750)
            # The host's standard sbin directories are root-owned and
            # searchable; reproduce that metadata inside the root prefix.
            for parent in (prefix, prefix / "usr", prefix / "usr/local",
                           prefix / "usr/local/sbin", prefix / "etc",
                           prefix / "var", prefix / "var/lib",
                           prefix / "var/www"):
                if parent.exists() and parent.is_dir():
                    parent.chmod(0o755)
            os.chown(conf, 0, www_gid)
            conf.chmod(0o640)
            # This matches ExecStart with the unit's primary and supplementary
            # groups, but runs only against the isolated fixture.
            command = [
                shutil.which("setpriv"), f"--reuid={www.pw_uid}",
                f"--regid={www_gid}", f"--groups={prefs_gid}", sys.executable,
                str(runtime),
            ]
            process = subprocess.Popen(
                command, cwd=web, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            base = f"http://127.0.0.1:{port}"

            def request(path, method="GET", payload=None, host=None, extra_headers=None):
                body = None if payload is None else json.dumps(payload).encode("utf-8")
                headers = {}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                if host:
                    headers["Host"] = host
                if extra_headers:
                    headers.update(extra_headers)
                req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=2) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as error:
                    return error.code, error.read()

            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        stderr = process.stderr.read() if process.stderr else ""
                        self.fail(f"server-dashboard-live.py no arrancó como www-data: {stderr}")
                    try:
                        status, _ = request("/api/preferences")
                        if status == 200:
                            break
                    except (OSError, TimeoutError):
                        time.sleep(0.05)
                else:
                    self.fail("el servicio no respondió a tiempo como www-data")

                payload = {"type": "servers", "value": [{
                    "host": "192.0.2.8", "port": 8766,
                    "label": "Visual server entry", "secure": False,
                }]}
                status, body = request("/api/preferences", "POST", payload)
                self.assertEqual(status, 403)
                status, body = request(
                    "/api/preferences", "POST", payload,
                    host=f"127.0.0.1:{port}",
                    extra_headers={"X-SEP-Preferences": "1",
                                   "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", "replace"))
                ui = prefix / PREF_REL / "ui.json"
                saved = json.loads(ui.read_text(encoding="utf-8"))
                self.assertEqual(saved["servers"][0]["label"], "Visual server entry")
                self.assertEqual(ui.stat().st_uid, www.pw_uid)
                self.assertEqual(ui.stat().st_gid, prefs_gid)
                self.assertEqual(ui.stat().st_mode & 0o777, 0o660)
                self.assertEqual(list((prefix / PREF_REL).glob(".ui-*.tmp")), [])
                payload["value"][0]["label"] = "Updated visual server entry"
                status, body = request(
                    "/api/preferences", "POST", payload,
                    host=f"127.0.0.1:{port}",
                    extra_headers={"X-SEP-Preferences": "1",
                                   "Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(status, 200, body.decode("utf-8", "replace"))
                self.assertEqual(json.loads(ui.read_text(encoding="utf-8"))["servers"][0]["label"],
                                 "Updated visual server entry")
                self.assertEqual(list((prefix / PREF_REL).glob(".ui-*.tmp")), [])

                # A request from a different host cannot write preferences.
                status, _ = request("/api/preferences", "POST", payload,
                                    host="attacker.invalid",
                                    extra_headers={"X-SEP-Preferences": "1",
                                                   "Origin": "http://attacker.invalid"})
                self.assertEqual(status, 403)
                status, _ = request("/api/preferences", "POST", payload,
                                    host=f"127.0.0.1:{port}",
                                    extra_headers={"X-SEP-Preferences": "1",
                                                   "Origin": "http://attacker.invalid"})
                self.assertEqual(status, 403)
                # Privileged admin actions remain disabled over this HTTP service.
                action_req = urllib.request.Request(
                    base + "/api/docker-action", data=b'{"action":"status"}',
                    method="POST", headers={"Content-Type": "application/json",
                                             "X-Dashboard-Action": "1"},
                )
                try:
                    with urllib.request.urlopen(action_req, timeout=3) as response:
                        action_status = response.status
                except urllib.error.HTTPError as error:
                    action_status = error.code
                self.assertEqual(action_status, 403)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
