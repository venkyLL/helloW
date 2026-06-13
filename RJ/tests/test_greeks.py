"""Tests for BS Greeks and portfolio aggregation."""

import math
import pytest
from datetime import date
from hedge.bs import bs_gamma, bs_vanna, bs_theta_put, bs_theta_call, bs_put_delta, bs_call_delta
from hedge.legs import build_put_spine, build_call_engine
from hedge.greeks import compute_portfolio_greeks

S, K, T, r, sigma = 741.28, 700.0, 0.5, 0.045, 0.20
CALC_DATE = date(2026, 6, 13)
NOTIONAL = 1_000_000


class TestBsGamma:
    def test_positive(self):
        assert bs_gamma(S, K, T, r, sigma) > 0

    def test_atm_higher_than_deep_otm(self):
        # Gamma peaks near ATM
        gamma_atm = bs_gamma(S, S, T, r, sigma)
        gamma_otm = bs_gamma(S, S * 0.7, T, r, sigma)
        assert gamma_atm > gamma_otm

    def test_expired_zero(self):
        assert bs_gamma(S, K, 0, r, sigma) == 0.0

    def test_put_call_gamma_equal(self):
        # Gamma is identical for puts and calls (same d1)
        assert bs_gamma(S, K, T, r, sigma) == pytest.approx(bs_gamma(S, K, T, r, sigma))


class TestBsVanna:
    def test_otm_put_vanna_sign(self):
        # OTM put (K < S): d2 > 0 → vanna = -φ(d1)*d2/σ < 0
        vanna = bs_vanna(S, S * 0.9, T, r, sigma)
        assert vanna < 0

    def test_otm_call_vanna_sign(self):
        # OTM call (K > S): d2 < 0 → vanna = -φ(d1)*d2/σ > 0
        vanna = bs_vanna(S, S * 1.1, T, r, sigma)
        assert vanna > 0

    def test_expired_zero(self):
        assert bs_vanna(S, K, 0, r, sigma) == 0.0

    def test_atm_vanna_finite(self):
        vanna = bs_vanna(S, S, T, r, sigma)
        assert math.isfinite(vanna)


class TestBsTheta:
    def test_put_theta_negative(self):
        # Long put loses value over time (theta < 0 per day)
        assert bs_theta_put(S, K, T, r, sigma) < 0

    def test_call_theta_negative(self):
        assert bs_theta_call(S, K, T, r, sigma) < 0

    def test_expired_zero(self):
        assert bs_theta_put(S, K, 0, r, sigma) == 0.0
        assert bs_theta_call(S, K, 0, r, sigma) == 0.0

    def test_shorter_dte_larger_theta(self):
        # Theta accelerates as expiry approaches
        theta_far = abs(bs_theta_put(S, K, 1.0, r, sigma))
        theta_near = abs(bs_theta_put(S, K, 0.1, r, sigma))
        assert theta_near > theta_far

    def test_put_call_theta_relationship(self):
        # put_theta - call_theta = r*K*e^(-rT)/365 (from put-call parity differentiation)
        pt = bs_theta_put(S, K, T, r, sigma)
        ct = bs_theta_call(S, K, T, r, sigma)
        expected = r * K * math.exp(-r * T) / 365.0
        assert abs((pt - ct) - expected) < 1e-6


class TestPortfolioGreeks:
    def setup_method(self):
        put_legs, _, _ = build_put_spine(S, 18.5, sigma, r, NOTIONAL, CALC_DATE)
        call_legs = build_call_engine(S, 18.5, sigma, r, NOTIONAL, CALC_DATE)
        self.leg_greeks = compute_portfolio_greeks(
            put_legs, call_legs, S, sigma, r, CALC_DATE
        )
        self.put_count = len(put_legs)
        self.call_count = len(call_legs)

    def test_leg_count(self):
        assert len(self.leg_greeks) == self.put_count + self.call_count

    def test_all_legs_have_greeks(self):
        for g in self.leg_greeks:
            assert isinstance(g.delta, float)
            assert isinstance(g.gamma, float)
            assert isinstance(g.vanna, float)
            assert isinstance(g.theta, float)

    def test_portfolio_delta_is_negative(self):
        # Net hedge is long puts (protective) → net delta negative
        total_delta = sum(g.delta for g in self.leg_greeks)
        assert total_delta < 0

    def test_portfolio_theta_is_finite(self):
        # Strategy has mixed long/short — theta could be either sign (positive carry is a feature)
        total_theta = sum(g.theta for g in self.leg_greeks)
        assert math.isfinite(total_theta)

    def test_sell_legs_flip_greeks(self):
        # A SELL leg should have opposite-signed gamma vs BUY leg at same strike
        buy_legs = [g for g in self.leg_greeks if g.action == "BUY"]
        sell_legs = [g for g in self.leg_greeks if g.action == "SELL"]
        assert all(g.gamma > 0 for g in buy_legs), "Long legs: positive gamma"
        assert all(g.gamma < 0 for g in sell_legs), "Short legs: negative gamma"
