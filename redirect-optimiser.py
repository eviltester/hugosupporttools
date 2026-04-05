#!/usr/bin/env python3
import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class RedirectRule:
    source: str
    target: str
    redirect_type: str
    raw_line: str
    line_index: int
    format: str
    status_code: int
    line_ending: str
    leading: str
    keyword: str
    trailing: str


@dataclass
class Chain:
    hops: List[str]
    terminal: str
    cycle: bool


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read .htaccess or Netlify _redirects, detect redirect chains, "
            "and optionally rewrite chained redirects to their final target."
        )
    )
    parser.add_argument("redirect_file", help="Path to .htaccess or _redirects.")
    parser.add_argument("--chain", action="store_true", help="Detect and report redirect chains.")
    parser.add_argument("--modify", action="store_true", help="Rewrite chains to final destinations.")
    parser.add_argument("--json", action="store_true", help="Emit chain report in JSON format.")
    return parser.parse_args(argv)


def detect_format(path: Path) -> str:
    if path.name == ".htaccess":
        return "htaccess"
    if path.name == "_redirects":
        return "netlify"
    raise ValueError("redirect_file must be named .htaccess or _redirects")


def split_line_ending(line: str) -> Tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def parse_redirects(lines: List[str], fmt: str) -> List[RedirectRule]:
    rules: List[RedirectRule] = []
    for idx, full_line in enumerate(lines):
        line, line_ending = split_line_ending(full_line)
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if fmt == "htaccess":
            parsed = parse_htaccess_line(line)
        else:
            parsed = parse_netlify_line(line)

        if parsed is None:
            continue

        source, target, status_code, leading, keyword, trailing = parsed
        redirect_type = "permanent" if status_code == 301 else "temporary"
        rules.append(
            RedirectRule(
                source=source,
                target=target,
                redirect_type=redirect_type,
                raw_line=line,
                line_index=idx,
                format=fmt,
                status_code=status_code,
                line_ending=line_ending,
                leading=leading,
                keyword=keyword,
                trailing=trailing,
            )
        )
    return rules


