"""Tests that new installs and fleet checks use the canonical Pages feed."""
import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES_FEED = "https://juanramonvf.github.io/miura-panel/updates/stable.json"


class LegacyFeedDefaultsTests(unittest.TestCase):
    def test_fleet_checker_uses_pages_feed(self):
        spec = importlib.util.spec_from_file_location("miura_fleet", ROOT / "fleet.py")
        fleet = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fleet)
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"version":"1.0.42"}'
        with patch("pathlib.Path.read_text", return_value='{"version":"1.0.41"}'), \
             patch("urllib.request.urlopen", return_value=Response()) as open_url:
            result = fleet.local_check()
        self.assertEqual(open_url.call_args.args[0], PAGES_FEED)
        self.assertEqual(result["latest"], "1.0.42")

    def test_new_install_defaults_updater_to_pages_but_upgrade_keeps_existing_url(self):
        installer = (ROOT / "install-spanish-empire-panel.sh").read_text(encoding="utf-8")
        self.assertIn("DASHBOARD_UPDATE_MANIFEST=https://juanramonvf.github.io/miura-panel/updates/stable.json", installer)
        spec = importlib.util.spec_from_file_location("miura_update", ROOT / "update.py")
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "server-dashboard.conf"
            updater.CONFIG = config
            self.assertEqual(updater.cfg_get("DASHBOARD_UPDATE_MANIFEST"), PAGES_FEED)
            config.write_text("DASHBOARD_UPDATE_MANIFEST=\n")
            self.assertEqual(updater.cfg_get("DASHBOARD_UPDATE_MANIFEST"), PAGES_FEED)
            config.write_text("DASHBOARD_UPDATE_MANIFEST=https://sep.mundopeke.es/manifest.json\n")
            self.assertEqual(updater.cfg_get("DASHBOARD_UPDATE_MANIFEST"), "https://sep.mundopeke.es/manifest.json")

    def test_configured_legacy_feed_is_not_rewritten_by_default(self):
        installer = (ROOT / "install-spanish-empire-panel.sh").read_text(encoding="utf-8")
        start = installer.index('if [[ ! -f "$CONF" ]]; then')
        end = installer.index('if ((UPGRADE)); then', start)
        first_install_config = installer[start:end]
        self.assertIn("DASHBOARD_UPDATE_MANIFEST=" + PAGES_FEED, first_install_config)
        self.assertEqual(installer.count("DASHBOARD_UPDATE_MANIFEST="), 1)


if __name__ == "__main__":
    unittest.main()
