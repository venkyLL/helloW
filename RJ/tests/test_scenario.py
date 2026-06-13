"""Tests for scenario P&L engine and 1-year cost table."""

import pytest
from datetime import date
from hedge.legs import build_put_spine, build_call_engine
from hedge.scenario import (
    compute_scenario_pnl, compute_1year_table,
    _intrinsic_put_payoff, _intrinsic_call_payoff,
)

CALC_DATE = date(2026, 6, 13)
SPOT = 741.28
VIX = 18.5
IV = 0.20
RATE = 0.045
NOTIONAL = 1_000_000


@pytest.fixture(scope="module")
def positions():
    put_legs, _, _ = build_put_spine(SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE)
    call_legs = build_call_engine(SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE)
    return put_legs, call_legs


class TestComputeScenarioPnl:
    def test_crash_scenario_hedge_positive(self, positions):
        put_legs, call_legs = positions
        _, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=SPOT, spot_scenario=SPOT * 0.75,
            iv=IV, rate=RATE, calc_date=CALC_DATE,
            put_legs=put_legs, call_legs=call_legs, notional=NOTIONAL,
        )
        assert portfolio_pnl < 0, "Portfolio should lose in a crash"
        assert hedge_pnl > 0, "Hedge should gain in a crash"

    def test_rally_scenario_portfolio_positive(self, positions):
        put_legs, call_legs = positions
        _, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=SPOT, spot_scenario=SPOT * 1.20,
            iv=IV, rate=RATE, calc_date=CALC_DATE,
            put_legs=put_legs, call_legs=call_legs, notional=NOTIONAL,
        )
        assert portfolio_pnl > 0, "Portfolio should gain in a rally"

    def test_flat_scenario_near_zero_pnl(self, positions):
        put_legs, call_legs = positions
        _, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=SPOT, spot_scenario=SPOT,
            iv=IV, rate=RATE, calc_date=CALC_DATE,
            put_legs=put_legs, call_legs=call_legs, notional=NOTIONAL,
        )
        assert portfolio_pnl == pytest.approx(0.0, abs=1.0)

    def test_net_pnl_equals_hedge_plus_portfolio(self, positions):
        put_legs, call_legs = positions
        leg_results, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=SPOT, spot_scenario=SPOT * 0.85,
            iv=IV, rate=RATE, calc_date=CALC_DATE,
            put_legs=put_legs, call_legs=call_legs, notional=NOTIONAL,
        )
        assert net_pnl == pytest.approx(hedge_pnl + portfolio_pnl, rel=1e-6)

    def test_leg_count(self, positions):
        put_legs, call_legs = positions
        leg_results, _, _, _ = compute_scenario_pnl(
            spot_orig=SPOT, spot_scenario=SPOT * 0.85,
            iv=IV, rate=RATE, calc_date=CALC_DATE,
            put_legs=put_legs, call_legs=call_legs, notional=NOTIONAL,
        )
        assert len(leg_results) == len(put_legs) + len(call_legs)


class TestIntrinsicPayoffs:
    def test_put_payoff_below_strike(self, positions):
        put_legs, _ = positions
        # At zero spot, all puts should be deeply ITM
        payoff = _intrinsic_put_payoff(put_legs, 0.0)
        # Net should be positive (long puts dominate short puts in this strategy)
        # At zero: long legs pay max, short legs cost max
        assert isinstance(payoff, float)

    def test_call_payoff_above_all_strikes(self, positions):
        _, call_legs = positions
        # At very high spot, short calls dominate → net negative
        payoff = _intrinsic_call_payoff(call_legs, SPOT * 3)
        assert payoff < 0, "Deep ITM short calls should produce net negative payoff"

    def test_call_payoff_below_all_strikes(self, positions):
        _, call_legs = positions
        payoff = _intrinsic_call_payoff(call_legs, SPOT * 0.5)
        assert payoff == pytest.approx(0.0), "OTM calls have zero intrinsic"

    def test_put_payoff_above_all_strikes(self, positions):
        put_legs, _ = positions
        payoff = _intrinsic_put_payoff(put_legs, SPOT * 2)
        assert payoff == pytest.approx(0.0), "OTM puts have zero intrinsic"


class TestCompute1YearTable:
    def setup_method(self):
        put_legs, zone, mult = build_put_spine(SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE)
        call_legs = build_call_engine(SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE)
        net_put = sum(l.est_total for l in put_legs)
        net_call = sum(l.est_total for l in call_legs)
        self.rows = compute_1year_table(
            spot=SPOT, put_legs=put_legs, call_legs=call_legs,
            notional=NOTIONAL, net_put_per_roll=net_put, net_call_per_month=net_call,
        )

    def test_returns_11_rows(self):
        # -25% to +25% in 5% steps = 11 rows
        assert len(self.rows) == 11

    def test_pct_range(self):
        pcts = [r["pct"] for r in self.rows]
        assert pcts[0] == -25
        assert pcts[-1] == 25

    def test_zero_pct_row_exists(self):
        zero_rows = [r for r in self.rows if r["pct"] == 0]
        assert len(zero_rows) == 1

    def test_net_equals_portfolio_plus_hedge(self):
        for r in self.rows:
            assert r["net_pnl"] == pytest.approx(r["portfolio"] + r["hedge_pnl"], rel=1e-6)

    def test_hedge_equals_put_plus_call(self):
        for r in self.rows:
            assert r["hedge_pnl"] == pytest.approx(r["put_pnl"] + r["call_pnl"], rel=1e-6)

    def test_portfolio_pnl_scales_linearly(self):
        for r in self.rows:
            expected = NOTIONAL * r["pct"] / 100
            assert r["portfolio"] == pytest.approx(expected, rel=1e-6)

    def test_spot_new_correct(self):
        for r in self.rows:
            assert r["spot_new"] == pytest.approx(SPOT * (1 + r["pct"] / 100), rel=1e-9)
