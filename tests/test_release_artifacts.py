"""Offline release gates for Miura Panel's self-contained installer."""

import base64
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
EXPECTED_PAYLOAD = {
    "version.json", "index.html", "admin.html", "admin-login.html",
    "admin-reset.html", "sep-logo.png", "collect.py", "live.py",
    "admin-web.py", "admin-setup.py", "admin-fleet.py", "fleet.py",
    "fleet-docker.py", "fleet-receiver.py", "docker-action.py",
    "docker-updates.py", "update.py",
}
# This is the v1.0.34 stable main commit, kept fixed so future main updates
# cannot silently stop testing compatibility with already-installed panels.
V1034_STABLE_REF = "b262303b7c891a667eb6ade550d623ab94558ea1"


class ReleaseArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version = json.loads((ROOT / "version.json").read_text())["version"]
        cls.installer = pathlib.Path(os.environ.get(
            "MIURA_INSTALLER_PATH",
            DIST / f"install-spanish-empire-panel-v{cls.version}.sh",
        )).resolve()
        cls.installer_sha = pathlib.Path(str(cls.installer) + ".sha256")
        cls.manifest = pathlib.Path(os.environ.get(
            "MIURA_MANIFEST_PATH",
            DIST / "miura-panel-update-manifest.example.json",
        )).resolve()
        cls.manifest_sha = pathlib.Path(str(cls.manifest) + ".sha256")
        cls.check_example_manifest = "MIURA_MANIFEST_PATH" not in os.environ
        for path in (cls.installer, cls.installer_sha, cls.manifest, cls.manifest_sha):
            if not path.is_file():
                raise AssertionError(f"Falta artefacto: {path.relative_to(ROOT)}")

    def run_installer(self, root_prefix, *args, expect_success=True, env=None):
        process_env = os.environ.copy()
        process_env["MIURA_ROOT_PREFIX"] = str(root_prefix)
        if env:
            process_env.update(env)
        result = subprocess.run(
            ["bash", str(self.installer), *args],
            cwd=ROOT,
            env=process_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if expect_success and result.returncode != 0:
            self.fail(f"{args} falló ({result.returncode}): {result.stdout}\n{result.stderr}")
        if not expect_success and result.returncode == 0:
            self.fail(f"{args} debía fallar, pero terminó correctamente")
        return result

    def test_sha256_and_v1034_manifest_contract(self):
        for checksum_file in (self.installer_sha, self.manifest_sha):
            result = subprocess.run(
                ["sha256sum", "-c", checksum_file.name],
                cwd=checksum_file.parent,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        digest = hashlib.sha256(self.installer.read_bytes()).hexdigest()
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        # v1.0.34 update.py consumes exactly this three-key contract.
        self.assertEqual(set(manifest), {"version", "installer", "sha256"})
        self.assertEqual(manifest["version"], self.version)
        self.assertEqual(manifest["sha256"], digest)
        self.assertRegex(manifest["installer"], r"^https://")
        self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")
        if self.check_example_manifest:
            # The checked-in sample is branch-specific. Release builds pass
            # MIURA_MANIFEST_PATH and use the generated, version-specific feed.
            self.assertIn("development/v1.0.35-candidate", manifest["installer"])

        # Exercise the actual updater shipped in stable v1.0.34, including
        # its config parser, while replacing the only network read.
        old_updater_source = subprocess.check_output(
            ["git", "show", f"{V1034_STABLE_REF}:update.py"], cwd=ROOT, text=True
        )
        with tempfile.TemporaryDirectory(prefix="miura-v1034-manifest-") as temp:
            temp_path = pathlib.Path(temp)
            updater_path = temp_path / "old_update.py"
            updater_path.write_text(old_updater_source, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("miura_v1034_update", updater_path)
            self.assertIsNotNone(spec)
            old_update = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(old_update)

            feed_url = "https://updates.invalid/miura-panel-update-manifest.json"
            config = temp_path / "server-dashboard.conf"
            config.write_text(f"DASHBOARD_UPDATE_MANIFEST={feed_url}\n", encoding="utf-8")
            old_update.CONFIG = config
            manifest_bytes = self.manifest.read_bytes()
            requested_urls = []

            def offline_get_limited(url, limit):
                requested_urls.append(url)
                self.assertEqual(url, feed_url)
                self.assertLessEqual(len(manifest_bytes), limit)
                return manifest_bytes

            old_update.get_limited = offline_get_limited
            self.assertEqual(
                old_update.fetch_manifest(),
                (self.version, manifest["installer"], digest),
            )
            self.assertEqual(requested_urls, [feed_url])

        old_updater = subprocess.check_output(
            ["git", "show", f"{V1034_STABLE_REF}:update.py"], cwd=ROOT, text=True
        )
        self.assertIn("MAX_INSTALLER = 2 * 1024 * 1024", old_updater)
        self.assertLess(self.installer.stat().st_size, 2 * 1024 * 1024)

    def test_payload_archive_is_complete_safe_and_matches_sources(self):
        script = self.installer.read_bytes()
        result = subprocess.run(["bash", "-n", str(self.installer)], check=False)
        self.assertEqual(result.returncode, 0)

        marker = b"__MIURA_PANEL_SOURCE_ARCHIVE_BELOW__\n"
        self.assertEqual(script.count(marker), 1)
        encoded_lines = script.split(marker, 1)[1].strip().splitlines()
        self.assertTrue(encoded_lines)
        self.assertTrue(all(re.fullmatch(rb"[A-Za-z0-9+/=]+", line) for line in encoded_lines))
        encoded = b"".join(encoded_lines)
        payload = base64.b64decode(encoded, validate=True)
        with tarfile.open(fileobj=BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            files = [member for member in members if member.isfile()]
            dirs = [member for member in members if member.isdir()]
            self.assertEqual({pathlib.PurePosixPath(m.name).name for m in files}, EXPECTED_PAYLOAD)
            self.assertEqual(len(files), len(EXPECTED_PAYLOAD))
            self.assertEqual([m.name for m in dirs], ["source"])
            self.assertEqual(len(members), len(EXPECTED_PAYLOAD) + 1)
            for member in files:
                name = pathlib.PurePosixPath(member.name)
                self.assertEqual(name.parts[0], "source")
                self.assertEqual(len(name.parts), 2)
                self.assertNotIn("..", name.parts)
                self.assertLessEqual(member.size, 8 * 1024 * 1024)
                self.assertEqual(archive.extractfile(member).read(), (ROOT / name.name).read_bytes())

    def test_demo_mode_survives_v1034_upgrade_and_manual_rollback(self):
        with tempfile.TemporaryDirectory(prefix="miura-release-demo-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.run_installer(prefix, "--demo")
            conf = prefix / "etc/server-dashboard.conf"
            web = prefix / "var/www/server-dashboard"
            config = conf.read_text(encoding="utf-8")
            self.assertIn("DASHBOARD_MODE=demo", config)
            self.assertIn("DASHBOARD_BIND_IP=127.0.0.1", config)

            # Model the persistent configuration and installed version on v1.0.34.
            old_config = (
                "DASHBOARD_MODE=demo\n"
                "DASHBOARD_BIND_IP=0.0.0.0\n"
                "DASHBOARD_PORT=8891\n"
                "DASHBOARD_TITLE=Keep this title\n"
            )
            conf.write_text(old_config, encoding="utf-8")
            (web / "version.json").write_text('{"version":"1.0.34"}\n', encoding="utf-8")
            (web / "v1034-sentinel.txt").write_text("restore me\n", encoding="utf-8")

            self.run_installer(prefix, "--upgrade")
            upgraded = conf.read_text(encoding="utf-8")
            self.assertIn("DASHBOARD_MODE=demo", upgraded)
            self.assertIn("DASHBOARD_BIND_IP=127.0.0.1", upgraded)
            self.assertIn("DASHBOARD_PORT=8891", upgraded)
            self.assertIn("DASHBOARD_TITLE=Keep this title", upgraded)
            self.assertEqual(json.loads((web / "version.json").read_text())["version"], self.version)

            self.run_installer(prefix, "--rollback")
            self.assertEqual(json.loads((web / "version.json").read_text())["version"], "1.0.34")
            self.assertEqual((web / "v1034-sentinel.txt").read_text(), "restore me\n")
            self.assertIn("DASHBOARD_MODE=demo", conf.read_text(encoding="utf-8"))

    def test_failed_upgrade_rolls_back_demo_config_and_web_tree(self):
        with tempfile.TemporaryDirectory(prefix="miura-release-failure-") as temp:
            prefix = pathlib.Path(temp) / "root"
            self.run_installer(prefix, "--demo")
            conf = prefix / "etc/server-dashboard.conf"
            web = prefix / "var/www/server-dashboard"
            old_config = (
                "DASHBOARD_MODE=demo\n"
                "DASHBOARD_BIND_IP=0.0.0.0\n"
                "DASHBOARD_BIND_IP=127.0.0.1\n"
                "DASHBOARD_PORT=8877\n"
            )
            conf.write_text(old_config, encoding="utf-8")
            (web / "version.json").write_text('{"version":"1.0.34"}\n', encoding="utf-8")
            (web / "rollback-sentinel.txt").write_text("old tree\n", encoding="utf-8")

            self.run_installer(prefix, "--upgrade", expect_success=False)
            self.assertEqual(conf.read_text(encoding="utf-8"), old_config)
            self.assertEqual(json.loads((web / "version.json").read_text())["version"], "1.0.34")
            self.assertEqual((web / "rollback-sentinel.txt").read_text(), "old tree\n")

    def test_installed_demo_runtime_serves_fake_data_and_rejects_actions(self):
        with tempfile.TemporaryDirectory(prefix="miura-demo-http-") as temp:
            temp_path = pathlib.Path(temp)
            prefix = temp_path / "root"
            self.run_installer(prefix, "--demo")
            installed = prefix / "usr/local/sbin/server-dashboard-live.py"
            config = prefix / "etc/server-dashboard.conf"
            web = prefix / "var/www/server-dashboard"
            (web / "data.json").write_text('{"hostname":"SERVER-SECRET"}\n', encoding="utf-8")

            # Redirect only the installed runtime's absolute paths to this fixture.
            config_source = str(config)
            web_source = str(web)
            source = installed.read_text(encoding="utf-8")
            source = source.replace(
                'CONFIG_FILE = Path("/etc/server-dashboard.conf")',
                f"CONFIG_FILE = Path({config_source!r})",
                1,
            )
            source = source.replace(
                'WEB_DIR = Path("/var/www/server-dashboard")',
                f"WEB_DIR = Path({web_source!r})",
                1,
            )
            runtime = temp_path / "installed_live_runtime.py"
            runtime.write_text(source, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("miura_installed_demo_runtime", runtime)
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)

            class PsutilForbidden(types.ModuleType):
                def __getattr__(self, name):
                    raise AssertionError(f"demo runtime unexpectedly accessed psutil.{name}")

            # The demo route must not need host metrics or a real psutil install.
            with patch.dict(sys.modules, {"psutil": PsutilForbidden("psutil")}):
                spec.loader.exec_module(module)
            self.assertEqual(module.MODE, "demo")
            self.assertEqual(module.BIND_IP, "127.0.0.1")

            server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def get_json(path):
                with urllib.request.urlopen(base + path, timeout=3) as response:
                    return response.status, json.loads(response.read())

            def post_status(path):
                request = urllib.request.Request(
                    base + path,
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Dashboard-Action": "1",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return response.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            try:
                status, mode = get_json("/api/mode")
                self.assertEqual((status, mode["mode"]), (200, "demo"))
                status, data = get_json("/data.json")
                self.assertEqual(status, 200)
                self.assertEqual(data["hostname"], "miura-demo")
                self.assertNotIn("SERVER-SECRET", json.dumps(data))
                status, live = get_json("/api/live")
                self.assertEqual(status, 200)
                self.assertEqual(live["disks"][0]["device"], "/dev/demo0")
                self.assertEqual(post_status("/api/preferences"), 403)
                self.assertEqual(post_status("/api/docker-action"), 403)

                admin_request = urllib.request.Request(base + "/admin.html")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(admin_request, timeout=3)
                self.assertEqual(error.exception.code, 404)

                module.MODE = "normal"
                module.ADMIN_MODE = False
                self.assertEqual(post_status("/api/docker-action"), 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
