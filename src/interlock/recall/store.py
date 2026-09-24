"""Persistence, behind an interface thin enough to swap.

SQLite, chosen because the deployment target is a free tier and a second
service to keep alive is a second thing that can be down during an interview.
The repository interface exists so that choice is reversible: nothing outside
this module knows what the storage is.

What this module guarantees
---------------------------
**Saving a case and recording its history are one operation.** A case saved
without its ledger entries would be a case whose history could be
reconstructed differently later, which defeats the point of having a chain.
Both go in one transaction, and the chain head advances inside it.

**The chain is global, not per case.** One sequence across every case, so an
entry cannot be removed without leaving a gap. A per-case chain would let
someone delete a whole case and leave the remaining chains verifying
perfectly.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from interlock.recall.ledger import LedgerEntry, record_opening, record_transition
from interlock.recall.state import CaseFile, CaseState, HistoryStep, Transition
from interlock.schema.audit import AuditEntry
from interlock.schema.case import RecallCase
from interlock.schema.rails import Provenance, ResponseWindow, SourceConfidence, WindowUnit
from interlock.schema.recall import RecallDispositionCode
from interlock.sla.clock import Deadline, DeadlineAuthority, deadline_for

SCHEMA_VERSION = 2
"""Bumped whenever the ``cases`` table's constraints change.

SQLite cannot add a CHECK to an existing table, and ``CREATE TABLE IF NOT
EXISTS`` silently keeps whatever table was already there. So a database created
before the constraints existed kept running without them - a review found
exactly that. The version lives in ``PRAGMA user_version`` and an older file is
rebuilt on open (see :meth:`SqliteCaseRepository._migrate`).
"""

_STATES = ", ".join(f"'{s.value}'" for s in CaseState)
_DISPOSITIONS = ", ".join(f"'{d.value}'" for d in RecallDispositionCode)
_AUTHORITIES = ", ".join(f"'{a.value}'" for a in DeadlineAuthority)

CASES_TABLE = f"""
CREATE TABLE {{name}} (
    case_id             TEXT PRIMARY KEY,
    case_json           TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ({_STATES})),
    disposition         TEXT CHECK (disposition IS NULL OR disposition IN ({_DISPOSITIONS})),
    disposed_by         TEXT,
    disposed_at         TEXT,
    disposition_reason  TEXT,
    history_json        TEXT NOT NULL,
    due_at              TEXT NOT NULL,
    deadline_authority  TEXT NOT NULL CHECK (deadline_authority IN ({_AUTHORITIES})),
    deadline_json       TEXT NOT NULL,

    -- The disposition guarantee, at the storage layer. CaseFile.__post_init__
    -- enforces it in Python; this stops a row that violates it being written
    -- at all, by any future writer, including one that bypasses the model.
    -- Enumerated values rather than NOT NULL alone, because an earlier version
    -- accepted disposition = '' and so could be satisfied by an empty string.
    CHECK (state != 'disposed' OR disposition IS NOT NULL),
    CHECK (state = 'disposed' OR disposition IS NULL),
    -- IS NOT NULL spelled out: a CHECK that evaluates to NULL passes, so
    -- length(trim(NULL)) > 0 alone would wave through a missing actor.
    CHECK (state != 'disposed' OR (
        disposed_by IS NOT NULL AND length(trim(disposed_by)) > 0 AND disposed_at IS NOT NULL
    )),
    CHECK (state = 'disposed' OR (disposed_by IS NULL AND disposed_at IS NULL))
)
"""

SCHEMA = f"""
{CASES_TABLE.format(name="IF NOT EXISTS cases")};

CREATE TABLE IF NOT EXISTS ai_checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    feature   TEXT NOT NULL CHECK (feature IN ('intake', 'suggestion', 'draft')),
    outcome   TEXT NOT NULL CHECK (outcome IN ('ok', 'dropped', 'rejected', 'unavailable')),
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    sequence_number  INTEGER PRIMARY KEY,
    entry_json       TEXT NOT NULL,
    payload_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
