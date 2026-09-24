"""Deployment settings, including the one contact address the product shows.

Everything here is overridable by environment variable, because the whole
point of a deployment setting is that it changes without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

SUPPORT_EMAIL = os.environ.get("INTERLOCK_SUPPORT_EMAIL", "support@interlock-secure.com")
"""The support contact shown in the console footer, the API and the README.

**Defined once, here.** Changing who fields support mail is a single
environment variable or a single line, not a search across templates.

The default deliberately matches the GitHub organisation rather than anyone's
personal address. A personal mailbox published in a public repository is
scraped within days and cannot be unpublished, so the safe default is an
organisational alias and the decision to use something else is the owner's to
make explicitly.
"""

INSTITUTION_ID = os.environ.get("INTERLOCK_INSTITUTION_ID", "inst-harbor-national")
"""Which institution this instance runs as.

Interlock is deployed *by* a bank and works that bank's queue, so almost every
question - is this case inbound, whose deadline is running - is answered
relative to this value.
"""

INSTITUTION_NAME = os.environ.get("INTERLOCK_INSTITUTION_NAME", "Harbor National")

# On Vercel (and other serverless hosts) only /tmp is writable, so the default
# moves there when the platform announces itself. Explicit configuration wins.
_DEFAULT_DATABASE = "/tmp/interlock.db" if os.environ.get("VERCEL") else "data/interlock.db"
DATABASE_PATH = os.environ.get("INTERLOCK_DATABASE", _DEFAULT_DATABASE)
"""SQLite file. ``:memory:`` for an ephemeral demo instance."""

DEMO_SEED = int(os.environ.get("INTERLOCK_DEMO_SEED", "20260919"))
"""Seed for the demo dataset, so the instance an interviewer opens is the same
one every time."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the application reads at startup."""

    support_email: str = SUPPORT_EMAIL
    institution_id: str = INSTITUTION_ID
    institution_name: str = INSTITUTION_NAME
    database_path: str = DATABASE_PATH
    demo_seed: int = DEMO_SEED
    seed_demo_data: bool = os.environ.get("INTERLOCK_SEED_DEMO", "1") == "1"


def settings() -> Settings:
    """Read settings fresh.

    A function rather than a module-level singleton so tests can set an
    environment variable and get a different answer, without reaching into
    module state.
    """
    return Settings(
        support_email=os.environ.get("INTERLOCK_SUPPORT_EMAIL", SUPPORT_EMAIL),
        institution_id=os.environ.get("INTERLOCK_INSTITUTION_ID", INSTITUTION_ID),
        institution_name=os.environ.get("INTERLOCK_INSTITUTION_NAME", INSTITUTION_NAME),
        database_path=os.environ.get(
            "INTERLOCK_DATABASE",
            "/tmp/interlock.db" if os.environ.get("VERCEL") else "data/interlock.db",
        ),
        demo_seed=int(os.environ.get("INTERLOCK_DEMO_SEED", str(DEMO_SEED))),
        seed_demo_data=os.environ.get("INTERLOCK_SEED_DEMO", "1") == "1",
    )
