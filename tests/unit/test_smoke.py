"""M0 smoke test - proves the package imports and CI is wired correctly."""

import interlock


def test_package_imports():
    assert interlock.__version__


def test_protocol_version_is_pinned():
    # The protocol version is part of the product contract, not an
    # implementation detail. If this changes, the schema changed.
    assert interlock.PROTOCOL_VERSION == "1.0"
