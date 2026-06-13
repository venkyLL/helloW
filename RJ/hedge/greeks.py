"""Portfolio-level Greeks aggregation and display."""

from dataclasses import dataclass
from datetime import date
from typing import List

from .bs import bs_gamma, bs_vanna, bs_theta_put, bs_theta_call, bs_put_delta, bs_call_delta
from .legs import Leg, XSP_MULTIPLIER
from .compat import HAS_TABULATE, tabulate


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


def print_greeks_summary(leg_greeks: List[LegGreeks]):
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

    vanna_neutral_threshold = 0.5
    vanna_status = "✓ VANNA-NEUTRAL" if abs(total_vanna) < vanna_neutral_threshold else f"✗ net vanna = {total_vanna:+.4f}"
    print(f"\n  Vanna-neutral check : {vanna_status}  (threshold ±{vanna_neutral_threshold})")
    print(f"  Theta decay         : {total_theta:+.2f} / day  (${total_theta * 30:+.0f} / month est.)")
    print(f"{LINE}\n")
