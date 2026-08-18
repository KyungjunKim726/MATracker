from __future__ import annotations

import pytest

import config
import db
import repository
import signals
from models import User

from conftest import flat_closes


class _Strategy:
    round_unit = 100.0
    shares = 0
    sell_splits = 120


def add_user(**overrides) -> int:
    values = {
        "user_name": "테스터",
        "app_key": "APPKEY0000",
        "app_secret": "APPSECRET0000",
        "cano": "12345678",
        "acnt_prdt_cd": "01",
        "telegram_token": "111:AAA",
        "telegram_chat_id": "9001",
    }
    values.update(overrides)
    with db.session_scope() as session:
        user = User(**values)
        session.add(user)
        session.flush()
        return user.user_id


@pytest.fixture()
def user_id(sqlite_db):
    return add_user()


class TestUsers:
    def test_lists_only_notifiable_users(self, sqlite_db):
        ready = add_user(user_name="알림가능")
        add_user(user_name="토큰없음", telegram_token=None, telegram_chat_id="9002")
        add_user(user_name="챗없음", telegram_chat_id="")

        with db.session_scope() as session:
            assert len(repository.list_users(session)) == 3
            notifiable = repository.list_notifiable_users(session)

        assert [user.user_id for user in notifiable] == [ready]

    def test_finds_user_by_token_and_chat(self, user_id):
        with db.session_scope() as session:
            assert repository.find_user_by_chat(session, "111:AAA", "9001").user_id == user_id
            assert repository.find_user_by_chat(session, "111:AAA", "9999") is None
            assert repository.find_user_by_chat(session, "222:BBB", "9001") is None

    def test_can_notify_flag(self, sqlite_db):
        add_user(user_name="없음", telegram_token="", telegram_chat_id="")
        with db.session_scope() as session:
            user = repository.list_users(session)[0]
            assert user.can_notify is False
            assert user.display_name == "없음"


