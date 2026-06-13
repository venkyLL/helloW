#!/usr/bin/env python3
"""
Vanna-Neutral 3-1-1 Hedge Calculator
=====================================
Sizes a XSP options hedge for a $1M S&P 500 portfolio.
Strategy: 5-leg put spine + 3-tier call funding engine, VIX-adjusted.

Usage:
  python RJ.py --spot 741.28 --vix 18.5
  python RJ.py --live                              # auto-fetch XSP + VIX via yfinance
  python RJ.py --live --ibkr                       # live data + IBKR TWS option chain
  python RJ.py --live --ibkr --ibkr-port 4001      # IB Gateway (paper/live)
  python RJ.py --spot 741.28 --vix 18.5 --iv 19.2 --rate 4.5 --notional 1000000
  python RJ.py --spot 741.28 --vix 18.5 --last-roll-date 2026-03-15 --last-roll-spot 710

Dependencies: pip install scipy tabulate
Optional:     pip install yfinance          (--live flag)
              pip install ib_insync         (--ibkr flag)
"""

import argparse
import math
import sys
import time
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple

# ── optional deps ──────────────────────────────────────────────────────────────
try:
    from scipy.stats import norm
    from scipy.optimize import brentq

    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

try:
    from tabulate import tabulate

    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

try:
    import yfinance as yf

    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

try:
    from ib_insync import IB, Option as IBOption, util as ib_util

    HAS_IBKR = True
except ImportError:
    HAS_IBKR = False

# ══════════════════════════════════════════════════════════════════════════════
#  LIVE DATA — yfinance
# ══════════════════════════════════════════════════════════════════════════════

def _yf_last_price(ticker_sym: str) -> Optional[float]:
    """Return last price from yfinance fast_info, with history fallback."""
    t = yf.Ticker(ticker_sym)
    try:
        p = t.fast_info.get("lastPrice") or t.fast_info.get("regularMarketPrice")
        if p:
            return float(p)
    except Exception:
        pass
    hist = t.history(period="5d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])
    return None


def fetch_live_yfinance() -> Tuple[float, float]:
    """
    Fetch latest XSP spot price and VIX from Yahoo Finance.
    XSP = S&P 500 / 10; tries XSP direct first, falls back to ^GSPC / 10.
    Returns (xsp_spot, vix_level).
    Raises RuntimeError if yfinance is not installed or data unavailable.
    """
    if not HAS_YFINANCE:
        raise RuntimeError("yfinance not installed. Run: pip install yfinance")

    print("  [yfinance] Fetching XSP spot and VIX ...", flush=True)

    # VIX
    vix_level = _yf_last_price("^VIX")
    if not vix_level:
        raise RuntimeError("yfinance returned no data for ^VIX")

    # XSP — try direct ticker, fall back to ^GSPC / 10
    xsp_spot = _yf_last_price("XSP")
    if not xsp_spot:
        spx = _yf_last_price("^GSPC")
        if not spx:
            raise RuntimeError("yfinance returned no data for XSP or ^GSPC")
        xsp_spot = spx / 10.0
        print(f"  [yfinance] XSP not available directly — using ^GSPC/10", flush=True)

    print(f"  [yfinance] XSP={xsp_spot:.2f}  VIX={vix_level:.2f}", flush=True)
    return float(xsp_spot), float(vix_level)


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE DATA — IBKR TWS / IB Gateway
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class IBKRLeg:
    """Live strike + delta resolved from the IBKR option chain."""
    expiry_str: str     # YYYYMMDD
    right: str          # 'P' or 'C'
    strike: float
    delta: float
    mid_price: float    # (bid+ask)/2, 0.0 if market closed


def _ibkr_expiry_str(expiry_date: date) -> str:
    return expiry_date.strftime("%Y%m%d")


def fetch_ibkr_chain(
    expiry_dates: List[date],
    option_rights: List[str],           # 'P' or 'C' per expiry
    target_deltas: List[float],         # signed: negative for puts
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
    timeout: int = 30,
) -> Dict[Tuple[str, str], IBKRLeg]:
    """
    Connect to TWS/IB Gateway, pull XSP option chain for the given expiries,
    and find the strike whose live delta best matches each target.

    Returns a dict keyed by (expiry_str, right) → IBKRLeg.
    Raises RuntimeError on connection or data failure.
    """
    if not HAS_IBKR:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib_util.logToConsole(level=0)   # suppress ib_insync noise
    ib = IB()
    print(f"  [IBKR] Connecting to TWS at {host}:{port} ...", flush=True)
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"IBKR connection failed: {e}")

    results: Dict[Tuple[str, str], IBKRLeg] = {}

    try:
        for expiry_date, right, target_delta in zip(expiry_dates, option_rights, target_deltas):
            exp_str = _ibkr_expiry_str(expiry_date)
            print(f"  [IBKR] Requesting chain  XSP  {exp_str}  {right}  ...", flush=True)

            # Build a reference contract to discover available strikes
            ref = IBOption("XSP", exp_str, 0, right, "SMART", "USD")
            details = ib.reqContractDetails(ref)
            if not details:
                print(f"  [IBKR] WARNING: no chain data for {exp_str} {right} — skipping")
                continue

            strikes = sorted(set(float(d.contract.strike) for d in details))
            if not strikes:
                print(f"  [IBKR] WARNING: empty strike list for {exp_str} {right}")
                continue

            # Request market data + greeks for each strike (snapshot)
            contracts = [IBOption("XSP", exp_str, k, right, "SMART", "USD") for k in strikes]
            ib.qualifyContracts(*contracts)

            tickers = ib.reqTickers(*contracts)
            time.sleep(2)   # let market data populate
            tickers = ib.reqTickers(*contracts)  # refresh

            best_leg: Optional[IBKRLeg] = None
            best_diff = float("inf")

            for ticker, strike in zip(tickers, strikes):
                greeks = ticker.modelGreeks or ticker.lastGreeks
                if greeks is None:
                    continue
                live_delta = greeks.delta
                if live_delta is None:
                    continue

                diff = abs(live_delta - target_delta)
                if diff < best_diff:
                    best_diff = diff
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
                    mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                    best_leg = IBKRLeg(
                        expiry_str=exp_str,
                        right=right,
                        strike=strike,
                        delta=live_delta,
                        mid_price=mid,
                    )

            if best_leg:
                results[(exp_str, right)] = best_leg
                print(f"  [IBKR]   Best match: K={best_leg.strike:.0f}  δ={best_leg.delta:+.4f}"
                      f"  mid=${best_leg.mid_price:.2f}  (target δ={target_delta:+.4f})")
            else:
                print(f"  [IBKR] WARNING: could not match delta for {exp_str} {right}")

    finally:
        ib.disconnect()
        print("  [IBKR] Disconnected.", flush=True)

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

