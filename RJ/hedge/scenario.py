"""Scenario P&L engine and 1-year cost table."""

from dataclasses import dataclass
from datetime import date
from typing import List, Tuple

from .bs import bs_put_price, bs_call_price
from .legs import XSP_MULTIPLIER
from .compat import HAS_TABULATE, tabulate
from .output import _fmt_dollar, _fmt_prem


@dataclass
class ScenarioLegResult:
    name: str
    action: str
    quantity: int
    strike: float
    entry_price: float
    scenario_price: float
    pnl: float


def compute_scenario_pnl(
    spot_orig: float,
    spot_scenario: float,
    iv: float,
    rate: float,
    calc_date: date,
    put_legs: list,
    call_legs: list,
    notional: float,
) -> Tuple[List[ScenarioLegResult], float, float, float]:
    """Re-price all legs at spot_scenario. Returns (leg_results, hedge_pnl, portfolio_pnl, net_pnl)."""
    leg_results = []

    for leg in put_legs + call_legs:
        T = max((leg.expiry_date - calc_date).days / 365.0, 0.001)
        if leg.target_delta < 0:
            new_price = max(bs_put_price(spot_scenario, leg.computed_strike, T, rate, iv), 0.0)
        else:
            new_price = max(bs_call_price(spot_scenario, leg.computed_strike, T, rate, iv), 0.0)

        if leg.action == "BUY":
            pnl = (new_price - leg.est_premium) * leg.quantity * XSP_MULTIPLIER
        else:
            pnl = (leg.est_premium - new_price) * leg.quantity * XSP_MULTIPLIER

        leg_results.append(ScenarioLegResult(
            name=leg.name,
            action=leg.action,
            quantity=leg.quantity,
            strike=leg.computed_strike,
            entry_price=leg.est_premium,
            scenario_price=new_price,
            pnl=pnl,
        ))

    hedge_pnl = sum(r.pnl for r in leg_results)
    portfolio_pnl = notional * (spot_scenario - spot_orig) / spot_orig
    net_pnl = portfolio_pnl + hedge_pnl

    return leg_results, hedge_pnl, portfolio_pnl, net_pnl


