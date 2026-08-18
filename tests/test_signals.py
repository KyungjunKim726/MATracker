from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

import signals
from signals import BUY, HOLD, SELL

from conftest import flat_closes, make_closes


@dataclass
class Strategy:
    round_unit: float = 100.0
    shares: int = 0
    sell_splits: int = 120
    sell_threshold_pct: float | None = None  # None이면 종목별 기본값


def closes_with_last(last: float, *, base: float = 100.0, count: int = 120):
    """마지막 종가만 다른 시계열. 이동평균 대비 위치를 만들기 쉽다."""
    return make_closes([base] * (count - 1) + [last])


class TestMovingAverage:
    def test_uses_last_window_only(self):
        closes = make_closes([1.0] * 50 + [100.0] * 120)
        assert signals.moving_average(closes) == pytest.approx(100.0)

    def test_requires_full_window(self):
        with pytest.raises(ValueError, match="120"):
            signals.moving_average(flat_closes(100.0, count=119))


class TestAction:
    def test_below_moving_average_is_buy(self):
        signal = signals.calculate_signal("TQQQ", closes_with_last(80.0), Strategy(round_unit=250.0))
        assert signal.action == BUY
        assert signal.action_label == "분할매수"
        assert signal.deviation_pct < 0
        assert signal.estimated_buy_shares == 3  # 250 // 80

    def test_equal_to_moving_average_is_hold(self):
        signal = signals.calculate_signal("TQQQ", flat_closes(100.0), Strategy())
        assert signal.action == HOLD
        assert signal.deviation_pct == pytest.approx(0.0)

    def test_far_above_moving_average_is_sell(self):
        signal = signals.calculate_signal("TQQQ", closes_with_last(200.0), Strategy(shares=240))
        assert signal.action == SELL
        assert signal.suggested_sell_shares == 2  # 240 // 120

    def test_threshold_is_per_symbol(self):
        """이격도 약 29.7%: QLD(20%) 기준은 넘고 TQQQ(30%) 기준은 못 넘는다."""
        closes = closes_with_last(130.0)
        assert signals.calculate_signal("QLD", closes, Strategy()).action == SELL
        assert signals.calculate_signal("TQQQ", closes, Strategy()).action == HOLD

    def test_user_threshold_overrides_symbol_default(self):
        """이격도 약 29.7%: 기본 30%면 관망이지만 사용자가 25%로 낮추면 매도."""
        closes = closes_with_last(130.0)
        assert signals.calculate_signal("TQQQ", closes, Strategy()).action == HOLD

        signal = signals.calculate_signal("TQQQ", closes, Strategy(sell_threshold_pct=25.0))
        assert signal.action == SELL
        assert signal.sell_threshold_pct == pytest.approx(25.0)

    def test_user_threshold_can_be_stricter(self):
        signal = signals.calculate_signal("QLD", closes_with_last(130.0), Strategy(sell_threshold_pct=50.0))
        assert signal.action == HOLD  # QLD 기본 20%였다면 매도였다

    def test_unknown_symbol_uses_default_threshold(self):
        signal = signals.calculate_signal("SPXL", closes_with_last(130.0), Strategy())
        assert signal.sell_threshold_pct == pytest.approx(30.0)
        assert signal.action == HOLD

    def test_sell_suggestion_is_at_least_one_share(self):
        signal = signals.calculate_signal("TQQQ", closes_with_last(200.0), Strategy(shares=5))
        assert signal.suggested_sell_shares == 1

    def test_no_holdings_means_no_sell_quantity(self):
        signal = signals.calculate_signal("TQQQ", closes_with_last(200.0), Strategy(shares=0))
        assert signal.suggested_sell_shares == 0

    def test_zero_round_unit_means_no_buy_quantity(self):
        signal = signals.calculate_signal("TQQQ", closes_with_last(80.0), Strategy(round_unit=0.0))
        assert signal.action == BUY
        assert signal.estimated_buy_shares == 0

    def test_metadata_comes_from_latest_row(self):
        closes = make_closes([100.0] * 119 + [90.0], source="nasdaq")
        signal = signals.calculate_signal("TQQQ", closes, Strategy())
        assert signal.market_date == closes[-1].date
        assert signal.price_source == "nasdaq"
        assert signal.window == 120


class TestResolveSellThreshold:
    def test_falls_back_to_symbol_default(self):
        assert signals.resolve_sell_threshold("QLD", Strategy()) == pytest.approx(20.0)
        assert signals.resolve_sell_threshold("QLD", None) == pytest.approx(20.0)

    def test_uses_strategy_value(self):
        assert signals.resolve_sell_threshold("QLD", Strategy(sell_threshold_pct=12.5)) == pytest.approx(12.5)

    def test_object_without_field_is_fine(self):
        class Bare:
            round_unit = 100.0
            shares = 0
            sell_splits = 120

        assert signals.resolve_sell_threshold("TQQQ", Bare()) == pytest.approx(30.0)


class TestFingerprint:
    def test_is_stable(self):
        closes = closes_with_last(80.0)
        first = signals.calculate_signal("TQQQ", closes, Strategy())
        second = signals.calculate_signal("TQQQ", closes, Strategy(shares=999))
        assert first.fingerprint == second.fingerprint  # 보유수량은 신호 자체를 바꾸지 않는다

    def test_changes_with_price(self):
        a = signals.calculate_signal("TQQQ", closes_with_last(80.0), Strategy())
        b = signals.calculate_signal("TQQQ", closes_with_last(81.0), Strategy())
        assert a.fingerprint != b.fingerprint


class TestDuplicateDetection:
    def setup_method(self):
        self.signal = signals.calculate_signal("TQQQ", closes_with_last(80.0), Strategy())

    def test_same_fingerprint_is_duplicate(self):
        assert signals.is_duplicate_signal(self.signal, last_signal_hash=self.signal.fingerprint)

    def test_same_date_and_action_is_duplicate(self):
        assert signals.is_duplicate_signal(
            self.signal,
            last_signal_hash="다른값",
            last_signal_date=self.signal.market_date,
            last_signal_action=self.signal.action,
        )

    def test_same_date_different_action_is_not_duplicate(self):
        assert not signals.is_duplicate_signal(
            self.signal,
            last_signal_date=self.signal.market_date,
            last_signal_action="SELL",
        )

    def test_empty_state_is_not_duplicate(self):
        assert not signals.is_duplicate_signal(self.signal)


class TestStaleMarketDate:
    today = date(2026, 8, 18)

    def test_fresh_date(self):
        assert not signals.is_stale_market_date("2026-08-17", today=self.today)

    def test_boundary_is_not_stale(self):
        assert not signals.is_stale_market_date("2026-08-13", today=self.today)

    def test_older_than_limit_is_stale(self):
        assert signals.is_stale_market_date("2026-08-12", today=self.today)

    @pytest.mark.parametrize("value", ["", "not-a-date", "2026/08/17", None])
    def test_unparsable_is_stale(self, value):
        assert signals.is_stale_market_date(value, today=self.today)
