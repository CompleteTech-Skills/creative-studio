"""Exercise the CLI against synthetic commits in an isolated Git repository."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHECK = Path(__file__).resolve().parents[1] / "scripts" / "check_evidence_paths.py"
RECEIPT = "tests/results/synthetic/receipt.log"
ACCOUNT = "synthetic-operator"
LINUX = "/home/" + ACCOUNT + "/artifact"
MACOS = "/Users/" + ACCOUNT + "/artifact"
ROOT = "/root" + "/" + "synthetic-artifact"
WINDOWS = "\\".join(("C:", "Users", ACCOUNT, "artifact"))


class EvidencePathGuard(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.repo = Path(temp_dir.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.write(RECEIPT, f"historical {LINUX}\nresult: unchanged\n")
        self.base = self.commit("Historical receipt")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, relative_path, content):
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def scan(self, base, head="HEAD"):
        return subprocess.run(
            [sys.executable, str(CHECK), base, head], cwd=self.repo,
            capture_output=True, text=True, check=False,
        )

    def test_rejects_added_local_paths_across_committed_files(self):
        escaped_windows = WINDOWS.replace("\\", "\\\\")
        variants = [LINUX, MACOS, ROOT, WINDOWS, escaped_windows, "file://" + LINUX]
        self.write("new evidence.log", "\n".join(f"++ {path}" for path in variants))
        self.write("docs/other.txt", f"receipt: {ROOT}\n")
        result = self.scan(self.base, self.commit("Add synthetic paths"))

        self.assertEqual(result.returncode, 1)
        for line_number in range(1, len(variants) + 1):
            self.assertIn(f"new evidence.log:{line_number}:", result.stderr)
        self.assertIn("docs/other.txt:1:", result.stderr)
        self.assertIn("<local-work-dir>/", result.stderr)
        self.assertNotIn("synthetic-", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unrelated_edit_in_historical_receipt_passes(self):
        self.write(RECEIPT, f"historical {LINUX}\nresult: updated\n")
        head = self.commit("Update unrelated line")
        result = self.scan(self.base, head)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_uses_merge_base_on_diverged_branches(self):
        self.write(RECEIPT, "result: unchanged\n")
        target = self.commit("Remove synthetic path on target")
        self.git("checkout", "--detach", self.base)
        self.write("new.txt", "<local-work-dir>/artifact\nexamples" + MACOS + "\n")
        head = self.commit("Add allowed paths on proposed branch")

        self.assertEqual(self.scan(target, head).returncode, 0)

    def test_omits_sensitive_filename(self):
        self.write("home/" + ACCOUNT + "/new.txt", ROOT)
        result = self.scan(self.base, self.commit("Add a synthetic filename"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed file:1:", result.stderr)
        self.assertNotIn(ACCOUNT, result.stderr)

    def test_missing_commit_returns_error_without_echoing_input(self):
        result = self.scan("synthetic-missing-ref")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("synthetic-missing-ref", result.stderr)
