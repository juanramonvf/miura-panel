"""Security and race-condition tests for draft verification/cleanup."""
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import release_draft as draft  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return b"" if self.payload is None else json.dumps(self.payload).encode()


class ReleaseDraftTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "123456"
        self.tag = "v1.0.39"
        self.marker = f"<!-- miura-release-run:{self.run_id} -->"
        self.temp = tempfile.TemporaryDirectory(prefix="miura-release-draft-")
        self.addCleanup(self.temp.cleanup)
        self.asset_dir = pathlib.Path(self.temp.name)
        self.assets = {}
        for name in sorted(item.format(version="1.0.39") for item in draft.EXPECTED_ASSETS):
            content = f"approved {name}".encode()
            path = self.asset_dir / name
            path.write_bytes(content)
            self.assets[name] = {
                "name": name,
                "state": "uploaded",
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        self.release = {
            "id": 789,
            "tag_name": self.tag,
            "draft": True,
            "body": "notes\n" + self.marker,
            "assets": list(self.assets.values()),
        }

    def client(self, sequence, sleeps=None):
        responses = iter(sequence)
        requests = []

        def opener(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 20)
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return FakeResponse(*item)

        client = draft.ReleaseClient(
            draft.EXPECTED_REPO, "test-token", opener=opener,
            sleeper=(sleeps if sleeps is not None else lambda _delay: None),
        )
        client.test_requests = requests
        return client

    @staticmethod
    def listing(release=None):
        return 200, [] if release is None else [release]

    @staticmethod
    def direct(release):
        return 200, release

    @staticmethod
    def not_found():
        return urllib.error.HTTPError("https://api.github.com/releases", 404,
                                      "missing", {}, None)

    def test_verify_retries_transient_missing_draft_without_relaxing_asset_checks(self):
        delays = []
        client = self.client([self.not_found(), self.listing(), self.direct(self.release)], delays.append)
        draft.verify_draft(client, self.tag, self.run_id, self.marker, self.asset_dir)
        self.assertEqual(delays, [1])
        self.assertTrue(all(r.get_header("Authorization") == "Bearer test-token"
                            for r in client.test_requests))
        self.assertTrue(all("api.github.com/repos/juanramonvf/miura-panel/" in r.full_url
                            for r in client.test_requests))

    def test_verify_rejects_wrong_owner_or_incomplete_assets(self):
        foreign = dict(self.release, body="other run")
        with self.assertRaisesRegex(draft.ReleaseAPIError, "not owned"):
            draft.verify_draft(self.client([self.direct(foreign)]), self.tag,
                               self.run_id, self.marker, self.asset_dir)
        incomplete = dict(self.release, assets=list(self.assets.values())[:-1])
        with self.assertRaisesRegex(draft.ReleaseAPIError, "approved package"):
            draft.verify_draft(self.client([self.direct(incomplete)]), self.tag,
                               self.run_id, self.marker, self.asset_dir)

    def test_context_rejects_repo_mismatch_before_api_use(self):
        env = {
            "GH_REPO": draft.EXPECTED_REPO,
            "GITHUB_REPOSITORY": "attacker/fork",
            "MIURA_RELEASE_TAG": self.tag,
            "GITHUB_RUN_ID": self.run_id,
            "GH_TOKEN": "test-token",
        }
        with self.assertRaisesRegex(ValueError, "does not match"):
            draft.run_context(env)

    def test_cleanup_deletes_only_marked_draft_and_confirms_404(self):
        not_found = self.not_found()
        client = self.client([
            self.direct(self.release),
            (200, self.release),
            (204, None),
            not_found,
        ])
        draft.cleanup_draft(client, self.tag, self.marker, creation_succeeded=True)
        self.assertEqual([r.get_method() for r in client.test_requests], ["GET", "GET", "DELETE", "GET"])
        self.assertTrue(client.test_requests[2].full_url.endswith("/releases/789"))

    def test_cleanup_refuses_foreign_or_published_release(self):
        foreign = dict(self.release, body="unrelated")
        with self.assertRaisesRegex(draft.ReleaseAPIError, "Refusing"):
            draft.cleanup_draft(self.client([self.direct(foreign)]), self.tag, self.marker,
                                creation_succeeded=True)
        published = dict(self.release, draft=False)
        with self.assertRaisesRegex(draft.ReleaseAPIError, "Refusing"):
            draft.cleanup_draft(self.client([self.direct(published)]), self.tag, self.marker,
                                creation_succeeded=True)

    def test_cleanup_rechecks_draft_status_before_delete(self):
        published_between_reads = dict(self.release, draft=False)
        client = self.client([
            self.direct(self.release),
            (200, published_between_reads),
        ])
        with self.assertRaisesRegex(draft.ReleaseAPIError, "no longer this run's unpublished draft"):
            draft.cleanup_draft(client, self.tag, self.marker, creation_succeeded=True)
        self.assertEqual([r.get_method() for r in client.test_requests], ["GET", "GET"])

    def test_cleanup_fails_closed_when_successful_create_remains_unobservable(self):
        responses = []
        for _ in draft.WAIT_SECONDS:
            responses.extend((self.not_found(), self.listing()))
        delays = []
        client = self.client(responses, delays.append)
        with self.assertRaisesRegex(draft.ReleaseAPIError, "cleanup state is unverified"):
            draft.cleanup_draft(client, self.tag, self.marker, creation_succeeded=True)
        self.assertEqual(delays, list(draft.WAIT_SECONDS[:-1]))

    def test_recovery_uses_workflow_source_not_immutable_release_payload(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("path: workflow-source", workflow)
        self.assertIn("python3 workflow-source/scripts/release/release_draft.py verify", workflow)
        self.assertIn("python3 workflow-source/scripts/release/release_draft.py cleanup", workflow)
        self.assertNotIn("python3 release-source/scripts/release/release_draft.py", workflow)

    def test_auth_error_is_diagnostic_and_does_not_print_token(self):
        forbidden = urllib.error.HTTPError("https://api.github.com/releases", 403,
                                           "forbidden", {}, None)
        client = self.client([forbidden])
        with self.assertRaisesRegex(draft.ReleaseAPIError, "Contents read/write") as caught:
            client.release_by_tag_direct(self.tag)
        self.assertNotIn("test-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