XSP_MULTIPLIER = 100  # 1 XSP contract = 100 × index level notional
STRIKE_ROUND = 5  # Round computed strikes to nearest 5 points
BASE_NOTIONAL = 1_000_000  # Baseline portfolio size

# Baseline put spine (100% sizing, VIX Zone 2)
PUT_LEGS = [
    {"name": "Long Anchor  (560P equiv)", "action": "BUY", "target_delta": -0.35, "base_qty": 20,
     "role": "Early Trigger / 30% Crash Wall"},
    {"name": "Long Bridge  (520P equiv)", "action": "BUY", "target_delta": -0.375, "base_qty": 20,
     "role": "Apex Plateau / Convexity Gap-fill"},
    {"name": "Short Subsidy(440P equiv)", "action": "SELL", "target_delta": -0.22, "base_qty": 34,
     "role": "Cost Offset / Anti-Gamma Subsidy"},
    {"name": "Long Floor   (400P equiv)", "action": "BUY", "target_delta": -0.10, "base_qty": 14,
     "role": "Lower Wing / Tail Security"},
    {"name": "Short Tail   (330P equiv)", "action": "SELL", "target_delta": -0.05, "base_qty": 10,
     "role": "Tail Decay / Final Credit"},
]

# VIX zone definitions
#   zone, vix_max, multiplier, put_tenor_months, wing_tenor_months
VIX_ZONES = [
    (1, 15, 1.20, 6, 9),  # Zone 1: Low vol   → 120%, extend wings to 9M
    (2, 22, 1.00, 6, 6),  # Zone 2: Normal    → 100%, standard 6M
    (3, 28, 0.80, 6, 6),  # Zone 3: Elevated  → 80%
    (4, 35, 0.60, 4, 4),  # Zone 4: High vol  → 60%, 4M tenor
    (5, 999, 0.40, 3, 3),  # Zone 5: Extreme   → 40%, 3M tenor
]

VIX_ZONE_NAMES = {
    1: "Low Vol    (< 15)",
    2: "Normal     (15–22)",
    3: "Elevated   (22–28)",
    4: "High Vol   (28–35)",
    5: "Extreme    (> 35)",
}

# Call engine tier definitions per VIX zone
# Format: {zone: [(short_delta, long_delta, units), ...] for each of 3 tiers}
CALL_TIERS = {
    1: [(0.050, 0.020, 30), (0.035, 0.015, 40), (0.030, 0.010, 30)],  # Low vol
    2: [(0.045, 0.015, 30), (0.030, 0.010, 40), (0.025, 0.008, 30)],  # Normal
    3: [(0.040, 0.012, 20), (0.025, 0.008, 40), (0.020, 0.005, 40)],  # Elevated
    4: [(0.030, 0.010, 10), (0.020, 0.006, 30), (0.015, 0.004, 60)],  # High vol
    5: [(0.030, 0.010, 10), (0.020, 0.006, 30), (0.015, 0.004, 60)],  # Extreme (same as 4)
}


