"""Clickable pager and back-to-top links for long pages (Requirements).

The Requirements page used a number box ("Page" with +/- steppers) above the rules
only, and nothing brought a reader back up after twenty expanded rules.
"""

import hashlib
from typing import List, Optional

import streamlit as st

TOP_ANCHOR = "req-top"


def page_links(total: int, current: int, max_shown: int = 7) -> List[Optional[int]]:
    """Page numbers to draw, None for a gap: 1 … 6 7 [8] 9 10 … 24.

    Always the first, the last and the current page; neighbours of the current page
    fill the rest. Never two gaps in a row, never more than max_shown numbers.
    """
    total = max(1, int(total))
    current = min(max(1, int(current)), total)
    if total <= max_shown:
        return list(range(1, total + 1))
    inner = max_shown - 2                      # slots between the first and last page
    start = min(max(2, current - inner // 2), total - inner)
    middle: List[Optional[int]] = list(range(start, start + inner))
    # A slot next to a hidden stretch becomes the gap, unless it is the current page.
    if middle[0] != 2 and middle[0] != current:
        middle[0] = None
    if middle[-1] != total - 1 and middle[-1] != current:
        middle[-1] = None
    return [1, *middle, total]


def current_page(total: int, signature: str, key: str = "req_page_no") -> int:
    """The page to show: remembered, reset to 1 when the filters change, and clamped
    when the number of pages shrinks (after retiring rules, say)."""
    sig_key = f"{key}_sig"
    if st.session_state.get(sig_key) != signature:
        st.session_state[sig_key] = signature
        st.session_state[key] = 1
    page = int(st.session_state.get(key, 1) or 1)
    page = min(max(1, page), max(1, int(total)))
    st.session_state[key] = page
    return page


def signature_of(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _go(key: str, page: int, to_top: bool) -> None:
    st.session_state[key] = page
    if to_top:
        st.session_state["_scroll_to_top"] = True


PAGER_CSS = """<style>
.st-key-{k} [data-testid="stHorizontalBlock"] {{gap: 0.25rem; align-items: center;}}
.st-key-{k} [data-testid="stColumn"] {{width: auto !important; flex: 0 0 auto !important;
    min-width: 0 !important;}}
.st-key-{k} button {{min-height: 0; padding: 0.1rem 0.55rem; line-height: 1.3;}}
.st-key-{k} button p {{font-size: 0.85rem;}}
</style>"""


def render_pager(total: int, current: int, where: str, caption: str = "",
                 key: str = "req_page_no") -> None:
    """Previous 1 2 [3] 4 Next. Nothing for a single page. A click in the bottom pager
    also returns the reader to the top of the new page.

    Small buttons packed to the left: drawn full width in equal columns, each number
    filled a tenth of the page and the row looked like a toolbar of large tiles."""
    if total <= 1:
        return
    links = page_links(total, current)
    to_top = where == "bottom"
    box_key = f"{key}_{where}_box"
    try:
        box = st.container(key=box_key)          # gives the row a CSS class to scope to
    except TypeError:                            # older Streamlit: no keyed containers
        box = st.container()
    st.markdown(PAGER_CSS.format(k=box_key), unsafe_allow_html=True)
    with box:
        cols = st.columns(len(links) + 3, gap="small")
        cols[0].button("‹ Previous", key=f"{key}_{where}_prev", disabled=current <= 1,
                       on_click=_go, args=(key, current - 1, to_top))
        for i, page in enumerate(links, start=1):
            if page is None:
                cols[i].markdown("…")
                continue
            cols[i].button(str(page), key=f"{key}_{where}_{page}",
                           type="primary" if page == current else "secondary",
                           disabled=page == current, on_click=_go, args=(key, page, to_top))
        cols[len(links) + 1].button("Next ›", key=f"{key}_{where}_next",
                                    disabled=current >= total,
                                    on_click=_go, args=(key, current + 1, to_top))
        if caption:
            cols[-1].caption(caption)


def top_anchor() -> None:
    """Where the back-to-top links land; also runs a pending scroll-to-top."""
    st.markdown(f"<div id='{TOP_ANCHOR}'></div>", unsafe_allow_html=True)
    if st.session_state.pop("_scroll_to_top", False):
        _scroll_script()


def back_to_top_link() -> None:
    st.markdown(f"<div style='text-align:right;font-size:0.85em'>"
                f"<a href='#{TOP_ANCHOR}' target='_self'>↑ Back to top</a></div>",
                unsafe_allow_html=True)


def floating_top_button() -> None:
    """A small round button fixed at the bottom-right, visible while scrolling."""
    st.markdown(
        f"""<style>
        .req-top-btn {{position: fixed; right: 28px; bottom: 28px; z-index: 1000;
            padding: 8px 14px; border-radius: 20px; font-size: 0.9em; text-decoration: none;
            background: rgba(41, 181, 232, 0.95); color: white !important;
            box-shadow: 0 2px 6px rgba(0,0,0,0.25);}}
        .req-top-btn:hover {{background: rgb(17, 86, 127);}}
        </style><a class='req-top-btn' href='#{TOP_ANCHOR}' target='_self'>↑ Top</a>""",
        unsafe_allow_html=True)


def _scroll_script() -> None:
    """Scroll the app's main area to the top after a bottom-pager click. A rerun keeps
    the scroll position, so the reader would otherwise land at the end of page 3."""
    try:
        import streamlit.components.v1 as components
        components.html(
            """<script>
            const doc = window.parent.document;
            for (const sel of ['[data-testid="stMain"]', 'section.main',
                               '[data-testid="stAppViewContainer"]']) {
              const el = doc.querySelector(sel);
              if (el) { el.scrollTo({top: 0}); }
            }
            window.parent.scrollTo({top: 0});
            </script>""",
            height=0)
    except Exception:
        pass