def parse_htaccess_line(line: str) -> Optional[Tuple[str, str, int, str, str, str]]:
    # Supports only: Redirect 301|302 <from> <to>
    import re

    match = re.match(
        r"^(\s*)(Redirect)\s+(301|302)\s+(\S+)\s+(\S+)(.*)$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    leading = match.group(1)
    keyword = match.group(2)
    status_code = int(match.group(3))
    source = match.group(4).strip()
    target = match.group(5).strip()
    trailing = match.group(6)
    return source, target, status_code, leading, keyword, trailing


def parse_netlify_line(line: str) -> Optional[Tuple[str, str, int, str, str, str]]:
    import re

    match = re.match(r"^(\s*)(\S+)\s+(\S+)\s+(301|302)\b(.*)$", line)
    if not match:
        return None

    leading = match.group(1)
    source = match.group(2).strip()
    target = match.group(3).strip()
    status_code = int(match.group(4))
    trailing = match.group(5)
    return source, target, status_code, leading, "", trailing


def discover_chains(rules: List[RedirectRule]) -> List[Chain]:
    source_map: Dict[str, RedirectRule] = {}
    for rule in rules:
        if rule.source not in source_map:
            source_map[rule.source] = rule

    inbound_sources = {rule.target for rule in rules if rule.target in source_map}
    roots = [rule.source for rule in rules if rule.source in source_map and rule.source not in inbound_sources]

    chains: List[Chain] = []
    seen_signatures = set()
    covered_sources = set()

    for root in roots:
        path, cycle = trace_path(root, source_map)
        if cycle or len(path) >= 3:
            signature = (tuple(path), cycle)
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                chains.append(Chain(hops=path, terminal=path[-1], cycle=cycle))
        covered_sources.update(path[:-1] if cycle else path)

    for source in source_map:
        if source in covered_sources:
            continue
        path, cycle = trace_path(source, source_map)
        if cycle or len(path) >= 3:
            signature = (tuple(path), cycle)
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                chains.append(Chain(hops=path, terminal=path[-1], cycle=cycle))
        covered_sources.update(path[:-1] if cycle else path)

    return chains


def trace_path(start: str, source_map: Dict[str, RedirectRule]) -> Tuple[List[str], bool]:
    seen_pos = {start: 0}
    hops = [start]
    current = start

    while True:
        rule = source_map.get(current)
        if rule is None:
            return hops, False

        nxt = rule.target
        if nxt in seen_pos:
            hops.append(nxt)
            return hops, True

        hops.append(nxt)
        if nxt not in source_map:
            return hops, False

        seen_pos[nxt] = len(hops) - 1
        current = nxt


def rewrite_lines_for_chains(
    lines: List[str],
    rules: List[RedirectRule],
    chains: List[Chain],
) -> Tuple[List[str], int, int]:
    source_map: Dict[str, RedirectRule] = {}
    for rule in rules:
        if rule.source not in source_map:
            source_map[rule.source] = rule

    desired_target_by_line: Dict[int, str] = {}
    cycles_skipped = 0

    for chain in chains:
        if chain.cycle:
            cycles_skipped += 1
            continue
        terminal = chain.terminal
        for source in chain.hops[:-1]:
            rule = source_map.get(source)
            if rule is None:
                continue
            desired_target_by_line[rule.line_index] = terminal

    updated = list(lines)
    rewritten_count = 0
    for rule in rules:
        desired = desired_target_by_line.get(rule.line_index)
        if not desired or desired == rule.target:
            continue
        if rule.format == "htaccess":
            new_line = (
                f"{rule.leading}{rule.keyword} {rule.status_code} "
                f"{rule.source} {desired}{rule.trailing}{rule.line_ending}"
            )
        else:
            new_line = (
                f"{rule.leading}{rule.source} {desired} {rule.status_code}"
                f"{rule.trailing}{rule.line_ending}"
            )
        if new_line != lines[rule.line_index]:
            updated[rule.line_index] = new_line
            rewritten_count += 1

    return updated, rewritten_count, cycles_skipped


def make_backup(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(f"{path.name}.{timestamp}.bak")
    path.rename(backup)
    return backup


def print_human_report(chains: List[Chain], out) -> None:
    if not chains:
        print("No redirect chains found.", file=out)
        return
    for idx, chain in enumerate(chains, start=1):
        route = " -> ".join(chain.hops)
        if chain.cycle:
            print(f"Chain {idx}: {route} (cycle detected)", file=out)
        else:
            print(f"Chain {idx}: {route} (final: {chain.terminal})", file=out)


def print_json_report(chains: List[Chain], out) -> None:
    payload = {
        "chains": [
            {
                "hops": chain.hops,
                "terminal": chain.terminal,
                "cycle": chain.cycle,
            }
            for chain in chains
        ]
    }
    json.dump(payload, out, indent=2)
    out.write("\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    redirect_path = Path(args.redirect_file).resolve()

    if not redirect_path.exists() or not redirect_path.is_file():
        print(f"Error: redirect file does not exist: {redirect_path}", file=sys.stderr)
        return 2

    try:
        fmt = detect_format(redirect_path)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        text = redirect_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Error reading file: {exc}", file=sys.stderr)
        return 2

    lines = text.splitlines(keepends=True)
    rules = parse_redirects(lines, fmt)

    should_analyse = args.chain or args.modify
    chains = discover_chains(rules) if should_analyse else []

    if should_analyse:
        if args.json:
            print_json_report(chains, sys.stdout)
        else:
            print_human_report(chains, sys.stdout)

    if args.modify:
        new_lines, rewritten_count, cycles_skipped = rewrite_lines_for_chains(lines, rules, chains)
        if rewritten_count > 0:
            try:
                backup_path = make_backup(redirect_path)
                redirect_path.write_text("".join(new_lines), encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"Error writing updated redirects: {exc}", file=sys.stderr)
                return 2
            print(
                "Modify summary: "
                f"chains={len(chains)} rewritten={rewritten_count} "
                f"cycles_skipped={cycles_skipped} backup={backup_path}",
                file=sys.stdout,
            )
        else:
            print(
                "Modify summary: "
                f"chains={len(chains)} rewritten=0 cycles_skipped={cycles_skipped} backup=None",
                file=sys.stdout,
            )
        return 0

    if should_analyse and chains:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
