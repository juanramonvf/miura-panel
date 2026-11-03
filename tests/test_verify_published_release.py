"""Offline tests for the Pages-only immutable-release recovery guard."""
import hashlib
import json
import pathlib
import sys
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import verify_published_release as verify  # noqa: E402


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.body[:limit]


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.responses.get(request.full_url)
        if value is None:
            raise AssertionError(f"Unexpected URL: {request.full_url}")
        if isinstance(value, Exception):
            raise value
        return FakeResponse(value)


def make_release(*, immutable=True, draft=False, wrong_api_digest=False,
                 wrong_manifest_sha=False, wrong_manifest_url=False, wrong_sidecar=False,
                 extra_asset=False, duplicate_asset=False):
    tag = "v1.0.35"
    version = tag[1:]
    installer_name = f"install-spanish-empire-panel-v{version}.sh"
    base = f"https://github.com/juanramonvf/miura-panel/releases/download/{tag}/"
    installer = b"#!/bin/bash\necho verified\n"
    installer_sha = hashlib.sha256(installer).hexdigest()
    manifest_name = "miura-panel-update-manifest.json"
    manifest = {
        "version": version,
        "installer": base + ("other.sh" if wrong_manifest_url else installer_name),
        "sha256": "0" * 64 if wrong_manifest_sha else installer_sha,
    }
    manifest_bytes = (json.dumps(manifest, separators=(",", ":")) + "\n").encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    installer_sidecar = ("0" * 64 if wrong_sidecar else installer_sha) + f"  {installer_name}\n"
    assets_data = {
        installer_name: installer,
        installer_name + ".sha256": installer_sidecar.encode("ascii"),
        manifest_name: manifest_bytes,
        manifest_name + ".sha256": f"{manifest_sha}  {manifest_name}\n".encode("ascii"),
        "RELEASE_NOTES.md": b"Miura Panel v1.0.35 release notes\n",
    }
    if extra_asset:
        assets_data["extra.txt"] = b"unexpected"
    assets = []
    responses = {}
    for name, data in assets_data.items():
        url = base + name
        digest = hashlib.sha256(data).hexdigest()
        if wrong_api_digest and name == installer_name:
            digest = "f" * 64
        assets.append({
            "name": name,
            "state": "uploaded",
            "size": len(data),
            "digest": "sha256:" + digest,
            "browser_download_url": url,
        })
        responses[url] = data
    if duplicate_asset:
        assets.append(dict(assets[0]))
    release = {
        "tag_name": tag,
        "draft": draft,
        "prerelease": False,
        "immutable": immutable,
        "assets": assets,
    }
    api_url = "https://api.github.com/repos/juanramonvf/miura-panel/releases/tags/v1.0.35"
    responses[api_url] = json.dumps(release).encode("utf-8")
    return responses, api_url


class PublishedReleaseVerificationTests(unittest.TestCase):
    def call(self, opener, tag="v1.0.35", repository="juanramonvf/miura-panel"):
        return verify.verify_published_release(repository, tag, "contents-read-token", opener=opener)

    def test_complete_immutable_release_with_matching_sidecars_passes(self):
        responses, api_url = make_release()
        opener = FakeOpener(responses)
        digests = self.call(opener)
        self.assertEqual(len(digests), 5)
        self.assertEqual(opener.requests[0][0].full_url, api_url)
        self.assertEqual(opener.requests[0][0].get_header("Authorization"), "Bearer contents-read-token")

    def test_mutable_or_draft_release_is_rejected(self):
        for options, expected in (({"immutable": False}, "not confirmed immutable"),
                                  ({"draft": True}, "must be published")):
            with self.subTest(options=options):
                responses, _ = make_release(**options)
                with self.assertRaisesRegex(RuntimeError, expected):
                    self.call(FakeOpener(responses))

    def test_api_digest_mismatch_is_rejected(self):
        responses, _ = make_release(wrong_api_digest=True)
        with self.assertRaisesRegex(RuntimeError, "GitHub API SHA256"):
            self.call(FakeOpener(responses))

    def test_manifest_cannot_approve_different_installer_bytes(self):
        responses, _ = make_release(wrong_manifest_sha=True)
        with self.assertRaisesRegex(RuntimeError, "does not approve"):
            self.call(FakeOpener(responses))

    def test_manifest_cannot_point_at_a_different_installer_url(self):
        responses, _ = make_release(wrong_manifest_url=True)
        with self.assertRaisesRegex(RuntimeError, "does not approve"):
            self.call(FakeOpener(responses))

    def test_mismatched_installer_sidecar_is_rejected(self):
        responses, _ = make_release(wrong_sidecar=True)
        with self.assertRaisesRegex(RuntimeError, "sidecar"):
            self.call(FakeOpener(responses))

    def test_extra_release_asset_is_rejected(self):
        responses, _ = make_release(extra_asset=True)
        with self.assertRaisesRegex(RuntimeError, "asset names"):
            self.call(FakeOpener(responses))

    def test_duplicate_release_asset_name_is_rejected(self):
        responses, _ = make_release(duplicate_asset=True)
        with self.assertRaisesRegex(RuntimeError, "asset names"):
            self.call(FakeOpener(responses))

    def test_invalid_tag_and_repository_are_rejected_before_api_access(self):
        responses, _ = make_release()
        opener = FakeOpener(responses)
        with self.assertRaisesRegex(ValueError, "stable semver"):
            self.call(opener, tag="v1.0.35/../main")
        with self.assertRaisesRegex(ValueError, "restricted"):
            self.call(opener, repository="someone/else")
        self.assertEqual(opener.requests, [])

    def test_release_lookup_failure_is_rejected(self):
        responses, api_url = make_release()
        responses[api_url] = urllib.error.HTTPError(api_url, 404, "Not Found", {}, None)
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            self.call(FakeOpener(responses))


if __name__ == "__main__":
    unittest.main()