# ══════════════════════════════════════════════════════════════════════════════
#  BLACK-SCHOLES ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    """Standard normal CDF — uses scipy if available, else manual approximation."""
    if HAS_SCIPY:
        return float(norm.cdf(x))
    # Abramowitz & Stegun approximation (error < 7.5e-8)
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
                                                       + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put delta: N(d1) - 1"""
    if T <= 0 or sigma <= 0 or K <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call delta: N(d1)"""
    if T <= 0 or sigma <= 0 or K <= 0:
        return 1.0 if K < S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes put price."""
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call price."""
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def delta_to_strike_put(target_delta: float, S: float, T: float, r: float,
                        sigma: float) -> float:
    """
    Invert put delta to find strike K such that bs_put_delta(S,K,T,r,sigma) ≈ target_delta.
    Uses Brent's method (scipy) or bisection fallback.
    """
    if not (-1.0 < target_delta < 0.0):
        raise ValueError(f"Put delta must be in (-1, 0), got {target_delta}")

    f = lambda K: bs_put_delta(S, K, T, r, sigma) - target_delta
    K_low, K_high = S * 0.20, S * 1.05

    # Check bounds
    if f(K_low) * f(K_high) > 0:
        # Expand bounds
        K_low = S * 0.10
        K_high = S * 1.10

    if HAS_SCIPY:
        try:
            K_sol = brentq(f, K_low, K_high, xtol=0.01, rtol=1e-6)
        except ValueError:
            K_sol = _bisection(f, K_low, K_high)
    else:
        K_sol = _bisection(f, K_low, K_high)

    return _round_strike(K_sol)


def delta_to_strike_call(target_delta: float, S: float, T: float, r: float,
                         sigma: float) -> float:
    """
    Invert call delta to find strike K.
    """
    if not (0.0 < target_delta < 1.0):
        raise ValueError(f"Call delta must be in (0, 1), got {target_delta}")

    f = lambda K: bs_call_delta(S, K, T, r, sigma) - target_delta
    K_low, K_high = S * 0.95, S * 1.80

    if f(K_low) * f(K_high) > 0:
        K_high = S * 2.50

    if HAS_SCIPY:
        try:
            K_sol = brentq(f, K_low, K_high, xtol=0.01, rtol=1e-6)
        except ValueError:
            K_sol = _bisection(f, K_low, K_high)
    else:
        K_sol = _bisection(f, K_low, K_high)

    return _round_strike(K_sol)


def _bisection(f, a: float, b: float, tol: float = 0.10, max_iter: int = 60) -> float:
    """Simple bisection root finder."""
    fa, fb = f(a), f(b)
    if fa * fb > 0:
        # Return midpoint as best guess
        return (a + b) / 2.0
    for _ in range(max_iter):
        mid = (a + b) / 2.0
        fmid = f(mid)
        if abs(fmid) < tol or (b - a) / 2.0 < tol:
            return mid
        if fa * fmid < 0:
            b, fb = mid, fmid
        else:
            a, fa = mid, fmid
    return (a + b) / 2.0


def _round_strike(K: float) -> float:
    """Round to nearest STRIKE_ROUND (5) points."""
    return round(K / STRIKE_ROUND) * STRIKE_ROUND


# ══════════════════════════════════════════════════════════════════════════════
#  VIX CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_vix(vix: float):
    """Return (zone_num, multiplier, put_tenor_months, wing_tenor_months)."""
    for zone, vix_max, mult, put_tenor, wing_tenor in VIX_ZONES:
        if vix < vix_max:
            return zone, mult, put_tenor, wing_tenor
    return 5, 0.40, 3, 3  # fallback


# ══════════════════════════════════════════════════════════════════════════════
#  EXPIRY DATE LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def third_friday(year: int, month: int) -> date:
    """Return the 3rd Friday of a given month."""
    d = date(year, month, 1)
    # Find first Friday
    days_to_friday = (4 - d.weekday()) % 7
    first_friday = d + timedelta(days=days_to_friday)
    return first_friday + timedelta(weeks=2)


def nearest_expiry(calc_date: date, target_months: float,
                   avoid_october: bool = True) -> date:
    """
    Find the nearest monthly XSP expiry approximately target_months out.
    XSP expires on the 3rd Friday of each month.
    Avoids October if avoid_october=True.
    """
    target_date = calc_date + timedelta(days=int(target_months * 30.44))

    # Check the target month and ±1 month
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

    # Filter October if requested
    if avoid_october:
        filtered = [e for e in candidates if e.month != 10]
        if filtered:
            candidates = filtered

    # Pick closest to target
    candidates.sort(key=lambda e: abs((e - target_date).days))
    return candidates[0] if candidates else target_date


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Leg:
    name: str
    action: str  # BUY or SELL
    quantity: int
    target_delta: float
    computed_strike: float
    expiry_date: date
    dte: int
    est_premium: float  # per share (×100 for contract value)
    est_total: float  # positive = credit, negative = debit
    role: str = ""


