#!/usr/bin/env python3
import argparse
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, TextIO

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore


IMAGE_EXTENSIONS = (".jpg", ".png")


@dataclass(frozen=True)
class BlogThumbConfig:
    blog_dir: Path
    image_dir: Path
    output_dir: Path
    magick_command: str = "magick"
    width: int = 400
    height: int = 300
    background_colour: str = "red"
    text_colour: str = "white"


@dataclass(frozen=True)
class HugoPost:
    path: Path
    title: str
    output_filename: str
    has_image: bool


@dataclass(frozen=True)
class GenerationSummary:
    posts_found: int
    source_images_found: int
    generated: int
    skipped_with_image: int
    skipped_existing: int


CommandRunner = Callable[[Sequence[str], Path], None]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate missing Hugo blog post thumbnail images from random source images."
    )
    parser.add_argument("--blog-dir", required=True, help="Hugo blog content folder to scan recursively.")
    parser.add_argument("--image-dir", required=True, help="Folder containing source .jpg and .png images.")
    parser.add_argument("--output-dir", required=True, help="Existing folder where generated thumbnails are written.")
    parser.add_argument("--magick-command", default="magick", help="ImageMagick command to run (default: magick).")
    parser.add_argument("--width", type=int, default=400, help="Output thumbnail width in pixels (default: 400).")
    parser.add_argument("--height", type=int, default=300, help="Output thumbnail height in pixels (default: 300).")
    parser.add_argument("--background-colour", default="red", help="Caption undercolor (default: red).")
    parser.add_argument("--text-colour", default="white", help="Caption text colour (default: white).")
    parser.add_argument("--seed", type=int, help="Optional random seed for repeatable source image selection.")
    return parser.parse_args(argv)


def validate_directory(value: str, label: str, errors: List[str]) -> Optional[Path]:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        errors.append(f"{label} does not exist: {path}")
        return None
    if not path.is_dir():
        errors.append(f"{label} is not a directory: {path}")
        return None
    return path