CREATE INDEX IF NOT EXISTS idx_cases_due ON cases(due_at);
"""


LEGACY_REQUIRED_COLUMNS = frozenset(
    {
        "case_id",
        "case_json",
        "state",
        "disposition",
        "disposed_by",
        "disposed_at",
        "disposition_reason",
        "history_json",
        "due_at",
        "deadline_authority",
    }
)


class LegacyDatabaseError(RuntimeError):
    """An older database holds rows the current constraints refuse.

    Raised rather than repaired: a disposed case with no disposition is the
    failure this product exists to prevent, and silently inventing one during
    a migration would manufacture evidence.
    """


class CaseRepository(Protocol):
    """What the rest of the system may assume about storage."""

    def open_new(self, case: RecallCase) -> CaseFile: ...
    def save_transition(self, before: CaseFile, after: CaseFile) -> CaseFile: ...
    def get(self, case_id: str) -> CaseFile | None: ...
    def open_cases(self) -> list[CaseFile]: ...
    def all_cases(self) -> list[CaseFile]: ...
    def audit_entries(self) -> list[LedgerEntry]: ...


class SqliteCaseRepository:
    """The one implementation.

    Deliberately boring. The interesting invariants live in the state machine
    and the ledger; this just has to not lose anything and not let the two
    disagree.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        # check_same_thread=False because uvicorn serves requests on a thread
        # pool. Every write goes through one lock below, so concurrent use is
        # serialised rather than merely permitted.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Migrate before switching to WAL: the journal mode is written into
        # the file header, and a refused legacy file must be left untouched.
        self._migrate()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate(self) -> None:
        """Bring an older database up to the current constraints, or refuse.

        A fresh database gets the current schema directly. An older one is
        rebuilt into a constrained table inside one transaction; if any row
        breaks the new constraints the whole rebuild rolls back and
        :class:`LegacyDatabaseError` is raised, leaving the file untouched.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cases'"
        ).fetchone()

        if exists and version < SCHEMA_VERSION:
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(cases)")}
            missing = LEGACY_REQUIRED_COLUMNS - columns
            if missing:
                raise LegacyDatabaseError(
                    f"{self._path} has no {', '.join(sorted(missing))} column(s); it predates "
                    f"anything this version can migrate. Nothing was changed."
                )
            try:
                self._conn.execute("BEGIN")
                self._conn.execute(CASES_TABLE.format(name="cases_migrated"))
                for row in self._conn.execute("SELECT * FROM cases").fetchall():
                    self._copy_legacy_row(row, columns)
                self._conn.execute("DROP TABLE cases")
                self._conn.execute("ALTER TABLE cases_migrated RENAME TO cases")
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.execute("COMMIT")
            except (sqlite3.Error, ValueError, IndexError, KeyError) as refused:
                self._conn.execute("ROLLBACK")
                raise LegacyDatabaseError(
                    f"{self._path} holds a case the current constraints refuse ({refused}). "
                    f"Nothing was changed. Repair the row by hand; it will not be guessed."
                ) from refused

        with self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _copy_legacy_row(self, row: sqlite3.Row, columns: set[str]) -> None:
        """Insert one pre-v2 row into the constrained table.

        Rows written before ``deadline_json`` existed get it reconstructed from
        the stored due_at and authority - the two parts that were persisted -
        with the window and reason from the matrix, and the reason says so.
        """
        case = RecallCase.model_validate_json(row["case_json"])
        if "deadline_json" in columns and row["deadline_json"]:
            deadline_json = row["deadline_json"]
        else:
            rebuilt = deadline_for(case)
            deadline_json = _deadline_to_json(
                Deadline(
                    due_at=datetime.fromisoformat(row["due_at"]),
                    authority=DeadlineAuthority(row["deadline_authority"]),
                    window=rebuilt.window,
                    reason=rebuilt.reason
                    + " [Window and reason reconstructed during migration; only the due "
                    "time and authority were stored originally.]",
                )
            )
        self._conn.execute(
            """
            INSERT INTO cases_migrated (case_id, case_json, state, disposition, disposed_by,
                               disposed_at, disposition_reason, history_json,
                               due_at, deadline_authority, deadline_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["case_id"],
                row["case_json"],
                row["state"],
                row["disposition"],
                row["disposed_by"],
                row["disposed_at"],
                row["disposition_reason"],
                row["history_json"],
                row["due_at"],
                row["deadline_authority"],
                deadline_json,
            ),
        )

    def close(self) -> None:
        self._conn.close()

    # -- chain -------------------------------------------------------------

    def _head(self) -> AuditEntry | None:
        row = self._conn.execute(
            "SELECT entry_json FROM audit ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        return AuditEntry.model_validate_json(row["entry_json"]) if row else None

    def _append(self, item: LedgerEntry) -> None:
        self._conn.execute(
            "INSERT INTO audit (sequence_number, entry_json, payload_json) VALUES (?, ?, ?)",
            (
                item.entry.sequence_number,
                item.entry.model_dump_json(),
                json.dumps(item.payload, sort_keys=True, separators=(",", ":"), default=str),
            ),
        )

    def audit_entries(self) -> list[LedgerEntry]:
        rows = self._conn.execute(
            "SELECT entry_json, payload_json FROM audit ORDER BY sequence_number"
        ).fetchall()
        return [
            LedgerEntry(
                entry=AuditEntry.model_validate_json(r["entry_json"]),
                payload=json.loads(r["payload_json"]),
            )
            for r in rows
        ]

    # -- cases -------------------------------------------------------------

    def _write_case(self, case_file: CaseFile) -> None:
        self._conn.execute(
            """
            INSERT INTO cases (case_id, case_json, state, disposition, disposed_by,
                               disposed_at, disposition_reason, history_json,
                               due_at, deadline_authority, deadline_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                state = excluded.state,
                disposition = excluded.disposition,
                disposed_by = excluded.disposed_by,
                disposed_at = excluded.disposed_at,
                disposition_reason = excluded.disposition_reason,
                history_json = excluded.history_json
            """,
            (
                case_file.case_id,
                case_file.case.model_dump_json(),
                case_file.state.value,
                case_file.disposition.value if case_file.disposition else None,
                case_file.disposed_by,
                case_file.disposed_at.isoformat() if case_file.disposed_at else None,
                case_file.disposition_reason,
                json.dumps(
                    [
                        {
                            "transition": s.transition.value,
                            "actor": s.actor,
                            "at": s.at.isoformat(),
                            "reason": s.reason,
                            "ai_suggested": s.ai_suggested.value if s.ai_suggested else None,
                        }
                        for s in case_file.history
                    ]
                ),
                case_file.deadline.due_at.isoformat(),
                case_file.deadline.authority.value,
                _deadline_to_json(case_file.deadline),
            ),
        )

    def open_new(self, case: RecallCase) -> CaseFile:
        """Persist a newly arrived case and record its arrival on the chain."""
        from interlock.recall.state import open_case

        case_file = open_case(case)
        with self._conn:
            if self._conn.execute(
                "SELECT 1 FROM cases WHERE case_id = ?", (case.case_id,)
            ).fetchone():
                raise ValueError(
                    f"{case.case_id} already exists; a replayed message must not overwrite "
                    "a case whose history has already begun"
                )
            self._write_case(case_file)
            self._append(record_opening(case_file, previous=self._head()))
        return case_file

    def save_transition(self, before: CaseFile, after: CaseFile) -> CaseFile:
        """Persist a state change and its ledger entry together.

        Takes both sides so it can record exactly the step that was applied
        rather than inferring it, and so a caller passing an unrelated pair is
        caught here instead of writing a misleading audit entry.
        """
        if before.case_id != after.case_id:
            raise ValueError("before and after refer to different cases")
        if len(after.history) != len(before.history) + 1:
            raise ValueError(
                "expected exactly one new transition; save each step as it is applied so "
                "the chain records them individually"
            )

        step: HistoryStep = after.history[-1]
        with self._conn:
            self._write_case(after)
            self._append(record_transition(after, step, previous=self._head()))
        return after

    # -- AI check log ------------------------------------------------------

    def record_ai_check(self, feature: str, outcome: str, detail: str | None = None) -> None:
        """Log what the deterministic checks did with one model answer.

        Operational telemetry, not evidence: it is not on the hash chain,
        because it records the model's behaviour rather than a case's.
        """
        from interlock.schema.common import utc_now

        with self._conn:
            self._conn.execute(
                "INSERT INTO ai_checks (at, feature, outcome, detail) VALUES (?, ?, ?, ?)",
                (utc_now().isoformat(), feature, outcome, (detail or "")[:300] or None),
            )

    def ai_check_counts(self) -> dict[tuple[str, str], int]:
        rows = self._conn.execute(
            "SELECT feature, outcome, COUNT(*) AS n FROM ai_checks GROUP BY feature, outcome"
        ).fetchall()
        return {(r["feature"], r["outcome"]): r["n"] for r in rows}

    def ai_check_problems(self, limit: int = 15) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT at, feature, outcome, detail FROM ai_checks "
            "WHERE outcome IN ('dropped', 'rejected') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- reads -------------------------------------------------------------

    def _hydrate(self, row: sqlite3.Row) -> CaseFile:
        case = RecallCase.model_validate_json(row["case_json"])
        history = tuple(
            HistoryStep(
                transition=Transition(h["transition"]),
                actor=h["actor"],
                at=datetime.fromisoformat(h["at"]),
                reason=h["reason"],
                ai_suggested=Transition(h["ai_suggested"]) if h.get("ai_suggested") else None,
            )
            for h in json.loads(row["history_json"])
        )
        # The whole deadline as it was when the case opened - due time,
        # authority, window and the reason shown to the operator - not as the
        # current matrix would compute it. The first fix stored only due_at and
        # authority and recomputed the rest, so a rules change would leave a
        # case due on the old date while explaining itself with the new rule.
        stored = _deadline_from_json(row["deadline_json"])
        if stored.due_at != datetime.fromisoformat(row["due_at"]):
            raise ValueError(f"{case.case_id}: stored deadline and due_at column disagree")

        return CaseFile(
            case=case,
            state=CaseState(row["state"]),
            deadline=stored,
            history=history,
            disposition=(RecallDispositionCode(row["disposition"]) if row["disposition"] else None),
            disposed_by=row["disposed_by"],
            disposed_at=(
                datetime.fromisoformat(row["disposed_at"]) if row["disposed_at"] else None
            ),
            disposition_reason=row["disposition_reason"],
        )

    def get(self, case_id: str) -> CaseFile | None:
        row = self._conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return self._hydrate(row) if row else None

    def open_cases(self) -> list[CaseFile]:
        rows = self._conn.execute(
            "SELECT * FROM cases WHERE state != ? ORDER BY due_at",
            (CaseState.DISPOSED.value,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def all_cases(self) -> list[CaseFile]:
        rows = self._conn.execute("SELECT * FROM cases ORDER BY due_at").fetchall()
        return [self._hydrate(r) for r in rows]


@contextmanager
def sqlite_repository(path: str | Path = ":memory:") -> Iterator[SqliteCaseRepository]:
    """A repository that closes itself."""
    repo = SqliteCaseRepository(path)
    with closing(repo._conn):
        yield repo


def _deadline_to_json(deadline: Deadline) -> str:
    window = deadline.window
    return json.dumps(
        {
            "due_at": deadline.due_at.isoformat(),
            "authority": deadline.authority.value,
            "reason": deadline.reason,
            "window": {
                "amount": window.amount,
                "unit": window.unit.value,
                "fraud_exempt": window.fraud_exempt,
                "provenance": {
                    "claim": window.provenance.claim,
                    "source_url": window.provenance.source_url,
                    "confidence": window.provenance.confidence.value,
                    "retrieved": window.provenance.retrieved.isoformat(),
                    "note": window.provenance.note,
                },
            },
        },
        sort_keys=True,
    )


def _deadline_from_json(raw: str) -> Deadline:
    data = json.loads(raw)
    window = data["window"]
    provenance = window["provenance"]
    return Deadline(
        due_at=datetime.fromisoformat(data["due_at"]),
        authority=DeadlineAuthority(data["authority"]),
        reason=data["reason"],
        window=ResponseWindow(
            amount=window["amount"],
            unit=WindowUnit(window["unit"]),
            fraud_exempt=window["fraud_exempt"],
            provenance=Provenance(
                claim=provenance["claim"],
                source_url=provenance["source_url"],
                confidence=SourceConfidence(provenance["confidence"]),
                retrieved=date.fromisoformat(provenance["retrieved"]),
                note=provenance["note"],
            ),
        ),
    )