def build_put_spine(spot: float, vix: float, iv: float, rate: float,
                    notional: float, calc_date: date,
                    ibkr_chain: Optional[Dict] = None):
    """Build the 5-leg put spine with VIX-adjusted quantities and expiries.

    ibkr_chain: optional dict from fetch_ibkr_chain(), keyed (expiry_str, right).
    When present, live strikes and mid prices replace BS estimates.
    """
    zone, mult, put_tenor, wing_tenor = classify_vix(vix)
    sizing = mult * (notional / BASE_NOTIONAL)

    legs = []
    for i, leg_def in enumerate(PUT_LEGS):
        is_wing = i in (0, 1, 3)
        tenor = wing_tenor if (zone == 1 and is_wing) else put_tenor

        expiry = nearest_expiry(calc_date, tenor, avoid_october=True)
        T = max((expiry - calc_date).days / 365.0, 0.001)
        exp_str = expiry.strftime("%Y%m%d")

        # Try IBKR live chain first
        ibkr_leg = (ibkr_chain or {}).get((exp_str, "P"))
        if ibkr_leg and abs(ibkr_leg.delta - leg_def["target_delta"]) < 0.15:
            strike = ibkr_leg.strike
            price = ibkr_leg.mid_price if ibkr_leg.mid_price > 0 else \
                max(bs_put_price(spot, strike, T, rate, iv), 0.01)
            price_source = "live"
        else:
            # BS delta inversion
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

        if leg_def["action"] == "BUY":
            total = -price * qty * XSP_MULTIPLIER
        else:
            total = +price * qty * XSP_MULTIPLIER

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


def build_call_engine(spot: float, vix: float, iv: float, rate: float,
                      notional: float, calc_date: date,
                      ibkr_chain: Optional[Dict] = None):
    """Build the 3-tier call engine with VIX-adjusted deltas and quantities.

    ibkr_chain: optional dict from fetch_ibkr_chain(), keyed (expiry_str, right).
    """
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

        # Short leg (sell call)
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

        # Long leg (buy call)
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


# ══════════════════════════════════════════════════════════════════════════════
#  ROLL ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def check_roll_alerts(calc_date: date, spot: float, vix: float,
                      last_roll_date: Optional[date], last_roll_spot: Optional[float]):
    alerts = []

    if last_roll_date is None:
        alerts.append(("INFO", "Roll Alerts", "Provide --last-roll-date and --last-roll-spot to enable roll alerts."))
        return alerts

    days_since_roll = (calc_date - last_roll_date).days
    next_roll = last_roll_date + timedelta(days=90)

    # 1. Scheduled roll
    if days_since_roll >= 90:
        alerts.append(("ALERT", "Scheduled Roll",
                       f"Due NOW — {days_since_roll} days since last roll. Full rebuild required."))
    else:
        days_left = (next_roll - calc_date).days
        alerts.append(("OK", "Scheduled Roll",
                       f"Next roll due {next_roll.strftime('%Y-%m-%d')} ({days_left} days)."))

    # 2. Fast drop rule (>10% in <10 days)
    if last_roll_spot:
        pct_change = (spot - last_roll_spot) / last_roll_spot * 100
        if pct_change <= -10 and days_since_roll <= 10:
            alerts.append(("ALERT", "Fast Drop Rule",
                           f"Spot dropped {pct_change:.1f}% in {days_since_roll}d. "
                           f"Roll SHORT SUBSIDY down to restore –0.22δ. Do NOT touch anchor."))
        elif pct_change <= -10:
            alerts.append(("WARN", "Fast Drop Rule",
                           f"Spot down {pct_change:.1f}% since last roll. "
                           f"Monitor short subsidy delta — roll if delta breaches –0.25."))
        else:
            alerts.append(("OK", "Fast Drop Rule",
                           f"No action. Spot {pct_change:+.1f}% from last roll."))

        # 3. Rally rule (>10% rally)
        if pct_change >= 10:
            alerts.append(("ALERT", "Rally Rule",
                           f"Spot rallied {pct_change:.1f}%. Roll anchor PUT UP to restore –15% deductible. "
                           f"Roll call spread up."))
        else:
            alerts.append(("OK", "Rally Rule",
                           f"No action. Spot {pct_change:+.1f}% from last roll."))

    # 4. VIX shock override (can't fully detect without prior VIX, but warn if extreme)
    if vix > 35:
        alerts.append(("ALERT", "VIX Shock",
                       f"VIX={vix:.1f} (Zone 5). Apply 40% sizing immediately — do not wait for quarterly roll."))
    elif vix > 28:
        alerts.append(("WARN", "VIX Shock",
                       f"VIX={vix:.1f} (Zone 4). Consider reducing to 60% sizing if VIX jumped >8pts recently."))
    else:
        alerts.append(("OK", "VIX Shock",
                       f"VIX={vix:.1f} — no shock override needed."))

    return alerts


# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO P&L ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScenarioLegResult:
    name: str
    action: str
    quantity: int
    strike: float
    entry_price: float
    scenario_price: float
    pnl: float          # positive = gain


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
    """
    Re-price all legs at spot_scenario (same expiries, same IV, same calc_date).
    Returns (leg_results, hedge_pnl, portfolio_pnl, net_pnl).
    portfolio_pnl = notional × (scenario_spot - orig_spot) / orig_spot
    """
    leg_results = []

    for leg in put_legs + call_legs:
        T = max((leg.expiry_date - calc_date).days / 365.0, 0.001)
        if leg.action == "BUY":
            # long option: gain when price rises
            if leg.target_delta < 0:
                new_price = bs_put_price(spot_scenario, leg.computed_strike, T, rate, iv)
            else:
                new_price = bs_call_price(spot_scenario, leg.computed_strike, T, rate, iv)
            new_price = max(new_price, 0.0)
            pnl = (new_price - leg.est_premium) * leg.quantity * XSP_MULTIPLIER
        else:
            # short option: gain when price falls
            if leg.target_delta < 0:
                new_price = bs_put_price(spot_scenario, leg.computed_strike, T, rate, iv)
            else:
                new_price = bs_call_price(spot_scenario, leg.computed_strike, T, rate, iv)
            new_price = max(new_price, 0.0)
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
            r.name,
            r.action,
            r.quantity,
            f"{r.strike:.0f}",
            _fmt_prem(r.entry_price),
            _fmt_prem(r.scenario_price),
            _fmt_dollar(r.pnl),
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


# ══════════════════════════════════════════════════════════════════════════════
#  OUTPUT FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_dollar(n: float) -> str:
    if n >= 0:
        return f"+${n:,.0f}"
    return f"-${abs(n):,.0f}"


def _fmt_prem(n: float) -> str:
    return f"${n:.2f}"


def _alert_prefix(status: str) -> str:
    return {"OK": "[OK]   ", "WARN": "[WARN] ", "ALERT": "[!!!!] ", "INFO": "[INFO] "}.get(status, "       ")