def print_scenario_sheet(
    spot_orig: float,
    spot_scenario: float,
    leg_results: List[ScenarioLegResult],
    hedge_pnl: float,
    portfolio_pnl: float,
    net_pnl: float,
    notional: float,
):
    W = 72
    LINE = "=" * W
    DASH = "-" * W

    pct_move = (spot_scenario - spot_orig) / spot_orig * 100
    print(f"\n{LINE}")
    print(f"  SCENARIO ANALYSIS  —  XSP {spot_orig:.2f} → {spot_scenario:.2f}  ({pct_move:+.1f}%)")
    print(LINE)

    headers = ["Leg", "Act", "Qty", "Strike", "Entry $", "Scen $", "P&L"]
    rows = []
    for r in leg_results:
        rows.append([
            r.name, r.action, r.quantity,
            f"{r.strike:.0f}", _fmt_prem(r.entry_price),
            _fmt_prem(r.scenario_price), _fmt_dollar(r.pnl),
        ])

    rows.append(["─" * 22, "", "", "", "", "", ""])
    rows.append(["TOTAL HEDGE P&L", "", "", "", "", "", _fmt_dollar(hedge_pnl)])

    if HAS_TABULATE:
        print(tabulate(rows, headers=headers, tablefmt="simple",
                       colalign=("left", "center", "right", "right", "right", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Qty':>4} {'Strike':>7} {'Entry':>8} {'Scen':>8} {'P&L':>10}")
        for r in rows:
            print(f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>4} {str(r[3]):>7} "
                  f"{str(r[4]):>8} {str(r[5]):>8} {str(r[6]):>10}")

    print(f"\n{DASH}")
    print(f"  Portfolio loss ({pct_move:+.1f}% × ${notional:,.0f})  : {_fmt_dollar(portfolio_pnl)}")
    print(f"  Hedge P&L                            : {_fmt_dollar(hedge_pnl)}")
    print(f"  {'─' * 43}")
    net_label = "NET GAIN" if net_pnl >= 0 else "NET LOSS"
    net_pct = net_pnl / notional * 100
    print(f"  {net_label:<38} : {_fmt_dollar(net_pnl)}  ({net_pct:+.1f}% of notional)")
    print(f"{LINE}\n")


def _intrinsic_put_payoff(legs: list, spot: float) -> float:
    total = 0.0
    for leg in legs:
        intrinsic = max(leg.computed_strike - spot, 0.0)
        sign = 1 if leg.action == "BUY" else -1
        total += sign * intrinsic * leg.quantity * XSP_MULTIPLIER
    return total


def _intrinsic_call_payoff(legs: list, spot: float) -> float:
    total = 0.0
    for leg in legs:
        intrinsic = max(spot - leg.computed_strike, 0.0)
        sign = 1 if leg.action == "BUY" else -1
        total += sign * intrinsic * leg.quantity * XSP_MULTIPLIER
    return total


def compute_1year_table(
    spot: float,
    put_legs: list,
    call_legs: list,
    notional: float,
    net_put_per_roll: float,
    net_call_per_month: float,
) -> list:
    """
    For -25% to +25% in 5% steps, compute 1-year P&L assuming 2 put rolls + 12 call rolls.
    Returns list of dicts with keys: pct, spot_new, portfolio, put_pnl, call_pnl, hedge_pnl, net_pnl.
    """
    rows = []
    for pct in range(-25, 30, 5):
        spot_new = spot * (1 + pct / 100)
        portfolio_pnl = notional * pct / 100

        put_entry_cost = net_put_per_roll * 2
        put_payoff = _intrinsic_put_payoff(put_legs, spot_new)
        put_pnl = put_entry_cost + put_payoff

        call_income = net_call_per_month * 12
        call_payoff = _intrinsic_call_payoff(call_legs, spot_new)
        call_pnl = call_income + call_payoff

        hedge_pnl = put_pnl + call_pnl
        net_pnl = portfolio_pnl + hedge_pnl

        rows.append(dict(
            pct=pct, spot_new=spot_new, portfolio=portfolio_pnl,
            put_pnl=put_pnl, call_pnl=call_pnl, hedge_pnl=hedge_pnl, net_pnl=net_pnl,
        ))
    return rows


def print_1year_table(rows: list, notional: float):
    W = 72
    LINE = "=" * W

    print(f"\n{LINE}")
    print(f"  1-YEAR SCENARIO COST TABLE  (flat premiums, 2 put rolls + 12 call rolls)")
    print(LINE)

    headers = ["Move", "XSP", "Portfolio", "Put P&L", "Call P&L", "Hedge P&L", "NET P&L", "Net %"]
    table_rows = []
    for r in rows:
        net_pct = r["net_pnl"] / notional * 100
        marker = " ◀" if r["pct"] == 0 else ""
        table_rows.append([
            f"{r['pct']:+d}%{marker}", f"{r['spot_new']:.0f}",
            _fmt_dollar(r["portfolio"]), _fmt_dollar(r["put_pnl"]),
            _fmt_dollar(r["call_pnl"]), _fmt_dollar(r["hedge_pnl"]),
            _fmt_dollar(r["net_pnl"]), f"{net_pct:+.1f}%",
        ])

    if HAS_TABULATE:
        print(tabulate(table_rows, headers=headers, tablefmt="simple",
                       colalign=("left", "right", "right", "right", "right", "right", "right", "right")))
    else:
        print(f"  {'Move':>6} {'XSP':>6} {'Portfolio':>11} {'Put P&L':>11} {'Call P&L':>10} {'Hedge P&L':>11} {'NET P&L':>11} {'Net%':>6}")
        for r in table_rows:
            print(f"  {r[0]:>6} {r[1]:>6} {r[2]:>11} {r[3]:>11} {r[4]:>10} {r[5]:>11} {r[6]:>11} {r[7]:>6}")

    print(f"\n  Assumptions: put spine rolled 2× at entry premiums; call engine")
    print(f"  rolled 12× at entry premiums; payoffs based on intrinsic at year-end.")
    print(f"{LINE}\n")
