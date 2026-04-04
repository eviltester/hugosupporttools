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

    def make_hugo_site_dirs(self, case_name: str):
        root = self.make_case_dir(case_name)
        site_root = root / "site"
        content_dir = site_root / "content"
        static_dir = site_root / "static"
        content_dir.mkdir(parents=True, exist_ok=True)
        return site_root, content_dir, static_dir

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

    def test_urls_flag_detects_html_against_url(self):
        root = self.make_case_dir("urls-html")

        (root / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                url: /mypage
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )

        (root / "post.md").write_text(
            textwrap.dedent(
                """\
                [Inline](/mypage.html)
                [ref-link]: /mypage.html
                <a href="/mypage.html">HTML Link</a>
                """
            ),
            encoding="utf-8",
        )

        result = self.run_checker(root, ["--urls"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("/mypage.html,/mypage/,post.md", result.stdout)
        self.assertIn("detected 3 mislinks", result.stderr)

    def test_urls_flag_detects_slashless_against_trailing_slash_url(self):
        root = self.make_case_dir("urls-slashless")

        (root / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                url: /mypage
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )

        (root / "post.md").write_text(
            textwrap.dedent(
                """\
                [Inline](/mypage)
                [ref-link]: /mypage
                <a href="/mypage">HTML Link</a>
                """
            ),
            encoding="utf-8",
        )

        result = self.run_checker(root, ["--urls"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("/mypage,/mypage/,post.md", result.stdout)
        self.assertIn("detected 3 mislinks", result.stderr)

    def test_modify_with_urls_rewrites_slashless_to_trailing_slash(self):
        root = self.make_case_dir("urls-modify-slashless")

        (root / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                url: /mypage
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )

        post_path = root / "post.md"
        post_path.write_text(
            textwrap.dedent(
                """\
                [Inline](/mypage)
                [ref-link]: /mypage
                <a href="/mypage">HTML Link</a>
                """
            ),
            encoding="utf-8",
        )

        first = self.run_checker(root, ["--modify", "--urls"])
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertIn("/mypage,/mypage/,post.md", first.stdout)
        self.assertIn("detected 3 mislinks", first.stderr)
        self.assertIn("Modified 1 files with 3 replacements.", first.stderr)

        updated = post_path.read_text(encoding="utf-8")
        self.assertIn("[Inline](/mypage/)", updated)
        self.assertIn("[ref-link]: /mypage/", updated)
        self.assertIn('<a href="/mypage/">HTML Link</a>', updated)

        second = self.run_checker(root, ["--modify", "--urls"])
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertEqual(second.stdout.strip(), "Alias,Use Instead,Found In")
        self.assertIn("detected 0 mislinks", second.stderr)
        self.assertIn("Modified 0 files with 0 replacements.", second.stderr)

    def test_modify_with_urls_rewrites_html_references_and_is_idempotent(self):
        root = self.make_case_dir("urls-modify")

        (root / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                url: /mypage.html
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )

        post_path = root / "post.md"
        post_path.write_text(
            textwrap.dedent(
                """\
                [Inline](/mypage.html)
                [ref-link]: /mypage.html
                <a href="/mypage.html">HTML Link</a>
                Plain /mypage.html text should not change.
                """
            ),
            encoding="utf-8",
        )

        first = self.run_checker(root, ["--modify", "--urls"])
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertIn("/mypage.html,/mypage/,post.md", first.stdout)
        self.assertIn("detected 3 mislinks", first.stderr)
        self.assertIn("Modified 2 files with 4 replacements.", first.stderr)

        updated = post_path.read_text(encoding="utf-8")
        self.assertIn("[Inline](/mypage/)", updated)
        self.assertIn("[ref-link]: /mypage/", updated)
        self.assertIn('<a href="/mypage/">HTML Link</a>', updated)
        self.assertIn("Plain /mypage.html text should not change.", updated)
        page_updated = (root / "page.md").read_text(encoding="utf-8")
        self.assertIn("url: /mypage", page_updated)
        self.assertNotIn("url: /mypage/", page_updated)
        self.assertNotIn("url: /mypage.html", page_updated)

        second = self.run_checker(root, ["--modify", "--urls"])
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertEqual(second.stdout.strip(), "Alias,Use Instead,Found In")
        self.assertIn("detected 0 mislinks", second.stderr)
        self.assertIn("Modified 0 files with 0 replacements.", second.stderr)

    def test_redirects_dry_run_reports_without_writing_files(self):
        _, content_dir, static_dir = self.make_hugo_site_dirs("redirects-dry-run")

        (content_dir / "new-page.md").write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                  - /old-page/
                ---
                New page.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "post.md").write_text("[Link](/old-page/)\n", encoding="utf-8")

        result = self.run_checker(content_dir, ["--redirects"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("Alias,Use Instead,Found In", result.stdout)
        self.assertIn("/old-page,/new-page/,post.md", result.stdout)
        self.assertIn("Dry-run redirect migration", result.stderr)
        self.assertIn("Would add 1 redirects to static/_redirects", result.stderr)
        self.assertIn("Would add 1 redirects to static/.htaccess", result.stderr)
        self.assertIn("Would remove aliases from 1 content files", result.stderr)
        self.assertIn("- /old-page /new-page/ 301", result.stderr)

        self.assertFalse((static_dir / "_redirects").exists())
        self.assertFalse((static_dir / ".htaccess").exists())
        self.assertIn("aliases:", (content_dir / "new-page.md").read_text(encoding="utf-8"))

    def test_modify_redirects_writes_files_and_removes_aliases(self):
        _, content_dir, static_dir = self.make_hugo_site_dirs("redirects-modify")

        (content_dir / "yaml-page.md").write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                  - /old-yaml/
                ---
                YAML page.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "toml-page.md").write_text(
            textwrap.dedent(
                """\
                +++
                aliases = ["/old-toml/"]
                +++
                TOML page.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "json-page.md").write_text(
            textwrap.dedent(
                """\
                {"aliases":["/old-json/"],"slug":"json-slug"}
                JSON page.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "post.md").write_text(
            textwrap.dedent(
                """\
                [YAML](/old-yaml/)
                [TOML](/old-toml/)
                [JSON](/old-json/)
                """
            ),
            encoding="utf-8",
        )

        first = self.run_checker(content_dir, ["--modify", "--redirects"])
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertIn("Adding 3 redirects to static/_redirects", first.stderr)
        self.assertIn("Adding 3 redirects to static/.htaccess", first.stderr)
        self.assertIn("Redirect migration wrote 2 static files and removed aliases from 3 content files.", first.stderr)

        redirects_text = (static_dir / "_redirects").read_text(encoding="utf-8")
        self.assertIn("/old-yaml /yaml-page/ 301", redirects_text)
        self.assertIn("/old-toml /toml-page/ 301", redirects_text)
        self.assertIn("/old-json /json-slug/ 301", redirects_text)

        htaccess_text = (static_dir / ".htaccess").read_text(encoding="utf-8")
        self.assertIn("Redirect 301 /old-yaml /yaml-page/", htaccess_text)
        self.assertIn("Redirect 301 /old-toml /toml-page/", htaccess_text)
        self.assertIn("Redirect 301 /old-json /json-slug/", htaccess_text)

        self.assertNotIn("aliases:", (content_dir / "yaml-page.md").read_text(encoding="utf-8"))
        self.assertNotIn("aliases =", (content_dir / "toml-page.md").read_text(encoding="utf-8"))
        self.assertNotIn('"aliases"', (content_dir / "json-page.md").read_text(encoding="utf-8"))

        second = self.run_checker(content_dir, ["--modify", "--redirects"])
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertIn("No new redirects needed for static/_redirects.", second.stderr)
        self.assertIn("No new redirects needed for static/.htaccess.", second.stderr)
        self.assertIn("Redirect migration wrote 0 static files and removed aliases from 0 content files.", second.stderr)
        self.assertEqual(redirects_text, (static_dir / "_redirects").read_text(encoding="utf-8"))
        self.assertEqual(htaccess_text, (static_dir / ".htaccess").read_text(encoding="utf-8"))

    def test_modify_redirects_removes_unindented_yaml_alias_list(self):
        _, content_dir, _ = self.make_hugo_site_dirs("redirects-unindented-yaml")

        page_path = content_dir / "page.md"
        page_path.write_text(
            textwrap.dedent(
                """\
                ---
                aliases:
                - /2013/10/london-tester-gathering-2013-workshops.html
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "post.md").write_text(
            "[Legacy](/2013/10/london-tester-gathering-2013-workshops.html)\n",
            encoding="utf-8",
        )

        result = self.run_checker(content_dir, ["--modify", "--redirects"])
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn(
            "/2013/10/london-tester-gathering-2013-workshops.html,/page/,post.md",
            result.stdout,
        )

        updated = page_path.read_text(encoding="utf-8")
        self.assertNotIn("aliases:", updated)
        self.assertNotIn("/2013/10/london-tester-gathering-2013-workshops.html", updated)

    def test_redirects_with_urls_only_migrates_explicit_aliases(self):
        _, content_dir, _ = self.make_hugo_site_dirs("redirects-with-urls")

        (content_dir / "page.md").write_text(
            textwrap.dedent(
                """\
                ---
                url: /mypage
                aliases:
                  - /old-page/
                ---
                Page body.
                """
            ),
            encoding="utf-8",
        )
        (content_dir / "post.md").write_text(
            textwrap.dedent(
                """\
                [HTML URL](/mypage.html)
                [Legacy](/old-page/)
                """
            ),
            encoding="utf-8",
        )

        result = self.run_checker(content_dir, ["--urls", "--redirects"])
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("/mypage.html,/mypage/,post.md", result.stdout)
        self.assertIn("/old-page,/mypage/,post.md", result.stdout)
        self.assertIn("- /old-page /mypage/ 301", result.stderr)
        self.assertNotIn("/mypage.html /mypage/ 301", result.stderr)
        self.assertNotIn("/mypage /mypage/ 301", result.stderr)


if __name__ == "__main__":
    unittest.main()
