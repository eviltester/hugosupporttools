#!/usr/bin/env python3
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, TextIO, Tuple
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


@dataclass(frozen=True)
class RedirectEntry:
    source: str
    target: str


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
    parser.add_argument(
        "--redirects",
        action="store_true",
        help=(
            "Migrate front matter aliases to static/_redirects and static/.htaccess; "
            "writes happen only with --modify."
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


def normalize_path_candidate(value: str, preserve_trailing_slash: bool = False) -> Optional[str]:
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
    if path != "/" and not preserve_trailing_slash:
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


def derive_url_aliases(front_matter: dict) -> List[Tuple[str, str]]:
    raw_url = front_matter.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return []
    normalized = normalize_path_candidate(raw_url)
    if not normalized:
        return []

    if normalized == "/":
        return []

    if normalized.lower().endswith(".html"):
        base = normalized[:-5] or "/"
    else:
        base = normalized

    canonical = ensure_directory_style(base)
    if canonical == "/":
        return []

    aliases: List[Tuple[str, str]] = []
    slashless = canonical.rstrip("/")
    aliases.append((slashless, canonical))
    aliases.append((f"{slashless}.html", canonical))
    return aliases


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


def remove_front_matter_aliases(raw_text: str) -> Tuple[str, int, Optional[str]]:
    bom_len = len(raw_text) - len(raw_text.lstrip("\ufeff"))
    bom = raw_text[:bom_len]
    text = raw_text[bom_len:]

    if text.startswith("---\n"):
        return remove_yaml_aliases(raw_text, bom, text)
    if text.startswith("+++\n"):
        return remove_toml_aliases(raw_text, bom, text)
    if text.startswith("{"):
        return remove_json_aliases(raw_text, bom, text)
    return raw_text, 0, "unsupported front matter format"


def remove_yaml_aliases(raw_text: str, bom: str, text: str) -> Tuple[str, int, Optional[str]]:
    lines = text.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return raw_text, 0, "invalid yaml front matter"

    aliases_line = None
    block_end = None
    for i in range(1, end):
        line = lines[i]
        line_content = line.rstrip("\r\n")
        match = re.match(r"^([ \t]*)aliases\s*:\s*(.*?)(\s*(?:#.*)?)$", line_content)
        if not match:
            continue
        aliases_line = i
        indent = len(match.group(1))
        value = match.group(2).strip()

        if not value:
            j = i + 1
            while j < end:
                nxt_content = lines[j].rstrip("\r\n")
                stripped = nxt_content.strip()
                if not stripped:
                    j += 1
                    continue
                nxt_indent = len(nxt_content) - len(nxt_content.lstrip(" \t"))
                if nxt_indent < indent:
                    break
                if stripped.startswith("-") or stripped.startswith("#"):
                    j += 1
                    continue
                if nxt_indent == indent:
                    break
                return raw_text, 0, "unsupported aliases yaml block syntax"
            block_end = j
        else:
            if value in ("|", ">", "|-", ">-", "|+", ">+"):
                return raw_text, 0, "unsupported aliases yaml block scalar"
            block_end = i + 1
        break

    if aliases_line is None:
        return raw_text, 0, None

    del lines[aliases_line:block_end]
    return f"{bom}{''.join(lines)}", 1, None


def remove_toml_aliases(raw_text: str, bom: str, text: str) -> Tuple[str, int, Optional[str]]:
    lines = text.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "+++":
            end = i
            break
    if end is None:
        return raw_text, 0, "invalid toml front matter"

    aliases_line = None
    for i in range(1, end):
        line = lines[i]
        line_content = line.rstrip("\r\n")
        match = re.match(r"^([ \t]*aliases\s*=\s*)(.+?)(\s*(?:#.*)?)$", line_content)
        if not match:
            continue
        value = match.group(2).strip()
        if not (
            (value.startswith("[") and value.endswith("]"))
            or (len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'))
        ):
            return raw_text, 0, "unsupported aliases toml syntax"
        aliases_line = i
        break

    if aliases_line is None:
        return raw_text, 0, None

    del lines[aliases_line]
    return f"{bom}{''.join(lines)}", 1, None


def remove_json_aliases(raw_text: str, bom: str, text: str) -> Tuple[str, int, Optional[str]]:
    parsed = parse_json_front_matter(text)
    if parsed is None:
        return raw_text, 0, "invalid json front matter"
    front_matter, body = parsed
    if "aliases" not in front_matter:
        return raw_text, 0, None
    front_matter.pop("aliases", None)
    json_front = json.dumps(front_matter, separators=(",", ":"))
    new_text = f"{bom}{json_front}"
    if body:
        new_text = f"{new_text}\n{body}"
    return new_text, 1, None


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
        strict_target = normalize_path_candidate(target, preserve_trailing_slash=True)
        normalized_target = normalize_path_candidate(target)
        if not strict_target or not normalized_target:
            return False, raw_value
        alias_used = strict_target
        canonical = alias_to_url.get(strict_target)
        if canonical is None:
            alias_used = normalized_target
            canonical = alias_to_url.get(normalized_target)
        if not canonical:
            return False, raw_value
        if strict_target == canonical:
            return False, raw_value

        occurrences.append((alias_used, canonical))
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
        strict_target = normalize_path_candidate(raw_value, preserve_trailing_slash=True)
        normalized_target = normalize_path_candidate(raw_value)
        if not strict_target or not normalized_target:
            return match.group(0)
        alias_used = strict_target
        canonical = alias_to_url.get(strict_target)
        if canonical is None:
            alias_used = normalized_target
            canonical = alias_to_url.get(normalized_target)
        if not canonical:
            return match.group(0)
        if strict_target == canonical:
            return match.group(0)

        occurrences.append((alias_used, canonical))
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


def parse_existing_netlify_redirects(text: str) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 3 or parts[2] != "301":
            continue
        source = normalize_path_candidate(parts[0])
        target = normalize_path_candidate(parts[1])
        if source and target:
            pairs.add((source, target))
    return pairs


def parse_existing_htaccess_redirects(text: str) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^Redirect\s+301\s+(\S+)\s+(\S+)", stripped, re.IGNORECASE)
        if not match:
            continue
        source = normalize_path_candidate(match.group(1))
        target = normalize_path_candidate(match.group(2))
        if source and target:
            pairs.add((source, target))
    return pairs


def append_lines(path: Path, lines_to_add: List[str]) -> int:
    if not lines_to_add:
        return 0
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    needs_separator = bool(existing) and not existing.endswith(("\n", "\r"))
    payload = existing
    if needs_separator:
        payload += "\n"
    payload += "\n".join(lines_to_add) + "\n"
    path.write_text(payload, encoding="utf-8", errors="replace")
    return 1


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
    redirect_entries: List[RedirectEntry] = []
    removable_alias_files: Set[Path] = set()
    non_removable_alias_files: Set[Path] = set()

    for md in markdown_files:
        text = read_text(md)
        raw_texts[md] = text
        front_matter, body = parse_front_matter(text)
        parsed_bodies[md] = body
        canonical_url = resolve_canonical_url(front_matter, md, content_dir)
        aliases = extract_aliases(front_matter)
        successful_aliases = 0
        for alias in aliases:
            normalized_alias = normalize_path_candidate(alias)
            if not normalized_alias:
                continue
            existing = alias_to_url.get(normalized_alias)
            if existing and existing != canonical_url:
                duplicates.setdefault(normalized_alias, [existing]).append(canonical_url)
                continue
            alias_to_url[normalized_alias] = canonical_url
            successful_aliases += 1
            if args.redirects:
                redirect_entries.append(RedirectEntry(source=normalized_alias, target=canonical_url))
        if args.redirects and aliases:
            if successful_aliases == len(aliases):
                removable_alias_files.add(md)
            else:
                non_removable_alias_files.add(md)
        if args.urls:
            for alias, canonical in derive_url_aliases(front_matter):
                existing = alias_to_url.get(alias)
                if existing and existing != canonical:
                    duplicates.setdefault(alias, [existing]).append(canonical)
                else:
                    alias_to_url[alias] = canonical

    redirect_file_updates = 0
    alias_fields_removed = 0
    skipped_alias_removals: List[Tuple[str, str]] = []
    if args.redirects:
        static_dir = content_dir.parent / "static"
        netlify_path = static_dir / "_redirects"
        htaccess_path = static_dir / ".htaccess"

        unique_redirects: Dict[Tuple[str, str], RedirectEntry] = {}
        for entry in redirect_entries:
            key = (entry.source, entry.target)
            unique_redirects[key] = entry
        sorted_entries = sorted(unique_redirects.values(), key=lambda item: (item.source, item.target))

        netlify_existing = (
            parse_existing_netlify_redirects(read_text(netlify_path)) if netlify_path.exists() else set()
        )
        htaccess_existing = (
            parse_existing_htaccess_redirects(read_text(htaccess_path)) if htaccess_path.exists() else set()
        )

        missing_for_netlify: List[RedirectEntry] = []
        missing_for_htaccess: List[RedirectEntry] = []
        for entry in sorted_entries:
            normalized_key = (
                normalize_path_candidate(entry.source),
                normalize_path_candidate(entry.target),
            )
            if normalized_key[0] and normalized_key[1]:
                tuple_key = (normalized_key[0], normalized_key[1])
                if tuple_key not in netlify_existing:
                    missing_for_netlify.append(entry)
                if tuple_key not in htaccess_existing:
                    missing_for_htaccess.append(entry)

        action = "Apply" if args.modify else "Dry-run"
        print(
            f"\n{action} redirect migration in {static_dir}: "
            f"discovered {len(redirect_entries)} alias entries, "
            f"planned {len(sorted_entries)} unique redirects.",
            file=sys.stderr,
        )
        if missing_for_netlify:
            print(
                f"{'Adding' if args.modify else 'Would add'} {len(missing_for_netlify)} redirects to "
                f"{netlify_path.relative_to(content_dir.parent).as_posix()}:",
                file=sys.stderr,
            )
            for entry in missing_for_netlify:
                print(f"- {entry.source} {entry.target} 301", file=sys.stderr)
        else:
            print(f"No new redirects needed for {netlify_path.relative_to(content_dir.parent).as_posix()}.", file=sys.stderr)

        if missing_for_htaccess:
            print(
                f"{'Adding' if args.modify else 'Would add'} {len(missing_for_htaccess)} redirects to "
                f"{htaccess_path.relative_to(content_dir.parent).as_posix()}:",
                file=sys.stderr,
            )
            for entry in missing_for_htaccess:
                print(f"- Redirect 301 {entry.source} {entry.target}", file=sys.stderr)
        else:
            print(f"No new redirects needed for {htaccess_path.relative_to(content_dir.parent).as_posix()}.", file=sys.stderr)

        if removable_alias_files:
            planned_removals = sorted(md.relative_to(content_dir).as_posix() for md in removable_alias_files)
            print(
                f"{'Removing' if args.modify else 'Would remove'} aliases from "
                f"{len(planned_removals)} content files:",
                file=sys.stderr,
            )
            for rel in planned_removals:
                print(f"- {rel}", file=sys.stderr)
        else:
            print("No alias front matter entries eligible for removal.", file=sys.stderr)

        if non_removable_alias_files:
            print("Skipping alias removal for files with unmigrated aliases:", file=sys.stderr)
            for rel in sorted(md.relative_to(content_dir).as_posix() for md in non_removable_alias_files):
                print(f"- {rel}", file=sys.stderr)

        if args.modify and (missing_for_netlify or missing_for_htaccess):
            static_dir.mkdir(parents=True, exist_ok=True)
        if args.modify:
            netlify_lines = [f"{entry.source} {entry.target} 301" for entry in missing_for_netlify]
            htaccess_lines = [f"Redirect 301 {entry.source} {entry.target}" for entry in missing_for_htaccess]
            redirect_file_updates += append_lines(netlify_path, netlify_lines)
            redirect_file_updates += append_lines(htaccess_path, htaccess_lines)

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

            if args.redirects and md in removable_alias_files:
                new_text, alias_updates, alias_error = remove_front_matter_aliases(new_text)
                if alias_error:
                    skipped_alias_removals.append((rel_path, alias_error))
                else:
                    metadata_updates += alias_updates
                    alias_fields_removed += alias_updates

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
        if args.redirects:
            print(
                f"Redirect migration wrote {redirect_file_updates} static files and "
                f"removed aliases from {alias_fields_removed} content files.",
                file=sys.stderr,
            )
            if skipped_alias_removals:
                print("Could not safely remove aliases from:", file=sys.stderr)
                for rel_path, reason in skipped_alias_removals:
                    print(f"- {rel_path}: {reason}", file=sys.stderr)

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
