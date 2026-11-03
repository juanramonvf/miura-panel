#!/usr/bin/env python3
"""Fail closed unless a new tag can be published as an immutable release."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.github.com"


def request_json(url: str, token: str, opener=urllib.request.urlopen) -> tuple[int, dict | None]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "miura-panel-release-preflight",
    })
    try:
        with opener(request, timeout=20) as response:
            body = response.read(8 * 1024 * 1024)
            payload = json.loads(body.decode("utf-8"))
            return response.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, None


def check_github_release(repository: str, tag: str, immutable_token: str, release_token: str,
                         opener=urllib.request.urlopen) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("MIURA_RELEASE_TAG must be vMAJOR.MINOR.PATCH")
    if not immutable_token:
        raise ValueError("IMMUTABILITY_CHECK_TOKEN is required")
    if not release_token:
        raise ValueError("GITHUB_TOKEN is required to check existing releases and drafts")
    base = f"{API}/repos/{repository}"
    status, policy = request_json(base + "/immutable-releases", immutable_token, opener)
    if status != 200 or not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise RuntimeError("Release immutability is disabled or could not be verified")
    page = 1
    while page <= 10000:
        status, releases = request_json(base + f"/releases?per_page=100&page={page}", release_token, opener)
        if status != 200 or not isinstance(releases, list):
            raise RuntimeError(f"Could not list releases and drafts (API HTTP {status})")
        if any(isinstance(release, dict) and release.get("tag_name") == tag for release in releases):
            raise RuntimeError(f"A release or draft already exists for {tag}")
        if len(releases) < 100:
            return
        page += 1
    raise RuntimeError("Release listing exceeded the pagination safety limit")


def main() -> None:
    try:
        check_github_release(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("MIURA_RELEASE_TAG", ""),
            os.environ.get("IMMUTABILITY_CHECK_TOKEN", ""),
            os.environ.get("GITHUB_TOKEN", ""),
        )
    except Exception as exc:
        raise SystemExit(f"ERROR: release preflight failed closed: {exc}") from exc
    print("Release immutability enabled; no release or draft exists for this tag")


if __name__ == "__main__":
    main()
