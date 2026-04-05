import json
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "redirect-optimiser.py"
TMP_ROOT = REPO_ROOT / "tests" / ".tmp"


class RedirectOptimiserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def make_case_dir(self, case_name: str) -> Path:
        case_dir = TMP_ROOT / case_name
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        return case_dir

    def run_tool(self, file_path: Path, extra_args=None):
        if extra_args is None:
            extra_args = []
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(file_path), *extra_args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

    def test_rejects_invalid_filename(self):
        case = self.make_case_dir("invalid-filename")
        bad = case / "redirects.txt"
        bad.write_text("/a /b 301\n", encoding="utf-8")

        result = self.run_tool(bad, ["--chain"])
        self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
        self.assertIn("must be named .htaccess or _redirects", result.stderr)

    def test_parses_htaccess_and_detects_chain(self):
        case = self.make_case_dir("htaccess-chain")
        htaccess = case / ".htaccess"
        htaccess.write_text(
            textwrap.dedent(
                """\
                # comment
                Redirect 301 /a /b
                Redirect 302 /b /c
                Redirect 301 /x /y
                RewriteRule ^old$ /new [R=301,L]
                """
            ),
            encoding="utf-8",
        )

        result = self.run_tool(htaccess, ["--chain"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("Chain 1: /a -> /b -> /c (final: /c)", result.stdout)
        self.assertNotIn("/x -> /y", result.stdout)

    def test_parses_netlify_and_ignores_non_301_302(self):
        case = self.make_case_dir("netlify-parse")
        redirects = case / "_redirects"
        redirects.write_text(
            textwrap.dedent(
                """\
                # comment
                /a /b 301
                /b /c 302
                /skip /dest 200
                /gone /dest 410
                """
            ),
            encoding="utf-8",
        )

        result = self.run_tool(redirects, ["--chain"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("Chain 1: /a -> /b -> /c (final: /c)", result.stdout)
        self.assertNotIn("/skip", result.stdout)
        self.assertNotIn("/gone", result.stdout)

    def test_no_chain_returns_zero(self):
        case = self.make_case_dir("no-chain")
        redirects = case / "_redirects"
        redirects.write_text(
            textwrap.dedent(
                """\
                /a /b 301
                /x /y 302
                """
            ),
            encoding="utf-8",
        )

        result = self.run_tool(redirects, ["--chain"])
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("No redirect chains found.", result.stdout)

    def test_detects_cycle_and_does_not_modify_it(self):
        case = self.make_case_dir("cycle")
        redirects = case / "_redirects"
        original = textwrap.dedent(
            """\
            /a /b 301
            /b /a 302
            """
        )
        redirects.write_text(original, encoding="utf-8")

        chained = self.run_tool(redirects, ["--chain"])
        self.assertEqual(chained.returncode, 1, msg=chained.stdout + chained.stderr)
        self.assertIn("cycle detected", chained.stdout)

        modified = self.run_tool(redirects, ["--chain", "--modify"])
        self.assertEqual(modified.returncode, 0, msg=modified.stdout + modified.stderr)
        self.assertIn("cycles_skipped=1", modified.stdout)
        self.assertEqual(redirects.read_text(encoding="utf-8"), original)
        backups = list(case.glob("_redirects.*.bak"))
        self.assertEqual(backups, [])

    def test_modify_flattens_chain_preserves_status_and_keeps_comments(self):
        case = self.make_case_dir("modify-flatten")
        htaccess = case / ".htaccess"
        original = textwrap.dedent(
            """\
            # keep
            Redirect 301 /a /b # c1
            Redirect 302 /b /c # c2
            Redirect 301 /free /target
            """
        )
        htaccess.write_text(original, encoding="utf-8")

        first = self.run_tool(htaccess, ["--chain", "--modify"])
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertIn("rewritten=1", first.stdout)
        self.assertRegex(first.stdout, r"\.htaccess\.\d{8}T\d{6}\.bak")

        updated = htaccess.read_text(encoding="utf-8")
        self.assertIn("Redirect 301 /a /c # c1", updated)
        self.assertIn("Redirect 302 /b /c # c2", updated)
        self.assertIn("Redirect 301 /free /target", updated)
        self.assertIn("# keep", updated)

        backups = list(case.glob(".htaccess.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

        second = self.run_tool(htaccess, ["--chain", "--modify"])
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertIn("rewritten=0", second.stdout)
        backups_after_second = list(case.glob(".htaccess.*.bak"))
        self.assertEqual(len(backups_after_second), 1)

    def test_json_output(self):
        case = self.make_case_dir("json-output")
        redirects = case / "_redirects"
        redirects.write_text(
            textwrap.dedent(
                """\
                /a /b 301
                /b /c 301
                """
            ),
            encoding="utf-8",
        )

        result = self.run_tool(redirects, ["--chain", "--json"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("chains", payload)
        self.assertEqual(len(payload["chains"]), 1)
        self.assertEqual(payload["chains"][0]["hops"], ["/a", "/b", "/c"])
        self.assertEqual(payload["chains"][0]["terminal"], "/c")
        self.assertFalse(payload["chains"][0]["cycle"])

    def test_modify_without_chain_still_checks_and_modifies_when_needed(self):
        case = self.make_case_dir("modify-no-chain-flag")
        redirects = case / "_redirects"
        redirects.write_text("/a /b 301\n/b /c 301\n", encoding="utf-8")

        result = self.run_tool(redirects, ["--modify"])
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("rewritten=1", result.stdout)
        self.assertIn("/a /c 301", redirects.read_text(encoding="utf-8"))

    def test_multihop_chain_report(self):
        case = self.make_case_dir("multi-hop")
        redirects = case / "_redirects"
        redirects.write_text(
            textwrap.dedent(
                """\
                /a /b 301
                /b /c 301
                /c /d 302
                """
            ),
            encoding="utf-8",
        )

        result = self.run_tool(redirects, ["--chain"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("Chain 1: /a -> /b -> /c -> /d (final: /d)", result.stdout)


if __name__ == "__main__":
    unittest.main()
