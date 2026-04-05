# hugosupporttools

## alias-checker.py

`alias-checker.py` scans a Hugo content directory and detects internal links that still point at legacy `aliases` instead of the canonical page URL.

This supports the workflow where aliases are kept for external redirects, while internal markdown should link to the new URL.

### What it reports

For each mislink, it reports:

- `Alias`: the alias that was linked
- `Use Instead`: canonical URL for the owning page
- `Found In`: markdown file where alias usage was found

The report is emitted as CSV to `stdout` with a header row:

```csv
Alias,Use Instead,Found In
```

### Supported front matter

- YAML (`---`)
- TOML (`+++`)
- JSON (`{ ... }`)

### Usage

```bash
python alias-checker.py ./content
```

With options:

```bash
python alias-checker.py ./content --extensions .md .markdown --verbose
```

Include front matter `url: ...html` values as alias sources:

```bash
python alias-checker.py ./content --urls
```

Auto-fix detected alias links in place while keeping CSV output:

```bash
python alias-checker.py ./content --modify
```

Auto-fix including `url: ...html` alias mappings:

```bash
python alias-checker.py ./content --modify --urls
```

Report planned alias migration into Hugo static redirect files (dry-run):

```bash
python alias-checker.py ./content --redirects
```

Apply alias migration (writes only with `--modify`):

```bash
python alias-checker.py ./content --modify --redirects
```

### Link matching behavior

The tool scans link targets only:

- Markdown inline links/images
- Reference-style markdown link definitions
- HTML `href` / `src` attributes in markdown

It does not flag plain-text URL mentions.

When `--modify` is used, only those detected link-target contexts are rewritten.
When `--urls` is used, front matter values like `url: /mypage` enforce trailing-slash link targets; values such as `/mypage` and `/mypage.html` are reported against `/mypage/`.
With `--modify --urls`, front matter `url` metadata is rewritten from `.html` to a slashless path (for example `/mypage.html` -> `/mypage`).
When `--redirects` is used, explicit front matter `aliases` are planned for migration to:

- `static/_redirects` in Netlify format: `<alias> <canonical> 301`
- `static/.htaccess` in Apache format: `Redirect 301 <alias> <canonical>`

The static directory is resolved as `<content_dir parent>/static`.
Redirect and front matter changes are only written when `--modify` is also provided; migration actions are always reported to `stderr`.

### Exit codes

- `0`: no alias mislinks found
- `1`: alias mislinks found
- `2`: invalid input (for example, missing directory)

With `--modify`, successful runs return `0` after applying rewrites.

The scan summary and verbose diagnostics are written to `stderr` so CSV output remains clean for redirects/pipelines.

## redirect-optimiser.py

`redirect-optimiser.py` reads either an Apache `.htaccess` file or a Netlify `_redirects` file, detects redirect chains, and can rewrite chained redirects so they point directly to the final destination.

### Supported redirect syntax

- `.htaccess`: `Redirect 301 <from> <to>` and `Redirect 302 <from> <to>`
- `_redirects`: `<from> <to> 301` and `<from> <to> 302`

Other lines (comments, blank lines, unsupported directives/status codes) are ignored and preserved.

### Usage

```bash
python redirect-optimiser.py /path/to/.htaccess --chain
python redirect-optimiser.py /path/to/_redirects --chain
```

With options:

```bash
python redirect-optimiser.py /path/to/.htaccess --chain --json
python redirect-optimiser.py /path/to/_redirects --chain --modify
python redirect-optimiser.py /path/to/_redirects --crawl --url https://www.eviltester.com
python redirect-optimiser.py /path/to/.htaccess --chain --crawl --modify --url https://www.eviltester.com
python redirect-optimiser.py /path/to/_redirects --duplicates
```

### Chain detection

When `--chain` is enabled, the tool reports chains where a rule target is also a redirect source, for example:

```text
/a -> /b -> /c
```

Cycles (for example `/a -> /b -> /a`) are detected and reported separately.

### Crawl validation

When `--crawl` is enabled, the tool sends HTTP `HEAD` requests for redirect targets:

- `2xx`: valid target
- `301`/`308`: valid permanent redirect (reported, with `Location`)
- `302`/`303`/`307`: temporary redirect (reported as a problem)
- `4xx`/`5xx`/network errors: invalid

If redirect targets are relative paths, `--url` is required so targets can be resolved (for example `/path` -> `https://www.eviltester.com/path`).

### Duplicate source detection

When `--duplicates` is enabled, the tool reports any duplicate redirect `from` values (multiple redirect rules sharing the same source path).

### Modify behavior

When `--modify` is provided:

- Non-cyclic chain members are rewritten to the terminal destination.
- With `--crawl`, entries with invalid or temporary-redirect targets are removed.
- With `--crawl`, targets returning `301`/`308` are rewritten to the returned `Location`.
- Each line keeps its original status code (`301`/`302`).
- Unrelated lines, comments, and file structure are preserved.
- The original file is backed up first:
  - `.htaccess.YYYYMMDDTHHMMSS.bak`
  - `_redirects.YYYYMMDDTHHMMSS.bak`

Cycles are not flattened.

### Output formats

- Default: human-readable chain lines
- `--json`: structured JSON including chain data, crawl findings, duplicate-source findings, and modify actions

### Exit codes

- `0`: success (no chains found, or modifications applied successfully)
- `1`: chains found, crawl problems found, or duplicate sources found when not using `--modify`
- `2`: invalid input or read/write error