def print_position_sheet(
        spot: float, vix: float, iv: float, rate: float, notional: float,
        calc_date: date, put_legs, call_legs, zone: int, mult: float,
        last_roll_date, last_roll_spot, alerts,
        data_source: str = "manual",
):
    W = 72
    LINE = "=" * W
    DASH = "-" * W

    print(f"\n{LINE}")
    print(f"  VANNA-NEUTRAL 3-1-1 HEDGE CALCULATOR")
    print(LINE)
    print(f"  Date       : {calc_date.strftime('%Y-%m-%d')}    Notional : ${notional:,.0f}")
    print(f"  XSP Spot   : {spot:.2f}           VIX      : {vix:.1f}")
    print(f"  IV (6M)    : {iv * 100:.1f}%            Rfr      : {rate * 100:.2f}%")
    print(f"  VIX ZONE   : {zone} — {VIX_ZONE_NAMES[zone]}  (sizing: {mult * 100:.0f}%)")
    print(f"  Data Source: {data_source}")
    print(DASH)

    # ── PUT SPINE ────────────────────────────────────────────────────────────
    put_expiries = sorted(set(l.expiry_date for l in put_legs))
    exp_str = ", ".join(f"{e.strftime('%b %d %Y')} ({(e - calc_date).days}d)" for e in put_expiries)
    print(f"\n  PUT SPINE  — {exp_str}")
    print(DASH)

    put_headers = ["Leg", "Act", "Qty", "Target δ", "Strike", "Expiry", "Prem/sh", "Total"]
    put_rows = []
    for l in put_legs:
        put_rows.append([
            l.name,
            l.action,
            l.quantity,
            f"{l.target_delta:+.3f}",
            f"{l.computed_strike:.0f}",
            l.expiry_date.strftime("%b %Y"),
            _fmt_prem(l.est_premium),
            _fmt_dollar(l.est_total),
        ])

    net_put = sum(l.est_total for l in put_legs)
    put_rows.append(["─" * 22, "", "", "", "", "", "", ""])
    put_rows.append(["NET PUT SPINE", "", sum(l.quantity for l in put_legs),
                     "", "", "", "", _fmt_dollar(net_put)])

    if HAS_TABULATE:
        print(tabulate(put_rows, headers=put_headers, tablefmt="simple",
                       colalign=("left", "center", "right", "center", "right", "center", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Qty':>4} {'δ':>8} {'Strike':>7} {'Expiry':^8} {'Prem':>7} {'Total':>10}")
        for r in put_rows:
            print(
                f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>4} {str(r[3]):>8} {str(r[4]):>7} {str(r[5]):^8} {str(r[6]):>7} {str(r[7]):>10}")

    # ── CALL ENGINE ──────────────────────────────────────────────────────────
    call_expiry = call_legs[0].expiry_date if call_legs else calc_date
    call_dte = (call_expiry - calc_date).days
    print(f"\n  CALL ENGINE — {call_expiry.strftime('%b %d %Y')} ({call_dte}d)")
    print(DASH)

    call_headers = ["Leg", "Act", "Qty", "Target δ", "Strike", "Prem/sh", "Total"]
    call_rows = []
    for l in call_legs:
        call_rows.append([
            l.name,
            l.action,
            l.quantity,
            f"{l.target_delta:+.4f}",
            f"{l.computed_strike:.0f}",
            _fmt_prem(l.est_premium),
            _fmt_dollar(l.est_total),
        ])

    net_call = sum(l.est_total for l in call_legs)
    call_rows.append(["─" * 22, "", "", "", "", "", ""])
    call_rows.append(["NET CALL CREDIT", "", sum(l.quantity for l in call_legs),
                      "", "", "", _fmt_dollar(net_call)])

    if HAS_TABULATE:
        print(tabulate(call_rows, headers=call_headers, tablefmt="simple",
                       colalign=("left", "center", "right", "center", "right", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Qty':>4} {'δ':>8} {'Strike':>7} {'Prem':>7} {'Total':>10}")
        for r in call_rows:
            print(
                f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>4} {str(r[3]):>8} {str(r[4]):>7} {str(r[5]):>7} {str(r[6]):>10}")

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    print(f"\n{LINE}")
    print("  COST SUMMARY")
    print(DASH)

    net_cycle = net_put + net_call
    ann_call = net_call * 12  # monthly call × 12
    ann_put = net_put * 2  # 6M put × 2 rolls/year
    ann_net = ann_put + ann_call
    ann_pct = ann_net / notional * 100

    total_put_contracts = sum(l.quantity for l in put_legs)
    total_call_contracts = sum(l.quantity for l in call_legs)
    total_contracts = total_put_contracts + total_call_contracts

    print(f"  Net put spine cost (6M cycle)    : {_fmt_dollar(net_put)}")
    print(f"  Net call engine credit (monthly) : {_fmt_dollar(net_call)}")
    print(f"  Annualized put cost (×2 rolls)   : {_fmt_dollar(ann_put)}")
    print(f"  Annualized call income (×12)     : {_fmt_dollar(ann_call)}")
    print(f"  ─────────────────────────────────────────────")
    carry_label = "POSITIVE CARRY" if ann_net >= 0 else "Net annual cost"
    print(f"  {carry_label:<32} : {_fmt_dollar(ann_net)}  ({ann_pct:+.2f}% of notional)")
    print(f"")
    print(f"  Total open contracts at any time : {total_contracts}")
    print(f"    Put spine : {total_put_contracts} contracts")
    print(f"    Call engine: {total_call_contracts} contracts")

    # Contract sizing sanity check
    gross_put_notional = sum(
        l.computed_strike * l.quantity * XSP_MULTIPLIER
        for l in put_legs if l.action == "BUY"
    )
    coverage_pct = gross_put_notional / notional * 100
    if coverage_pct < 50 or coverage_pct > 500:
        print(f"\n  [WARN] Long put notional ${gross_put_notional:,.0f} ({coverage_pct:.0f}% of portfolio)")
        print(f"         Check contract sizing vs. portfolio.")

    # ── ROLL ALERTS ──────────────────────────────────────────────────────────
    print(f"\n{LINE}")
    print("  ROLL ALERTS")
    print(DASH)
    for status, name, msg in alerts:
        prefix = _alert_prefix(status)
        # Word wrap long messages
        line = f"  {prefix}{name:<18}: {msg}"
        if len(line) <= W + 4:
            print(line)
        else:
            print(f"  {prefix}{name:<18}:")
            # Simple wrap
            words = msg.split()
            cur = "    " + " " * 26
            for w in words:
                if len(cur) + len(w) + 1 > W:
                    print(cur)
                    cur = "    " + " " * 26 + w
                else:
                    cur += " " + w
            if cur.strip():
                print(cur)

    # ── DISCLAIMER ───────────────────────────────────────────────────────────
    print(f"\n{LINE}")
    print("  DISCLAIMER: All strikes and premiums are ESTIMATES based on")
    print("  Black-Scholes theoretical mid-market values. Actual execution")
    print("  prices will differ. Use limit orders. Verify with live quotes.")
    print("  This is not financial advice.")
    print(f"{LINE}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Vanna-Neutral 3-1-1 Hedge Calculator for a $1M SPX portfolio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python RJ.py --spot 741.28 --vix 18.5
  python RJ.py --live                              # auto-fetch XSP + VIX via yfinance
  python RJ.py --live --ibkr                       # yfinance + IBKR TWS option chain
  python RJ.py --live --ibkr --ibkr-port 4001      # IB Gateway (paper: 4002, live: 4001)
  python RJ.py --spot 741.28 --vix 18.5 --iv 19.2 --rate 4.5 --notional 1000000
  python RJ.py --spot 741.28 --vix 18.5 --last-roll-date 2026-03-15 --last-roll-spot 710
  python RJ.py  (interactive mode — prompts for all inputs)
        """
    )
    # Manual inputs
    p.add_argument("--spot", type=float, help="XSP spot price (e.g. 741.28)")
    p.add_argument("--vix", type=float, help="VIX level (e.g. 18.5)")
    p.add_argument("--iv", type=float, default=None,
                   help="6M ATM implied vol in %% (e.g. 19.2). Defaults to VIX×1.1.")
    p.add_argument("--rate", type=float, default=4.5,
                   help="Risk-free rate in %% (default: 4.5)")
    p.add_argument("--notional", type=float, default=1_000_000,
                   help="Portfolio notional in USD (default: 1000000)")
    p.add_argument("--date", type=str, default=None,
                   help="Calculation date YYYY-MM-DD (default: today)")
    p.add_argument("--last-roll-date", type=str, default=None,
                   help="Date of last roll YYYY-MM-DD (enables roll alerts)")
    p.add_argument("--last-roll-spot", type=float, default=None,
                   help="XSP spot at last roll (enables rally/drop alerts)")
    # Live data flags
    p.add_argument("--live", action="store_true",
                   help="Fetch XSP spot and VIX live from Yahoo Finance (yfinance)")
    p.add_argument("--ibkr", action="store_true",
                   help="Fetch live option chain + real deltas from IBKR TWS/Gateway (ib_insync)")
    p.add_argument("--ibkr-host", type=str, default="127.0.0.1",
                   help="TWS/Gateway host (default: 127.0.0.1)")
    p.add_argument("--ibkr-port", type=int, default=7497,
                   help="TWS port 7497 (paper 7496) or IB Gateway 4001 (paper 4002). Default: 7497")
    p.add_argument("--ibkr-client-id", type=int, default=10,
                   help="IBKR client ID (default: 10)")
    # Scenario analysis
    p.add_argument("--scenario", type=float, default=None,
                   help="Hypothetical XSP spot price — shows hedge P&L vs. portfolio loss at that level")
    return p.parse_args()


def prompt_float(label: str, default: Optional[float] = None) -> float:
    default_str = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {label}{default_str}: ").strip()
        if raw == "":
            if default is not None:
                return default
            print("    Please enter a number.")
            continue
        try:
            return float(raw)
        except ValueError:
            print("    Please enter a number.")


def prompt_optional_float(label: str) -> Optional[float]:
    """Like prompt_float but pressing Enter returns None."""
    raw = input(f"  {label}: ").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("    Invalid number — skipping.")
        return None


def interactive_mode():
    print("\n  === Vanna-Neutral 3-1-1 Hedge Calculator (Interactive) ===\n")
    spot = prompt_float("XSP Spot Price")
    vix = prompt_float("VIX Level")
    iv_raw = prompt_optional_float("6M Implied Vol % (Enter to use VIX×1.1)")
    iv = (iv_raw / 100.0) if iv_raw else None
    rate = prompt_float("Risk-Free Rate %", 4.5) / 100.0
    notional = prompt_float("Portfolio Notional ($)", 1_000_000)
    date_raw = input(f"  Calculation Date [today={date.today()}]: ").strip()
    calc_date = date.fromisoformat(date_raw) if date_raw else date.today()
    lrd_raw = input("  Last Roll Date YYYY-MM-DD (Enter to skip): ").strip()
    last_roll_date = date.fromisoformat(lrd_raw) if lrd_raw else None
    lrs_raw = input("  Last Roll XSP Spot (Enter to skip): ").strip()
    last_roll_spot = float(lrs_raw) if lrs_raw else None
    return spot, vix, iv, rate, notional, calc_date, last_roll_date, last_roll_spot


def main():
    missing = []
    if not HAS_SCIPY:
        missing.append("scipy")
    if not HAS_TABULATE:
        missing.append("tabulate")
    if missing:
        print(f"\n[WARNING] Missing optional packages: {', '.join(missing)}")
        print(f"  Install with: pip install {' '.join(missing)}")
        print("  Falling back to built-in implementations (slightly less accurate).\n")

    args = parse_args()

    # ── Step 1: resolve spot + VIX ────────────────────────────────────────────
    data_source_parts = []

    if args.live:
        try:
            spot, vix = fetch_live_yfinance()
            data_source_parts.append("yfinance (live)")
        except RuntimeError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
    elif args.spot is not None and args.vix is not None:
        spot = args.spot
        vix = args.vix
        data_source_parts.append("manual")
    else:
        # Interactive mode
        spot, vix, _iv, _rate, _notional, _calc_date, _lrd, _lrs = interactive_mode()
        args.iv = _iv * 100.0 if _iv else None
        args.rate = _rate * 100.0
        args.notional = _notional
        args.date = _calc_date.isoformat()
        args.last_roll_date = _lrd.isoformat() if _lrd else None
        args.last_roll_spot = _lrs
        data_source_parts.append("interactive")

    iv = (args.iv / 100.0) if args.iv else None
    rate = args.rate / 100.0
    notional = args.notional
    calc_date = date.fromisoformat(args.date) if args.date else date.today()
    last_roll_date = date.fromisoformat(args.last_roll_date) if args.last_roll_date else None
    last_roll_spot = args.last_roll_spot

    if iv is None:
        iv = (vix * 1.1) / 100.0
    elif iv > 1.0:
        iv = iv / 100.0

    if rate > 1.0:
        rate = rate / 100.0

    # ── Step 2: optional IBKR chain fetch ─────────────────────────────────────
    ibkr_chain: Optional[Dict] = None

    if args.ibkr:
        if not HAS_IBKR:
            print("[ERROR] ib_insync not installed. Run: pip install ib_insync")
            sys.exit(1)

        zone_tmp, _, put_tenor_tmp, _ = classify_vix(vix)
        put_expiry = nearest_expiry(calc_date, put_tenor_tmp, avoid_october=True)
        call_expiry = nearest_expiry(calc_date, 2.2, avoid_october=False)

        # Collect unique (expiry, right, representative_target_delta) combos
        chain_requests_expiries = []
        chain_requests_rights = []
        chain_requests_deltas = []
        seen = set()
        for leg_def in PUT_LEGS:
            key = (put_expiry, "P")
            if key not in seen:
                seen.add(key)
                chain_requests_expiries.append(put_expiry)
                chain_requests_rights.append("P")
                chain_requests_deltas.append(leg_def["target_delta"])
        for right in ("C",):
            key = (call_expiry, right)
            if key not in seen:
                seen.add(key)
                chain_requests_expiries.append(call_expiry)
                chain_requests_rights.append(right)
                chain_requests_deltas.append(0.04)  # representative call delta

        try:
            ibkr_chain = fetch_ibkr_chain(
                expiry_dates=chain_requests_expiries,
                option_rights=chain_requests_rights,
                target_deltas=chain_requests_deltas,
                host=args.ibkr_host,
                port=args.ibkr_port,
                client_id=args.ibkr_client_id,
            )
            data_source_parts.append(f"IBKR TWS {args.ibkr_host}:{args.ibkr_port}")
        except RuntimeError as e:
            print(f"[WARN] IBKR fetch failed ({e}). Falling back to BS estimates.")

    data_source = " + ".join(data_source_parts) if data_source_parts else "manual"

    # ── Step 3: build positions ────────────────────────────────────────────────
    put_legs, zone, mult = build_put_spine(spot, vix, iv, rate, notional, calc_date,
                                           ibkr_chain=ibkr_chain)
    call_legs = build_call_engine(spot, vix, iv, rate, notional, calc_date,
                                  ibkr_chain=ibkr_chain)

    alerts = check_roll_alerts(calc_date, spot, vix, last_roll_date, last_roll_spot)

    print_position_sheet(
        spot, vix, iv, rate, notional, calc_date,
        put_legs, call_legs, zone, mult,
        last_roll_date, last_roll_spot, alerts,
        data_source=data_source,
    )

    # ── Step 4: optional scenario analysis ────────────────────────────────────
    if args.scenario is not None:
        leg_results, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=spot,
            spot_scenario=args.scenario,
            iv=iv,
            rate=rate,
            calc_date=calc_date,
            put_legs=put_legs,
            call_legs=call_legs,
            notional=notional,
        )
        print_scenario_sheet(
            spot_orig=spot,
            spot_scenario=args.scenario,
            leg_results=leg_results,
            hedge_pnl=hedge_pnl,
            portfolio_pnl=portfolio_pnl,
            net_pnl=net_pnl,
            notional=notional,
        )


if __name__ == "__main__":
    main()