import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "alias-checker.py"
TMP_ROOT = REPO_ROOT / "tests" / ".tmp"


class AliasCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def run_checker(self, content_dir: Path, extra_args=None):
        if extra_args is None:
            extra_args = []
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(content_dir), *extra_args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

    def make_case_dir(self, case_name: str) -> Path:
        case_dir = TMP_ROOT / case_name
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        return case_dir

    def test_detects_alias_mislinks_and_formats(self):
        root = self.make_case_dir("mislinks")

        (root / "new-page.md").write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                  - /old-page/
                  - /legacy/
                ---
                New page.
                """
            ),
            encoding="utf-8",
        )

        (root / "toml-page.md").write_text(
            textwrap.dedent(
                """\
                +++
                aliases = ["/old-toml/"]
                url = "/toml-new.html"
                +++
                TOML page.
                """
            ),
            encoding="utf-8",
        )

        (root / "json-page.md").write_text(
            textwrap.dedent(
                """\
                {"aliases":["/old-json/"],"slug":"json-slug"}
                JSON page.
                """
            ),
            encoding="utf-8",
        )

        (root / "post.md").write_text(
            textwrap.dedent(
                """\
                [Old page](/old-page/)
                [Old toml](/old-toml)
                [json-ref]: /old-json/
                <a href="/legacy/">Legacy link</a>

                Mention /old-page/ in plain text should not count.
                """
            ),
            encoding="utf-8",
        )

        result = self.run_checker(root)

        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("Alias,Use Instead,Found In", result.stdout)
        self.assertIn("/old-page,/new-page/,post.md", result.stdout)
        self.assertIn("/old-toml,/toml-new.html,post.md", result.stdout)
        self.assertIn("/old-json,/json-slug/,post.md", result.stdout)
        self.assertIn("/legacy,/new-page/,post.md", result.stdout)
        self.assertIn("detected 4 mislinks", result.stderr)

    def test_returns_zero_when_no_findings(self):
        root = self.make_case_dir("clean")
        (root / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                  - /old-url/
                ---
                Link to canonical:
                [Page](/page/)
                """
            ),
            encoding="utf-8",
        )

        result = self.run_checker(root)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "Alias,Use Instead,Found In")
        self.assertIn("detected 0 mislinks", result.stderr)

    def test_modify_rewrites_links_and_is_idempotent(self):
        root = self.make_case_dir("modify")

        (root / "new-page.md").write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                  - /old-page/
                  - /legacy/
                ---
                New page.
                """
            ),
            encoding="utf-8",
        )

        (root / "toml-page.md").write_text(
            textwrap.dedent(
                """\
                +++
                aliases = ["/old-toml/"]
                url = "/toml-new.html"
                +++
                TOML page.
                """
            ),
            encoding="utf-8",
        )

        post_path = root / "post.md"
        post_path.write_text(
            textwrap.dedent(
                """\
                [Old page](/old-page/)
                [Old toml](/old-toml "Title")
                [legacy-ref]: /legacy/
                <a href="/legacy/">Legacy link</a>

                Mention /old-page/ in plain text should not count.
                """
            ),
            encoding="utf-8",
        )

        first = self.run_checker(root, ["--modify"])
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertIn("Alias,Use Instead,Found In", first.stdout)
        self.assertIn("/old-page,/new-page/,post.md", first.stdout)
        self.assertIn("/old-toml,/toml-new.html,post.md", first.stdout)
        self.assertIn("/legacy,/new-page/,post.md", first.stdout)
        self.assertIn("detected 4 mislinks", first.stderr)
        self.assertIn("Modified 1 files with 4 replacements.", first.stderr)

        updated = post_path.read_text(encoding="utf-8")
        self.assertIn("[Old page](/new-page/)", updated)
        self.assertIn('[Old toml](/toml-new.html "Title")', updated)
        self.assertIn("[legacy-ref]: /new-page/", updated)
        self.assertIn('<a href="/new-page/">Legacy link</a>', updated)
        self.assertIn("Mention /old-page/ in plain text should not count.", updated)

        second = self.run_checker(root, ["--modify"])
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertEqual(second.stdout.strip(), "Alias,Use Instead,Found In")
        self.assertIn("detected 0 mislinks", second.stderr)
        self.assertIn("Modified 0 files with 0 replacements.", second.stderr)


if __name__ == "__main__":
    unittest.main()
