"""Interlock - a cross-institution signal and recall network for US instant payments.

The hub routes and logs. It never scores. See CLAUDE.md.
"""

__version__ = "0.1.0"

# The wire protocol version, bumped independently of the package version.
# Any change to a published model in interlock.schema requires bumping this
# and adding a round-trip test against the previous version.
PROTOCOL_VERSION = "1.0"
