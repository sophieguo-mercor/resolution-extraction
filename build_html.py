#!/usr/bin/env python3
"""
build_html.py  —  convert the JSX explorer template + merged data into a
single self-contained HTML file that opens in any browser without a build
tool, server, or internet connection (CDN assets are the one exception).

Can be run standalone:
    python build_html.py --data merged_data.json --out explorer.html

Or imported and called from run.py:
    import build_html
    build_html.build(data_dict, "build_explorer.py", "explorer.html")

How it works
------------
1. Extract the JSX template string from build_explorer.py (everything between
   TEMPLATE = r\\''' and the closing \\''').
2. Inject merged_data as a JS object literal in place of __DATA__.
3. Wrap the JSX in a single HTML page that:
     - Loads React + ReactDOM from the unpkg CDN
     - Loads Babel standalone from the unpkg CDN (transpiles JSX in-browser)
     - Adds a <script type="text/babel"> tag containing the JSX
     - Sets a <meta charset> and a basic reset so the page looks the same
       as the Claude.ai artifact renderer
The output is fully self-contained aside from the two CDN script tags.
To make it work offline, swap those for locally-hosted copies.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# CDN versions pinned so the output is reproducible.
# To update: change the version strings; the page will re-fetch on next open.
REACT_VERSION = "18.2.0"
BABEL_VERSION  = "7.23.5"

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Distributional Shape Explorer</title>
  <script src="https://unpkg.com/react@{react}/umd/react.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/react-dom@{react}/umd/react-dom.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/@babel/standalone@{babel}/babel.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 0; font-family: 'Inter', system-ui, sans-serif; }}
    #root {{ min-height: 100vh; }}
  </style>
</head>
<body>
  <div id="root"></div>
  <script type="text/babel" data-presets="react">
{jsx}
  // Mount
  const {{ useState }} = React;
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(React.createElement(App));
  </script>
</body>
</html>
"""


def extract_template(build_explorer_path: str) -> str:
    """Pull the JSX template string out of build_explorer.py."""
    src = Path(build_explorer_path).read_text(encoding="utf-8")
    m = re.search(r"TEMPLATE = r'''(.*?)'''", src, re.DOTALL)
    if not m:
        raise SystemExit(f"Could not find TEMPLATE = r'''...''' in {build_explorer_path}")
    return m.group(1)


def inject_data(template: str, data: dict) -> str:
    """Replace __DATA__ with the JSON-serialised merged data.

    The payload is embedded inside a <script> tag, so a string value containing
    '<' (a company name, an action/object label) could otherwise close the tag
    early — a literal "</script>" would break out of the block. Escaping '<' and
    the U+2028/U+2029 line separators (which are illegal bare in JS string
    literals) as \\uXXXX sequences keeps the data inert while staying valid,
    equal JSON.
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    payload = (
        payload.replace("<", "\\u003c")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    if "__DATA__" not in template:
        raise SystemExit("Template does not contain __DATA__ placeholder")
    return template.replace("__DATA__", payload, 1)


def jsx_to_html(jsx: str) -> str:
    """
    Adapt the JSX for the in-browser Babel environment.

    The Claude.ai artifact renderer pre-imports React hooks for you.
    In a plain HTML page we have React as a global, so `useState` etc.
    must be accessed via `React.useState`.  We handle this by adding a
    destructuring line at the top of the script (in HTML_TEMPLATE) and
    keeping the `import { useState } from "react"` line — Babel ignores
    `import` statements when the module is not available and the
    transformer is configured for browser mode.  Actually, the safest
    approach is to strip the import line and let the destructuring at the
    bottom of HTML_TEMPLATE handle it.
    """
    # Strip the ES module import — React is a UMD global in the HTML page
    jsx = re.sub(r'import\s*\{[^}]+\}\s*from\s*["\']react["\'];?\s*\n?', '', jsx)
    # Ensure `export default` is removed so App is a plain function in scope
    jsx = re.sub(r'\bexport\s+default\s+', '', jsx)
    return jsx


def build(data: dict, build_explorer_path: str, out_path: str) -> None:
    """Main entry point when called from run.py."""
    template = extract_template(build_explorer_path)
    jsx_with_data = inject_data(template, data)
    jsx_clean = jsx_to_html(jsx_with_data)
    html = HTML_TEMPLATE.format(
        react=REACT_VERSION,
        babel=BABEL_VERSION,
        jsx=jsx_clean,
    )
    Path(out_path).write_text(html, encoding="utf-8")
    size_kb = len(html.encode()) // 1024
    print(f"  → {out_path}  ({size_kb} KB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data",     default="merged_data.json")
    ap.add_argument("--template", default="build_explorer.py",
                    help="path to build_explorer.py containing the JSX template")
    ap.add_argument("--out",      default="explorer.html")
    args = ap.parse_args()

    if not Path(args.data).exists():
        raise SystemExit(f"Data file not found: {args.data}\n"
                         f"Run 'python run.py' first to generate it.")
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    build(data, args.template, args.out)


if __name__ == "__main__":
    main()
