"""Regression checks for panel version reloads and live metric repainting."""

import json
import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def git_file(revision, path):
    return subprocess.run(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout


def reload_script(html):
    for body in re.findall(r"<script>(.*?)</script>", html, re.DOTALL):
        if "const installedAtLoad=" in body:
            return body
    raise AssertionError("No se encontró el comprobador de versión del frontend")


def run_reload_check(html, served_version):
    script = reload_script(html)
    harness = r"""
const vm = require('node:vm');
let reloads = 0;
let interval;
const context = {
  AbortSignal: { timeout: () => ({}) },
  fetch: async () => ({ ok: true, json: async () => ({ version: SERVED_VERSION }) }),
  location: { reload: () => { reloads += 1; } },
  window: {},
  sessionStorage: { getItem: () => null, removeItem: () => {}, setItem: () => {} },
  setInterval: fn => { interval = fn; },
  setTimeout: () => 0,
  clearTimeout: () => {},
};
vm.runInNewContext(SCRIPT, context);
(async () => { await interval(); process.stdout.write(String(reloads)); })();
""".replace("SERV ED_VERSION", "SERVED_VERSION")
    # JSON serialization keeps source/version data literal inside the harness.
    harness = harness.replace("SCRIPT", json.dumps(script)).replace(
        "SERVED_VERSION", json.dumps(served_version)
    )
    result = subprocess.run(
        ["node", "-e", harness], cwd=ROOT, check=True,
        capture_output=True, text=True, timeout=5,
    )
    return int(result.stdout)


class FrontendRefreshTests(unittest.TestCase):
    def test_historical_version_watcher_characterizes_v1034_v1036_v1037(self):
        # v1.0.34's baked-in version matches the endpoint, but .36 and .37
        # retained 1.0.35 and reload every time the page is loaded.
        cases = (
            ("db917c3", "1.0.34", 0),
            ("v1.0.36", "1.0.36", 1),
            ("v1.0.37", "1.0.37", 1),
        )
        for revision, served, expected_reloads in cases:
            with self.subTest(revision=revision):
                self.assertEqual(
                    run_reload_check(git_file(revision, "index.html"), served),
                    expected_reloads,
                )

    def test_candidate_watcher_matches_endpoint_and_does_not_reload(self):
        version = json.loads((ROOT / "version.json").read_text())["version"]
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        baked_version = re.search(r"const installedAtLoad='([^']+)'", reload_script(html))
        self.assertIsNotNone(baked_version)
        self.assertEqual(baked_version.group(1), version)
        self.assertEqual(run_reload_check(html, version), 0)

    def test_live_render_updates_nodes_and_skips_unchanged_disk_and_chart_markup(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        render = re.search(r"function renderLive\(l\)\{(.*?)\n\}\nasync function loadDockerVersions", html, re.S)
        self.assertIsNotNone(render)
        body = render.group(1)
        self.assertNotIn("$('#metrics').innerHTML", body)
        self.assertIn("ensureLiveMetricCards()", body)
        self.assertIn("setHtmlIfChanged($('#disks'),diskMarkup)", body)
        self.assertIn("setHtmlIfChanged($('#sepUnmountedDisks'),hiddenMarkup)", body)
        self.assertIn("const chartValuesChanged=!last||chartFields.some(key=>last[key]!==l[key])", body)
        self.assertIn("chartRefreshDue", body)
        self.assertLess(body.index("liveHistory.push(l)"), body.index("if(chartValuesChanged||chartRefreshDue)"))

    def test_rendering_same_live_payload_twice_causes_no_second_dom_write(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        source = re.search(
            r"(function ensureLiveMetricCards\(\)\{.*?\n\})\nasync function loadDockerVersions",
            html, re.S,
        )
        self.assertIsNotNone(source)
        harness = r"""
const vm = require('node:vm');
const stats = { writes: 0, charts: 0 };
class Element {
  constructor() { this.dataset = {}; this.style = new Proxy({}, {set: (obj,key,value) => { if (obj[key] !== value) stats.writes++; obj[key]=value; return true; }}); this.children = new Map(); this.__html = ''; this.__text = ''; this.__class = ''; }
  set innerHTML(value) { stats.writes++; this.__html = value; }
  get innerHTML() { return this.__html; }
  set textContent(value) { if (this.__text !== String(value)) { stats.writes++; this.__text = String(value); } }
  get textContent() { return this.__text; }
  set className(value) { if (this.__class !== value) { stats.writes++; this.__class = value; } }
  get className() { return this.__class; }
  querySelector(selector) { if (!this.children.has(selector)) this.children.set(selector, new Element()); return this.children.get(selector); }
  querySelectorAll() { return []; }
}
const roots = new Map();
const get = selector => { if (!roots.has(selector)) roots.set(selector, new Element()); return roots.get(selector); };
const context = {
  $: get, slowData: {}, liveHistory: [], MAX_POINTS: 300,
  CHART_REFRESH_EVERY: 30, liveSampleCount: 0, lastChartRenderSample: 0, stats,
  hb: value => String(value), pctState: () => 'ok', tempState: () => 'ok',
  formatUptime: () => '1 h 0 min', esc: value => String(value),
  baseDiskDevice: value => value, sepDiskName: value => value, sepDiskKey: value => value,
  bindLiveDiskActions: () => {}, svgChart: () => { stats.charts++; stats.writes++; },
};
context.sample = SAMPLE;
vm.runInNewContext(SOURCE + '\nrenderLive(sample); const first={...stats}; const n1=liveHistory.length; renderLive(sample); const second={...stats}; liveHistory=Array.from({length:300},()=>sample); liveHistory[2]={...sample,cpu:8}; renderLive(sample); const fullIdentical={...stats}; for(let i=0;i<28;i++)renderLive(sample); result={first,second,fullIdentical,windowCharts:stats.charts,windowHistory:liveHistory.length,history:n1+1,n1};', context);
process.stdout.write(JSON.stringify(context.result));
""".replace("SOURCE", json.dumps(source.group(1))).replace("SAMPLE", json.dumps({
            "cpu": 9, "ram": 21, "ram_used": 210, "ram_total": 1000,
            "cpu_temp": 42, "nvme_temp": 39, "load1": 0.1, "load5": 0.2,
            "load15": 0.3, "uptime_seconds": 3600, "swap_total": 0,
            "swap_used": 0, "swap_percent": 0, "rx_mbps": 1.1,
            "tx_mbps": 0.5, "iface": "eth0", "disks": [],
        }))
        result = subprocess.run(
            ["node", "-e", harness], cwd=ROOT, check=False,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(result.stdout)
        self.assertEqual(stats["second"]["writes"], stats["first"]["writes"])
        self.assertEqual(stats["second"]["charts"], stats["first"]["charts"])
        self.assertEqual(stats["history"], stats["n1"] + 1)
        self.assertEqual(stats["fullIdentical"]["charts"], stats["second"]["charts"])
        self.assertEqual(stats["windowCharts"], stats["fullIdentical"]["charts"] + 3)
        self.assertEqual(stats["windowHistory"], 300)


if __name__ == "__main__":
    unittest.main()
