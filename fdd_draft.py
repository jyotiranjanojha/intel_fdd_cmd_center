"""Saved Import FDD reviews (FDD_IMPORT_DRAFT), so progress survives a reload, a
session timeout or a redeploy.

The whole review lives in st.session_state, and nothing used to be written until
Import - a large FDD reviewed for an hour was lost with the tab. A draft holds
everything needed to redraw the review exactly, with no model call on resume: the
plan (every fix, ignore, undo, hop tick and update/skip choice), the edited rule
texts, the pasted or uploaded text and its workbook row map, the module and mode.

Storage: zlib-compressed JSON, base64, in parts of PART_SIZE characters
(FDD_IMPORT_DRAFT_PART), so no statement ever gets near Snowflake's size limits.
Measured: 29 rules is 180 KB of JSON and 8 KB compressed.
"""

import base64
import hashlib
import json
import time
import zlib
from typing import Callable, Dict, List, Optional

PART_SIZE = 400_000
AUTOSAVE_SECONDS = 4

# session_state keys that make up a review. Rule-text keys are matched by prefix.
STATE_KEYS = ("fddimp_plan", "fddimp_text", "fddimp_module", "fddimp_mode",
              "fddimp_timing", "fddimp_xlsx_map", "fddimp_open")
STATE_PREFIXES = ("fddimp_rule_text_",)


class DraftConflict(Exception):
    """The draft was saved elsewhere (another tab or person) since it was opened."""

    def __init__(self, saved_by: str, saved_ts) -> None:
        super().__init__(f"Saved by {saved_by} at {saved_ts} since you opened it.")
        self.saved_by, self.saved_ts = saved_by, saved_ts


# ------------------------------------------------------------------
# Packing
# ------------------------------------------------------------------
def capture(state) -> Dict:
    """The parts of session_state that make up the review."""
    out = {k: state[k] for k in STATE_KEYS if k in state}
    for key in list(state.keys()):
        if any(str(key).startswith(p) for p in STATE_PREFIXES):
            out[key] = state[key]
    return out


def restore(state, payload: Dict) -> None:
    """Put a captured review back. Old review keys are cleared first."""
    for key in list(state.keys()):
        if str(key) in STATE_KEYS or any(str(key).startswith(p) for p in STATE_PREFIXES):
            del state[key]
    for key, value in (payload or {}).items():
        state[key] = value


def pack(payload: Dict) -> List[str]:
    raw = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
    blob = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return [blob[i:i + PART_SIZE] for i in range(0, len(blob), PART_SIZE)] or [""]


def unpack(parts: List[str]) -> Dict:
    blob = "".join(parts)
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8")) if blob else {}


def fingerprint(payload: Dict) -> str:
    return hashlib.sha1(json.dumps(payload, default=str, sort_keys=True)
                        .encode("utf-8")).hexdigest()


def summary(payload: Dict) -> Dict:
    """Title, rule and open-problem counts for the list of saved reviews."""
    plan = payload.get("fddimp_plan") or {}
    rules = plan.get("rules") or []
    open_issues = 0
    for entry in rules:
        handled = entry.get("resolutions") or {}
        open_issues += sum(1 for i in entry.get("issues") or [] if i.get("id") not in handled)
        open_issues += 1 if entry.get("errors") else 0
    held = payload.get("fddimp_xlsx_map") or {}
    objects = plan.get("objects") or []
    title = (held.get("file") or (objects[0].get("object_name") if objects else "")
             or str(payload.get("fddimp_text") or "")[:60].split("\n")[0])
    return {"module_name": plan.get("module_name") or payload.get("fddimp_module") or "",
            "title": str(title or "Untitled FDD")[:500], "rule_count": len(rules),
            "open_issues": open_issues}


