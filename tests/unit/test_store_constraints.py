"""Storage refuses what the model refuses, including on databases older than the rules.

The second review found three routes round the storage guarantee: the CHECKs
only applied to freshly created files, ``disposition = ''`` satisfied
``IS NOT NULL``, and a disposal with no actor or time was accepted. It also
found the deadline half-stored. Each test below is one of those routes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from interlock.recall import store as store_module
from interlock.recall.state import Transition
from interlock.recall.store import SCHEMA_VERSION, LegacyDatabaseError, SqliteCaseRepository
from interlock.schema.case import CaseState
from interlock.schema.recall import RecallDispositionCode
from tests.unit.test_ledger_and_evidence import RECEIVED_AT, a_case

LEGACY_SCHEMA = """
CREATE TABLE cases (
    case_id TEXT PRIMARY KEY, case_json TEXT NOT NULL, state TEXT NOT NULL,
    disposition TEXT, disposed_by TEXT, disposed_at TEXT, disposition_reason TEXT,
    history_json TEXT NOT NULL, due_at TEXT NOT NULL, deadline_authority TEXT NOT NULL
);
CREATE TABLE audit (
    sequence_number INTEGER PRIMARY KEY, entry_json TEXT NOT NULL, payload_json TEXT NOT NULL
);
"""


def _legacy_db(path: Path, *, state: str = "received", disposition: str | None = None) -> None:
    case = a_case()
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO cases VALUES (?, ?, ?, ?, NULL, NULL, NULL, '[]', ?, 'rail_rule')",
        (
            case.case_id,
            case.model_dump_json(),
            state,
            disposition,
            "2026-09-28T23:59:59+00:00",
        ),
    )
    conn.commit()
    conn.close()


def _raw_update(repo: SqliteCaseRepository, sql: str, *args) -> None:
    with repo._conn:
        repo._conn.execute(sql, args)


class TestConstraintsOnANewDatabase:
    @pytest.fixture
    def repo(self) -> SqliteCaseRepository:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(a_case())
        return repo

    def test_empty_string_disposition_is_refused(self, repo) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_update(
                repo,
                "UPDATE cases SET state='disposed', disposition='', disposed_by='op', "
                "disposed_at='2026-09-15T00:00:00+00:00'",
            )

    def test_unknown_disposition_is_refused(self, repo) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_update(
                repo,
                "UPDATE cases SET state='disposed', disposition='made_up', disposed_by='op', "
                "disposed_at='2026-09-15T00:00:00+00:00'",
            )

    def test_unknown_state_is_refused(self, repo) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_update(repo, "UPDATE cases SET state='closed'")

    @pytest.mark.parametrize(
        ("by", "at"),
        [(None, "2026-09-15T00:00:00+00:00"), ("   ", "2026-09-15T00:00:00+00:00"), ("op", None)],
    )
    def test_disposal_without_actor_or_time_is_refused(self, repo, by, at) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_update(
                repo,
                "UPDATE cases SET state='disposed', disposition='funds_returned', "
                "disposed_by=?, disposed_at=?",
                by,
                at,
            )

    def test_file_is_stamped_with_the_schema_version(self, repo) -> None:
        assert repo._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


class TestTheModelRefusesTheSame:
    def test_disposed_without_actor(self) -> None:
        opened = SqliteCaseRepository(":memory:").open_new(a_case())
        closed = opened.apply(Transition.DISPOSE_FUNDS_RETURNED, actor="op")
        with pytest.raises(ValueError, match="who made it and when"):
            replace(closed, disposed_by=None)
        with pytest.raises(ValueError, match="who made it and when"):
            replace(closed, disposed_at=None)

    def test_open_case_claiming_a_disposer(self) -> None:
        opened = SqliteCaseRepository(":memory:").open_new(a_case())
        with pytest.raises(ValueError, match="while still"):
            replace(opened, disposed_by="op")


class TestLegacyDatabases:
    def test_a_clean_legacy_file_is_migrated_and_constrained(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        _legacy_db(path)

        repo = SqliteCaseRepository(path)
        fetched = repo.get(a_case().case_id)
        assert fetched is not None
        assert "reconstructed during migration" in fetched.deadline.reason
        assert repo._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        with pytest.raises(sqlite3.IntegrityError):
            _raw_update(repo, "UPDATE cases SET state='disposed', disposition=''")

    def test_a_corrupt_legacy_row_is_refused_not_repaired(self, tmp_path: Path) -> None:
        """Inventing a disposition during migration would manufacture evidence."""
        path = tmp_path / "corrupt.db"
        _legacy_db(path, state="disposed", disposition="")

        with pytest.raises(LegacyDatabaseError, match="Nothing was changed"):
            SqliteCaseRepository(path)

        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT disposition FROM cases").fetchone()[0] == ""
        conn.close()

    def test_reopening_a_current_file_changes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "current.db"
        first = SqliteCaseRepository(path)
        opened = first.open_new(a_case())
        first.save_transition(opened, opened.apply(Transition.DISPOSE_FUNDS_RETURNED, actor="op"))
        first.close()

        again = SqliteCaseRepository(path)
        fetched = again.get(a_case().case_id)
        assert fetched is not None
        assert fetched.state is CaseState.DISPOSED
        assert fetched.disposition is RecallDispositionCode.FUNDS_RETURNED


class TestTheWholeDeadlineIsStored:
    def test_a_rules_change_does_not_rewrite_an_old_case(self, monkeypatch) -> None:
        repo = SqliteCaseRepository(":memory:")
        opened = repo.open_new(a_case())
        original = opened.deadline

        def a_different_rule(case, *, received_at=None):
            return replace(
                original,
                due_at=RECEIVED_AT + timedelta(hours=1),
                reason="A new rule nobody agreed to at the time",
                window=replace(original.window, amount=1),
            )

        monkeypatch.setattr(store_module, "deadline_for", a_different_rule)
        fetched = repo.get(opened.case_id)
        assert fetched is not None
        assert fetched.deadline == original

    def test_due_at_column_and_stored_deadline_must_agree(self) -> None:
        repo = SqliteCaseRepository(":memory:")
        repo.open_new(a_case())
        _raw_update(repo, "UPDATE cases SET due_at='2030-01-01T00:00:00+00:00'")
        with pytest.raises(ValueError, match="disagree"):
            repo.get(a_case().case_id)


class TestRecallCaseNoLongerCarriesState:
    def test_legacy_json_with_received_state_still_loads(self) -> None:
        from interlock.schema.case import RecallCase

        legacy = a_case().model_dump(mode="json") | {"state": "received"}
        assert RecallCase.model_validate(legacy) == a_case()

    def test_any_other_state_on_the_wire_model_is_refused(self) -> None:
        from pydantic import ValidationError

        from interlock.schema.case import RecallCase

        with pytest.raises(ValidationError, match="no longer carries state"):
            RecallCase.model_validate(a_case().model_dump(mode="json") | {"state": "disposed"})

    def test_the_field_is_gone(self) -> None:
        from interlock.schema.case import RecallCase

        assert "state" not in RecallCase.model_fields


class TestRefusedFilesAreUntouched:
    def test_bytes_identical_after_refusal(self, tmp_path: Path) -> None:
        import hashlib

        path = tmp_path / "corrupt.db"
        _legacy_db(path, state="disposed", disposition="")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with pytest.raises(LegacyDatabaseError):
            SqliteCaseRepository(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    def test_missing_columns_are_a_clear_refusal(self, tmp_path: Path) -> None:
        path = tmp_path / "ancient.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cases (case_id TEXT PRIMARY KEY, case_json TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(LegacyDatabaseError, match="column"):
            SqliteCaseRepository(path)
