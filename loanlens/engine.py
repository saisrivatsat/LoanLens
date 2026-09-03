"""Pure-Python loan calculation engine used by the Streamlit interface.

The engine uses a transparent daily simple-interest accrual on the total unpaid
balance. Interest is applied first, then payments are allocated to unpaid
interest and finally principal. Unpaid interest remains in the total balance,
so a payment below accrued interest can make the outstanding balance rise.

This is an estimate, not a lender statement. Lenders can use different posting,
rounding, capitalization, fee, holiday, and day-count rules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil
from typing import Iterable, Sequence

from .models import LoanConfig, Payment, RateChange

MONEY_EPSILON = 0.005


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    date: date
    opening_balance: float
    annual_rate: float
    interest_accrued: float
    payment_amount: float
    interest_paid: float
    principal_paid: float
    closing_balance: float
    recorded_payment: float = 0.0
    modeled_payment: float = 0.0


@dataclass(frozen=True, slots=True)
class SimulationResult:
    entries: tuple[LedgerEntry, ...]
    ending_balance: float
    total_interest_accrued: float
    total_interest_paid: float
    total_principal_paid: float
    total_recorded_payments: float
    total_modeled_payments: float


@dataclass(frozen=True, slots=True)
class PayoffProjection:
    entries: tuple[LedgerEntry, ...]
    payoff_date: date | None
    months_to_payoff: int | None
    total_future_interest: float
    total_future_payments: float
    ending_balance: float


@dataclass(frozen=True, slots=True)
class WhatIfResult:
    baseline: PayoffProjection
    with_extra_payment: PayoffProjection
    extra_payment_date: date
    estimated_balance_after_extra: float
    estimated_interest_savings: float | None
    estimated_months_saved: int | None


def daily_interest(balance: float, annual_rate: float, day_count_basis: int = 365) -> float:
    """Calculate one day's interest without rounding intermediate values."""

    if balance < 0:
        raise ValueError("Balance cannot be negative.")
    if not 0 <= annual_rate <= 100:
        raise ValueError("Annual interest rate must be between 0 and 100 percent.")
    if day_count_basis <= 0:
        raise ValueError("Day-count basis must be positive.")
    return balance * (annual_rate / 100.0) / day_count_basis


def rate_on(day: date, rate_history: Sequence[RateChange], fallback_rate: float) -> float:
    """Return the latest rate effective on ``day`` or the supplied fallback."""

    applicable = fallback_rate
    for change in sorted(rate_history, key=lambda item: item.effective_date):
        if change.effective_date > day:
            break
        applicable = change.annual_rate
    return applicable


def _next_monthly_date(after: date, due_day: int) -> date:
    """Return the first due-day occurrence strictly after ``after``."""

    if after.day < due_day:
        return date(after.year, after.month, due_day)
    if after.month == 12:
        return date(after.year + 1, 1, due_day)
    return date(after.year, after.month + 1, due_day)


def _add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month_zero_based = divmod(month_index, 12)
    month = month_zero_based + 1
    return date(year, month, min(day.day, 28))


def _months_between_ceil(start: date, end: date) -> int:
    raw = (end.year - start.year) * 12 + end.month - start.month
    return max(0, raw + (1 if end.day > start.day else 0))


def modeled_part_interest_payments(config: LoanConfig, through_date: date) -> tuple[Payment, ...]:
    """Generate optional expected part-interest events for history estimation.

    These events are deliberately marked as modeled and are never presented as
    confirmed payments. Modeling stops when regular EMI begins.
    """

    if not config.model_part_interest or config.part_interest_amount <= 0:
        return ()

    period_months = config.moratorium_months + config.grace_months
    if period_months <= 0:
        return ()

    configured_end = _add_months(config.disbursement_date, period_months)
    end_date = min(config.emi_start_date, configured_end, through_date + timedelta(days=1))
    payment_day = _next_monthly_date(config.disbursement_date, config.emi_day)
    results: list[Payment] = []

    while payment_day < end_date and payment_day <= through_date:
        results.append(
            Payment(
                payment_id=f"modeled-part-interest-{payment_day.isoformat()}",
                amount=config.part_interest_amount,
                payment_date=payment_day,
                payment_type="Part-interest",
                source="modeled",
            )
        )
        payment_day = _next_monthly_date(payment_day, config.emi_day)

    return tuple(results)


