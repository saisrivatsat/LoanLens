from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pandas as pd
import streamlit as st

from loanbreaker.csv_io import (
    payments_from_csv,
    payments_to_csv,
    rates_from_csv,
    rates_to_csv,
)
from loanbreaker.engine import project_payoff, simulate_extra_payment, simulate_history
from loanbreaker.models import LoanConfig, Payment, RateChange


st.set_page_config(
    page_title="LoanBreaker",
    page_icon="↘",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {max-width: 1180px; padding-top: 2rem; padding-bottom: 4rem;}
    [data-testid="stMetric"] {
        background: rgba(255,255,255,.82);
        border: 1px solid rgba(23,53,45,.10);
        border-radius: 16px;
        padding: 16px 18px;
        box-shadow: 0 8px 24px rgba(23,53,45,.05);
    }
    [data-testid="stMetricLabel"] {color: #547068;}
    .lb-hero {
        padding: 24px 26px;
        border-radius: 20px;
        background: linear-gradient(120deg, #143F34 0%, #1E7058 62%, #2A9777 100%);
        color: white;
        margin-bottom: 20px;
        box-shadow: 0 16px 38px rgba(20,63,52,.18);
    }
    .lb-hero h1 {font-size: 2.15rem; margin: 0 0 .25rem; color: white;}
    .lb-hero p {font-size: 1.02rem; margin: 0; color: rgba(255,255,255,.82);}
    .lb-note {
        padding: 12px 15px;
        border-radius: 12px;
        background: #EAF4F0;
        border-left: 4px solid #1E8E6A;
        color: #274C42;
        margin: 10px 0 18px;
    }
    .stButton > button, .stDownloadButton > button {border-radius: 10px;}
    div[data-testid="stDataFrame"] {border-radius: 12px; overflow: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)


def _default_setup() -> dict:
    today = date.today()
    try:
        one_year_ago = today.replace(year=today.year - 1)
    except ValueError:
        one_year_ago = today - timedelta(days=365)
    return {
        "original_amount": 1_000_000.0,
        "disbursement_date": one_year_ago,
        "current_outstanding": 1_000_000.0,
        "outstanding_as_of": today,
        "current_annual_rate": 10.0,
        "current_emi": 15_000.0,
        "emi_start_date": today,
        "emi_day": min(today.day, 28),
        "tenure_months": 120,
        "moratorium_months": 0,
        "grace_months": 0,
        "part_interest_amount": 0.0,
        "model_part_interest": False,
        "day_count_basis": 365,
    }


def initialize_state() -> None:
    st.session_state.setdefault("loan_saved", False)
    st.session_state.setdefault("loan_setup", _default_setup())
    st.session_state.setdefault("payments", [])
    st.session_state.setdefault("rate_history", [])


def current_config() -> LoanConfig:
    return LoanConfig(**st.session_state.loan_setup)


def format_inr(value: float) -> str:
    rounded = int(round(abs(value)))
    digits = str(rounded)
    if len(digits) <= 3:
        grouped = digits
    else:
        tail = digits[-3:]
        head = digits[:-3]
        groups: list[str] = []
        while head:
            groups.append(head[-2:])
            head = head[:-2]
        grouped = ",".join(reversed(groups)) + "," + tail
    sign = "-" if value < 0 else ""
    return f"{sign}₹{grouped}"


def format_months(months: int | None) -> str:
    if months is None:
        return "Not reached"
    years, remaining = divmod(months, 12)
    if years and remaining:
        return f"{years}y {remaining}m"
    if years:
        return f"{years}y"
    return f"{remaining}m"


def payment_table(payments: list[Payment]) -> pd.DataFrame:
    rows = [
        {
            "Payment date": item.payment_date,
            "Credited date": item.credited_date,
            "Amount": item.amount,
            "Type": item.payment_type,
            "Calculation date": item.effective_date,
        }
        for item in sorted(payments, key=lambda payment: payment.effective_date, reverse=True)
    ]
    return pd.DataFrame(rows)


def rate_table(rates: list[RateChange]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"Effective date": item.effective_date, "Annual rate (%)": item.annual_rate}
            for item in sorted(rates, key=lambda change: change.effective_date)
        ]
    )


def projection_series(projection, label: str) -> pd.Series:
    if not projection.entries:
        return pd.Series(dtype="float64", name=label)
    sampled = [
        entry
        for index, entry in enumerate(projection.entries)
        if index == 0
        or entry.date.day == 1
        or index == len(projection.entries) - 1
    ]
    return pd.Series(
        {entry.date: entry.closing_balance for entry in sampled},
        name=label,
        dtype="float64",
    )


def render_header() -> None:
    st.markdown(
        """
        <div class="lb-hero">
          <h1>LoanBreaker</h1>
          <p>Know where your loan stands. See what an extra payment could change.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_setup_form() -> None:
    draft = st.session_state.loan_setup
    with st.expander("Loan setup", expanded=not st.session_state.loan_saved):
        st.caption("Enter amounts from your latest lender schedule or statement. INR is used in V1.")
        with st.form("loan_setup_form"):
            col1, col2, col3 = st.columns(3)
            with col1:
                original_amount = st.number_input(
                    "Original / disbursed amount",
                    min_value=1.0,
                    value=float(draft["original_amount"]),
                    step=10_000.0,
                    format="%.2f",
                )
                disbursement_date = st.date_input(
                    "Disbursement date", value=draft["disbursement_date"]
                )
                tenure_months = st.number_input(
                    "Original tenure (months)",
                    min_value=1,
                    max_value=1_200,
                    value=int(draft["tenure_months"]),
                )
            with col2:
                current_outstanding = st.number_input(
                    "Current outstanding",
                    min_value=0.0,
                    value=float(draft["current_outstanding"]),
                    step=10_000.0,
                    format="%.2f",
                )
                outstanding_as_of = st.date_input(
                    "Current balance as of", value=draft["outstanding_as_of"]
                )
                current_rate = st.number_input(
                    "Current annual interest rate (%)",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(draft["current_annual_rate"]),
                    step=0.05,
                    format="%.4f",
                )
            with col3:
                current_emi = st.number_input(
                    "Current EMI",
                    min_value=0.0,
                    value=float(draft["current_emi"]),
                    step=1_000.0,
                    format="%.2f",
                )
                emi_start_date = st.date_input(
                    "EMI start date", value=draft["emi_start_date"]
                )
                emi_day = st.number_input(
                    "EMI day of month",
                    min_value=1,
                    max_value=28,
                    value=int(draft["emi_day"]),
                )

            st.markdown("##### Optional moratorium / grace / part-interest details")
            opt1, opt2, opt3 = st.columns(3)
            with opt1:
                moratorium_months = st.number_input(
                    "Moratorium / study period (months)",
                    min_value=0,
                    max_value=240,
                    value=int(draft["moratorium_months"]),
                )
            with opt2:
                grace_months = st.number_input(
                    "Grace period (months)",
                    min_value=0,
                    max_value=120,
                    value=int(draft["grace_months"]),
                )
            with opt3:
                part_interest_amount = st.number_input(
                    "Monthly part-interest payment",
                    min_value=0.0,
                    value=float(draft["part_interest_amount"]),
                    step=500.0,
                    format="%.2f",
                )

            model_part_interest = st.checkbox(
                "Model these part-interest payments in the estimated history",
                value=bool(draft["model_part_interest"]),
                help=(
                    "These are clearly labeled as modeled, not confirmed. Turn this off if you "
                    "recorded those payments individually."
                ),
            )
            day_count_basis = st.selectbox(
                "Day-count assumption",
                options=[365, 360, 366],
                index=[365, 360, 366].index(int(draft["day_count_basis"])),
                help="Use the convention stated by your lender. Actual/365 is the V1 default.",
            )
            saved = st.form_submit_button("Save loan setup", type="primary")

        if saved:
            try:
                config = LoanConfig(
                    original_amount=original_amount,
                    disbursement_date=disbursement_date,
                    current_outstanding=current_outstanding,
                    outstanding_as_of=outstanding_as_of,
                    current_annual_rate=current_rate,
                    current_emi=current_emi,
                    emi_start_date=emi_start_date,
                    emi_day=int(emi_day),
                    tenure_months=int(tenure_months),
                    moratorium_months=int(moratorium_months),
                    grace_months=int(grace_months),
                    part_interest_amount=part_interest_amount,
                    model_part_interest=model_part_interest,
                    day_count_basis=int(day_count_basis),
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                first_save = not st.session_state.loan_saved
                st.session_state.loan_setup = {
                    field: getattr(config, field)
                    for field in config.__dataclass_fields__
                }
                st.session_state.loan_saved = True
                if first_save and not st.session_state.rate_history:
                    st.session_state.rate_history = [
                        RateChange(config.disbursement_date, config.current_annual_rate)
                    ]
                st.success("Loan setup saved in this browser session.")
                st.rerun()


def render_rate_history(config: LoanConfig) -> None:
    st.subheader("Interest-rate history")
    st.caption(
        "Add each rate from the date it became effective. The current rate above is used for future projections."
    )
    rates = st.session_state.rate_history
    if rates:
        st.dataframe(
            rate_table(rates),
            use_container_width=True,
            hide_index=True,
            column_config={"Annual rate (%)": st.column_config.NumberColumn(format="%.4f%%")},
        )

    with st.form("add_rate_form", clear_on_submit=False):
        col1, col2, col3 = st.columns([1.3, 1, 1])
        with col1:
            effective_date = st.date_input(
                "Effective date", value=config.outstanding_as_of, key="new_rate_date"
            )
        with col2:
            annual_rate = st.number_input(
                "Annual rate (%)",
                min_value=0.0,
                max_value=100.0,
                value=float(config.current_annual_rate),
                step=0.05,
                format="%.4f",
                key="new_rate_value",
            )
        with col3:
            st.write("")
            st.write("")
            add_rate = st.form_submit_button("Add / replace rate")

    if add_rate:
        change = RateChange(effective_date, annual_rate)
        st.session_state.rate_history = sorted(
            [item for item in rates if item.effective_date != effective_date] + [change],
            key=lambda item: item.effective_date,
        )
        st.rerun()

    if rates:
        col1, col2 = st.columns([2, 1])
        with col1:
            remove_date = st.selectbox(
                "Remove a rate entry",
                options=[item.effective_date for item in rates],
                format_func=lambda value: value.strftime("%d %b %Y"),
            )
        with col2:
            st.write("")
            st.write("")
            if st.button("Remove selected rate", use_container_width=True):
                st.session_state.rate_history = [
                    item for item in rates if item.effective_date != remove_date
                ]
                st.rerun()

    io1, io2 = st.columns(2)
    with io1:
        st.download_button(
            "Download rate history CSV",
            data=rates_to_csv(rates),
            file_name="loanbreaker_rate_history.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with io2:
        rate_upload = st.file_uploader(
            "Import rate history CSV",
            type="csv",
            key="rate_upload",
            help="Required columns: effective_date, annual_rate.",
        )
        if rate_upload and st.button("Replace rate history with CSV", use_container_width=True):
            try:
                imported = rates_from_csv(rate_upload.getvalue())
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state.rate_history = imported
                st.success(f"Imported {len(imported)} rate entries.")
                st.rerun()

    if rates and rates[-1].annual_rate != config.current_annual_rate:
        st.warning(
            "Your last rate-history entry differs from the current rate. Add the effective date of the current rate for a better history estimate."
        )


def render_dashboard(config: LoanConfig) -> None:
    history = simulate_history(
        config,
        st.session_state.payments,
        st.session_state.rate_history,
    )
    projection = None
    if config.current_outstanding <= 0:
        payoff_label = config.outstanding_as_of.strftime("%b %Y")
    elif config.current_emi > 0:
        projection = project_payoff(
            config.current_outstanding,
            config.outstanding_as_of,
            config.current_annual_rate,
            config.current_emi,
            config.emi_day,
            day_count_basis=config.day_count_basis,
        )
        payoff_label = (
            projection.payoff_date.strftime("%b %Y")
            if projection.payoff_date
            else "Not reached"
        )
    else:
        payoff_label = "Add an EMI"

    st.subheader("Your loan snapshot")
    row1 = st.columns(4)
    row1[0].metric("Originally borrowed", format_inr(config.original_amount))
    row1[1].metric("Current lender balance", format_inr(config.current_outstanding))
    row1[2].metric("Total recorded payments", format_inr(history.total_recorded_payments))
    row1[3].metric("Estimated payoff", payoff_label)

    row2 = st.columns(4)
    row2[0].metric("Estimated interest paid", format_inr(history.total_interest_paid))
    row2[1].metric("Estimated principal paid", format_inr(history.total_principal_paid))
    row2[2].metric("Current rate", f"{config.current_annual_rate:.3f}%")
    row2[3].metric("Current EMI", format_inr(config.current_emi))

    difference = history.ending_balance - config.current_outstanding
    st.markdown(
        f"""
        <div class="lb-note">
          Engine-estimated balance from entered history: <strong>{format_inr(history.ending_balance)}</strong>.
          Your lender-reported balance remains the source for future projections.
          Difference: <strong>{format_inr(difference)}</strong>.
        </div>
        """,
        unsafe_allow_html=True,
    )

    if history.ending_balance > config.original_amount and (
        config.part_interest_amount > 0 or history.total_interest_paid > 0
    ):
        st.info(
            "Why can the balance rise? When a payment is smaller than accrued interest, no principal is reduced and the unpaid interest remains in the balance."
        )

    st.subheader("Estimated balance trend")
    trend = pd.DataFrame(
        {
            "Date": [entry.date for entry in history.entries],
            "Estimated balance": [entry.closing_balance for entry in history.entries],
        }
    ).set_index("Date")
    st.line_chart(trend, color=["#1E8E6A"], height=330)
    st.caption(
        "The historical line is an estimate from the inputs, rate history, modeled part-interest (if enabled), and recorded payments."
    )

    if projection and projection.payoff_date is None:
        st.warning(
            "At the current rate and EMI, the projection did not reach payoff within 100 years. The EMI may be below accruing interest."
        )


def render_my_loan() -> None:
    st.header("My Loan")
    render_setup_form()
    if not st.session_state.loan_saved:
        st.info("Save your loan setup to see the dashboard and add a rate history.")
        return
    config = current_config()
    render_dashboard(config)
    st.divider()
    render_rate_history(config)


def render_add_payment() -> None:
    st.header("Add Payment")
    if not st.session_state.loan_saved:
        st.info("Save your loan setup in My Loan first.")
        return

    config = current_config()
    st.caption(
        "Payments affect calculations on the credited date when supplied; otherwise the payment date is used."
    )
    with st.form("add_payment_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            amount = st.number_input(
                "Amount", min_value=1.0, value=1_000.0, step=1_000.0, format="%.2f"
            )
            payment_type = st.selectbox(
                "Payment type", ["EMI", "Extra payment", "Part-interest", "Interest-only", "Other"]
            )
        with col2:
            payment_date = st.date_input("Payment date", value=config.outstanding_as_of)
            use_credited_date = st.checkbox("Lender credited it on another date")
        with col3:
            credited_date = st.date_input(
                "Credited / received date",
                value=max(config.outstanding_as_of, payment_date),
                disabled=not use_credited_date,
            )
        add_payment = st.form_submit_button("Add payment", type="primary")

    if add_payment:
        try:
            payment = Payment(
                payment_id=uuid4().hex,
                amount=amount,
                payment_date=payment_date,
                credited_date=credited_date if use_credited_date else None,
                payment_type=payment_type,
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state.payments = st.session_state.payments + [payment]
            st.success("Payment added to this browser session.")
            st.rerun()

    payments = st.session_state.payments
    st.subheader("Payment history")
    if not payments:
        st.info("No payments recorded yet. Add one above or import a CSV.")
    else:
        st.dataframe(
            payment_table(payments),
            use_container_width=True,
            hide_index=True,
            column_config={"Amount": st.column_config.NumberColumn(format="₹ %.2f")},
        )

        selected_id = st.selectbox(
            "Choose a payment to edit or delete",
            options=[item.payment_id for item in payments],
            format_func=lambda payment_id: next(
                (
                    f"{item.effective_date:%d %b %Y} · {format_inr(item.amount)} · {item.payment_type}"
                    for item in payments
                    if item.payment_id == payment_id
                ),
                payment_id,
            ),
        )
        selected = next(item for item in payments if item.payment_id == selected_id)
        with st.form(f"edit_payment_{selected.payment_id}"):
            edit1, edit2, edit3 = st.columns(3)
            with edit1:
                edit_amount = st.number_input(
                    "Edit amount",
                    min_value=1.0,
                    value=float(selected.amount),
                    step=1_000.0,
                    format="%.2f",
                )
                edit_type = st.selectbox(
                    "Edit type",
                    ["EMI", "Extra payment", "Part-interest", "Interest-only", "Other"],
                    index=["EMI", "Extra payment", "Part-interest", "Interest-only", "Other"].index(
                        selected.payment_type
                        if selected.payment_type
                        in {"EMI", "Extra payment", "Part-interest", "Interest-only", "Other"}
                        else "Other"
                    ),
                )
            with edit2:
                edit_payment_date = st.date_input("Edit payment date", value=selected.payment_date)
                edit_use_credit = st.checkbox(
                    "Use a separate credited date", value=selected.credited_date is not None
                )
            with edit3:
                edit_credited_date = st.date_input(
                    "Edit credited date",
                    value=selected.credited_date or selected.payment_date,
                    disabled=not edit_use_credit,
                )
            action1, action2 = st.columns(2)
            save_edit = action1.form_submit_button("Save changes", use_container_width=True)
            delete_payment = action2.form_submit_button("Delete payment", use_container_width=True)

        if save_edit:
            try:
                updated = Payment(
                    payment_id=selected.payment_id,
                    amount=edit_amount,
                    payment_date=edit_payment_date,
                    credited_date=edit_credited_date if edit_use_credit else None,
                    payment_type=edit_type,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state.payments = [
                    updated if item.payment_id == selected.payment_id else item
                    for item in payments
                ]
                st.success("Payment updated.")
                st.rerun()
        if delete_payment:
            st.session_state.payments = [
                item for item in payments if item.payment_id != selected.payment_id
            ]
            st.success("Payment deleted from this browser session.")
            st.rerun()

    st.divider()
    st.subheader("Import or export payments")
    csv1, csv2 = st.columns(2)
    with csv1:
        st.download_button(
            "Download payments CSV",
            data=payments_to_csv(payments),
            file_name="loanbreaker_payments.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with csv2:
        payment_upload = st.file_uploader(
            "Import payments CSV",
            type="csv",
            key="payment_upload",
            help="Required columns: payment_date, amount, payment_type. credited_date is optional.",
        )
        if payment_upload and st.button("Import and merge payments", use_container_width=True):
            try:
                imported = payments_from_csv(payment_upload.getvalue())
            except ValueError as exc:
                st.error(str(exc))
            else:
                merged = {item.payment_id: item for item in payments}
                merged.update({item.payment_id: item for item in imported})
                st.session_state.payments = list(merged.values())
                st.success(f"Imported {len(imported)} payments.")
                st.rerun()


def render_what_if() -> None:
    st.header("What If")
    if not st.session_state.loan_saved:
        st.info("Save your loan setup in My Loan first.")
        return

    config = current_config()
    if config.current_outstanding <= 0:
        st.success("Your entered current outstanding is already zero.")
        return
    if config.current_emi <= 0:
        st.warning("Add a current EMI in My Loan before running a payoff simulation.")
        return

    st.markdown(
        """
        <div class="lb-note">
          This scenario keeps your current EMI unchanged and applies one extra payment.
          It uses the current rate as a constant future assumption.
        </div>
        """,
        unsafe_allow_html=True,
    )
    min_extra_date = config.outstanding_as_of + timedelta(days=1)
    with st.form("what_if_form"):
        col1, col2 = st.columns(2)
        with col1:
            extra_amount = st.number_input(
                "Extra payment amount",
                min_value=1.0,
                value=min(100_000.0, max(1.0, config.current_outstanding)),
                step=10_000.0,
                format="%.2f",
            )
        with col2:
            extra_date = st.date_input(
                "Extra payment date",
                value=min_extra_date,
                min_value=min_extra_date,
            )
        calculate = st.form_submit_button("Calculate impact", type="primary")

    if not calculate:
        st.caption("Choose an amount and date, then calculate the estimated impact.")
        return

    try:
        result = simulate_extra_payment(config, extra_amount, extra_date)
    except ValueError as exc:
        st.error(str(exc))
        return

    metrics = st.columns(4)
    metrics[0].metric(
        "Balance after extra payment", format_inr(result.estimated_balance_after_extra)
    )
    metrics[1].metric(
        "Estimated months saved",
        format_months(result.estimated_months_saved),
    )
    metrics[2].metric(
        "Estimated interest saved",
        format_inr(result.estimated_interest_savings)
        if result.estimated_interest_savings is not None
        else "Not available",
    )
    metrics[3].metric(
        "New payoff estimate",
        result.with_extra_payment.payoff_date.strftime("%b %Y")
        if result.with_extra_payment.payoff_date
        else "Not reached",
    )

    before = (
        result.baseline.payoff_date.strftime("%d %b %Y")
        if result.baseline.payoff_date
        else "not reached within 100 years"
    )
    after = (
        result.with_extra_payment.payoff_date.strftime("%d %b %Y")
        if result.with_extra_payment.payoff_date
        else "not reached within 100 years"
    )
    st.write(f"Estimated payoff changes from **{before}** to **{after}**.")

    comparison = pd.concat(
        [
            projection_series(result.baseline, "Current plan"),
            projection_series(result.with_extra_payment, "With extra payment"),
        ],
        axis=1,
    )
    st.subheader("Projected balance comparison")
    st.line_chart(comparison, color=["#8AA39B", "#1E8E6A"], height=360)

    if result.estimated_interest_savings is None:
        st.warning(
            "A reliable savings comparison needs both plans to reach payoff within the projection window. Consider increasing the EMI."
        )


initialize_state()
render_header()

with st.sidebar:
    st.title("LoanBreaker")
    area = st.radio("Go to", ["My Loan", "Add Payment", "What If"])
    st.divider()
    st.caption("V1 privacy")
    st.write("No login, bank connection, or cloud database. Data stays in this app session.")
    st.caption("Export CSV before closing or refreshing if you want a backup.")

if area == "My Loan":
    render_my_loan()
elif area == "Add Payment":
    render_add_payment()
else:
    render_what_if()

st.divider()
st.caption(
    "LoanBreaker provides estimates for educational planning only. Lender calculations can differ due to daily conventions, posting dates, rounding, capitalization, fees, holidays, and contract terms. Always confirm decisions with your lender statement or a qualified financial professional."
)
