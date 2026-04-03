#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, TextIO, Tuple
from urllib.parse import urlparse

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore


INLINE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)|\[[^\]]*\]\(([^)]+)\)")
REFERENCE_LINK_RE = re.compile(r"^(\s*\[[^\]]+\]:\s*)(.+?)(\s*)$", re.MULTILINE)
HTML_LINK_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    alias: str
    canonical_url: str
    file_path: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Hugo markdown content for links that use legacy aliases instead "
            "of canonical URLs."
        )
    )
    parser.add_argument("content_dir", help="Path to Hugo content directory to scan recursively.")
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=[".md", ".markdown"],
        help="File extensions to scan (default: .md .markdown).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show additional diagnostics such as duplicate aliases.",
    )
    parser.add_argument(
        "--modify",
        action="store_true",
        help="Rewrite detected alias link targets in place with canonical URLs.",
    )
    parser.add_argument(
        "--urls",
        action="store_true",
        help=(
            "Treat front matter url values ending in .html as aliases and map them "
            "to directory-style URLs (for example /page.html -> /page/)."
        ),
    )
    return parser.parse_args(argv)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_front_matter(raw_text: str) -> Tuple[dict, str]:
    text = raw_text.lstrip("\ufeff")
    if text.startswith("---\n"):
        return parse_yaml_front_matter(text)
    if text.startswith("+++\n"):
        return parse_toml_front_matter(text)
    if text.startswith("{"):
        parsed = parse_json_front_matter(text)
        if parsed is not None:
            return parsed
    return {}, raw_text


def parse_yaml_front_matter(text: str) -> Tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        marker = lines[i].strip()
        if marker in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    return parse_yaml_subset(fm_text), body


def parse_yaml_subset(fm_text: str) -> dict:
    result: dict = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            items: List[str] = []
            while i < len(lines):
                nxt = lines[i].rstrip()
                stripped = nxt.strip()
                if not stripped:
                    i += 1
                    continue
                if stripped.startswith("-"):
                    item = stripped[1:].strip()
                    items.append(strip_wrapping_quotes(item))
                    i += 1
                    continue
                break
            result[key] = items
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [strip_wrapping_quotes(part.strip()) for part in inner.split(",")]
            continue
        result[key] = strip_wrapping_quotes(value)
    return result


def parse_toml_front_matter(text: str) -> Tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    if tomllib is None:
        raise RuntimeError("TOML front matter detected but tomllib is unavailable on this Python.")
    try:
        data = tomllib.loads(fm_text)
    except Exception:
        data = {}
    return data, body


def parse_json_front_matter(text: str) -> Optional[Tuple[dict, str]]:
    depth = 0
    in_str = False
    escaped = False
    end_idx = -1
    for idx, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = idx
                break
    if end_idx == -1:
        return None
    front = text[: end_idx + 1]
    remainder = text[end_idx + 1 :]
    if remainder and not remainder.startswith("\n") and not remainder.startswith("\r\n"):
        return None
    try:
        data = json.loads(front)
    except Exception:
        return None
    body = remainder.lstrip("\r\n")
    return data, body


def strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def normalize_path_candidate(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    lower = value.lower()
    if lower.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None

    parsed = urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    if not path:
        path = "/"

    if path.startswith("//"):
        path = "/" + path.lstrip("/")
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    path = path.strip()
    if path != "/":
        path = path.rstrip("/")
    if not path:
        path = "/"
    return path


def collect_markdown_paths(content_dir: Path, extensions: Iterable[str]) -> List[Path]:
    allowed = {ext if ext.startswith(".") else f".{ext}" for ext in extensions}
    allowed = {ext.lower() for ext in allowed}
    return sorted(
        [
            path
            for path in content_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in allowed
        ]
    )


def extract_aliases(front_matter: dict) -> List[str]:
    aliases = front_matter.get("aliases")
    if aliases is None:
        return []
    if isinstance(aliases, str):
        return [aliases]
    if isinstance(aliases, list):
        return [str(item) for item in aliases if str(item).strip()]
    return []


def derive_url_alias(front_matter: dict) -> Optional[Tuple[str, str]]:
    raw_url = front_matter.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    normalized = normalize_path_candidate(raw_url)
    if not normalized or not normalized.lower().endswith(".html"):
        return None
    canonical = ensure_directory_style(normalized[:-5])
    return normalized, canonical


def rewrite_front_matter_url_html(raw_text: str) -> Tuple[str, int]:
    bom_len = len(raw_text) - len(raw_text.lstrip("\ufeff"))
    bom = raw_text[:bom_len]
    text = raw_text[bom_len:]

    if text.startswith("---\n"):
        lines = text.splitlines(keepends=True)
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                end = i
                break
        if end is None:
            return raw_text, 0
        changed = 0
        for i in range(1, end):
            line = lines[i]
            match = re.match(r"^(\s*url\s*:\s*)(['\"]?)([^\"'\n#]+?)(\2)(\s*(?:#.*)?)$", line.rstrip("\r\n"))
            if not match:
                continue
            current = match.group(3).strip()
            normalized = normalize_path_candidate(current)
            if not normalized or not normalized.lower().endswith(".html"):
                continue
            updated = normalized[:-5] or "/"
            new_line = f"{match.group(1)}{match.group(2)}{updated}{match.group(4)}{match.group(5)}"
            line_ending = line[len(line.rstrip('\r\n')) :]
            lines[i] = f"{new_line}{line_ending}"
            changed += 1
            break
        if changed:
            return f"{bom}{''.join(lines)}", changed
        return raw_text, 0

    if text.startswith("+++\n"):
        lines = text.splitlines(keepends=True)
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "+++":
                end = i
                break
        if end is None:
            return raw_text, 0
        changed = 0
        for i in range(1, end):
            line = lines[i]
            match = re.match(r"^(\s*url\s*=\s*)(['\"])([^\"'\n#]+?)(\2)(\s*(?:#.*)?)$", line.rstrip("\r\n"))
            if not match:
                continue
            current = match.group(3).strip()
            normalized = normalize_path_candidate(current)
            if not normalized or not normalized.lower().endswith(".html"):
                continue
            updated = normalized[:-5] or "/"
            new_line = f"{match.group(1)}{match.group(2)}{updated}{match.group(4)}{match.group(5)}"
            line_ending = line[len(line.rstrip('\r\n')) :]
            lines[i] = f"{new_line}{line_ending}"
            changed += 1
            break
        if changed:
            return f"{bom}{''.join(lines)}", changed
        return raw_text, 0

    parsed = parse_json_front_matter(text)
    if parsed is None:
        return raw_text, 0
    front_matter, body = parsed
    raw_url = front_matter.get("url")
    if not isinstance(raw_url, str):
        return raw_text, 0
    normalized = normalize_path_candidate(raw_url)
    if not normalized or not normalized.lower().endswith(".html"):
        return raw_text, 0
    front_matter["url"] = normalized[:-5] or "/"
    json_front = json.dumps(front_matter, separators=(",", ":"))
    new_text = f"{bom}{json_front}"
    if body:
        new_text = f"{new_text}\n{body}"
    return new_text, 1


def resolve_canonical_url(front_matter: dict, file_path: Path, content_dir: Path) -> str:
    explicit_url = front_matter.get("url")
    if isinstance(explicit_url, str) and explicit_url.strip():
        normalized = normalize_path_candidate(explicit_url)
        if normalized:
            return ensure_directory_style(normalized)

    slug = front_matter.get("slug")
    slug = slug.strip("/") if isinstance(slug, str) and slug.strip() else None

    relative = file_path.relative_to(content_dir).with_suffix("")
    parts = list(relative.parts)
    if not parts:
        return "/"

    stem = parts[-1]
    dirs = parts[:-1]

    if stem == "_index":
        if slug and dirs:
            dirs = dirs[:-1] + [slug]
        elif slug and not dirs:
            dirs = [slug]
        path = "/" + "/".join(dirs) if dirs else "/"
    elif stem == "index":
        if slug and dirs:
            dirs = dirs[:-1] + [slug]
        elif slug and not dirs:
            dirs = [slug]
        path = "/" + "/".join(dirs) if dirs else "/"
    else:
        final = slug if slug else stem
        joined = dirs + [final]
        path = "/" + "/".join(joined)

    return ensure_directory_style(path)


def ensure_directory_style(path: str) -> str:
    if path == "/":
        return path
    if path.lower().endswith(".html"):
        return path.rstrip("/")
    return path.rstrip("/") + "/"


def extract_link_targets(markdown_body: str) -> List[str]:
    targets: List[str] = []

    for match in INLINE_LINK_RE.finditer(markdown_body):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if not raw:
            continue
        target = clean_link_target(raw)
        if target:
            targets.append(target)

    for match in REFERENCE_LINK_RE.finditer(markdown_body):
        raw = match.group(1)
        target = clean_link_target(raw)
        if target:
            targets.append(target)

    for match in HTML_LINK_RE.finditer(markdown_body):
        raw = next(group for group in match.groups() if group is not None)
        target = clean_link_target(raw)
        if target:
            targets.append(target)

    return targets


def clean_link_target(raw: str) -> Optional[str]:
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.startswith("<") and ">" in candidate:
        candidate = candidate[1 : candidate.index(">")].strip()
    else:
        candidate = candidate.split(maxsplit=1)[0]
    if candidate.startswith(("'", '"')) and candidate.endswith(("'", '"')) and len(candidate) >= 2:
        candidate = candidate[1:-1]
    if not candidate:
        return None
    return candidate


def split_markdown_target_and_suffix(raw_value: str) -> Tuple[Optional[str], str, Optional[str], str, str]:
    leading_len = len(raw_value) - len(raw_value.lstrip())
    trailing_len = len(raw_value) - len(raw_value.rstrip())
    leading_ws = raw_value[:leading_len]
    trailing_ws = raw_value[len(raw_value) - trailing_len :] if trailing_len else ""
    core = raw_value.strip()
    if not core:
        return None, "", None, leading_ws, trailing_ws

    wrapper: Optional[str] = None
    suffix = ""
    target = core

    if core.startswith("<") and ">" in core:
        close_idx = core.find(">")
        wrapper = "angle"
        target = core[1:close_idx].strip()
        suffix = core[close_idx + 1 :]
    else:
        parts = core.split(maxsplit=1)
        target = parts[0]
        suffix = f" {parts[1]}" if len(parts) > 1 else ""
        if len(target) >= 2 and target[0] == target[-1] and target[0] in ("'", '"'):
            wrapper = target[0]
            target = target[1:-1]

    return target, suffix, wrapper, leading_ws, trailing_ws


def build_markdown_target_value(
    replacement_target: str,
    suffix: str,
    wrapper: Optional[str],
    leading_ws: str,
    trailing_ws: str,
) -> str:
    target = replacement_target
    if wrapper == "angle":
        target = f"<{replacement_target}>"
    elif wrapper in ("'", '"'):
        target = f"{wrapper}{replacement_target}{wrapper}"
    return f"{leading_ws}{target}{suffix}{trailing_ws}"


def find_and_optionally_modify_body(
    body: str,
    alias_to_url: Dict[str, str],
    modify: bool,
) -> Tuple[List[Tuple[str, str]], str, int]:
    occurrences: List[Tuple[str, str]] = []
    replacements = 0

    def process_markdown_value(raw_value: str) -> Tuple[bool, str]:
        nonlocal replacements
        target, suffix, wrapper, leading_ws, trailing_ws = split_markdown_target_and_suffix(raw_value)
        if not target:
            return False, raw_value
        normalized_target = normalize_path_candidate(target)
        if not normalized_target:
            return False, raw_value
        canonical = alias_to_url.get(normalized_target)
        if not canonical:
            return False, raw_value

        occurrences.append((normalized_target, canonical))
        if not modify:
            return False, raw_value
        updated = build_markdown_target_value(canonical, suffix, wrapper, leading_ws, trailing_ws)
        if updated != raw_value:
            replacements += 1
            return True, updated
        return False, raw_value

    def inline_repl(match: re.Match[str]) -> str:
        group_idx = 1 if match.group(1) is not None else 2
        raw_value = match.group(group_idx)
        changed, updated = process_markdown_value(raw_value)
        if not changed:
            return match.group(0)
        whole = match.group(0)
        value_start = match.start(group_idx) - match.start(0)
        value_end = match.end(group_idx) - match.start(0)
        return f"{whole[:value_start]}{updated}{whole[value_end:]}"

    def reference_repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        raw_value = match.group(2)
        suffix_ws = match.group(3)
        changed, updated = process_markdown_value(raw_value)
        if not changed:
            return match.group(0)
        return f"{prefix}{updated}{suffix_ws}"

    def html_repl(match: re.Match[str]) -> str:
        nonlocal replacements
        group_idx = 1
        for idx in (1, 2, 3):
            if match.group(idx) is not None:
                group_idx = idx
                break
        raw_value = match.group(group_idx)
        normalized_target = normalize_path_candidate(raw_value)
        if not normalized_target:
            return match.group(0)
        canonical = alias_to_url.get(normalized_target)
        if not canonical:
            return match.group(0)

        occurrences.append((normalized_target, canonical))
        if not modify:
            return match.group(0)

        whole = match.group(0)
        value_start = match.start(group_idx) - match.start(0)
        value_end = match.end(group_idx) - match.start(0)
        updated = f"{whole[:value_start]}{canonical}{whole[value_end:]}"
        if updated != whole:
            replacements += 1
        return updated

    updated_body = INLINE_LINK_RE.sub(inline_repl, body)
    updated_body = REFERENCE_LINK_RE.sub(reference_repl, updated_body)
    updated_body = HTML_LINK_RE.sub(html_repl, updated_body)
    return occurrences, updated_body, replacements


def print_csv(findings: List[Finding], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(["Alias", "Use Instead", "Found In"])
    for finding in findings:
        writer.writerow([finding.alias, finding.canonical_url, finding.file_path])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    content_dir = Path(args.content_dir).resolve()
    if not content_dir.exists() or not content_dir.is_dir():
        print(f"Error: content directory does not exist or is not a directory: {content_dir}", file=sys.stderr)
        return 2

    markdown_files = collect_markdown_paths(content_dir, args.extensions)
    alias_to_url: Dict[str, str] = {}
    duplicates: Dict[str, List[str]] = {}
    parsed_bodies: Dict[Path, str] = {}
    raw_texts: Dict[Path, str] = {}

    for md in markdown_files:
        text = read_text(md)
        raw_texts[md] = text
        front_matter, body = parse_front_matter(text)
        parsed_bodies[md] = body
        canonical_url = resolve_canonical_url(front_matter, md, content_dir)
        aliases = extract_aliases(front_matter)
        for alias in aliases:
            normalized_alias = normalize_path_candidate(alias)
            if not normalized_alias:
                continue
            existing = alias_to_url.get(normalized_alias)
            if existing and existing != canonical_url:
                duplicates.setdefault(normalized_alias, [existing]).append(canonical_url)
                continue
            alias_to_url[normalized_alias] = canonical_url
        if args.urls:
            url_alias = derive_url_alias(front_matter)
            if url_alias:
                alias, canonical = url_alias
                existing = alias_to_url.get(alias)
                if existing and existing != canonical:
                    duplicates.setdefault(alias, [existing]).append(canonical)
                else:
                    alias_to_url[alias] = canonical

    findings: List[Finding] = []
    modified_files = 0
    total_replacements = 0
    for md in markdown_files:
        body = parsed_bodies[md]
        rel_path = md.relative_to(content_dir).as_posix()
        occurrences, new_body, replacements = find_and_optionally_modify_body(
            body=body,
            alias_to_url=alias_to_url,
            modify=args.modify,
        )
        for alias, canonical in occurrences:
            findings.append(Finding(alias=alias, canonical_url=canonical, file_path=rel_path))

        if args.modify:
            raw_text = raw_texts[md]
            new_text = raw_text
            metadata_updates = 0

            if args.urls:
                new_text, metadata_updates = rewrite_front_matter_url_html(new_text)

            if replacements > 0:
                prefix = new_text[: len(new_text) - len(body)] if body else new_text
                new_text = f"{prefix}{new_body}"

            if new_text != raw_text:
                md.write_text(new_text, encoding="utf-8", errors="replace")
                modified_files += 1
                total_replacements += replacements + metadata_updates

    print_csv(findings, sys.stdout)
    print(
        f"\nScanned {len(markdown_files)} markdown files, "
        f"found {len(alias_to_url)} aliases, detected {len(findings)} mislinks.",
        file=sys.stderr,
    )
    if args.modify:
        print(
            f"Modified {modified_files} files with {total_replacements} replacements.",
            file=sys.stderr,
        )

    if args.verbose and duplicates:
        print("\nDuplicate aliases mapped to multiple canonical URLs:", file=sys.stderr)
        for alias, urls in sorted(duplicates.items()):
            unique_urls = sorted(set(urls))
            print(f"- {alias} -> {', '.join(unique_urls)}", file=sys.stderr)

    if args.modify:
        return 0
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
