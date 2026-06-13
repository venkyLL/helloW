"""Tests for the Black-Scholes engine."""

import math
import pytest
from hedge.bs import (
    bs_put_delta, bs_call_delta,
    bs_put_price, bs_call_price,
    delta_to_strike_put, delta_to_strike_call,
    _round_strike, _bisection,
)

S, K, T, r, sigma = 741.28, 700.0, 0.5, 0.045, 0.20


class TestDeltas:
    def test_put_delta_range(self):
        d = bs_put_delta(S, K, T, r, sigma)
        assert -1.0 < d < 0.0

    def test_call_delta_range(self):
        d = bs_call_delta(S, K, T, r, sigma)
        assert 0.0 < d < 1.0

    def test_put_call_delta_relationship(self):
        # call_delta - put_delta = 1 (both share same d1; put = N(d1)-1, call = N(d1))
        c = bs_call_delta(S, K, T, r, sigma)
        p = bs_put_delta(S, K, T, r, sigma)
        assert abs((c - p) - 1.0) < 1e-6

    def test_deep_itm_put(self):
        # Very high strike put → delta close to -1
        assert bs_put_delta(S, S * 2, T, r, sigma) < -0.9

    def test_deep_otm_put(self):
        # Very low strike put → delta close to 0
        assert bs_put_delta(S, S * 0.1, T, r, sigma) > -0.01

    def test_expired_put_itm(self):
        assert bs_put_delta(S, S + 10, 0, r, sigma) == -1.0

    def test_expired_put_otm(self):
        assert bs_put_delta(S, S - 10, 0, r, sigma) == 0.0


class TestPrices:
    def test_put_price_positive(self):
        assert bs_put_price(S, K, T, r, sigma) > 0

    def test_call_price_positive(self):
        assert bs_call_price(S, K, T, r, sigma) > 0

    def test_put_call_parity(self):
        # C - P = S - K*e^(-rT)
        C = bs_call_price(S, K, T, r, sigma)
        P = bs_put_price(S, K, T, r, sigma)
        forward = S - K * math.exp(-r * T)
        assert abs((C - P) - forward) < 0.01

    def test_expired_put_payoff(self):
        assert bs_put_price(S, S + 50, 0, r, sigma) == pytest.approx(50.0)

    def test_expired_put_worthless(self):
        assert bs_put_price(S, S - 50, 0, r, sigma) == 0.0

    def test_expired_call_payoff(self):
        assert bs_call_price(S, S - 50, 0, r, sigma) == pytest.approx(50.0)

    def test_expired_call_worthless(self):
        assert bs_call_price(S, S + 50, 0, r, sigma) == 0.0


class TestDeltaInversion:
    @pytest.mark.parametrize("target", [-0.05, -0.10, -0.22, -0.35, -0.375])
    def test_put_inversion_accuracy(self, target):
        K_sol = delta_to_strike_put(target, S, T, r, sigma)
        recovered = bs_put_delta(S, K_sol, T, r, sigma)
        assert abs(recovered - target) < 0.01, f"target={target}, got delta={recovered:.4f} at K={K_sol}"

    @pytest.mark.parametrize("target", [0.008, 0.015, 0.030, 0.045, 0.050])
    def test_call_inversion_accuracy(self, target):
        K_sol = delta_to_strike_call(target, S, T, r, sigma)
        recovered = bs_call_delta(S, K_sol, T, r, sigma)
        assert abs(recovered - target) < 0.01, f"target={target}, got delta={recovered:.4f} at K={K_sol}"

    def test_put_invalid_delta_raises(self):
        with pytest.raises(ValueError):
            delta_to_strike_put(0.5, S, T, r, sigma)

    def test_call_invalid_delta_raises(self):
        with pytest.raises(ValueError):
            delta_to_strike_call(-0.3, S, T, r, sigma)

    def test_strike_is_rounded(self):
        K_sol = delta_to_strike_put(-0.35, S, T, r, sigma)
        assert K_sol % 5 == 0


class TestHelpers:
    def test_round_strike(self):
        assert _round_strike(742.3) == 740.0
        assert _round_strike(743.0) == 745.0
        assert _round_strike(740.0) == 740.0

    def test_bisection_finds_root(self):
        root = _bisection(lambda x: x - 3.0, 0.0, 10.0)
        assert abs(root - 3.0) < 0.2
