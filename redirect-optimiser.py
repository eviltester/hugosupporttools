#!/usr/bin/env python3
import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib import error, parse, request


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


@dataclass
class HeadResponse:
    status_code: Optional[int]
    location: Optional[str]
    error_message: Optional[str]


@dataclass
class CrawlFinding:
    line_index: int
    source: str
    original_target: str
    effective_target: str
    resolved_url: str
    status_code: Optional[int]
    classification: str
    location: Optional[str]
    reason: Optional[str]
    action: str


class NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


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
    parser.add_argument("--json", action="store_true", help="Emit report in JSON format.")
    parser.add_argument(
        "--crawl",
        action="store_true",
        help="HEAD each redirect target and report invalid or redirected targets.",
    )
    parser.add_argument(
        "--url",
        help="Base URL used to resolve relative redirect targets when --crawl is used.",
    )
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
            parsed_line = parse_htaccess_line(line)
        else:
            parsed_line = parse_netlify_line(line)

        if parsed_line is None:
            continue

        source, target, status_code, leading, keyword, trailing = parsed_line
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
    import re

    match = re.match(
        r"^(\s*)(Redirect)\s+(301|302)\s+(\S+)\s+(\S+)(.*)$",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    return (
        match.group(4).strip(),
        match.group(5).strip(),
        int(match.group(3)),
        match.group(1),
        match.group(2),
        match.group(6),
    )


def parse_netlify_line(line: str) -> Optional[Tuple[str, str, int, str, str, str]]:
    import re

    match = re.match(r"^(\s*)(\S+)\s+(\S+)\s+(301|302)\b(.*)$", line)
    if not match:
        return None

    return (
        match.group(2).strip(),
        match.group(3).strip(),
        int(match.group(4)),
        match.group(1),
        "",
        match.group(5),
    )


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


def desired_targets_from_chains(
    rules: List[RedirectRule],
    chains: List[Chain],
) -> Tuple[Dict[int, str], int]:
    source_map: Dict[str, RedirectRule] = {}
    for rule in rules:
        if rule.source not in source_map:
            source_map[rule.source] = rule

    desired: Dict[int, str] = {}
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
            desired[rule.line_index] = terminal
    return desired, cycles_skipped


def parse_base_url(base_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if base_url is None:
        return None, None
    parsed = parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "Error: --url must be an absolute http/https URL."
    return base_url, None


def is_relative_target(target: str) -> bool:
    parsed = parse.urlparse(target)
    return parsed.scheme == "" and parsed.netloc == ""


def resolve_target_url(target: str, base_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    parsed = parse.urlparse(target)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return target, None
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None, f"unsupported scheme: {parsed.scheme}"
    if parsed.netloc:
        return None, "unsupported scheme-relative URL"
    if base_url is None:
        return None, "relative target requires --url"

    resolved = parse.urljoin(base_url, target)
    parsed_resolved = parse.urlparse(resolved)
    if parsed_resolved.scheme not in ("http", "https") or not parsed_resolved.netloc:
        return None, "could not resolve relative target to absolute http/https URL"
    return resolved, None


def fetch_head(url: str, timeout: int = 10) -> HeadResponse:
    req = request.Request(url, method="HEAD")
    opener = request.build_opener(NoRedirectHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            location = resp.headers.get("Location")
            return HeadResponse(status_code=status, location=location, error_message=None)
    except error.HTTPError as exc:
        return HeadResponse(
            status_code=exc.code,
            location=exc.headers.get("Location") if exc.headers else None,
            error_message=None,
        )
    except Exception as exc:  # pragma: no cover
        return HeadResponse(status_code=None, location=None, error_message=str(exc))


def classify_head_result(url: str, head: HeadResponse) -> Tuple[str, Optional[str], Optional[str]]:
    if head.error_message:
        return "invalid", None, head.error_message
    if head.status_code is None:
        return "invalid", None, "no status code received"

    if 200 <= head.status_code <= 299:
        return "valid", None, None

    if head.status_code in (301, 308):
        if not head.location:
            return "invalid", None, f"HTTP {head.status_code} missing Location header"
        new_location = parse.urljoin(url, head.location)
        parsed_location = parse.urlparse(new_location)
        if parsed_location.scheme not in ("http", "https") or not parsed_location.netloc:
            return "invalid", None, f"HTTP {head.status_code} returned non-http Location"
        return "permanent_redirect", new_location, None

    if head.status_code in (302, 303, 307):
        return "temporary_redirect", head.location, f"HTTP {head.status_code} temporary redirect"

    if 300 <= head.status_code <= 399:
        return "invalid", None, f"HTTP {head.status_code} unsupported redirect response"

    return "invalid", None, f"HTTP {head.status_code}"


def build_line(rule: RedirectRule, new_target: str) -> str:
    if rule.format == "htaccess":
        return (
            f"{rule.leading}{rule.keyword} {rule.status_code} "
            f"{rule.source} {new_target}{rule.trailing}{rule.line_ending}"
        )
    return f"{rule.leading}{rule.source} {new_target} {rule.status_code}{rule.trailing}{rule.line_ending}"


def make_backup(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_name(f"{path.name}.{timestamp}.bak")
    path.rename(backup)
    return backup


def print_human_chain_report(chains: List[Chain], out) -> None:
    if not chains:
        print("No redirect chains found.", file=out)
        return
    for idx, chain in enumerate(chains, start=1):
        route = " -> ".join(chain.hops)
        if chain.cycle:
            print(f"Chain {idx}: {route} (cycle detected)", file=out)
        else:
            print(f"Chain {idx}: {route} (final: {chain.terminal})", file=out)


def print_human_crawl_report(findings: List[CrawlFinding], unique_checked: int, out) -> None:
    if not findings:
        print(f"Crawl summary: checked={unique_checked} valid={unique_checked} invalid=0 permanent=0 temporary=0", file=out)
        return

    invalid = sum(1 for f in findings if f.classification == "invalid")
    permanent = sum(1 for f in findings if f.classification == "permanent_redirect")
    temporary = sum(1 for f in findings if f.classification == "temporary_redirect")
    valid = unique_checked - invalid - permanent - temporary

    print(
        f"Crawl summary: checked={unique_checked} valid={valid} invalid={invalid} "
        f"permanent={permanent} temporary={temporary}",
        file=out,
    )
    for finding in findings:
        status = f"status={finding.status_code}" if finding.status_code is not None else "status=error"
        location = f" location={finding.location}" if finding.location else ""
        reason = f" reason={finding.reason}" if finding.reason else ""
        print(
            f"Crawl: source={finding.source} target={finding.effective_target} "
            f"url={finding.resolved_url} class={finding.classification} {status}{location}{reason}",
            file=out,
        )


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

    base_url, base_error = parse_base_url(args.url)
    if base_error:
        print(base_error, file=sys.stderr)
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
    desired_from_chains, cycles_skipped = desired_targets_from_chains(rules, chains) if args.modify else ({}, 0)

    effective_target_by_line: Dict[int, str] = {}
    for rule in rules:
        effective_target_by_line[rule.line_index] = desired_from_chains.get(rule.line_index, rule.target)

    if args.crawl and any(is_relative_target(rule.target) for rule in rules) and base_url is None:
        print("Error: --crawl requires --url when redirect targets are relative.", file=sys.stderr)
        return 2

    crawl_findings: List[CrawlFinding] = []
    crawl_cache: Dict[str, Tuple[HeadResponse, str, Optional[str], Optional[str]]] = {}
    unique_checked = 0
    crawl_problem_count = 0

    if args.crawl:
        for rule in rules:
            effective_target = effective_target_by_line[rule.line_index]
            resolved_url, resolve_error = resolve_target_url(effective_target, base_url)
            if resolve_error:
                classification = "invalid"
                finding = CrawlFinding(
                    line_index=rule.line_index,
                    source=rule.source,
                    original_target=rule.target,
                    effective_target=effective_target,
                    resolved_url=effective_target,
                    status_code=None,
                    classification=classification,
                    location=None,
                    reason=resolve_error,
                    action="kept",
                )
                crawl_findings.append(finding)
                crawl_problem_count += 1
                continue

            assert resolved_url is not None
            if resolved_url not in crawl_cache:
                head = fetch_head(resolved_url)
                classification, location, reason = classify_head_result(resolved_url, head)
                crawl_cache[resolved_url] = (head, classification, location, reason)
            head, classification, location, reason = crawl_cache[resolved_url]
            unique_checked = len(crawl_cache)

            finding = CrawlFinding(
                line_index=rule.line_index,
                source=rule.source,
                original_target=rule.target,
                effective_target=effective_target,
                resolved_url=resolved_url,
                status_code=head.status_code,
                classification=classification,
                location=location,
                reason=reason,
                action="kept",
            )
            if classification in ("invalid", "temporary_redirect"):
                crawl_problem_count += 1
            if classification in ("invalid", "temporary_redirect", "permanent_redirect"):
                crawl_findings.append(finding)

    lines_to_remove = set()
    final_target_by_line: Dict[int, str] = {}
    rewritten_from_crawl = 0

    for rule in rules:
        final_target_by_line[rule.line_index] = effective_target_by_line[rule.line_index]

    if args.modify and args.crawl:
        by_line = {f.line_index: f for f in crawl_findings}
        for rule in rules:
            finding = by_line.get(rule.line_index)
            if finding is None:
                continue
            if finding.classification in ("invalid", "temporary_redirect"):
                lines_to_remove.add(rule.line_index)
                finding.action = "removed_invalid"
                continue
            if finding.classification == "permanent_redirect" and finding.location:
                final_target_by_line[rule.line_index] = finding.location
                if finding.location != effective_target_by_line[rule.line_index]:
                    rewritten_from_crawl += 1
                    finding.action = "rewritten_permanent_redirect"

    rewritten_total = 0
    removed_total = 0
    backup_path: Optional[Path] = None

    if args.modify:
        updated_lines = list(lines)
        for rule in rules:
            if rule.line_index in lines_to_remove:
                if updated_lines[rule.line_index] != "":
                    removed_total += 1
                updated_lines[rule.line_index] = ""
                continue

            desired_target = final_target_by_line[rule.line_index]
            new_line = build_line(rule, desired_target)
            if new_line != lines[rule.line_index]:
                updated_lines[rule.line_index] = new_line
                rewritten_total += 1

        if rewritten_total > 0 or removed_total > 0:
            try:
                backup_path = make_backup(redirect_path)
                redirect_path.write_text("".join(updated_lines), encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"Error writing updated redirects: {exc}", file=sys.stderr)
                return 2

    json_payload: Dict[str, object] = {}
    if should_analyse:
        json_payload["chains"] = [
            {"hops": chain.hops, "terminal": chain.terminal, "cycle": chain.cycle}
            for chain in chains
        ]
    if args.crawl:
        invalid = sum(1 for f in crawl_findings if f.classification == "invalid")
        permanent = sum(1 for f in crawl_findings if f.classification == "permanent_redirect")
        temporary = sum(1 for f in crawl_findings if f.classification == "temporary_redirect")
        valid = unique_checked - invalid - permanent - temporary
        json_payload["crawl"] = {
            "summary": {
                "checked": unique_checked,
                "valid": valid,
                "invalid": invalid,
                "permanent_redirect": permanent,
                "temporary_redirect": temporary,
            },
            "findings": [
                {
                    "source": f.source,
                    "target": f.effective_target,
                    "resolved_url": f.resolved_url,
                    "status_code": f.status_code,
                    "classification": f.classification,
                    "location": f.location,
                    "reason": f.reason,
                    "action": f.action,
                }
                for f in crawl_findings
            ],
        }
    if args.modify:
        json_payload["modify"] = {
            "chains": len(chains),
            "rewritten": rewritten_total,
            "removed": removed_total,
            "rewritten_from_crawl": rewritten_from_crawl,
            "cycles_skipped": cycles_skipped,
            "backup": str(backup_path) if backup_path else None,
        }

    if args.json:
        json.dump(json_payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        if should_analyse:
            print_human_chain_report(chains, sys.stdout)
        if args.crawl:
            print_human_crawl_report(crawl_findings, unique_checked, sys.stdout)
        if args.modify:
            print(
                "Modify summary: "
                f"chains={len(chains)} rewritten={rewritten_total} removed={removed_total} "
                f"rewritten_from_crawl={rewritten_from_crawl} cycles_skipped={cycles_skipped} "
                f"backup={backup_path if backup_path else 'None'}",
                file=sys.stdout,
            )

    if args.modify:
        return 0

    has_chain_findings = bool(chains) if should_analyse else False
    if args.crawl and (has_chain_findings or crawl_problem_count > 0):
        return 1
    if should_analyse and has_chain_findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
