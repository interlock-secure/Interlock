"""M10: can somebody else actually run this, and is the contact reachable?

A repository that only its authors can start is not deployed, it is stored.
These tests check the things that break that: a stale README, a container that
runs as root, a database path that only works on one machine, and a support
address nobody can find.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from interlock.config import Settings, settings

ROOT = Path(__file__).resolve().parents[2]


class TestTheSupportContact:
    def test_it_has_a_default(self) -> None:
        assert "@" in settings().support_email

    def test_the_environment_overrides_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One variable, not a code change."""
        monkeypatch.setenv("INTERLOCK_SUPPORT_EMAIL", "ops@example.test")
        assert settings().support_email == "ops@example.test"

    def test_the_default_is_organisational_not_personal(self) -> None:
        """A personal mailbox in a public repository is scraped within days
        and cannot be unpublished. Pointing this elsewhere should be a
        deliberate choice, so the default is an organisational alias."""
        default = Settings().support_email
        assert default.startswith("support@")
        assert "gmail" not in default and "outlook" not in default

    def test_the_readme_publishes_it(self) -> None:
        readme = (ROOT / "README.md").read_text()
        assert Settings().support_email in readme

    def test_the_readme_documents_how_to_change_it(self) -> None:
        readme = (ROOT / "README.md").read_text()
        assert "INTERLOCK_SUPPORT_EMAIL" in readme


class TestConfiguration:
    def test_every_setting_reads_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("INTERLOCK_INSTITUTION_NAME", "Testville Mutual")
        monkeypatch.setenv("INTERLOCK_DATABASE", ":memory:")
        monkeypatch.setenv("INTERLOCK_SEED_DEMO", "0")
        monkeypatch.setenv("INTERLOCK_DEMO_SEED", "7")

        config = settings()
        assert config.institution_name == "Testville Mutual"
        assert config.database_path == ":memory:"
        assert config.seed_demo_data is False
        assert config.demo_seed == 7

    def test_no_absolute_developer_paths_leak_into_defaults(self) -> None:
        """A default that only works on the machine it was written on."""
        config = Settings()
        assert not config.database_path.startswith("/home")
        assert not config.database_path.startswith("/Users")


class TestTheContainer:
    """Properties of the Dockerfile, not of a built image.

    No Docker daemon was available where this was written, so the image has
    never been built. These tests catch the mistakes that are visible in the
    file - running as root, a database path on a read-only layer, a health
    check pointing at nothing - and cannot catch the ones that only appear at
    build time. The README says so too.
    """

    @pytest.fixture
    def dockerfile(self) -> str:
        return (ROOT / "Dockerfile").read_text()

    def test_it_exists(self, dockerfile: str) -> None:
        assert "FROM python" in dockerfile

    def test_it_does_not_run_as_root(self, dockerfile: str) -> None:
        assert re.search(r"^USER\s+interlock", dockerfile, re.MULTILINE)

    def test_it_has_a_health_check(self, dockerfile: str) -> None:
        assert "HEALTHCHECK" in dockerfile
        assert "/healthz" in dockerfile

    def test_the_database_lives_on_a_writable_path(self, dockerfile: str) -> None:
        """Otherwise the first write fails on a read-only image layer."""
        assert "INTERLOCK_DATABASE=/data" in dockerfile
        assert "mkdir -p /data" in dockerfile

    def test_the_build_context_excludes_the_virtualenv(self) -> None:
        ignore = (ROOT / ".dockerignore").read_text()
        assert ".venv" in ignore
        assert ".git" in ignore


class TestTheReadmeIsNotStale:
    @pytest.fixture
    def readme(self) -> str:
        return (ROOT / "README.md").read_text()

    def test_the_run_command_matches_the_real_entrypoint(self, readme: str) -> None:
        assert "interlock.api.app:app" in readme
        assert "interlock.api.app:app" in (ROOT / "Dockerfile").read_text()

    def test_it_states_the_data_is_synthetic(self, readme: str) -> None:
        assert "synthetic" in readme.lower()

    def test_it_names_the_unverified_rules(self, readme: str) -> None:
        """The epistemic debt belongs on the front page, not in an appendix."""
        assert "UNVERIFIED" in readme

    def test_it_states_what_the_project_does_not_prove(self, readme: str) -> None:
        assert "does not prove" in readme.lower()

    def test_every_referenced_path_exists(self, readme: str) -> None:
        for match in re.findall(r"`(src/[\w/.]+|docs/[\w/.]+)`", readme):
            assert (ROOT / match).exists(), f"README points at missing {match}"


class TestSecrets:
    def test_nothing_that_looks_like_a_credential_is_committed(self) -> None:
        """A grep, not a guarantee - but it catches the obvious mistake."""
        patterns = (
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
            re.compile(r"ghp_[A-Za-z0-9]{30,}"),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            re.compile(r"AKIA[0-9A-Z]{16}"),
        )
        offenders: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(
                part in path.parts for part in (".git", ".venv", "__pycache__", "data")
            ):
                continue
            if path.suffix in {".docx", ".pdf", ".png", ".db"}:
                continue
            try:
                text = path.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):  # pragma: no cover
                continue
            for pattern in patterns:
                if pattern.search(text):
                    offenders.append(str(path.relative_to(ROOT)))

        assert not offenders, f"possible credentials committed: {offenders}"

    def test_gitignore_covers_the_usual_suspects(self) -> None:
        ignore = (ROOT / ".gitignore").read_text()
        for entry in (".env", "*.pem", "*.key"):
            assert entry in ignore, f".gitignore does not cover {entry}"


class TestStartupFromCold:
    def test_the_app_starts_and_seeds_itself(self) -> None:
        """What an interviewer's first thirty seconds looks like."""
        import warnings

        from fastapi.testclient import TestClient

        from interlock.api.app import create_app

        warnings.filterwarnings("ignore")
        os.environ.pop("INTERLOCK_SEED_DEMO", None)

        with TestClient(create_app(Settings(database_path=":memory:"))) as client:
            assert client.get("/healthz").json()["status"] == "ok"
            queue = client.get("/")
            assert queue.status_code == 200
            assert "ILK-" in queue.text, "the demo did not seed"

    def test_the_demo_is_reproducible(self) -> None:
        """The instance an interviewer opens is the same one every time, so it
        can be talked through from notes."""
        import warnings

        from fastapi.testclient import TestClient

        from interlock.api.app import create_app

        warnings.filterwarnings("ignore")
        ids = []
        for _ in range(2):
            with TestClient(create_app(Settings(database_path=":memory:"))) as client:
                ids.append(re.findall(r"/cases/(ILK-[A-Z0-9]+)", client.get("/").text))
        assert ids[0] == ids[1], "two cold starts produced different queues"