def _simulate_daily(
    opening_principal: float,
    start_date: date,
    through_date: date,
    payments: Iterable[Payment],
    rate_history: Sequence[RateChange],
    fallback_rate: float,
    day_count_basis: int,
) -> SimulationResult:
    if through_date < start_date:
        raise ValueError("Simulation end date cannot be before the start date.")

    events: dict[date, list[Payment]] = defaultdict(list)
    for payment in payments:
        if start_date <= payment.effective_date <= through_date:
            events[payment.effective_date].append(payment)

    principal = float(opening_principal)
    unpaid_interest = 0.0
    total_interest_accrued = 0.0
    total_interest_paid = 0.0
    total_principal_paid = 0.0
    total_recorded_payments = 0.0
    total_modeled_payments = 0.0
    entries: list[LedgerEntry] = []

    day = start_date
    while day <= through_date:
        opening_balance = principal + unpaid_interest
        annual_rate = rate_on(day, rate_history, fallback_rate)
        interest = daily_interest(opening_balance, annual_rate, day_count_basis)
        unpaid_interest += interest
        total_interest_accrued += interest

        payments_today = events.get(day, ())
        requested_payment = sum(payment.amount for payment in payments_today)
        recorded_payment = sum(
            payment.amount for payment in payments_today if payment.source == "recorded"
        )
        modeled_payment = requested_payment - recorded_payment
        available_balance = principal + unpaid_interest
        applied_payment = min(requested_payment, available_balance)

        interest_paid = min(applied_payment, unpaid_interest)
        unpaid_interest -= interest_paid
        principal_paid = min(applied_payment - interest_paid, principal)
        principal -= principal_paid

        total_interest_paid += interest_paid
        total_principal_paid += principal_paid
        total_recorded_payments += recorded_payment
        total_modeled_payments += modeled_payment
        closing_balance = max(0.0, principal + unpaid_interest)

        entries.append(
            LedgerEntry(
                date=day,
                opening_balance=opening_balance,
                annual_rate=annual_rate,
                interest_accrued=interest,
                payment_amount=requested_payment,
                interest_paid=interest_paid,
                principal_paid=principal_paid,
                closing_balance=closing_balance,
                recorded_payment=recorded_payment,
                modeled_payment=modeled_payment,
            )
        )

        if closing_balance <= MONEY_EPSILON:
            break
        day += timedelta(days=1)

    return SimulationResult(
        entries=tuple(entries),
        ending_balance=entries[-1].closing_balance,
        total_interest_accrued=total_interest_accrued,
        total_interest_paid=total_interest_paid,
        total_principal_paid=total_principal_paid,
        total_recorded_payments=total_recorded_payments,
        total_modeled_payments=total_modeled_payments,
    )


def simulate_history(
    config: LoanConfig,
    payments: Iterable[Payment],
    rate_history: Sequence[RateChange] = (),
    through_date: date | None = None,
) -> SimulationResult:
    """Reconstruct an estimated loan history from source events."""

    end_date = through_date or config.outstanding_as_of
    recorded = tuple(payments)
    modeled = modeled_part_interest_payments(config, end_date)
    return _simulate_daily(
        opening_principal=config.original_amount,
        start_date=config.disbursement_date,
        through_date=end_date,
        payments=recorded + modeled,
        rate_history=rate_history,
        fallback_rate=config.current_annual_rate,
        day_count_basis=config.day_count_basis,
    )


