from datetime import date

import pytest

from loanbreaker.engine import (
    daily_interest,
    project_payoff,
    rate_on,
    simulate_extra_payment,
    simulate_history,
)
from loanbreaker.models import LoanConfig, Payment, RateChange


def make_config(**overrides) -> LoanConfig:
    values = {
        "original_amount": 1_000_000,
        "disbursement_date": date(2025, 1, 1),
        "current_outstanding": 900_000,
        "outstanding_as_of": date(2025, 6, 30),
        "current_annual_rate": 12.0,
        "current_emi": 25_000,
        "emi_start_date": date(2025, 7, 5),
        "emi_day": 5,
        "tenure_months": 60,
    }
    values.update(overrides)
    return LoanConfig(**values)


def test_daily_interest_uses_selected_day_count_basis() -> None:
    assert daily_interest(1_000_000, 12.0, 365) == pytest.approx(328.767123, rel=1e-8)
    assert daily_interest(1_000_000, 12.0, 360) == pytest.approx(333.333333, rel=1e-8)


def test_rate_history_uses_latest_effective_rate() -> None:
    history = [
        RateChange(date(2025, 1, 1), 10.0),
        RateChange(date(2025, 4, 1), 11.5),
    ]
    assert rate_on(date(2025, 3, 31), history, 9.0) == 10.0
    assert rate_on(date(2025, 4, 1), history, 9.0) == 11.5


def test_payment_is_allocated_to_interest_before_principal() -> None:
    config = make_config(
        original_amount=100_000,
        disbursement_date=date(2025, 1, 1),
        outstanding_as_of=date(2025, 1, 10),
        current_annual_rate=12.0,
    )
    payment = Payment("p1", 500, date(2025, 1, 10), payment_type="EMI")
    result = simulate_history(config, [payment])

    assert result.total_interest_paid > 0
    assert result.total_principal_paid > 0
    assert result.total_interest_paid + result.total_principal_paid == pytest.approx(500)


def test_credited_date_controls_when_payment_reduces_balance() -> None:
    config = make_config(
        original_amount=100_000,
        disbursement_date=date(2025, 1, 1),
        outstanding_as_of=date(2025, 1, 10),
        current_annual_rate=12.0,
    )
    payment = Payment(
        "p1",
        10_000,
        date(2025, 1, 5),
        credited_date=date(2025, 1, 7),
        payment_type="Extra payment",
    )
    result = simulate_history(config, [payment])
    by_date = {entry.date: entry for entry in result.entries}

    assert by_date[date(2025, 1, 5)].payment_amount == 0
    assert by_date[date(2025, 1, 7)].payment_amount == 10_000


def test_representative_part_interest_period_can_increase_outstanding() -> None:
    """Anonymized validation of the supplied education-loan schedule behavior.

    A small monthly part-interest payment does not cover interest on a large
    balance. The remainder stays unpaid, so total outstanding rises even though
    the borrower made the scheduled payment.
    """

    config = make_config(
        original_amount=2_500_000,
        disbursement_date=date(2025, 1, 1),
        current_outstanding=2_500_000,
        outstanding_as_of=date(2025, 2, 4),
        current_annual_rate=12.25,
        current_emi=35_000,
        emi_start_date=date(2026, 1, 5),
        emi_day=5,
        moratorium_months=12,
        part_interest_amount=3_000,
        model_part_interest=True,
    )
    result = simulate_history(
        config,
        [],
        [RateChange(date(2025, 1, 1), 12.25)],
    )

    assert result.total_modeled_payments == 3_000
    assert result.total_principal_paid == 0
    assert result.total_interest_accrued > result.total_modeled_payments
    assert result.ending_balance > config.original_amount


def test_zero_interest_projection_pays_off_exactly() -> None:
    result = project_payoff(
        starting_balance=12_000,
        as_of_date=date(2025, 1, 1),
        annual_rate=0,
        monthly_payment=1_000,
        emi_day=5,
    )
    assert result.payoff_date == date(2025, 12, 5)
    assert result.months_to_payoff == 12
    assert result.total_future_interest == 0


def test_extra_payment_saves_time_and_interest() -> None:
    config = make_config()
    result = simulate_extra_payment(config, 100_000, date(2025, 7, 1))

    assert result.baseline.payoff_date is not None
    assert result.with_extra_payment.payoff_date is not None
    assert result.estimated_months_saved is not None
    assert result.estimated_months_saved > 0
    assert result.estimated_interest_savings is not None
    assert result.estimated_interest_savings > 0
    assert result.estimated_balance_after_extra < config.current_outstanding


def test_projection_reports_no_payoff_when_emi_is_too_small() -> None:
    result = project_payoff(
        starting_balance=1_000_000,
        as_of_date=date(2025, 1, 1),
        annual_rate=12,
        monthly_payment=1_000,
        emi_day=5,
        max_months=24,
    )
    assert result.payoff_date is None
    assert result.months_to_payoff is None
    assert result.ending_balance > 1_000_000
