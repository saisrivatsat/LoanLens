"""CSV import/export helpers for session-only LoanBreaker data."""

from __future__ import annotations

import csv
import io
from datetime import date
from uuid import uuid4

from .models import Payment, RateChange

PAYMENT_COLUMNS = ("payment_id", "payment_date", "credited_date", "amount", "payment_type")
RATE_COLUMNS = ("effective_date", "annual_rate")


def payments_to_csv(payments: list[Payment]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=PAYMENT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for payment in sorted(payments, key=lambda item: item.effective_date):
        if payment.source != "recorded":
            continue
        writer.writerow(
            {
                "payment_id": payment.payment_id,
                "payment_date": payment.payment_date.isoformat(),
                "credited_date": payment.credited_date.isoformat()
                if payment.credited_date
                else "",
                "amount": f"{payment.amount:.2f}",
                "payment_type": payment.payment_type,
            }
        )
    return buffer.getvalue()


def payments_from_csv(content: bytes | str) -> list[Payment]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    required = {"payment_date", "amount", "payment_type"}
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"Payments CSV is missing columns: {', '.join(sorted(missing))}.")

    payments: list[Payment] = []
    for row_number, row in enumerate(reader, start=2):
        try:
            credited_raw = (row.get("credited_date") or "").strip()
            payments.append(
                Payment(
                    payment_id=(row.get("payment_id") or "").strip() or uuid4().hex,
                    payment_date=date.fromisoformat(row["payment_date"].strip()),
                    credited_date=date.fromisoformat(credited_raw) if credited_raw else None,
                    amount=float(row["amount"]),
                    payment_type=row["payment_type"].strip() or "Other",
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid payments CSV row {row_number}: {exc}") from exc
    return payments


def rates_to_csv(rates: list[RateChange]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=RATE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for change in sorted(rates, key=lambda item: item.effective_date):
        writer.writerow(
            {
                "effective_date": change.effective_date.isoformat(),
                "annual_rate": f"{change.annual_rate:.4f}",
            }
        )
    return buffer.getvalue()


def rates_from_csv(content: bytes | str) -> list[RateChange]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    required = set(RATE_COLUMNS)
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise ValueError(f"Rate CSV is missing columns: {', '.join(sorted(missing))}.")

    rates: list[RateChange] = []
    for row_number, row in enumerate(reader, start=2):
        try:
            rates.append(
                RateChange(
                    effective_date=date.fromisoformat(row["effective_date"].strip()),
                    annual_rate=float(row["annual_rate"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid rate CSV row {row_number}: {exc}") from exc
    return sorted(rates, key=lambda item: item.effective_date)
