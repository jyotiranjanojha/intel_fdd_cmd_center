"""
Converts fdd_render.render_rule()'s markdown into HTML.

Deliberately pure - no database, no Streamlit, no app imports - so it can be
unit tested directly. fdd_publish.py pulls in catalog -> db -> streamlit, which
would otherwise make testing this text transformation require stubbing the whole
data layer.

Handles exactly the subset render_rule() emits (the short format: Metadata / When /
Then / Joins / Context bullets, and the same shapes in render_rule_classic()):
  ### header
  a plain chip line
  **a bold headline paragraph**
  - **Label:** text          with optional 4-space-indented sub-bullets
  | a | markdown | table |
  - plain bullets (the References block)

Anything unrecognised becomes a plain paragraph rather than being dropped, so a
future renderer change degrades visibly instead of silently losing content.
"""

import html as _html
import re
from typing import List

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    """Escape HTML, then re-apply the inline markdown we know about."""
    # Escaping first is safe: it only touches & < > " ' - never * or `.
    text = _html.escape(str(text or ""), quote=False)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _CODE_RE.sub(r"<code>\1</code>", text)
    return text


def rule_markdown_to_html(markdown: str) -> str:
    lines = str(markdown or "").split("\n")
    out: List[str] = []
    i, n = 0, len(lines)
    in_ul = False

    def close_list():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    while i < n:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue

        if line.startswith("### "):
            close_list()
            out.append(f"<h4>{inline(line[4:])}</h4>")

        elif line.startswith("|"):
            close_list()
            header = [c.strip() for c in line.strip("|").split("|")]
            i += 1
            if i < n and lines[i].strip().startswith("|") and "-" in lines[i]:
                i += 1  # separator row
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append('<table class="mapping"><thead><tr>'
                       + "".join(f"<th>{inline(h)}</th>" for h in header)
                       + "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue  # i already advanced

        elif line.startswith("- "):
            if not in_ul:
                out.append('<ul class="rule-bullets">')
                in_ul = True
            out.append(f"<li>{inline(line[2:])}")
            j = i + 1
            nested = []
            while j < n and lines[j].startswith("    - "):
                nested.append(lines[j].strip()[2:])
                j += 1
            if nested:
                out.append("<ul>" + "".join(f"<li>{inline(nl)}</li>" for nl in nested) + "</ul>")
                i = j - 1
            out.append("</li>")

        elif line.startswith("**") and line.endswith("**") and line.count("**") == 2:
            close_list()
            out.append(f'<p class="headline">{inline(line)}</p>')

        else:
            close_list()
            out.append(f'<p class="chip">{inline(line)}</p>')

        i += 1

    close_list()
    return "\n".join(out)
