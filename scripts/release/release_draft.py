#!/usr/bin/env python3
"""Verify or safely clean up a run-owned GitHub draft release."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"
API_VERSION = "2026-03-10"
WAIT_SECONDS = (1, 2, 4, 8, 8, 8)
EXPECTED_REPO = "juanramonvf/miura-panel"
EXPECTED_ASSETS = {
    "install-spanish-empire-panel-v{version}.sh",
    "install-spanish-empire-panel-v{version}.sh.sha256",
    "miura-panel-update-manifest.json",
    "miura-panel-update-manifest.json.sha256",
    "RELEASE_NOTES.md",
}


class ReleaseAPIError(RuntimeError):
    pass


class ReleaseClient:
    def __init__(self, repo: str, token: str,
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 sleeper: Callable[[float], None] = time.sleep):
        if repo != EXPECTED_REPO:
            raise ValueError("Release verification is restricted to the official repository")
        if not token:
            raise ValueError("GitHub Actions token is missing")
        self.repo = repo
        self.token = token
        self.opener = opener
        self.sleeper = sleeper

    def request(self, path: str, method: str = "GET") -> tuple[int, Any]:
        request = urllib.request.Request(
            f"{API}/repos/{self.repo}/{path.lstrip('/')}",
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "miura-panel-release-workflow",
            },
        )
        try:
            with self.opener(request, timeout=20) as response:
                body = response.read(8 * 1024 * 1024)
                if not body:
                    return response.status, None
                try:
                    return response.status, json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ReleaseAPIError("GitHub API returned invalid JSON") from None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, None
            if exc.code in (401, 403):
                raise ReleaseAPIError(
                    f"GitHub API rejected the Actions token (HTTP {exc.code}); "
                    "verify repository access and Contents read/write permissions"
                ) from None
            raise ReleaseAPIError(f"GitHub API request failed (HTTP {exc.code})") from None
        except (OSError, TimeoutError) as exc:
            raise ReleaseAPIError(f"GitHub API request failed ({type(exc).__name__})") from None

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        page = 1
        while page <= 10000:
            status, releases = self.request(f"releases?per_page=100&page={page}")
            if status != 200 or not isinstance(releases, list):
                raise ReleaseAPIError(f"GitHub API release listing was invalid (HTTP {status})")
            match = next((item for item in releases
                          if isinstance(item, dict) and item.get("tag_name") == tag), None)
            if match is not None or len(releases) < 100:
                return match
            page += 1
        raise ReleaseAPIError("Release listing exceeded the pagination safety limit")

    def release_by_tag_direct(self, tag: str) -> dict[str, Any] | None:
        status, release = self.request(f"releases/tags/{tag}")
        if status == 404:
            return None
        if status != 200 or not isinstance(release, dict):
            raise ReleaseAPIError(f"GitHub API tag lookup was invalid (HTTP {status})")
        return release

    def find_tag_with_retry(self, tag: str) -> dict[str, Any] | None:
        for attempt, delay in enumerate(WAIT_SECONDS):
            # The tag endpoint avoids release-list pagination and can become
            # consistent before the collection endpoint. Keep the list lookup
            # as a fallback and enforce the same exact tag check either way.
            release = self.release_by_tag_direct(tag)
            if release is None:
                release = self.release_by_tag(tag)
            if release is not None:
                return release
            if attempt + 1 < len(WAIT_SECONDS):
                self.sleeper(delay)
        return None

    def release_by_id(self, release_id: int) -> dict[str, Any] | None:
        status, release = self.request(f"releases/{release_id}")
        if status == 404:
            return None
        if status != 200 or not isinstance(release, dict):
            raise ReleaseAPIError(f"GitHub API release lookup was invalid (HTTP {status})")
        return release


def run_context(env: dict[str, str] | os._Environ[str]) -> tuple[ReleaseClient, str, str, str]:
    repo = env.get("GH_REPO", "")
    if env.get("GITHUB_REPOSITORY", "") != repo:
        raise ValueError("Workflow repository does not match the configured official repository")
    tag = env.get("MIURA_RELEASE_TAG", "")
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("Release tag is not stable semantic versioning")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not run_id.isdigit():
        raise ValueError("GitHub Actions run ID is invalid")
    client = ReleaseClient(repo, env.get("GH_TOKEN", ""))
    return client, tag, run_id, f"<!-- miura-release-run:{run_id} -->"


def verify_draft(client: ReleaseClient, tag: str, run_id: str,
                 marker: str, asset_dir: pathlib.Path) -> None:
    release = client.find_tag_with_retry(tag)
    if release is None:
        raise ReleaseAPIError(
            "Draft was not visible after bounded authenticated retries "
            f"in {client.repo} (run {run_id})"
        )
    if release.get("draft") is not True:
        raise ReleaseAPIError("Expected an unpublished draft release")
    if release.get("tag_name") != tag or marker not in release.get("body", ""):
        raise ReleaseAPIError("Draft release is not owned by this workflow run")
    version = tag[1:]
    expected = {item.format(version=version) for item in EXPECTED_ASSETS}
    assets = release.get("assets")
    if not isinstance(assets, list) or {asset.get("name") for asset in assets if isinstance(asset, dict)} != expected:
        raise ReleaseAPIError("Draft assets do not exactly match the approved package file set")
    for asset in assets:
        name = asset["name"]
        path = asset_dir / name
        if (not path.is_file() or asset.get("state") != "uploaded"
                or asset.get("size") != path.stat().st_size):
            raise ReleaseAPIError(f"Uploaded asset size/state mismatch: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if asset.get("digest") != f"sha256:{digest}":
            raise ReleaseAPIError(f"Uploaded asset digest mismatch: {name}")
    print("Draft owner, asset names, upload state, sizes, and SHA256 digests verified")


def cleanup_draft(client: ReleaseClient, tag: str, marker: str,
                  creation_succeeded: bool) -> None:
    release = client.find_tag_with_retry(tag)
    if release is None:
        if creation_succeeded:
            raise ReleaseAPIError(
                "Draft creation succeeded but absence could not be confirmed; "
                "cleanup state is unverified"
            )
        print("No draft for this tag is visible after authenticated retries; nothing to remove")
        return
    if release.get("tag_name") != tag or release.get("draft") is not True or marker not in release.get("body", ""):
        raise ReleaseAPIError("Refusing to remove a release not owned by this run and still unpublished")
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise ReleaseAPIError("Run-owned draft has an invalid release ID")

    # Re-read the exact release immediately before deletion. Never delete a
    # release that has become published or whose run marker changed.
    current = client.release_by_id(release_id)
    if current is None:
        print("Run-owned draft was already absent")
        return
    if (current.get("tag_name") != tag or current.get("draft") is not True
            or marker not in current.get("body", "")):
        raise ReleaseAPIError("Refusing to remove a release that is no longer this run's unpublished draft")
    status, _ = client.request(f"releases/{release_id}", method="DELETE")
    if status not in (204, 404):
        raise ReleaseAPIError(f"GitHub API did not remove the run-owned draft (HTTP {status})")

    for attempt, delay in enumerate(WAIT_SECONDS):
        remaining = client.release_by_id(release_id)
        if remaining is None:
            print("Confirmed removal of this run-owned draft; the Git tag was preserved")
            return
        if (remaining.get("tag_name") != tag or remaining.get("draft") is not True
                or marker not in remaining.get("body", "")):
            raise ReleaseAPIError("Release changed during cleanup; leaving it untouched")
        if attempt + 1 < len(WAIT_SECONDS):
            client.sleeper(delay)
            status, _ = client.request(f"releases/{release_id}", method="DELETE")
            if status not in (204, 404):
                raise ReleaseAPIError(f"GitHub API did not remove the run-owned draft (HTTP {status})")
    raise ReleaseAPIError("Could not confirm removal of the run-owned unpublished draft")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["verify"], ["cleanup"]):
        print("Usage: release_draft.py verify|cleanup", file=sys.stderr)
        return 2
    try:
        client, tag, run_id, marker = run_context(os.environ)
        if args == ["verify"]:
            verify_draft(client, tag, run_id, marker, pathlib.Path("release-assets"))
        else:
            cleanup_draft(
                client, tag, marker,
                creation_succeeded=os.environ.get("CREATE_DRAFT_OUTCOME") == "success",
            )
    except (ReleaseAPIError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
