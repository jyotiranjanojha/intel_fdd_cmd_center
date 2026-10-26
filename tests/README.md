# Tests

Run from the app root (`RULE_REPOSITORY_APP/`):

```
python3 -m unittest discover -s tests -t .
```

Stdlib `unittest`, not pytest — pytest is not available in the Snowflake sandbox,
and these need to run anywhere without an install step.

**These are not deployed.** `tests/` is deliberately absent from `snowflake.yml`
artifacts; it is developer tooling, not app runtime code.

## What is covered, and why these modules

| File | Covers | Why it matters |
|---|---|---|
| `test_md_to_html.py` | The markdown → HTML converter behind the FDD export | Pure text transformation with many shapes (headers, nested bullets, tables). Easy to break silently. |
| `test_fdd_render.py`, `test_short_rule_format.py` | `fdd_render.render_rule()` (short format) / `render_rule_classic()` / `rule_summary()` | The single source of truth for how every rule reads on every page and in every export. One regression here changes all 131 rules at once. |
| `test_rollback_ordering.py` | `_apply_rollback()` ordering | Guards a real bug: ids come from block-reserving sequences and are **not** monotonic, so rollback must order by `CHANGED_TS`. Comparing ids silently under-applies a rollback. |
| `test_fdd_import.py` | The FDD paste-importer: section splitting, object/column/rule parsing, column reconciliation, plan arithmetic | The importer decides what a whole document becomes. Two bugs it caught on first run: `"Rule 3: ..."` was being swallowed as a *section heading* (so the rule vanished silently), and `DB.SCH.T_X` failed the technical-name pattern. Both are the exact failure this parser exists to prevent. |

`fdd_render` and `md_to_html` are pure (no database, no Streamlit), which is why
they are testable at all — `md_to_html` was extracted out of `fdd_publish.py`
specifically so it could be tested without stubbing the whole data layer.
`test_rollback_ordering.py` does need stubs, so it installs minimal ones inline
and is self-contained.

## Not covered here

Anything needing a live Snowflake session — `reserve_ids()` against real
sequences, `save_rule()` transaction behaviour, the 131-rule render audit. Those
were verified with throwaway scripts against the live database. If they are worth
keeping, they belong in a separate integration suite that is explicitly opt-in,
because they cost warehouse time and write (then roll back) real rows.

## Writing new tests

Keep the pass-through `cache_data` stub if you add one. A `MagicMock` there makes
every `@st.cache_data` function return a mock, which produces tests that pass
while asserting nothing — that has already bitten this project once.
