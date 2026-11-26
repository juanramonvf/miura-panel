"""Regression checks for panel version reloads and live metric repainting."""

import json
import pathlib
import re
import subprocess
import unittest
from html.parser import HTMLParser


ROOT = pathlib.Path(__file__).resolve().parents[1]


def git_file(revision, path):
    return subprocess.run(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout


class ScriptExtractor(HTMLParser):
    """Collect script contents using HTML's case-insensitive tag parsing."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current_script = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current_script = []

    def handle_data(self, data):
        if self.current_script is not None:
            self.current_script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current_script is not None:
            self.scripts.append("".join(self.current_script))
            self.current_script = None


def reload_script(html):
    parser = ScriptExtractor()
    parser.feed(html)
    parser.close()
    for body in parser.scripts:
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
    def test_update_offer_never_downgrades_installed_semver(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        start = html.index("async function checkPanelVersion(){")
        end = html.index("\n}", start) + 2
        harness = r"""
const vm = require('node:vm');
const btn = { hidden: true, disabled: false, textContent: '', title: '', classList: { add(){}, remove(){} }, onclick: null };
const document = { getElementById: id => id === 'sepCopyUpdate' ? btn : null };
const installed = process.argv[1], offered = process.argv[2];
const context = {
  fetch: async url => url.startsWith('/version.json')
    ? { ok: true, json: async () => ({version: installed}) }
    : { ok: true, json: async () => ({status:'ok',update_available:true,latest:offered}) },
  document, location: {protocol:'https:'}, sepAuthenticated: true, window: {},
};
vm.runInNewContext('('+process.argv[3]+')()', context);
setImmediate(()=>process.stdout.write(JSON.stringify({hidden:btn.hidden,disabled:btn.disabled,text:btn.textContent})));
"""
        for installed, offered, should_offer in (
            ("1.0.40", "1.0.39", False), ("1.0.40", "1.0.40", False),
            ("1.0.40", "1.0.41", True), ("1.0.9", "1.0.10", True),
            ("1.0.40", "1.00.41", False),
        ):
            result = subprocess.run(
                ["node", "-e", harness, installed, offered, html[start:end]], cwd=ROOT,
                check=True, capture_output=True, text=True, timeout=5,
            )
            state = json.loads(result.stdout)
            self.assertEqual(not state["hidden"] and not state["disabled"], should_offer, state)

    def test_https_session_indicator_uses_readable_admin_status(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        start = html.index("fetch('admin-status.json?")
        end = html.index("fetch('fleet-role.json?", start)
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const link = { hidden: true };
const requests = [];
const context = {
  $: selector => { assert.equal(selector, '#sepAdminLink'); return link; },
  location: { protocol: 'https:', hostname: 'panel.example.test', port: '8767' },
  fetch: async url => {
    requests.push(url);
    const data = url.startsWith('admin-status.json?') ? { enabled: true }
      : { authenticated: true, account: 'owner@example.test' };
    return { ok: true, json: async () => data };
  },
  document: { body: { classList: { remove() {} } } },
  sepAuthenticated: false,
  sepRenderServers() {},
  checkPanelVersion() {},
};
vm.runInNewContext(process.argv[1], context);
setImmediate(() => {
  assert.ok(requests[0].startsWith('admin-status.json?'));
  assert.equal(requests[1], '/api/admin/status');
  assert.equal(link.hidden, false);
  assert.equal(link.textContent, '👤 Sesión: owner@example.test');
  assert.equal(link.href, 'https://panel.example.test:8767/configuracion');
  assert.equal(context.sepAuthenticated, true);
});
"""
        subprocess.run(["node", "-e", harness, html[start:end]], cwd=ROOT, check=True,
                       capture_output=True, text=True, timeout=5)

    def test_script_extractor_handles_case_and_attributes(self):
        html = '<SCRIPT type="text/javascript">const installedAtLoad=\'x\';</SCRIPT>'
        self.assertIn("const installedAtLoad=", reload_script(html))

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