def project_payoff(
    starting_balance: float,
    as_of_date: date,
    annual_rate: float,
    monthly_payment: float,
    emi_day: int,
    extra_payments: Iterable[Payment] = (),
    day_count_basis: int = 365,
    max_months: int = 1_200,
) -> PayoffProjection:
    """Project payoff using the current rate and a fixed monthly payment.

    The starting balance is treated as the lender-reported close-of-day balance
    on ``as_of_date``. Projection begins on the following day.
    """

    if starting_balance < 0:
        raise ValueError("Starting balance cannot be negative.")
    if monthly_payment < 0:
        raise ValueError("Monthly payment cannot be negative.")
    if not 1 <= emi_day <= 28:
        raise ValueError("EMI day must be between 1 and 28.")
    if max_months <= 0:
        raise ValueError("Maximum projection months must be positive.")
    if starting_balance <= MONEY_EPSILON:
        return PayoffProjection((), as_of_date, 0, 0.0, 0.0, 0.0)

    projection_start = as_of_date + timedelta(days=1)
    projection_end = _add_months(as_of_date, max_months)
    extras = tuple(extra_payments)
    extra_events: dict[date, list[Payment]] = defaultdict(list)
    for payment in extras:
        if payment.effective_date <= as_of_date:
            raise ValueError("Extra payment date must be after the balance as-of date.")
        extra_events[payment.effective_date].append(payment)

    principal = float(starting_balance)
    unpaid_interest = 0.0
    total_interest = 0.0
    total_payments = 0.0
    entries: list[LedgerEntry] = []
    payoff_date: date | None = None

    day = projection_start
    while day <= projection_end:
        opening_balance = principal + unpaid_interest
        interest = daily_interest(opening_balance, annual_rate, day_count_basis)
        unpaid_interest += interest
        total_interest += interest

        requested_payment = monthly_payment if day.day == emi_day else 0.0
        requested_payment += sum(payment.amount for payment in extra_events.get(day, ()))
        available_balance = principal + unpaid_interest
        applied_payment = min(requested_payment, available_balance)
        interest_paid = min(applied_payment, unpaid_interest)
        unpaid_interest -= interest_paid
        principal_paid = min(applied_payment - interest_paid, principal)
        principal -= principal_paid
        total_payments += applied_payment
        closing_balance = max(0.0, principal + unpaid_interest)

        entries.append(
            LedgerEntry(
                date=day,
                opening_balance=opening_balance,
                annual_rate=annual_rate,
                interest_accrued=interest,
                payment_amount=requested_payment,
                interest_paid=interest_paid,
                principal_paid=principal_paid,
                closing_balance=closing_balance,
                recorded_payment=requested_payment,
            )
        )

        if closing_balance <= MONEY_EPSILON:
            payoff_date = day
            break
        day += timedelta(days=1)

    months_to_payoff = (
        _months_between_ceil(as_of_date, payoff_date) if payoff_date is not None else None
    )
    return PayoffProjection(
        entries=tuple(entries),
        payoff_date=payoff_date,
        months_to_payoff=months_to_payoff,
        total_future_interest=total_interest,
        total_future_payments=total_payments,
        ending_balance=entries[-1].closing_balance,
    )


def simulate_extra_payment(
    config: LoanConfig,
    extra_amount: float,
    extra_payment_date: date,
) -> WhatIfResult:
    """Compare the current plan with a one-time extra payment."""

    if extra_amount <= 0:
        raise ValueError("Extra payment must be greater than zero.")
    if extra_payment_date <= config.outstanding_as_of:
        raise ValueError("Extra payment date must be after the current balance date.")

    baseline = project_payoff(
        starting_balance=config.current_outstanding,
        as_of_date=config.outstanding_as_of,
        annual_rate=config.current_annual_rate,
        monthly_payment=config.current_emi,
        emi_day=config.emi_day,
        day_count_basis=config.day_count_basis,
    )
    extra = Payment(
        payment_id="what-if-extra",
        amount=extra_amount,
        payment_date=extra_payment_date,
        payment_type="Extra payment",
    )
    scenario = project_payoff(
        starting_balance=config.current_outstanding,
        as_of_date=config.outstanding_as_of,
        annual_rate=config.current_annual_rate,
        monthly_payment=config.current_emi,
        emi_day=config.emi_day,
        extra_payments=(extra,),
        day_count_basis=config.day_count_basis,
    )

    scenario_on_extra_date = next(
        (entry for entry in scenario.entries if entry.date == extra_payment_date), None
    )
    estimated_balance_after_extra = (
        scenario_on_extra_date.closing_balance
        if scenario_on_extra_date is not None
        else scenario.ending_balance
    )

    interest_savings: float | None = None
    months_saved: int | None = None
    if baseline.payoff_date is not None and scenario.payoff_date is not None:
        interest_savings = max(
            0.0, baseline.total_future_interest - scenario.total_future_interest
        )
        months_saved = max(
            0,
            (baseline.months_to_payoff or 0) - (scenario.months_to_payoff or 0),
        )

    return WhatIfResult(
        baseline=baseline,
        with_extra_payment=scenario,
        extra_payment_date=extra_payment_date,
        estimated_balance_after_extra=estimated_balance_after_extra,
        estimated_interest_savings=interest_savings,
        estimated_months_saved=months_saved,
    )
