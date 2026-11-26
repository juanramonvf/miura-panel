#!/usr/bin/env python3
"""Fail-closed validation for the privileged Pages workflow_run trigger."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

REPOSITORY = "juanramonvf/miura-panel"
WORKFLOW_PATH = ".github/workflows/release.yml"
API = "https://api.github.com"


def same_run(observed: dict, expected: dict) -> bool:
    fields = ("id", "name", "workflow_id", "event", "conclusion", "head_branch", "head_sha")
    if any(observed.get(k) != expected.get(k) for k in fields):
        return False
    for field in ("repository", "head_repository"):
        if (observed.get(field) or {}).get("full_name") != REPOSITORY:
            return False
    return True


def validate_event(payload: dict, observed_run: dict, workflow: dict) -> str:
    repo = payload.get("repository") or {}
    run = payload.get("workflow_run") or {}
    if repo.get("full_name") != REPOSITORY:
        raise ValueError("workflow_run repository is not the official repository")
    if run.get("name") != "Miura immutable release":
        raise ValueError("trigger workflow name does not match the release workflow")
    if run.get("event") != "push" or run.get("conclusion") != "success":
        raise ValueError("only successful tag-push release runs may publish Pages")
    if (run.get("head_repository") or {}).get("full_name") != REPOSITORY:
        raise ValueError("release source repository is not the official repository")
    tag = run.get("head_branch", "")
    if not re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag):
        raise ValueError("release run ref is not a stable SemVer tag")
    if not re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha", ""))):
        raise ValueError("release run head SHA is invalid")
    if workflow.get("path") != WORKFLOW_PATH or workflow.get("state") != "active":
        raise ValueError("release workflow path is not the approved active workflow")
    if run.get("workflow_id") != workflow.get("id"):
        raise ValueError("trigger workflow id does not match the approved workflow file")
    if not same_run(observed_run, run):
        raise ValueError("Actions API run does not match the workflow_run event")
    return tag


def api_json(path: str, token: str) -> dict:
    request = urllib.request.Request(
        API + path,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
                 "Authorization": "Bearer " + token, "User-Agent": "miura-pages-workflow-run"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"GitHub API returned HTTP {response.status}")
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("GitHub API response exceeded the size limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API response must be a JSON object")
    return value


def validate_live_event(payload: dict, token: str) -> str:
    run_event = payload.get("workflow_run") or {}
    run_id = run_event.get("id")
    if not isinstance(run_id, int) or run_id < 1:
        raise ValueError("workflow_run id is invalid")
    run = api_json(f"/repos/{REPOSITORY}/actions/runs/{run_id}", token)
    workflow = api_json(f"/repos/{REPOSITORY}/actions/workflows/release.yml", token)
    tag = validate_event(payload, run, workflow)

    # Resolve lightweight and annotated Git tags and require the exact SHA that
    # produced the completed, successful release workflow.
    ref = api_json(f"/repos/{REPOSITORY}/git/ref/tags/{urllib.parse.quote(tag, safe='')}", token)
    obj = ref.get("object") or {}
    while obj.get("type") == "tag":
        obj = api_json(f"/repos/{REPOSITORY}/git/tags/{obj.get('sha', '')}", token).get("object") or {}
    if obj.get("type") != "commit" or obj.get("sha") != run_event.get("head_sha"):
        raise ValueError("release tag does not point to the successful workflow head commit")
    return tag


def main() -> int:
    try:
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise ValueError("GH_TOKEN is required")
        event_path = os.environ.get("GITHUB_EVENT_PATH", "")
        if not event_path:
            raise ValueError("GITHUB_EVENT_PATH is required")
        with open(event_path, encoding="utf-8") as stream:
            payload = json.load(stream)
        tag = validate_live_event(payload, token)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"release_tag={tag}\n")
        print(f"Validated successful official release workflow for {tag}")
        return 0
    except Exception as exc:
        print(f"Release workflow_run rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
