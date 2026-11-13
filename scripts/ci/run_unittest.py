#!/usr/bin/env python3
"""Run unittest quietly and publish a redacted, structured failure report."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import stat
import sys
import traceback
import unittest
from pathlib import Path


FIXTURE_SECRETS = (
    "Miura-Reconfigure-Password-39!",
    "Miura-Test-Password-39!",
    "contents-write-token",
    "contents-read-token",
    "admin-read-token",
    "incorrect-password",
    "owner@example.test",
    "sep_adm=test-session-cookie",
)

SECRET_ENV_NAME = re.compile(
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE.?KEY|API.?KEY|COOKIE|SESSION|AUTH)", re.I
)
SECRET_FIELD = re.compile(
    r"(?i)([\"']?(?:password|passwd|passphrase|token|secret|authorization|set-cookie|cookie|session|"
    r"api[_-]?key|private[_-]?key)[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]*)"
)
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
URL_CREDENTIALS = re.compile(r"(?i)(https?://[^:/\s]+:)[^@/\s]+(@)")
ADMIN_COOKIE = re.compile(r"(?i)\bsep_adm=[^;\s]+")
PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.S,
)


def redact(text: str) -> str:
    """Remove known fixtures and credential-shaped values from diagnostics."""
    secrets = set(FIXTURE_SECRETS)
    for name, value in os.environ.items():
        if SECRET_ENV_NAME.search(name) and value:
            secrets.add(value)
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = URL_CREDENTIALS.sub(r"\1[REDACTED]\2", text)
    text = BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = ADMIN_COOKIE.sub("sep_adm=[REDACTED]", text)
    return SECRET_FIELD.sub(r"\1[REDACTED]", text)


def clear_old_report(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise OSError(f"refusing non-regular report path: {path}")
    path.unlink()


def write_report(path: Path, report: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(report)


def read_report(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError(f"refusing non-regular report path: {path}")
    with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
        return stream.read()


def run_tests(start_directory: Path, report_path: Path) -> int:
    clear_old_report(report_path)
    loader = unittest.defaultTestLoader
    runner_output = io.StringIO()
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    runner = unittest.TextTestRunner(stream=runner_output, verbosity=2)

    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            suite = loader.discover(str(start_directory))
            result = runner.run(suite)
    finally:
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        os.close(null_fd)

    if result.wasSuccessful():
        print(
            f"Candidate unittest suite passed: {result.testsRun} tests, "
            f"{len(result.skipped)} skipped."
        )
        return 0

    failures = [("FAIL", test, detail) for test, detail in result.failures]
    failures.extend(("ERROR", test, detail) for test, detail in result.errors)
    report = [
        f"Candidate unittest suite failed: {result.testsRun} tests, "
        f"{len(result.failures)} failures, {len(result.errors)} errors, "
        f"{len(result.skipped)} skipped.",
        "",
    ]
    for kind, test, traceback_text in failures:
        report.extend((f"{kind}: {test.id()}", traceback_text.rstrip(), ""))
    write_report(report_path, "\n".join(report).rstrip() + "\n")
    print(f"Candidate unittest suite failed: {len(failures)} failing test(s); see sanitized traceback below.")
    return 1


def escape_workflow_command(text: str) -> str:
    return (text.replace("%", "%25").replace("\r", "%0D")
            .replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C"))


def emit_report(path: Path) -> int:
    report = redact(read_report(path))
    if not report.strip():
        print("UNITTEST DIAGNOSTIC: failure report was empty.")
        return 2

    # Prefix every ordinary log line so test-controlled text cannot become an
    # Actions workflow command. Keep the full sanitized trace in the job log.
    for line in report.splitlines():
        print(f"UNITTEST DIAGNOSTIC: {line}")

    # A single escaped annotation makes the complete traceback available in
    # the public check summary without interpreting trace text as commands.
    escaped = escape_workflow_command(report)
    print(f"::error title=Candidate unittest failure::{escaped}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="discover and run tests")
    parser.add_argument("--start-directory", type=Path, default=Path("tests"))
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()

    if not args.run:
        parser.error("--run is required")
    if args.report_file is None:
        parser.error("--report-file is required with --run")
    try:
        status = run_tests(args.start_directory, args.report_file)
    except Exception:
        try:
            write_report(
                args.report_file,
                "Candidate unittest runner failed before completing discovery or execution.\n"
                + traceback.format_exc(),
            )
            status = 1
        except OSError:
            print("Candidate unittest runner failed and could not safely create its report file.")
            return 1
    if status:
        try:
            emit_report(args.report_file)
        except OSError:
            print("Candidate unittest suite failed; the traceback report could not be read safely.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
