"""Offline tests for the immutable GitHub Release preflight gate."""

import json
import pathlib
import sys
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import preflight_github_release as preflight  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.body = json.dumps(payload if payload is not None else {}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.outcomes:
            raise AssertionError("Unexpected HTTP request")
        status, payload = self.outcomes.pop(0)
        if isinstance(status, Exception):
            raise status
        return FakeResponse(status, payload)


class ImmutableReleasePreflightTests(unittest.TestCase):
    repo = "juanramonvf/miura-panel"
    tag = "v9.8.7"

    def check(self, opener, *, immutable_token="admin-read-token"):
        return preflight.check_github_release(
            self.repo, self.tag, immutable_token, opener=opener
        )

    def test_immutable_enabled_and_tag_lookup_404_allows_new_release(self):
        opener = FakeOpener([(200, {"enabled": True}), (404, {})])
        self.assertIsNone(self.check(opener))
        self.assertEqual(len(opener.requests), 2)
        immutable_request, immutable_timeout = opener.requests[0]
        release_request, release_timeout = opener.requests[1]
        self.assertTrue(immutable_request.full_url.endswith("/immutable-releases"))
        self.assertTrue(release_request.full_url.endswith(f"/releases/tags/{self.tag}"))
        self.assertEqual(immutable_request.get_header("Authorization"), "Bearer admin-read-token")
        self.assertEqual(release_request.get_header("Authorization"), "Bearer admin-read-token")
        self.assertEqual((immutable_timeout, release_timeout), (20, 20))

    def test_existing_published_release_is_rejected(self):
        opener = FakeOpener([(200, {"enabled": True}), (200, {"draft": False})])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.check(opener)
        self.assertEqual(len(opener.requests), 2)

    def test_existing_draft_is_rejected(self):
        opener = FakeOpener([(200, {"enabled": True}), (200, {"draft": True})])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.check(opener)

    def test_missing_tokens_fail_before_any_api_request(self):
        opener = FakeOpener([])
        with self.assertRaisesRegex(ValueError, "required"):
            self.check(opener, immutable_token="")
        self.assertEqual(opener.requests, [])

    def test_malformed_tag_fails_before_any_api_request(self):
        for tag in ("", "v1.2.3/../../other", "v01.2.3"):
            with self.subTest(tag=tag):
                opener = FakeOpener([])
                with self.assertRaisesRegex(ValueError, "vMAJOR.MINOR.PATCH"):
                    preflight.check_github_release(
                        self.repo, tag, "admin-read-token", opener=opener
                    )
                self.assertEqual(opener.requests, [])

    def test_malformed_repository_fails_before_any_api_request(self):
        for repo in ("", "owner", "owner/repo/extra", "owner/repo?query=1"):
            with self.subTest(repo=repo):
                opener = FakeOpener([])
                with self.assertRaisesRegex(ValueError, "owner/repository"):
                    preflight.check_github_release(
                        repo, self.tag, "admin-read-token", opener=opener
                    )
                self.assertEqual(opener.requests, [])

    def test_immutability_disabled_fails_closed(self):
        opener = FakeOpener([(200, {"enabled": False})])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.check(opener)
        self.assertEqual(len(opener.requests), 1)

    def test_immutability_endpoint_404_fails_closed(self):
        opener = FakeOpener([(404, {})])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.check(opener)
        self.assertEqual(len(opener.requests), 1)

    def test_invalid_immutability_status_or_body_fails_closed(self):
        for outcomes in (
            [(500, {})],
            [(200, {"enabled": "true"})],
            [(200, [])],
        ):
            with self.subTest(outcomes=outcomes):
                opener = FakeOpener(outcomes)
                with self.assertRaisesRegex(RuntimeError, "disabled"):
                    self.check(opener)
                self.assertEqual(len(opener.requests), 1)

    def test_unexpected_release_lookup_status_fails_closed(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                opener = FakeOpener([(200, {"enabled": True}), (status, {})])
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    self.check(opener)

    def test_http_error_is_treated_as_api_status(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/juanramonvf/miura-panel/immutable-releases",
            403,
            "Forbidden",
            hdrs=None,
            fp=None,
        )
        opener = FakeOpener([(error, None)])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.check(opener)


if __name__ == "__main__":
    unittest.main()