def config_from_args(args: argparse.Namespace) -> tuple[Optional[BlogThumbConfig], List[str]]:
    errors: List[str] = []
    blog_dir = validate_directory(args.blog_dir, "blog directory", errors)
    image_dir = validate_directory(args.image_dir, "image directory", errors)
    output_dir = validate_directory(args.output_dir, "output directory", errors)

    if args.width <= 100:
        errors.append("width must be greater than 100 so the caption area can be calculated.")
    if args.height <= 100:
        errors.append("height must be greater than 100 so the caption area can be calculated.")

    if errors or blog_dir is None or image_dir is None or output_dir is None:
        return None, errors

    return (
        BlogThumbConfig(
            blog_dir=blog_dir,
            image_dir=image_dir,
            output_dir=output_dir,
            magick_command=args.magick_command,
            width=args.width,
            height=args.height,
            background_colour=args.background_colour,
            text_colour=args.text_colour,
        ),
        errors,
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def collect_markdown_files(blog_dir: Path) -> List[Path]:
    return sorted(path for path in blog_dir.rglob("*") if path.is_file() and path.name.endswith(".md"))


def collect_source_images(image_dir: Path) -> List[Path]:
    return sorted(
        path
        for path in image_dir.rglob("*")
        if path.is_file() and path.name.endswith(IMAGE_EXTENSIONS)
    )


def extract_yaml_front_matter(raw_text: str) -> str:
    text = raw_text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def parse_front_matter(raw_text: str) -> Dict[str, object]:
    yaml_text = extract_yaml_front_matter(raw_text)
    if not yaml_text:
        return {}

    if yaml is not None:
        try:
            loaded = yaml.safe_load(yaml_text)
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass

    return parse_yaml_title_subset(yaml_text)


def parse_yaml_title_subset(yaml_text: str) -> Dict[str, object]:
    fields: Dict[str, object] = {}
    for raw_line in yaml_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in ("title", "h1", "image"):
            continue
        fields[key] = strip_wrapping_quotes(value.strip())
    return fields


def strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def title_from_front_matter(fields: Dict[str, object]) -> str:
    title = ""
    if "title" in fields:
        title = str(fields["title"])
    if "h1" in fields:
        title = str(fields["h1"])
    return title.replace("'", "")


def has_front_matter_image(fields: Dict[str, object]) -> bool:
    return bool(fields.get("image"))


def post_from_markdown(path: Path) -> HugoPost:
    fields = parse_front_matter(read_text(path))
    return HugoPost(
        path=path,
        title=title_from_front_matter(fields),
        output_filename=path.name[:-3] + ".jpg",
        has_image=has_front_matter_image(fields),
    )


def collect_hugo_posts(blog_dir: Path) -> List[HugoPost]:
    return [post_from_markdown(path) for path in collect_markdown_files(blog_dir)]


def sanitize_title_for_magick(title: str) -> str:
    return title.replace("'", "").replace('"', "")


def build_magick_command(
    input_image: Path,
    title: str,
    output_filename: str,
    config: BlogThumbConfig,
) -> List[str]:
    clean_title = sanitize_title_for_magick(title)
    return [
        config.magick_command,
        str(input_image),
        "-resize",
        f"{config.width}x{config.height}",
        "-gravity",
        "center",
        "-extent",
        f"{config.width}x{config.height}",
        "-background",
        "none",
        "-stroke",
        config.text_colour,
        "-fill",
        config.text_colour,
        "-size",
        f"{config.width - 100}x{config.height - 100}",
        "-undercolor",
        config.background_colour,
        "-gravity",
        "Center",
        f"caption:{clean_title}",
        "-channel",
        "A",
        "-evaluate",
        "multiply",
        "0.8",
        "+channel",
        "-composite",
        "-strip",
        "-interlace",
        "Plane",
        "-gaussian-blur",
        "0.05",
        "-quality",
        "85%",
        output_filename,
    ]


def default_command_runner(command: Sequence[str], cwd: Path) -> None:
    subprocess.run(list(command), cwd=str(cwd), check=True)


def generate_image_from(
    input_image: Path,
    title: str,
    output_filename: str,
    config: BlogThumbConfig,
    command_runner: CommandRunner = default_command_runner,
) -> None:
    command = build_magick_command(input_image, title, output_filename, config)
    command_runner(command, config.output_dir)


def generate_blog_thumbs(
    config: BlogThumbConfig,
    rng: random.Random,
    command_runner: CommandRunner = default_command_runner,
    out: TextIO = sys.stdout,
) -> GenerationSummary:
    posts = collect_hugo_posts(config.blog_dir)
    source_images = collect_source_images(config.image_dir)

    generated = 0
    skipped_with_image = 0
    skipped_existing = 0

    for post in posts:
        if post.has_image:
            skipped_with_image += 1
            continue

        output_path = config.output_dir / post.output_filename
        if output_path.exists():
            skipped_existing += 1
            continue

        if not source_images:
            raise ValueError(f"No source .jpg or .png images found in {config.image_dir}")

        selected_image = rng.choice(source_images)
        blog_filename = post.path.relative_to(config.blog_dir).as_posix()
        print(f"Blog file: {blog_filename}", file=out)
        print("I choose", file=out)
        print(selected_image, file=out)
        print(f"Generating {post.output_filename}", file=out)
        generate_image_from(
            selected_image,
            post.title,
            post.output_filename,
            config,
            command_runner=command_runner,
        )
        generated += 1

    return GenerationSummary(
        posts_found=len(posts),
        source_images_found=len(source_images),
        generated=generated,
        skipped_with_image=skipped_with_image,
        skipped_existing=skipped_existing,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config, errors = config_from_args(args)
    if errors or config is None:
        for error in errors:
            print(f"Error: {error}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    try:
        summary = generate_blog_thumbs(config, rng)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    except FileNotFoundError as err:
        print(f"Error running ImageMagick command: {err}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as err:
        print(f"ImageMagick command failed with exit code {err.returncode}", file=sys.stderr)
        return 1

    print(
        "\n"
        f"Scanned {summary.posts_found} markdown files, "
        f"found {summary.source_images_found} source images, "
        f"generated {summary.generated} thumbnails, "
        f"skipped {summary.skipped_with_image} posts with front matter image, "
        f"skipped {summary.skipped_existing} existing thumbnails.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
