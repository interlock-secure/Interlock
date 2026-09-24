"""Build and persist a testbed arm.

    uv run python -m interlock.generator.testbed

Writes to data/generated/<arm>/ (gitignored) plus a manifest that is small
enough to commit. The data is not committed because it is reproducible: the
manifest records the seed, the settings and a digest of the output, so anyone
can regenerate byte-identical files and check them against it.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from interlock import PROTOCOL_VERSION
from interlock.generator.config import (
    ARMS,
    TARGET_MEAN_FRAUD_RANGE_CENTS,
    TRAIN_TEST_SPLIT_FRACTION,
    ArmSpec,
    GeneratorSettings,
)
from interlock.generator.payments import Payment, generate_payments
from interlock.generator.population import Account, build_population, summarise

WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
"""Fixed so every run is reproducible. A generator anchored to "now" produces
a different dataset every day and a benchmark nobody can re-run."""

ACCOUNT_COLUMNS = (
    "account_id",
    "account_hash",
    "institution_id",
    "account_type",
    "opened_at",
    "is_mule",
    "segment",
)

PAYMENT_COLUMNS = (
    "payment_id",
    "timestamp",
    "sender_account_id",
    "sender_institution_id",
    "receiver_account_id",
    "receiver_institution_id",
    "receiver_account_hash",
    "amount_cents",
    "rail",
    "is_first_time_payee",
    "is_fraud",
    "scam_category",
    "is_onward_sweep",
    "split",
)


def _split_for(timestamp: datetime, window_start: datetime, arm: ArmSpec) -> str:
    """Temporal split label.

    Temporal rather than random: mule accounts age and tactics shift inside a
    window, so a random split lets a model learn from an account's own future.
    """
    cutoff = window_start + timedelta(days=arm.window_days * TRAIN_TEST_SPLIT_FRACTION)
    return "train" if timestamp < cutoff else "test"


def build_arm(
    arm: ArmSpec, settings: GeneratorSettings | None = None
) -> tuple[list[Account], list[Payment], GeneratorSettings]:
    settings = settings or GeneratorSettings(arm=arm)
    accounts = build_population(settings, WINDOW_START, arm)
    payments = generate_payments(
        settings=settings, arm=arm, accounts=accounts, window_start=WINDOW_START
    )
    return accounts, payments, settings


def payment_statistics(payments: list[Payment], arm: ArmSpec) -> dict[str, object]:
    """Statistics used both for the manifest and for the calibration tests."""
    total = len(payments)
    fraud = [p for p in payments if p.is_fraud]
    cross = sum(1 for p in payments if p.is_cross_institution)
    sweeps = sum(1 for p in payments if p.is_onward_sweep)

    fraud_amounts = sorted(p.amount_cents for p in fraud)
    mean_fraud = sum(fraud_amounts) // len(fraud_amounts) if fraud_amounts else 0
    median_fraud = fraud_amounts[len(fraud_amounts) // 2] if fraud_amounts else 0

    by_category: dict[str, int] = {}
    for payment in fraud:
        if payment.scam_category:
            by_category[payment.scam_category] = by_category.get(payment.scam_category, 0) + 1

    by_split: dict[str, dict[str, int]] = {}
    for payment in payments:
        label = _split_for(payment.timestamp, WINDOW_START, arm)
        bucket = by_split.setdefault(label, {"payments": 0, "fraud": 0})
        bucket["payments"] += 1
        if payment.is_fraud:
            bucket["fraud"] += 1

    return {
        "total_payments": total,
        "fraudulent_payments": len(fraud),
        "observed_fraud_rate": round(len(fraud) / total, 8) if total else 0.0,
        "target_fraud_rate": arm.fraud_rate,
        "cross_institution_payments": cross,
        "onward_sweep_payments": sweeps,
        "fraud_value_cents": sum(fraud_amounts),
        "mean_fraud_amount_cents": mean_fraud,
        "median_fraud_amount_cents": median_fraud,
        "calibration_anchor_range_cents": list(TARGET_MEAN_FRAUD_RANGE_CENTS),
        "fraud_by_category": dict(sorted(by_category.items())),
        "by_split": {k: by_split[k] for k in sorted(by_split)},
    }


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_arm(arm: ArmSpec, out_root: Path) -> dict[str, object]:
    accounts, payments, settings = build_arm(arm)
    target = out_root / arm.name
    target.mkdir(parents=True, exist_ok=True)

    accounts_path = target / "accounts.csv"
    with accounts_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(ACCOUNT_COLUMNS)
        for account in accounts:
            writer.writerow(
                [
                    account.account_id,
                    account.account_hash,
                    account.institution_id,
                    account.account_type.value,
                    account.opened_at.isoformat(),
                    int(account.is_mule),
                    account.segment,
                ]
            )

    payments_path = target / "payments.csv"
    with payments_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(PAYMENT_COLUMNS)
        for payment in payments:
            writer.writerow(
                [
                    payment.payment_id,
                    payment.timestamp.isoformat(),
                    payment.sender_account_id,
                    payment.sender_institution_id,
                    payment.receiver_account_id,
                    payment.receiver_institution_id,
                    payment.receiver_account_hash,
                    payment.amount_cents,
                    payment.rail,
                    int(payment.is_first_time_payee),
                    int(payment.is_fraud),
                    payment.scam_category or "",
                    int(payment.is_onward_sweep),
                    _split_for(payment.timestamp, WINDOW_START, arm),
                ]
            )

    return {
        "arm": arm.name,
        "rationale": arm.rationale,
        "window_days": arm.window_days,
        "window_start": WINDOW_START.isoformat(),
        "seed": settings.seed,
        "protocol_version": PROTOCOL_VERSION,
        "population": summarise(accounts),
        "payments": payment_statistics(payments, arm),
        "files": {
            "accounts.csv": {"rows": len(accounts), "sha256": _digest(accounts_path)},
            "payments.csv": {"rows": len(payments), "sha256": _digest(payments_path)},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Interlock synthetic testbed")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/generated"),
        help="output directory (gitignored)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/testbed_manifest.json"),
        help="committed manifest describing the run",
    )
    args = parser.parse_args()

    manifests = [write_arm(arm, args.out) for arm in ARMS]

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(
            {
                "generated_by": "interlock.generator.testbed",
                "window_start": WINDOW_START.isoformat(),
                "settings": {
                    "institutions": [asdict(i) for i in GeneratorSettings().institutions],
                    "train_test_split_fraction": TRAIN_TEST_SPLIT_FRACTION,
                },
                "arms": manifests,
                "caveat": (
                    "Synthetic data. These figures describe a generator, not a bank. A "
                    "detector scored on this measures how closely it matches the "
                    "generator's assumptions, which is why no accuracy claim is made "
                    "anywhere in this project. The testbed exists to exercise the routing "
                    "and recall paths and to give the fairness analysis a population with "
                    "real overlap between mules and legitimate lookalikes."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    for manifest in manifests:
        payments = manifest["payments"]
        population = manifest["population"]
        print(
            f"{manifest['arm']:>10}: "
            f"{payments['total_payments']:>8,} payments, "
            f"{payments['fraudulent_payments']:>5,} fraudulent "
            f"({payments['observed_fraud_rate']:.5%}), "
            f"{population['mule_accounts']:>3} mules"
        )
    print(f"manifest -> {args.manifest}")


if __name__ == "__main__":
    main()
