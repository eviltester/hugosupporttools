import importlib.util
import io
import shutil
import textwrap
import unittest
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "blogThumbs.py"
TMP_ROOT = REPO_ROOT / "tests" / ".tmp"


class FixedRandom:
    def __init__(self, index=0):
        self.index = index

    def choice(self, values):
        return values[self.index]


class BlogThumbsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        spec = importlib.util.spec_from_file_location(
            f"blog_thumbs_{uuid.uuid4().hex}",
            SCRIPT,
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.blog_thumbs = module

    def make_case_dir(self, case_name: str) -> Path:
        case_dir = TMP_ROOT / case_name
        if case_dir.exists():
            shutil.rmtree(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        return case_dir

    def make_project_dirs(self, case_name: str):
        root = self.make_case_dir(case_name)
        blog_dir = root / "blog"
        image_dir = root / "images"
        output_dir = root / "output"
        blog_dir.mkdir()
        image_dir.mkdir()
        output_dir.mkdir()
        return blog_dir, image_dir, output_dir

    def test_rejects_missing_and_invalid_directories_before_running(self):
        root = self.make_case_dir("invalid-dirs")
        file_not_dir = root / "not-a-dir"
        file_not_dir.write_text("not a directory", encoding="utf-8")

        args = self.blog_thumbs.parse_args(
            [
                "--blog-dir",
                str(root / "missing-blog"),
                "--image-dir",
                str(file_not_dir),
                "--output-dir",
                str(root / "missing-output"),
            ]
        )
        config, errors = self.blog_thumbs.config_from_args(args)

        self.assertIsNone(config)
        self.assertEqual(len(errors), 3)
        self.assertIn("blog directory does not exist", errors[0])
        self.assertIn("image directory is not a directory", errors[1])
        self.assertIn("output directory does not exist", errors[2])

    def test_generates_missing_thumbs_for_all_markdown_posts(self):
        blog_dir, image_dir, output_dir = self.make_project_dirs("generate")
        nested = blog_dir / "nested"
        nested.mkdir()
        (blog_dir / "2026-01-01-first.md").write_text(
            textwrap.dedent(
                """\
                ---
                title: "Don't Quote This"
                ---
                Body.
                """
            ),
            encoding="utf-8",
        )
        (nested / "2026-01-02-second.md").write_text(
            textwrap.dedent(
                """\
                ---
                title: "Ignored Title"
                h1: "Use The H1"
                ---
                Body.
                """
            ),
            encoding="utf-8",
        )
        (blog_dir / "ignored.txt").write_text("not markdown", encoding="utf-8")
        (image_dir / "source.jpg").write_text("fake jpg", encoding="utf-8")
        (image_dir / "source.png").write_text("fake png", encoding="utf-8")

        config = self.blog_thumbs.BlogThumbConfig(
            blog_dir=blog_dir,
            image_dir=image_dir,
            output_dir=output_dir,
            magick_command="magick-test",
        )
        commands = []

        def fake_runner(command, cwd):
            commands.append((list(command), cwd))

        stdout = io.StringIO()
        summary = self.blog_thumbs.generate_blog_thumbs(
            config,
            FixedRandom(0),
            command_runner=fake_runner,
            out=stdout,
        )

        self.assertEqual(summary.posts_found, 2)
        self.assertEqual(summary.source_images_found, 2)
        self.assertEqual(summary.generated, 2)
        self.assertEqual(summary.skipped_existing, 0)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][1], output_dir)
        self.assertEqual(commands[0][0][0], "magick-test")
        self.assertIn("caption:Dont Quote This", commands[0][0])
        self.assertEqual(commands[0][0][-1], "2026-01-01-first.jpg")
        self.assertIn("caption:Use The H1", commands[1][0])
        self.assertEqual(commands[1][0][-1], "2026-01-02-second.jpg")
        self.assertIn("Generating 2026-01-01-first.jpg", stdout.getvalue())
        self.assertIn("Generating 2026-01-02-second.jpg", stdout.getvalue())

    def test_skips_when_output_thumbnail_already_exists(self):
        blog_dir, image_dir, output_dir = self.make_project_dirs("skip-existing")
        (blog_dir / "2026-01-01-first.md").write_text(
            textwrap.dedent(
                """\
                ---
                title: Existing Thumb
                ---
                Body.
                """
            ),
            encoding="utf-8",
        )
        (image_dir / "source.jpg").write_text("fake jpg", encoding="utf-8")
        (output_dir / "2026-01-01-first.jpg").write_text("already there", encoding="utf-8")

        config = self.blog_thumbs.BlogThumbConfig(
            blog_dir=blog_dir,
            image_dir=image_dir,
            output_dir=output_dir,
        )
        commands = []
        stdout = io.StringIO()

        summary = self.blog_thumbs.generate_blog_thumbs(
            config,
            FixedRandom(0),
            command_runner=lambda command, cwd: commands.append((command, cwd)),
            out=stdout,
        )

        self.assertEqual(summary.generated, 0)
        self.assertEqual(summary.skipped_existing, 1)
        self.assertEqual(commands, [])
        self.assertIn("I choose", stdout.getvalue())
        self.assertNotIn("Generating 2026-01-01-first.jpg", stdout.getvalue())

    def test_errors_when_image_folder_contains_no_source_images(self):
        blog_dir, image_dir, output_dir = self.make_project_dirs("no-images")
        (blog_dir / "2026-01-01-first.md").write_text(
            "---\ntitle: No Images\n---\nBody.\n",
            encoding="utf-8",
        )
        config = self.blog_thumbs.BlogThumbConfig(
            blog_dir=blog_dir,
            image_dir=image_dir,
            output_dir=output_dir,
        )

        with self.assertRaisesRegex(ValueError, "No source .jpg or .png images found"):
            self.blog_thumbs.generate_blog_thumbs(
                config,
                FixedRandom(0),
                command_runner=lambda command, cwd: None,
                out=io.StringIO(),
            )

    def test_builds_imagemagick_command_with_java_defaults(self):
        blog_dir, image_dir, output_dir = self.make_project_dirs("command")
        config = self.blog_thumbs.BlogThumbConfig(
            blog_dir=blog_dir,
            image_dir=image_dir,
            output_dir=output_dir,
        )

        command = self.blog_thumbs.build_magick_command(
            Path("input.jpg"),
            'A "Quoted" Title',
            "output.jpg",
            config,
        )

        self.assertEqual(command[0], "magick")
        self.assertIn("-resize", command)
        self.assertIn("400x300", command)
        self.assertIn("-size", command)
        self.assertIn("300x200", command)
        self.assertIn("-undercolor", command)
        self.assertIn("red", command)
        self.assertIn("-fill", command)
        self.assertIn("white", command)
        self.assertIn("caption:A Quoted Title", command)
        self.assertIn("-quality", command)
        self.assertIn("85%", command)
        self.assertEqual(command[-1], "output.jpg")


if __name__ == "__main__":
    unittest.main()