# ------------------------------------------------------------------
# Storage
# ------------------------------------------------------------------
class DraftStore:
    """Read and write drafts. read(sql) -> DataFrame, execute(sql); injectable for tests."""

    def __init__(self, read: Callable, execute: Callable, next_id: Callable,
                 value: Callable, table: Callable, actor: Callable) -> None:
        self.read, self.execute, self.next_id = read, execute, next_id
        self.value, self.table, self.actor = value, table, actor

    @classmethod
    def default(cls) -> "DraftStore":
        from db import execute, reserve_ids, run_query, sql_value, tbl
        from repository import _actor
        return cls(read=run_query, execute=execute,
                   next_id=lambda: reserve_ids("FDD_IMPORT_DRAFT", "DRAFT_ID", 1)[0],
                   value=sql_value, table=tbl, actor=_actor)

    def _header(self, draft_id: int):
        df = self.read(f"SELECT * FROM {self.table('FDD_IMPORT_DRAFT')} "
                       f"WHERE DRAFT_ID = {int(draft_id)}")
        return None if df is None or df.empty else df.iloc[0]

    def save(self, payload: Dict, *, draft_id: Optional[int] = None,
             version: Optional[int] = None, team_id: Optional[int] = None,
             module_id: Optional[int] = None, performed_by: Optional[str] = None,
             force: bool = False) -> Dict:
        """Create or update a draft. Returns {draft_id, version}. Raises DraftConflict
        when the stored version is newer than `version` (unless force)."""
        info = summary(payload)
        parts = pack(payload)
        who = self.actor(performed_by)
        v = self.value
        if draft_id is None:
            draft_id, new_version = int(self.next_id()), 1
            self.execute(f"""
                INSERT INTO {self.table('FDD_IMPORT_DRAFT')}
                    (DRAFT_ID, MODULE_NAME, MODULE_ID, TEAM_ID, TITLE, RULE_COUNT, OPEN_ISSUES,
                     STATUS, VERSION_NO, CREATED_BY, UPDATED_BY, UPDATED_TS)
                SELECT {draft_id}, {v(info['module_name'])},
                       {int(module_id) if module_id is not None else 'NULL'},
                       {int(team_id) if team_id is not None else 'NULL'},
                       {v(info['title'])}, {info['rule_count']}, {info['open_issues']},
                       'OPEN', 1, {who}, {who}, CURRENT_TIMESTAMP()""")
        else:
            header = self._header(draft_id)
            if header is None:
                return self.save(payload, team_id=team_id, module_id=module_id,
                                 performed_by=performed_by)
            stored = int(header["VERSION_NO"])
            if not force and version is not None and stored != int(version):
                raise DraftConflict(header.get("UPDATED_BY"), header.get("UPDATED_TS"))
            new_version = stored + 1
            self.execute(f"""
                UPDATE {self.table('FDD_IMPORT_DRAFT')}
                SET VERSION_NO = {new_version}, MODULE_NAME = {v(info['module_name'])},
                    TITLE = {v(info['title'])}, RULE_COUNT = {info['rule_count']},
                    OPEN_ISSUES = {info['open_issues']}, STATUS = 'OPEN',
                    MODULE_ID = {int(module_id) if module_id is not None else 'MODULE_ID'},
                    UPDATED_BY = {who}, UPDATED_TS = CURRENT_TIMESTAMP()
                WHERE DRAFT_ID = {int(draft_id)}""")
        for number, data in enumerate(parts):
            self.execute(f"""
                INSERT INTO {self.table('FDD_IMPORT_DRAFT_PART')}
                    (DRAFT_ID, VERSION_NO, PART_NO, DATA)
                SELECT {int(draft_id)}, {new_version}, {number}, {v(data)}""")
        # Only the current version is kept; the header says which one that is.
        self.execute(f"DELETE FROM {self.table('FDD_IMPORT_DRAFT_PART')} "
                     f"WHERE DRAFT_ID = {int(draft_id)} AND VERSION_NO < {new_version}")
        return {"draft_id": int(draft_id), "version": new_version}

    def load(self, draft_id: int) -> Dict:
        """{payload, version, header} of a draft's current version."""
        header = self._header(draft_id)
        if header is None:
            raise KeyError(f"Draft {draft_id} does not exist.")
        version = int(header["VERSION_NO"])
        df = self.read(f"SELECT PART_NO, DATA FROM {self.table('FDD_IMPORT_DRAFT_PART')} "
                       f"WHERE DRAFT_ID = {int(draft_id)} AND VERSION_NO = {version} "
                       f"ORDER BY PART_NO")
        parts = [] if df is None or df.empty else df["DATA"].tolist()
        return {"payload": unpack(parts), "version": version, "header": header}

    def list_open(self, team_id: Optional[int] = None, owner: Optional[str] = None):
        """Open drafts, newest first. owner limits them to ones that person created or
        last saved; without it every open draft of the team is returned, as before."""
        team = f"AND TEAM_ID = {int(team_id)}" if team_id is not None else ""
        mine = (f"AND (UPPER(CREATED_BY) = UPPER({self.value(owner)}) "
                f"OR UPPER(UPDATED_BY) = UPPER({self.value(owner)}))") if owner else ""
        return self.read(f"""
            SELECT DRAFT_ID, MODULE_NAME, TITLE, RULE_COUNT, OPEN_ISSUES, VERSION_NO,
                   CREATED_BY, UPDATED_BY, UPDATED_TS
            FROM {self.table('FDD_IMPORT_DRAFT')}
            WHERE STATUS = 'OPEN' {team} {mine}
            ORDER BY UPDATED_TS DESC NULLS LAST LIMIT 50""")

    def set_status(self, draft_id: int, status: str, performed_by: Optional[str] = None,
                   rule_ids: Optional[List[int]] = None) -> None:
        ids = ",".join(str(int(i)) for i in rule_ids or [])[:4000]
        self.execute(f"""
            UPDATE {self.table('FDD_IMPORT_DRAFT')}
            SET STATUS = {self.value(status)},
                IMPORTED_RULE_IDS = {self.value(ids) if ids else 'IMPORTED_RULE_IDS'},
                UPDATED_BY = {self.actor(performed_by)}, UPDATED_TS = CURRENT_TIMESTAMP()
            WHERE DRAFT_ID = {int(draft_id)}""")


# ------------------------------------------------------------------
# Autosave decision
# ------------------------------------------------------------------
def should_autosave(payload: Dict, last: Optional[Dict], now: Optional[float] = None) -> bool:
    """Save only when something changed, and not more often than AUTOSAVE_SECONDS."""
    if not (payload or {}).get("fddimp_plan"):
        return False
    if not last:
        return True
    if fingerprint(payload) == last.get("fingerprint"):
        return False
    return (now if now is not None else time.time()) - float(last.get("at", 0)) \
        >= AUTOSAVE_SECONDS


def remaining_after_import(plan: Dict) -> bool:
    """True when hops were left for a later part, so the draft should stay open."""
    return any(spec.get("import_now") is False for spec in plan.get("rule_sets") or [])
