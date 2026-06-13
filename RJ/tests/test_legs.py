"""Tests for VIX classification, expiry helpers, and position builders."""

import pytest
from datetime import date
from hedge.legs import (
    classify_vix, third_friday, nearest_expiry,
    build_put_spine, build_call_engine,
    PUT_LEGS, XSP_MULTIPLIER,
)

CALC_DATE = date(2026, 6, 13)
SPOT = 741.28
VIX = 18.5
IV = 0.20
RATE = 0.045
NOTIONAL = 1_000_000


class TestClassifyVix:
    def test_zone1(self):
        zone, mult, _, _ = classify_vix(12.0)
        assert zone == 1 and mult == 1.20

    def test_zone2(self):
        zone, mult, _, _ = classify_vix(18.5)
        assert zone == 2 and mult == 1.00

    def test_zone3(self):
        zone, mult, _, _ = classify_vix(25.0)
        assert zone == 3 and mult == 0.80

    def test_zone4(self):
        zone, mult, _, _ = classify_vix(30.0)
        assert zone == 4 and mult == 0.60

    def test_zone5(self):
        zone, mult, _, _ = classify_vix(40.0)
        assert zone == 5 and mult == 0.40

    def test_boundary_15(self):
        # vix=15 is NOT < 15, so zone 2
        zone, _, _, _ = classify_vix(15.0)
        assert zone == 2

    def test_boundary_just_under_15(self):
        zone, _, _, _ = classify_vix(14.99)
        assert zone == 1


class TestThirdFriday:
    def test_known_date(self):
        # 3rd Friday of June 2026
        tf = third_friday(2026, 6)
        assert tf == date(2026, 6, 19)
        assert tf.weekday() == 4  # Friday

    def test_always_friday(self):
        for month in range(1, 13):
            tf = third_friday(2026, month)
            assert tf.weekday() == 4

    def test_always_third_week(self):
        for month in range(1, 13):
            tf = third_friday(2026, month)
            assert 15 <= tf.day <= 21


class TestNearestExpiry:
    def test_returns_future_date(self):
        exp = nearest_expiry(CALC_DATE, 6)
        assert exp > CALC_DATE

    def test_avoids_october_when_requested(self):
        # From June, a 4-month expiry would land around October
        exp = nearest_expiry(CALC_DATE, 4, avoid_october=True)
        assert exp.month != 10

    def test_allows_october_when_not_avoided(self):
        # No constraint — Oct is allowed
        exp = nearest_expiry(CALC_DATE, 4, avoid_october=False)
        # Just check it's a future 3rd Friday
        assert exp > CALC_DATE
        assert exp.weekday() == 4

    def test_approximately_correct_tenor(self):
        exp = nearest_expiry(CALC_DATE, 6)
        days_out = (exp - CALC_DATE).days
        assert 150 <= days_out <= 220  # roughly 5–7 months out


class TestBuildPutSpine:
    def setup_method(self):
        self.legs, self.zone, self.mult = build_put_spine(
            SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE
        )

    def test_returns_five_legs(self):
        assert len(self.legs) == 5

    def test_zone2_at_vix_18(self):
        assert self.zone == 2 and self.mult == 1.0

    def test_actions_correct(self):
        actions = [l.action for l in self.legs]
        assert actions == ["BUY", "BUY", "SELL", "BUY", "SELL"]

    def test_strikes_below_spot(self):
        for leg in self.legs:
            assert leg.computed_strike < SPOT

    def test_strikes_rounded_to_5(self):
        for leg in self.legs:
            assert leg.computed_strike % 5 == 0

    def test_premiums_positive(self):
        for leg in self.legs:
            assert leg.est_premium > 0

    def test_buy_legs_negative_total(self):
        for leg in self.legs:
            if leg.action == "BUY":
                assert leg.est_total < 0

    def test_sell_legs_positive_total(self):
        for leg in self.legs:
            if leg.action == "SELL":
                assert leg.est_total > 0

    def test_deltas_in_range(self):
        for leg in self.legs:
            assert -1.0 < leg.target_delta < 0.0

    def test_quantities_positive(self):
        for leg in self.legs:
            assert leg.quantity >= 1


class TestBuildCallEngine:
    def setup_method(self):
        self.legs = build_call_engine(SPOT, VIX, IV, RATE, NOTIONAL, CALC_DATE)

    def test_returns_six_legs(self):
        # 3 tiers × 2 (short + long) = 6
        assert len(self.legs) == 6

    def test_alternating_sell_buy(self):
        actions = [l.action for l in self.legs]
        assert actions == ["SELL", "BUY", "SELL", "BUY", "SELL", "BUY"]

    def test_strikes_above_spot(self):
        for leg in self.legs:
            assert leg.computed_strike > SPOT

    def test_long_strike_higher_than_short(self):
        # Each tier: short strike (lower OTM delta) < long strike (higher OTM delta)
        for i in range(0, 6, 2):
            short_leg = self.legs[i]
            long_leg = self.legs[i + 1]
            assert long_leg.computed_strike > short_leg.computed_strike

    def test_sell_legs_positive_total(self):
        for leg in self.legs:
            if leg.action == "SELL":
                assert leg.est_total > 0

    def test_buy_legs_negative_total(self):
        for leg in self.legs:
            if leg.action == "BUY":
                assert leg.est_total < 0
