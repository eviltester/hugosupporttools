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

Auto-fix detected alias links in place while keeping CSV output:

```bash
python alias-checker.py ./content --modify
```

### Link matching behavior

The tool scans link targets only:

- Markdown inline links/images
- Reference-style markdown link definitions
- HTML `href` / `src` attributes in markdown

It does not flag plain-text URL mentions.

When `--modify` is used, only those detected link-target contexts are rewritten.

### Exit codes

- `0`: no alias mislinks found
- `1`: alias mislinks found
- `2`: invalid input (for example, missing directory)

With `--modify`, successful runs return `0` after applying rewrites.

The scan summary and verbose diagnostics are written to `stderr` so CSV output remains clean for redirects/pipelines.
