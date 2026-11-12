"""Reject a root prefix before admin setup can touch files or systemd."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP = ROOT / "admin-setup.py"


class AdminSetupRootPrefixGuardTests(unittest.TestCase):
    def test_root_prefix_is_rejected_before_mutation_or_systemctl(self):
        # Run the real script in a subprocess with MIURA_ROOT_PREFIX=/, but
        # force a non-root identity and instrument mutating operations. This
        # makes the regression test safe even if the guard is accidentally
        # moved or removed: any attempted mutation/child process is blocked.
        runner = textwrap.dedent(
            r"""
            import os
            import pathlib
            import runpy
            import subprocess
            import sys

            def blocked(label):
                def fail(*args, **kwargs):
                    print('BLOCKED_OPERATION:' + label, file=sys.stderr)
                    raise RuntimeError('blocked by root-prefix regression harness')
                return fail

            for name in ('chmod', 'chown', 'mkdir', 'makedirs', 'unlink',
                         'remove', 'replace', 'rename', 'symlink', 'link', 'open'):
                if hasattr(os, name):
                    setattr(os, name, blocked('os.' + name))
            for name in ('chmod', 'chown', 'mkdir', 'touch', 'unlink',
                         'replace', 'rename', 'write_text', 'write_bytes'):
                if hasattr(pathlib.Path, name):
                    setattr(pathlib.Path, name, blocked('Path.' + name))
            subprocess.run = blocked('subprocess.run')
            subprocess.Popen = blocked('subprocess.Popen')
            # Keep the script unprivileged even if the outer test suite runs
            # under sudo. The root-prefix guard must run before setup logic.
            os.geteuid = lambda: 1001
            try:
                runpy.run_path(sys.argv[1], run_name='__main__')
            except SystemExit as exc:
                print('ADMIN_SETUP_EXIT:' + str(exc.code))
            """
        )
        env = os.environ.copy()
        env["MIURA_ROOT_PREFIX"] = "/"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-c", runner, str(SETUP)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ADMIN_SETUP_EXIT:MIURA_ROOT_PREFIX no puede ser /", result.stdout)
        self.assertNotIn("BLOCKED_OPERATION:", result.stderr)


if __name__ == "__main__":
    unittest.main()
