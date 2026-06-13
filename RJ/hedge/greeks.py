"""Portfolio-level Greeks aggregation and display."""

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from .bs import bs_gamma, bs_vanna, bs_theta_put, bs_theta_call, bs_put_delta, bs_call_delta
from .legs import Leg, XSP_MULTIPLIER
from .compat import HAS_TABULATE, tabulate

# PUT_LEGS index of the Short Subsidy — the vanna lever
_SUBSIDY_IDX = 2


@dataclass
class LegGreeks:
    name: str
    action: str
    delta: float   # portfolio-scaled (qty × multiplier × per-share)
    gamma: float
    vanna: float
    theta: float   # per day


def compute_portfolio_greeks(
    put_legs: List[Leg],
    call_legs: List[Leg],
    spot: float,
    iv: float,
    rate: float,
    calc_date: date,
) -> List[LegGreeks]:
    results = []
    for leg in put_legs + call_legs:
        T = max((leg.expiry_date - calc_date).days / 365.0, 0.001)
        K = leg.computed_strike
        sign = 1 if leg.action == "BUY" else -1
        scale = sign * leg.quantity * XSP_MULTIPLIER

        if leg.target_delta < 0:  # put
            delta = bs_put_delta(spot, K, T, rate, iv)
            theta = bs_theta_put(spot, K, T, rate, iv)
        else:                      # call
            delta = bs_call_delta(spot, K, T, rate, iv)
            theta = bs_theta_call(spot, K, T, rate, iv)

        gamma = bs_gamma(spot, K, T, rate, iv)
        vanna = bs_vanna(spot, K, T, rate, iv)

        results.append(LegGreeks(
            name=leg.name,
            action=leg.action,
            delta=delta * scale,
            gamma=gamma * scale,
            vanna=vanna * scale,
            theta=theta * scale,
        ))

    return results


def compute_vanna_neutral_adjustment(
    leg_greeks: List[LegGreeks],
    put_legs: List[Leg],
    spot: float,
    iv: float,
    rate: float,
    calc_date: date,
) -> Optional[Dict]:
    """
    Solve for the Short Subsidy quantity that brings portfolio vanna to zero.

    Returns a dict with current_qty, recommended_qty, delta_qty, projected_vanna,
    or None if the subsidy leg has negligible vanna per contract.
    """
    total_vanna = sum(g.vanna for g in leg_greeks)

    subsidy_leg = put_legs[_SUBSIDY_IDX]
    T = max((subsidy_leg.expiry_date - calc_date).days / 365.0, 0.001)
    # Vanna per 1 additional SELL contract of Short Subsidy (sign = -1)
    vanna_per_contract = bs_vanna(spot, subsidy_leg.computed_strike, T, rate, iv) * (-1) * XSP_MULTIPLIER

    if abs(vanna_per_contract) < 1e-8:
        return None

    contracts_to_add = -total_vanna / vanna_per_contract
    recommended_qty = max(1, round(subsidy_leg.quantity + contracts_to_add))
    projected_vanna = total_vanna + (recommended_qty - subsidy_leg.quantity) * vanna_per_contract

    return dict(
        leg_name=subsidy_leg.name.strip(),
        current_qty=subsidy_leg.quantity,
        recommended_qty=recommended_qty,
        delta_qty=recommended_qty - subsidy_leg.quantity,
        vanna_per_contract=vanna_per_contract,
        projected_vanna=projected_vanna,
    )


def print_greeks_summary(leg_greeks: List[LegGreeks], adjustment: Optional[Dict] = None):
    W = 72
    LINE = "=" * W
    DASH = "-" * W

    print(f"\n{LINE}")
    print("  PORTFOLIO GREEKS  (position-scaled: qty × 100 × per-share)")
    print(DASH)

    headers = ["Leg", "Act", "Δ (delta)", "Γ (gamma)", "Vanna", "Θ/day"]
    rows = []
    for g in leg_greeks:
        rows.append([
            g.name, g.action,
            f"{g.delta:+.1f}",
            f"{g.gamma:+.4f}",
            f"{g.vanna:+.4f}",
            f"{g.theta:+.2f}",
        ])

    total_delta = sum(g.delta for g in leg_greeks)
    total_gamma = sum(g.gamma for g in leg_greeks)
    total_vanna = sum(g.vanna for g in leg_greeks)
    total_theta = sum(g.theta for g in leg_greeks)

    rows.append(["─" * 22, "", "", "", "", ""])
    rows.append([
        "PORTFOLIO TOTAL", "",
        f"{total_delta:+.1f}",
        f"{total_gamma:+.4f}",
        f"{total_vanna:+.4f}",
        f"{total_theta:+.2f}",
    ])

    if HAS_TABULATE:
        print(tabulate(rows, headers=headers, tablefmt="simple",
                       colalign=("left", "center", "right", "right", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Delta':>10} {'Gamma':>10} {'Vanna':>10} {'Theta/d':>8}")
        for r in rows:
            print(f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>10} {str(r[3]):>10} {str(r[4]):>10} {str(r[5]):>8}")

    vanna_neutral_threshold = 50  # position-scaled units
    vanna_status = "✓ VANNA-NEUTRAL" if abs(total_vanna) < vanna_neutral_threshold else f"✗ net vanna = {total_vanna:+.1f}"
    print(f"\n  Vanna-neutral check : {vanna_status}  (threshold ±{vanna_neutral_threshold})")
    print(f"  Theta decay         : {total_theta:+.2f} / day  (${total_theta * 30:+.0f} / month est.)")

    if adjustment:
        adj = adjustment
        direction = "▲ add" if adj["delta_qty"] > 0 else "▼ reduce by"
        qty_change = abs(adj["delta_qty"])
        print(f"\n  VANNA REBALANCE SUGGESTION")
        print(f"  {'─' * 44}")
        print(f"  Lever leg    : {adj['leg_name']}")
        print(f"  Current qty  : {adj['current_qty']} contracts")
        print(f"  Recommended  : {adj['recommended_qty']} contracts  ({direction} {qty_change})")
        print(f"  Vanna/contract: {adj['vanna_per_contract']:+.2f}")
        print(f"  Projected vanna after rebalance: {adj['projected_vanna']:+.1f}")
        print(f"  Use --auto-vanna to apply automatically.")

    print(f"{LINE}\n")
