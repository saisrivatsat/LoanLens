# Financial-engine validation notes

The MVP was checked against the behavior observed in private education-loan repayment schedules supplied for product discovery. No schedule, account identifier, borrower name, or exact personal payment history is included here.

## Representative behavior covered

| Behavior | Engine rule | Automated coverage |
|---|---|---|
| Rate changes over time | Latest effective rate is selected for each day | `test_rate_history_uses_latest_effective_rate` |
| Lender posting date matters | Credited date takes precedence over payment date | `test_credited_date_controls_when_payment_reduces_balance` |
| Payment allocation | Unpaid interest is paid before principal | `test_payment_is_allocated_to_interest_before_principal` |
| Part-interest balance growth | Unpaid interest remains in outstanding balance | `test_representative_part_interest_period_can_increase_outstanding` |
| Extra-payment impact | Compare fixed-EMI payoff paths with and without one extra payment | `test_extra_payment_saves_time_and_interest` |
| Negative amortization warning | Projection can remain unpaid when EMI is below accruing interest | `test_projection_reports_no_payoff_when_emi_is_too_small` |

## Deliberate V1 limitations

- Daily accrual is modeled with a selectable 360, 365, or 366 day-count basis.
- Intermediate amounts are not rounded each day; display values are rounded separately.
- Future interest rates and lender fees are not predicted.
- Business-day shifts, holidays, taxes, insurance, penalties, and lender-specific prepayment rules are not modeled.
- The entered current lender balance is the anchor for future projections because an incomplete payment history cannot reproduce an exact lender balance.
