"""Black-Scholes pricing and delta-inversion engine."""

import math
from .compat import HAS_SCIPY, norm, brentq

STRIKE_ROUND = 5  # round computed strikes to nearest 5 points


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — uses scipy if available, else Abramowitz & Stegun approx."""
    if HAS_SCIPY:
        return float(norm.cdf(x))
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
                                                       + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0 else 1.0 - cdf


def bs_put_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or K <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def bs_call_delta(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or K <= 0:
        return 1.0 if K < S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1)


def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def _bisection(f, a: float, b: float, tol: float = 0.10, max_iter: int = 60) -> float:
    fa, fb = f(a), f(b)
    if fa * fb > 0:
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
    return round(K / STRIKE_ROUND) * STRIKE_ROUND


def delta_to_strike_put(target_delta: float, S: float, T: float, r: float,
                        sigma: float) -> float:
    if not (-1.0 < target_delta < 0.0):
        raise ValueError(f"Put delta must be in (-1, 0), got {target_delta}")
    f = lambda K: bs_put_delta(S, K, T, r, sigma) - target_delta
    K_low, K_high = S * 0.20, S * 1.05
    if f(K_low) * f(K_high) > 0:
        K_low, K_high = S * 0.10, S * 1.10
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
