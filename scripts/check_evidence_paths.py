#!/usr/bin/env python3
"""Reject absolute local paths added since the merge base of two commits."""

import argparse
import re
import subprocess
import sys


LOCAL_PATH = re.compile(
    rb"(?<![\w./\\-])(?:file://)?/(?:home/[^\s/\\<>\"'`]+/"
    rb"|Users/[^\s/\\<>\"'`]+/|root/)"
    rb"|(?<![\w])[A-Z]:[/\\]+Users[/\\]+[^/\\\r\n<>\"'`]+[/\\]",
    re.IGNORECASE,
)
HUNK_START = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
SAFE_FILENAME = re.compile(rb"[A-Za-z0-9._ /-]+")


def git_output(*args):
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL).strip()


def added_lines(patch):
    filename = None
    line_number = None
    for line in patch:
        if line.startswith(b"diff --git "):
            filename = None
            line_number = None
        elif hunk := HUNK_START.match(line):
            line_number = int(hunk[1])
        elif line_number is None and line.startswith(b"+++ b/"):
            # Git adds a tab after unquoted names containing spaces.
            filename = line[6:].rstrip(b"\r\n").removesuffix(b"\t")
        elif line_number is not None and line.startswith(b"+"):
            yield filename, line_number, line[1:]
            line_number += 1
        elif line_number is not None and line.startswith(b" "):
            line_number += 1


def report_paths(merge_base, head):
    matches = 0
    with subprocess.Popen(
        [
            "git", "diff", "--no-color", "--no-ext-diff", "--no-textconv",
            "--no-relative", "--src-prefix=a/", "--dst-prefix=b/",
            "--find-renames", "--unified=0", merge_base, head, "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as diff:
        for filename, line_number, line in added_lines(diff.stdout):
            if not LOCAL_PATH.search(line):
                continue
            matches += 1
            location = "changed file"
            if filename and SAFE_FILENAME.fullmatch(filename) and not LOCAL_PATH.search(b"/" + filename):
                location = filename.decode("ascii")
            print(f"{location}:{line_number}: absolute local path", file=sys.stderr)
        if diff.wait():
            raise subprocess.CalledProcessError(diff.returncode, "git diff")
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="target branch commit")
    parser.add_argument("head", help="proposed branch commit")
    args = parser.parse_args()

    try:
        base = git_output("rev-parse", "--verify", "--end-of-options", args.base + "^{commit}")
        head = git_output("rev-parse", "--verify", "--end-of-options", args.head + "^{commit}")
        merge_base = git_output("merge-base", base, head)
        matches = report_paths(merge_base, head)
    except (OSError, subprocess.CalledProcessError):
        print("Evidence path check could not compare the Git commits.", file=sys.stderr)
        return 2

    if matches:
        print("Use <local-work-dir>/ or a repository-relative path.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
