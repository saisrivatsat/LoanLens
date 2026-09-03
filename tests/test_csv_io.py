from datetime import date

import pytest

from loanlens.csv_io import (
    payments_from_csv,
    payments_to_csv,
    rates_from_csv,
    rates_to_csv,
)
from loanlens.models import Payment, RateChange


def test_payment_csv_round_trip_preserves_optional_credited_date() -> None:
    original = [
        Payment("one", 5_000, date(2025, 1, 5), None, "EMI"),
        Payment("two", 20_000, date(2025, 2, 5), date(2025, 2, 7), "Extra payment"),
    ]
    restored = payments_from_csv(payments_to_csv(original))
    assert restored == original


def test_rate_csv_round_trip() -> None:
    original = [
        RateChange(date(2025, 1, 1), 10.25),
        RateChange(date(2025, 6, 1), 11.0),
    ]
    assert rates_from_csv(rates_to_csv(original)) == original


def test_payment_csv_rejects_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        payments_from_csv("payment_date,amount\n2025-01-01,1000\n")
