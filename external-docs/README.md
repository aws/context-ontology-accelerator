# External Documentation Site

React + Vite documentation site for Context Ontology Accelerator, with markdown content rendered via `react-markdown`.

## Development

```bash
pnpm install
pnpm dev        # http://localhost:5175
```

## Build (HTML)

```bash
pnpm build      # outputs to dist/
```

## Generate PDF

Requires [Pandoc](https://pandoc.org/) and LuaLaTeX:

```bash
# macOS
brew install pandoc basictex

# Linux (Debian/Ubuntu)
sudo apt-get install pandoc texlive-luatex texlive-latex-extra
```

From the `external-docs/` directory, run:

```bash
pandoc \
  content/getting-started.md \
  content/deploying.md \
  content/namespaces.md \
  content/sources.md \
  content/ontologies.md \
  content/metrics.md \
  content/serve.md \
  content/authentication-setup.md \
  content/role-permission-management.md \
  content/agent-access.md \
  content/cedar-policy-authoring.md \
  content/cross-account-sources.md \
  content/custom-connector-sources.md \
  content/databricks-sources.md \
  content/package-guide.md \
  content/smithy-codegen.md \
  --toc --toc-depth=2 --pdf-engine=lualatex \
  -V mainfont="Helvetica Neue" \
  -V monofont="Menlo" \
  -V geometry:margin=0.75in \
  -V tables=true \
  --columns=72 \
  -H pdf-header.tex \
  --lua-filter=pdf-emoji-filter.lua \
  -o ontology-accelerator.pdf
```

The file order matches the logical documentation flow: setup → user guide → platform guides → data source guides → developer guides. Adjust the order by reordering the filenames in the command.