class TestSymbolState:
    def test_ensure_state_creates_with_defaults(self, user_id):
        with db.session_scope() as session:
            state = repository.ensure_state(session, user_id, "tqqq")

        assert state.symbol == "TQQQ"
        assert state.enabled is True
        assert state.round_unit == config.DEFAULT_ROUND_UNIT
        assert state.total_budget == config.DEFAULT_TOTAL_BUDGET
        assert state.sell_splits == config.DEFAULT_SELL_SPLITS
        assert state.shares == 0

    def test_ensure_state_is_idempotent(self, user_id):
        with db.session_scope() as session:
            first = repository.ensure_state(session, user_id, "TQQQ")
            first.round_unit = 250.0

        with db.session_scope() as session:
            second = repository.ensure_state(session, user_id, "TQQQ")
            assert second.id == first.id
            assert second.round_unit == 250.0
            assert len(repository.list_states(session, user_id)) == 1

    def test_default_states_seeded(self, user_id):
        with db.session_scope() as session:
            repository.ensure_default_states(session, user_id)

        with db.session_scope() as session:
            # 기본 종목 순서가 유지돼야 한다(첫 종목이 명령의 기본값이 된다).
            assert repository.tracked_symbols(session, user_id) == list(config.DEFAULT_TRACKED_SYMBOLS)

    def test_extra_symbols_come_after_defaults(self, user_id):
        with db.session_scope() as session:
            repository.ensure_state(session, user_id, "AAPL")
            repository.ensure_default_states(session, user_id)

        with db.session_scope() as session:
            assert repository.tracked_symbols(session, user_id) == [
                *config.DEFAULT_TRACKED_SYMBOLS,
                "AAPL",
            ]

    def test_seeds_defaults_only_for_brand_new_user(self, user_id):
        with db.session_scope() as session:
            assert repository.tracked_or_seed(session, user_id) == list(config.DEFAULT_TRACKED_SYMBOLS)

        with db.session_scope() as session:
            for symbol in config.DEFAULT_TRACKED_SYMBOLS:
                repository.set_enabled(session, user_id, symbol, False)

        with db.session_scope() as session:
            # 전부 껐다면 사용자의 의도이므로 다시 심지 않는다.
            assert repository.tracked_or_seed(session, user_id) == []

    def test_disabled_symbols_are_excluded_from_tracking(self, user_id):
        with db.session_scope() as session:
            repository.ensure_default_states(session, user_id)
            repository.set_enabled(session, user_id, "QLD", False)

        with db.session_scope() as session:
            assert "QLD" not in repository.tracked_symbols(session, user_id)
            assert len(repository.list_states(session, user_id)) == len(config.DEFAULT_TRACKED_SYMBOLS)
            assert "QLD" not in repository.all_tracked_symbols(session)

    def test_set_holdings_computes_invested_amount(self, user_id):
        with db.session_scope() as session:
            state = repository.set_holdings(
                session, user_id, "TQQQ", avg_price=59.96, shares=12, current_round=2
            )

        assert state.total_invested == pytest.approx(719.52)
        assert state.enabled is True

    def test_set_strategy_floors_sell_splits(self, user_id):
        with db.session_scope() as session:
            state = repository.set_strategy(
                session, user_id, "TQQQ", round_unit=150.0, total_budget=18000.0, sell_splits=0
            )
        assert state.sell_splits == 1

    def test_track_symbol_sets_threshold_and_enables(self, user_id):
        with db.session_scope() as session:
            repository.set_enabled(session, user_id, "SOXL", False)
            state = repository.track_symbol(session, user_id, "SOXL", sell_threshold_pct=25.0)

        assert state.enabled is True
        assert state.sell_threshold_pct == pytest.approx(25.0)

    def test_track_symbol_without_threshold_preserves_it(self, user_id):
        with db.session_scope() as session:
            repository.track_symbol(session, user_id, "SOXL", sell_threshold_pct=25.0)

        with db.session_scope() as session:
            state = repository.track_symbol(session, user_id, "SOXL")

        assert state.sell_threshold_pct == pytest.approx(25.0)

    def test_clear_sell_threshold(self, user_id):
        with db.session_scope() as session:
            repository.track_symbol(session, user_id, "SOXL", sell_threshold_pct=25.0)

        with db.session_scope() as session:
            state = repository.clear_sell_threshold(session, user_id, "SOXL")

        assert state.sell_threshold_pct is None

    def test_new_state_has_no_user_threshold(self, user_id):
        with db.session_scope() as session:
            state = repository.ensure_state(session, user_id, "SOXL")
        assert state.sell_threshold_pct is None

    def test_record_sent_signal(self, user_id):
        signal = signals.calculate_signal("TQQQ", flat_closes(100.0), _Strategy())

        with db.session_scope() as session:
            repository.record_sent_signal(session, user_id, signal)

        with db.session_scope() as session:
            state = repository.get_state(session, user_id, "TQQQ")

        assert state.last_signal_hash == signal.fingerprint
        assert state.last_signal_action == signal.action
        assert state.last_signal_date == signal.market_date
        assert state.last_notified_at is not None

    def test_unique_per_user_and_symbol(self, sqlite_db):
        first = add_user(user_name="A", telegram_chat_id="1")
        second = add_user(user_name="B", telegram_chat_id="2")

        with db.session_scope() as session:
            repository.set_strategy(
                session, first, "TQQQ", round_unit=100.0, total_budget=1000.0, sell_splits=10
            )
            repository.set_strategy(
                session, second, "TQQQ", round_unit=999.0, total_budget=2000.0, sell_splits=20
            )

        with db.session_scope() as session:
            assert repository.get_state(session, first, "TQQQ").round_unit == 100.0
            assert repository.get_state(session, second, "TQQQ").round_unit == 999.0


class TestAsyncRunner:
    async def test_run_wraps_session(self, user_id):
        state = await repository.run(repository.ensure_state, user_id, "SOXL")
        assert state.symbol == "SOXL"

        symbols = await repository.run(repository.tracked_symbols, user_id)
        assert symbols == ["SOXL"]
