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

