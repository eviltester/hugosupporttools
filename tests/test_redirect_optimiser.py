import json
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "redirect-optimiser.py"
TMP_ROOT = REPO_ROOT / "tests" / ".tmp"


class RedirectOptimiserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        spec = importlib.util.spec_from_file_location(
            f"redirect_optimiser_{uuid.uuid4().hex}",
            SCRIPT,
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.redirect_module = module

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

    def run_tool_inproc(self, argv, side_effect):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(self.redirect_module, "fetch_head", side_effect=side_effect),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = self.redirect_module.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

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

    def test_crawl_requires_base_url_for_relative_targets(self):
        case = self.make_case_dir("crawl-relative-needs-base")
        redirects = case / "_redirects"
        redirects.write_text("/a /relative 301\n", encoding="utf-8")

        result = self.run_tool(redirects, ["--crawl"])
        self.assertEqual(result.returncode, 2, msg=result.stdout + result.stderr)
        self.assertIn("requires --url", result.stderr)

    def test_crawl_classifies_head_results(self):
        case = self.make_case_dir("crawl-classify")
        redirects = case / "_redirects"
        redirects.write_text(
            textwrap.dedent(
                """\
                /a https://ok.example/a 301
                /b https://perm.example/b 301
                /c https://temp.example/c 301
                /d https://bad.example/d 301
                """
            ),
            encoding="utf-8",
        )

        responses = {
            "https://ok.example/a": self.redirect_module.HeadResponse(204, None, None),
            "https://perm.example/b": self.redirect_module.HeadResponse(301, "https://new.example/final", None),
            "https://temp.example/c": self.redirect_module.HeadResponse(302, "https://tmp.example/next", None),
            "https://bad.example/d": self.redirect_module.HeadResponse(None, None, "timeout"),
        }
        calls = []

        def fake_fetch(url, timeout=10):
            calls.append(url)
            return responses[url]

        code, stdout, _ = self.run_tool_inproc([str(redirects), "--crawl"], fake_fetch)
        self.assertEqual(code, 1, msg=stdout)
        self.assertIn("Crawl summary: checked=4", stdout)
        self.assertIn("class=permanent_redirect", stdout)
        self.assertIn("class=temporary_redirect", stdout)
        self.assertIn("class=invalid", stdout)
        self.assertEqual(len(calls), 4)

    def test_crawl_with_base_url_resolves_relative_targets(self):
        case = self.make_case_dir("crawl-relative-base")
        redirects = case / "_redirects"
        redirects.write_text("/a /path 301\n", encoding="utf-8")

        seen_urls = []

        def fake_fetch(url, timeout=10):
            seen_urls.append(url)
            return self.redirect_module.HeadResponse(200, None, None)

        code, stdout, _ = self.run_tool_inproc(
            [str(redirects), "--crawl", "--url", "https://www.eviltester.com"],
            fake_fetch,
        )
        self.assertEqual(code, 0, msg=stdout)
        self.assertEqual(seen_urls, ["https://www.eviltester.com/path"])
        self.assertIn("valid=1", stdout)

    def test_modify_with_crawl_rewrites_permanent_and_removes_invalid(self):
        case = self.make_case_dir("modify-crawl")
        redirects = case / "_redirects"
        original = textwrap.dedent(
            """\
            # keep
            /one /good 301
            /two /perm 301
            /three /temp 302
            /four /bad 301
            """
        )
        redirects.write_text(original, encoding="utf-8")

        responses = {
            "https://www.eviltester.com/good": self.redirect_module.HeadResponse(200, None, None),
            "https://www.eviltester.com/perm": self.redirect_module.HeadResponse(308, "/final", None),
            "https://www.eviltester.com/temp": self.redirect_module.HeadResponse(302, "/tmp", None),
            "https://www.eviltester.com/bad": self.redirect_module.HeadResponse(404, None, None),
        }

        def fake_fetch(url, timeout=10):
            return responses[url]

        code, stdout, _ = self.run_tool_inproc(
            [str(redirects), "--crawl", "--modify", "--url", "https://www.eviltester.com"],
            fake_fetch,
        )
        self.assertEqual(code, 0, msg=stdout)
        self.assertIn("removed=2", stdout)
        self.assertIn("rewritten_from_crawl=1", stdout)

        updated = redirects.read_text(encoding="utf-8")
        self.assertIn("/one /good 301", updated)
        self.assertIn("/two https://www.eviltester.com/final 301", updated)
        self.assertNotIn("/three /temp 302", updated)
        self.assertNotIn("/four /bad 301", updated)
        self.assertIn("# keep", updated)
        backups = list(case.glob("_redirects.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_chain_then_crawl_applies_to_flattened_targets(self):
        case = self.make_case_dir("chain-crawl-combined")
        redirects = case / "_redirects"
        redirects.write_text("/a /b 301\n/b /c 301\n", encoding="utf-8")

        def fake_fetch(url, timeout=10):
            if url == "https://www.eviltester.com/c":
                return self.redirect_module.HeadResponse(301, "https://cdn.example/final", None)
            raise AssertionError(f"Unexpected URL: {url}")

        code, stdout, _ = self.run_tool_inproc(
            [str(redirects), "--chain", "--crawl", "--modify", "--url", "https://www.eviltester.com"],
            fake_fetch,
        )
        self.assertEqual(code, 0, msg=stdout)
        updated = redirects.read_text(encoding="utf-8")
        self.assertIn("/a https://cdn.example/final 301", updated)
        self.assertIn("/b https://cdn.example/final 301", updated)

    def test_crawl_json_payload_contains_actions(self):
        case = self.make_case_dir("crawl-json")
        redirects = case / "_redirects"
        redirects.write_text("/a /perm 301\n/b /bad 301\n", encoding="utf-8")

        responses = {
            "https://www.eviltester.com/perm": self.redirect_module.HeadResponse(301, "/moved", None),
            "https://www.eviltester.com/bad": self.redirect_module.HeadResponse(404, None, None),
        }

        def fake_fetch(url, timeout=10):
            return responses[url]

        code, stdout, _ = self.run_tool_inproc(
            [str(redirects), "--crawl", "--modify", "--json", "--url", "https://www.eviltester.com"],
            fake_fetch,
        )
        self.assertEqual(code, 0, msg=stdout)
        payload = json.loads(stdout)
        self.assertIn("crawl", payload)
        self.assertIn("modify", payload)
        actions = {item["source"]: item["action"] for item in payload["crawl"]["findings"]}
        self.assertEqual(actions["/a"], "rewritten_permanent_redirect")
        self.assertEqual(actions["/b"], "removed_invalid")

    def test_modify_with_crawl_is_idempotent(self):
        case = self.make_case_dir("crawl-idempotent")
        redirects = case / "_redirects"
        redirects.write_text("/a /perm 301\n", encoding="utf-8")

        def fake_fetch_first(url, timeout=10):
            if url == "https://www.eviltester.com/perm":
                return self.redirect_module.HeadResponse(301, "https://cdn.example/final", None)
            if url == "https://cdn.example/final":
                return self.redirect_module.HeadResponse(200, None, None)
            raise AssertionError(f"Unexpected URL: {url}")

        first_code, first_stdout, _ = self.run_tool_inproc(
            [str(redirects), "--crawl", "--modify", "--url", "https://www.eviltester.com"],
            fake_fetch_first,
        )
        self.assertEqual(first_code, 0, msg=first_stdout)
        self.assertIn("rewritten=1", first_stdout)

        def fake_fetch_second(url, timeout=10):
            if url == "https://cdn.example/final":
                return self.redirect_module.HeadResponse(200, None, None)
            raise AssertionError(f"Unexpected URL: {url}")

        second_code, second_stdout, _ = self.run_tool_inproc(
            [str(redirects), "--crawl", "--modify", "--url", "https://www.eviltester.com"],
            fake_fetch_second,
        )
        self.assertEqual(second_code, 0, msg=second_stdout)
        self.assertIn("rewritten=0", second_stdout)
        self.assertIn("removed=0", second_stdout)


if __name__ == "__main__":
    unittest.main()
