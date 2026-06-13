"""Strategy constants, VIX classification, expiry helpers, and position builders."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .bs import (
    bs_put_price, bs_call_price,
    delta_to_strike_put, delta_to_strike_call,
    _round_strike,
)

XSP_MULTIPLIER = 100
BASE_NOTIONAL = 1_000_000

PUT_LEGS = [
    {"name": "Long Anchor  (560P equiv)", "action": "BUY",  "target_delta": -0.35,  "base_qty": 20,
     "role": "Early Trigger / 30% Crash Wall"},
    {"name": "Long Bridge  (520P equiv)", "action": "BUY",  "target_delta": -0.375, "base_qty": 20,
     "role": "Apex Plateau / Convexity Gap-fill"},
    {"name": "Short Subsidy(440P equiv)", "action": "SELL", "target_delta": -0.22,  "base_qty": 34,
     "role": "Cost Offset / Anti-Gamma Subsidy"},
    {"name": "Long Floor   (400P equiv)", "action": "BUY",  "target_delta": -0.10,  "base_qty": 14,
     "role": "Lower Wing / Tail Security"},
    {"name": "Short Tail   (330P equiv)", "action": "SELL", "target_delta": -0.05,  "base_qty": 10,
     "role": "Tail Decay / Final Credit"},
]

# (zone, vix_max, multiplier, put_tenor_months, wing_tenor_months)
VIX_ZONES = [
    (1,   15, 1.20, 6, 9),
    (2,   22, 1.00, 6, 6),
    (3,   28, 0.80, 6, 6),
    (4,   35, 0.60, 4, 4),
    (5,  999, 0.40, 3, 3),
]

VIX_ZONE_NAMES = {
    1: "Low Vol    (< 15)",
    2: "Normal     (15–22)",
    3: "Elevated   (22–28)",
    4: "High Vol   (28–35)",
    5: "Extreme    (> 35)",
}

# {zone: [(short_delta, long_delta, base_units), ...] for each of 3 tiers}
CALL_TIERS = {
    1: [(0.050, 0.020, 30), (0.035, 0.015, 40), (0.030, 0.010, 30)],
    2: [(0.045, 0.015, 30), (0.030, 0.010, 40), (0.025, 0.008, 30)],
    3: [(0.040, 0.012, 20), (0.025, 0.008, 40), (0.020, 0.005, 40)],
    4: [(0.030, 0.010, 10), (0.020, 0.006, 30), (0.015, 0.004, 60)],
    5: [(0.030, 0.010, 10), (0.020, 0.006, 30), (0.015, 0.004, 60)],
}


def classify_vix(vix: float) -> Tuple[int, float, int, int]:
    """Return (zone, multiplier, put_tenor_months, wing_tenor_months)."""
    for zone, vix_max, mult, put_tenor, wing_tenor in VIX_ZONES:
        if vix < vix_max:
            return zone, mult, put_tenor, wing_tenor
    return 5, 0.40, 3, 3


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    days_to_friday = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=days_to_friday)
    return first_friday + timedelta(weeks=2)


def nearest_expiry(calc_date: date, target_months: float,
                   avoid_october: bool = True) -> date:
    """Find the nearest monthly XSP expiry (3rd Friday) approximately target_months out."""
    target_date = calc_date + timedelta(days=int(target_months * 30.44))
    candidates = []
    for delta_months in range(-1, 3):
        m = target_date.month + delta_months
        y = target_date.year
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        exp = third_friday(y, m)
        if exp > calc_date:
            candidates.append(exp)
    if avoid_october:
        filtered = [e for e in candidates if e.month != 10]
        if filtered:
            candidates = filtered
    candidates.sort(key=lambda e: abs((e - target_date).days))
    return candidates[0] if candidates else target_date


@dataclass
class Leg:
    name: str
    action: str        # BUY or SELL
    quantity: int
    target_delta: float
    computed_strike: float
    expiry_date: date
    dte: int
    est_premium: float
    est_total: float   # positive = credit, negative = debit
    role: str = ""


def build_put_spine(
    spot: float, vix: float, iv: float, rate: float,
    notional: float, calc_date: date,
    ibkr_chain: Optional[Dict] = None,
) -> Tuple[List[Leg], int, float]:
    """Build the 5-leg put spine. Returns (legs, zone, mult)."""
    zone, mult, put_tenor, wing_tenor = classify_vix(vix)
    sizing = mult * (notional / BASE_NOTIONAL)

    legs = []
    for i, leg_def in enumerate(PUT_LEGS):
        is_wing = i in (0, 1, 3)
        tenor = wing_tenor if (zone == 1 and is_wing) else put_tenor

        expiry = nearest_expiry(calc_date, tenor, avoid_october=True)
        T = max((expiry - calc_date).days / 365.0, 0.001)
        exp_str = expiry.strftime("%Y%m%d")

        ibkr_leg = (ibkr_chain or {}).get((exp_str, "P"))
        if ibkr_leg and abs(ibkr_leg.delta - leg_def["target_delta"]) < 0.15:
            strike = ibkr_leg.strike
            price = ibkr_leg.mid_price if ibkr_leg.mid_price > 0 else \
                max(bs_put_price(spot, strike, T, rate, iv), 0.01)
            price_source = "live"
        else:
            try:
                strike = delta_to_strike_put(leg_def["target_delta"], spot, T, rate, iv)
            except Exception:
                otm_map = {-0.35: 0.15, -0.375: 0.18, -0.22: 0.23, -0.10: 0.29, -0.05: 0.35}
                closest = min(otm_map, key=lambda k: abs(k - leg_def["target_delta"]))
                strike = _round_strike(spot * (1 - otm_map[closest]))
            price = max(bs_put_price(spot, strike, T, rate, iv), 0.01)
            price_source = "BS"

        qty = max(1, round(leg_def["base_qty"] * sizing))
        dte = (expiry - calc_date).days

        total = (-price if leg_def["action"] == "BUY" else +price) * qty * XSP_MULTIPLIER

        legs.append(Leg(
            name=leg_def["name"],
            action=leg_def["action"],
            quantity=qty,
            target_delta=leg_def["target_delta"],
            computed_strike=strike,
            expiry_date=expiry,
            dte=dte,
            est_premium=price,
            est_total=total,
            role=f"{leg_def['role']} [{price_source}]",
        ))

    return legs, zone, mult


def build_call_engine(
    spot: float, vix: float, iv: float, rate: float,
    notional: float, calc_date: date,
    ibkr_chain: Optional[Dict] = None,
) -> List[Leg]:
    """Build the 3-tier call engine."""
    zone, mult, _, _ = classify_vix(vix)
    sizing = mult * (notional / BASE_NOTIONAL)
    tiers = CALL_TIERS[zone]

    expiry = nearest_expiry(calc_date, 2.2, avoid_october=False)
    T = max((expiry - calc_date).days / 365.0, 0.001)
    dte = (expiry - calc_date).days
    exp_str = expiry.strftime("%Y%m%d")

    legs = []
    tier_names = ["Tier 1 (High-Yield)", "Tier 2 (Stability)", "Tier 3 (Tail Anchor)"]

    for i, (short_d, long_d, base_units) in enumerate(tiers):
        qty = max(1, round(base_units * sizing))
        ibkr_call = (ibkr_chain or {}).get((exp_str, "C"))

        if ibkr_call and abs(ibkr_call.delta - short_d) < 0.10:
            k_short = ibkr_call.strike
            p_short = ibkr_call.mid_price if ibkr_call.mid_price > 0 else \
                max(bs_call_price(spot, k_short, T, rate, iv), 0.01)
            short_src = "live"
        else:
            try:
                k_short = delta_to_strike_call(short_d, spot, T, rate, iv)
            except Exception:
                k_short = _round_strike(spot * (1 + (0.15 - short_d * 2)))
            p_short = max(bs_call_price(spot, k_short, T, rate, iv), 0.01)
            short_src = "BS"

        legs.append(Leg(
            name=f"{tier_names[i]} Short",
            action="SELL",
            quantity=qty,
            target_delta=short_d,
            computed_strike=k_short,
            expiry_date=expiry,
            dte=dte,
            est_premium=p_short,
            est_total=+p_short * qty * XSP_MULTIPLIER,
            role=f"Short call, Δ={short_d:.3f} [{short_src}]",
        ))

        try:
            k_long = delta_to_strike_call(long_d, spot, T, rate, iv)
        except Exception:
            k_long = _round_strike(spot * (1 + (0.18 - long_d * 2)))
        p_long = max(bs_call_price(spot, k_long, T, rate, iv), 0.005)

        legs.append(Leg(
            name=f"{tier_names[i]} Long ",
            action="BUY",
            quantity=qty,
            target_delta=long_d,
            computed_strike=k_long,
            expiry_date=expiry,
            dte=dte,
            est_premium=p_long,
            est_total=-p_long * qty * XSP_MULTIPLIER,
            role=f"Long call, Δ={long_d:.3f} [BS]",
        ))

    return legs
