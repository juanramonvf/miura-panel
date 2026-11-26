"""Security checks for release-completion-triggered protected Pages deploys."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
import validate_release_workflow_run as guard  # noqa: E402
import build_pages_manifest as pages_builder  # noqa: E402


def fixture():
    run = {
        "id": 42, "name": "Miura immutable release", "workflow_id": 7,
        "event": "push", "conclusion": "success", "head_branch": "v1.0.41",
        "head_sha": "a" * 40,
        "repository": {"full_name": guard.REPOSITORY},
        "head_repository": {"full_name": guard.REPOSITORY},
    }
    return {"repository": {"full_name": guard.REPOSITORY}, "workflow_run": run}, run, {
        "id": 7, "path": guard.WORKFLOW_PATH, "state": "active",
    }


class ReleaseWorkflowRunTests(unittest.TestCase):
    def test_pages_selects_highest_numeric_semver_and_refuses_downgrade(self):
        manifests = [((1, 0, 9), b"old"), ((1, 0, 10), b"new"), ((1, 0, 41), b"latest")]
        self.assertEqual(pages_builder.highest_manifest(manifests), manifests[-1])
        self.assertEqual(pages_builder.semver_key("1.0.10"), (1, 0, 10))
        with self.assertRaises(ValueError):
            pages_builder.semver_key("1.00.10")
        pages_builder.validate_stable_transition((1, 0, 40), (1, 0, 41), b"v40", b"v41")
        with self.assertRaisesRegex(ValueError, "downgrade"):
            pages_builder.validate_stable_transition((1, 0, 41), (1, 0, 40), b"v41", b"v40")
        with self.assertRaisesRegex(ValueError, "differs"):
            pages_builder.validate_stable_transition((1, 0, 41), (1, 0, 41), b"one", b"two")

    def test_accepts_only_verified_successful_release_tag_run(self):
        payload, observed, workflow = fixture()
        self.assertEqual(guard.validate_event(payload, observed, workflow), "v1.0.41")

    def test_rejects_wrong_repository_event_conclusion_workflow_and_tag(self):
        for mutate in (
            lambda p, r, w: p["repository"].update(full_name="attacker/repo"),
            lambda p, r, w: r.update(event="workflow_dispatch"),
            lambda p, r, w: r.update(conclusion="failure"),
            lambda p, r, w: r.update(name="Unrelated workflow"),
            lambda p, r, w: r.update(head_branch="main"),
            lambda p, r, w: w.update(path=".github/workflows/other.yml"),
            lambda p, r, w: r.update(workflow_id=99),
        ):
            payload, run, workflow = fixture()
            mutate(payload, run, workflow)
            with self.assertRaises(ValueError):
                guard.validate_event(payload, run, workflow)

    def test_rejects_run_api_mismatch_and_fork_source(self):
        payload, observed, workflow = fixture()
        changed = dict(observed, head_sha="b" * 40)
        with self.assertRaisesRegex(ValueError, "does not match"):
            guard.validate_event(payload, changed, workflow)
        payload, observed, workflow = fixture()
        observed["head_repository"] = {"full_name": "fork/miura-panel"}
        with self.assertRaises(ValueError):
            guard.validate_event(payload, observed, workflow)

    def test_pages_workflow_keeps_environment_protection_and_recovery(self):
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        pages = (ROOT / ".github/workflows/pages-after-release.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["Miura immutable release"]', pages)
        self.assertIn("workflow_run:", pages)
        self.assertIn("name: github-pages", pages)
        self.assertIn("pages: write", pages)
        self.assertIn("publish_pages_only", release)
        self.assertNotIn("github.event_name == 'push' && needs.publish.result == 'success'", release)
        self.assertIn("validate_release_workflow_run.py", pages)
        self.assertIn("actions/checkout", pages)
        self.assertIn("github.event.repository.default_branch", pages)
        builder = (ROOT / "scripts/release/build_pages_manifest.py").read_text(encoding="utf-8")
        self.assertIn("highest_manifest(manifests)", builder)
        self.assertIn("validate_stable_transition", builder)
        self.assertIn("sha256sum", builder)


if __name__ == "__main__":
    unittest.main()
