#!/usr/bin/env python3
"""Verify an existing immutable release and its complete asset set for Pages recovery."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPOSITORY = "juanramonvf/miura-panel"
MAX_INSTALLER = 2 * 1024 * 1024
MAX_MANIFEST = 64 * 1024
MAX_NOTES = 2 * 1024 * 1024


def read_url(url: str, token: str, limit: int, *, api: bool,
             opener=urllib.request.urlopen) -> tuple[int, bytes]:
    headers = {"User-Agent": "miura-pages-recovery", "Accept": "application/vnd.github+json"}
    if api:
        headers["X-GitHub-Api-Version"] = "2026-03-10"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener(request, timeout=30) as response:
            return response.status, response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def verify_published_release(repository: str, tag: str, token: str,
                             opener=urllib.request.urlopen) -> dict[str, str]:
    if repository != REPOSITORY:
        raise ValueError("Pages recovery is restricted to juanramonvf/miura-panel")
    match = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag)
    if not match:
        raise ValueError("release tag must be stable semver vMAJOR.MINOR.PATCH")
    version = tag[1:]
    api_url = f"{API}/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    status, body = read_url(api_url, token, 4 * 1024 * 1024, api=True, opener=opener)
    if status != 200:
        raise RuntimeError(f"published release lookup failed (HTTP {status})")
    try:
        release = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release API returned invalid JSON") from exc
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise RuntimeError("release API response tag does not match the requested tag")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise RuntimeError("release must be published and stable")
    if release.get("immutable") is not True:
        raise RuntimeError("release is not confirmed immutable")

    installer_name = f"install-spanish-empire-panel-v{version}.sh"
    expected_names = {
        installer_name,
        installer_name + ".sha256",
        "miura-panel-update-manifest.json",
        "miura-panel-update-manifest.json.sha256",
        "RELEASE_NOTES.md",
    }
    assets = release.get("assets")
    if (not isinstance(assets, list) or len(assets) != len(expected_names) or
            {a.get("name") for a in assets if isinstance(a, dict)} != expected_names):
        raise RuntimeError("release asset names do not match the approved five-file package")

    limits = {
        installer_name: MAX_INSTALLER,
        installer_name + ".sha256": 1024,
        "miura-panel-update-manifest.json": MAX_MANIFEST,
        "miura-panel-update-manifest.json.sha256": 1024,
        "RELEASE_NOTES.md": MAX_NOTES,
    }
    raw_assets: dict[str, bytes] = {}
    digests: dict[str, str] = {}
    prefix = f"https://github.com/{repository}/releases/download/{tag}/"
    for asset in assets:
        name = asset.get("name")
        if asset.get("state") != "uploaded":
            raise RuntimeError(f"release asset is not fully uploaded: {name}")
        size = asset.get("size")
        if not isinstance(size, int) or size < 0 or size > limits[name]:
            raise RuntimeError(f"release asset has invalid or excessive size: {name}")
        expected_digest = asset.get("digest")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest):
            raise RuntimeError(f"GitHub API omitted a valid SHA256 digest for {name}")
        url = asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith(prefix):
            raise RuntimeError(f"release asset URL is outside the expected GitHub release: {name}")
        status, raw = read_url(url, "", limits[name], api=False, opener=opener)
        if status != 200 or len(raw) > limits[name] or len(raw) != size:
            raise RuntimeError(f"downloaded release asset status/size mismatch: {name}")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_digest != "sha256:" + digest:
            raise RuntimeError(f"downloaded release asset does not match GitHub API SHA256: {name}")
        raw_assets[name] = raw
        digests[name] = digest

    installer_sha = digests[installer_name]
    manifest_name = "miura-panel-update-manifest.json"
    try:
        manifest = json.loads(raw_assets[manifest_name].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release update manifest is invalid JSON") from exc
    installer_asset = next(a for a in assets if a.get("name") == installer_name)
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "installer", "sha256"} or
            manifest.get("version") != version or
            manifest.get("installer") != installer_asset.get("browser_download_url") or
            manifest.get("sha256") != installer_sha):
        raise RuntimeError("release manifest does not approve the published installer bytes")

    expected_sidecars = {
        installer_name + ".sha256": f"{installer_sha}  {installer_name}",
        manifest_name + ".sha256": f"{digests[manifest_name]}  {manifest_name}",
    }
    for name, expected in expected_sidecars.items():
        if raw_assets[name].decode("ascii", errors="replace").strip() != expected:
            raise RuntimeError(f"release SHA256 sidecar does not match asset bytes: {name}")

    return digests


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_published_release.py REPOSITORY TAG")
    try:
        digests = verify_published_release(
            sys.argv[1], sys.argv[2], os.environ.get("GITHUB_TOKEN", "")
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: Pages recovery refused: {exc}") from exc
    for name, digest in sorted(digests.items()):
        print(f"Verified immutable release asset {name}: sha256:{digest}")


if __name__ == "__main__":
    main()
