from __future__ import annotations

import pytest

import commands
import db
import repository
from commands import CommandContext, parse_command, resolve_symbol
from prices import PriceService

from conftest import flat_closes, make_closes
from test_repository import add_user


class TestParseCommand:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/signal", ("signal", [])),
            ("/signal TQQQ", ("signal", ["TQQQ"])),
            ("/signal@ma_tracker_bot TQQQ", ("signal", ["TQQQ"])),
            ("/CONFIG TQQQ 150 18000 120", ("config", ["TQQQ", "150", "18000", "120"])),
            ("  /debug  ", ("debug", [])),
            ("안녕하세요", ("", [])),
            ("", ("", [])),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_command(text) == expected


class TestResolveSymbol:
    tracked = ["TQQQ", "QLD"]

    def test_defaults_to_first_tracked_symbol(self):
        assert resolve_symbol([], self.tracked) == ("TQQQ", [])

    def test_explicit_symbol(self):
        assert resolve_symbol(["qld"], self.tracked) == ("QLD", [])

    def test_symbol_with_extra_args(self):
        assert resolve_symbol(["QLD", "150", "18000"], self.tracked) == ("QLD", ["150", "18000"])

    def test_numeric_first_arg_keeps_default_symbol(self):
        assert resolve_symbol(["150", "18000"], self.tracked) == ("TQQQ", ["150", "18000"])

    def test_untracked_symbol_is_rejected(self):
        assert resolve_symbol(["SOXL"], self.tracked) == (None, [])

    def test_no_tracked_symbols(self):
        assert resolve_symbol([], []) == (None, [])
        assert resolve_symbol(["150"], []) == (None, ["150"])


class Recorder:
    """ctx.reply 대역. 사용자에게 보낸 메시지를 모아둔다."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True

    @property
    def last(self) -> str:
        assert self.sent, "메시지가 전송되지 않았습니다."
        return self.sent[-1]


@pytest.fixture()
def ctx(sqlite_db):
    user_id = add_user()
    return CommandContext(user_id=user_id, user_name="테스터", app_key="APPKEY0000", reply=Recorder())


@pytest.fixture()
def prices():
    async def source(symbol: str):
        # TQQQ는 이동평균 아래(매수), QLD는 이동평균과 같음(관망)
        last = 80.0 if symbol == "TQQQ" else 100.0
        return make_closes([100.0] * 119 + [last], source="test")

    return PriceService([source])


async def run(text: str, ctx: CommandContext, prices: PriceService) -> str:
    await commands.handle_command(text, ctx, prices)
    return ctx.reply.last


class TestStart:
    async def test_seeds_default_symbols(self, ctx, prices):
        message = await run("/start", ctx, prices)
        assert "신호봇" in message
        assert "TQQQ" in message

        symbols = await repository.run(repository.tracked_symbols, ctx.user_id)
        assert "TQQQ" in symbols


class TestSignal:
    async def test_reports_buy_for_price_below_average(self, ctx, prices):
        await run("/start", ctx, prices)
        message = await run("/signal TQQQ", ctx, prices)
        assert "분할매수" in message
        assert "120일선" in message

    async def test_hold_for_price_at_average(self, ctx, prices):
        await run("/start", ctx, prices)
        assert "관망" in await run("/signal QLD", ctx, prices)

    async def test_untracked_symbol_is_rejected(self, ctx, prices):
        await run("/start", ctx, prices)
        await repository.run(repository.set_enabled, ctx.user_id, "QLD", False)
        assert "지원하지 않는 종목" in await run("/signal QLD", ctx, prices)

    async def test_price_failure_is_reported(self, ctx):
        async def broken(symbol: str):
            raise RuntimeError("소스 차단")

        await run("/start", ctx, PriceService([broken]))
        message = await run("/signal TQQQ", ctx, PriceService([broken]))
        assert "시세 조회 실패" in message

    async def test_summary_lists_every_tracked_symbol(self, ctx, prices):
        await run("/start", ctx, prices)
        message = await run("/signals", ctx, prices)
        for symbol in ("TQQQ", "QLD"):
            assert symbol in message


class TestConfig:
    async def test_shows_current_strategy(self, ctx, prices):
        message = await run("/config TQQQ", ctx, prices)
        assert "전략 설정" in message

    async def test_updates_strategy(self, ctx, prices):
        message = await run("/config TQQQ 150 18000 60", ctx, prices)
        assert "업데이트" in message

        state = await repository.run(repository.get_state, ctx.user_id, "TQQQ")
        assert (state.round_unit, state.total_budget, state.sell_splits) == (150.0, 18000.0, 60)

    async def test_symbol_can_be_omitted(self, ctx, prices):
        await run("/start", ctx, prices)
        await run("/config 200 20000 120", ctx, prices)
        state = await repository.run(repository.get_state, ctx.user_id, "TQQQ")
        assert state.round_unit == 200.0

    async def test_rejects_non_numeric(self, ctx, prices):
        assert "숫자" in await run("/config TQQQ 백오십 18000 60", ctx, prices)

    async def test_rejects_missing_arguments(self, ctx, prices):
        assert "사용법" in await run("/config TQQQ 150", ctx, prices)

    async def test_rejects_non_positive_amount(self, ctx, prices):
        assert "0보다" in await run("/config TQQQ 0 18000 60", ctx, prices)


class TestUpdate:
    async def test_updates_holdings(self, ctx, prices):
        message = await run("/update TQQQ 59.96 12 2", ctx, prices)
        assert "보유정보 업데이트" in message

        state = await repository.run(repository.get_state, ctx.user_id, "TQQQ")
        assert (state.avg_price, state.shares, state.current_round) == (59.96, 12, 2)
        assert state.total_invested == pytest.approx(719.52)

    async def test_rejects_bad_values(self, ctx, prices):
        assert "이상" in await run("/update TQQQ 59.96 -1 2", ctx, prices)

    async def test_rejects_missing_arguments(self, ctx, prices):
        assert "사용법" in await run("/update TQQQ 59.96", ctx, prices)


class TestTracking:
    async def test_track_adds_new_symbol(self, ctx, prices):
        message = await run("/track SOXL", ctx, prices)
        assert "추적을 시작" in message
        assert "SOXL" in await repository.run(repository.tracked_symbols, ctx.user_id)

    async def test_track_stores_sell_threshold(self, ctx, prices):
        message = await run("/track SOXL 25", ctx, prices)
        assert "25.0%" in message
        assert "사용자 지정" in message

        state = await repository.run(repository.get_state, ctx.user_id, "SOXL")
        assert state.sell_threshold_pct == pytest.approx(25.0)
        assert state.enabled is True

    async def test_threshold_defaults_to_config_when_omitted(self, ctx, prices):
        message = await run("/track SOXL", ctx, prices)
        assert "30.0%" in message  # config 의 SOXL 기본값
        assert "기본값" in message

        state = await repository.run(repository.get_state, ctx.user_id, "SOXL")
        assert state.sell_threshold_pct is None

    async def test_track_without_threshold_keeps_existing_value(self, ctx, prices):
        await run("/track SOXL 25", ctx, prices)
        await run("/untrack SOXL", ctx, prices)
        message = await run("/track SOXL", ctx, prices)

        assert "25.0%" in message
        state = await repository.run(repository.get_state, ctx.user_id, "SOXL")
        assert state.sell_threshold_pct == pytest.approx(25.0)

    async def test_threshold_can_be_reset_to_default(self, ctx, prices):
        await run("/track SOXL 25", ctx, prices)
        message = await run("/track SOXL 기본", ctx, prices)

        assert "30.0%" in message
        assert "기본값" in message
        state = await repository.run(repository.get_state, ctx.user_id, "SOXL")
        assert state.sell_threshold_pct is None

    async def test_threshold_affects_signal(self, ctx, prices):
        """이격도 약 29.7%인 시세에서 기준을 25%로 낮추면 매도 판정이 된다."""

        async def source(symbol: str):
            return make_closes([100.0] * 119 + [130.0], source="test")

        service = PriceService([source])
        await run("/track TQQQ", ctx, service)
        assert "관망" in await run("/signal TQQQ", ctx, service)

        await run("/track TQQQ 25", ctx, service)
        assert "분할매도" in await run("/signal TQQQ", ctx, service)

    @pytest.mark.parametrize("value", ["0", "-5", "301"])
    async def test_rejects_threshold_out_of_range(self, ctx, prices, value):
        assert "이하로 입력" in await run(f"/track SOXL {value}", ctx, prices)

    async def test_rejects_non_numeric_threshold(self, ctx, prices):
        assert "숫자" in await run("/track SOXL 이십오", ctx, prices)

    async def test_config_shows_threshold_origin(self, ctx, prices):
        await run("/track SOXL 25", ctx, prices)
        message = await run("/config SOXL", ctx, prices)
        assert "매도 기준 이격도" in message
        assert "25.0%" in message

    async def test_untrack_disables_symbol(self, ctx, prices):
        await run("/start", ctx, prices)
        message = await run("/untrack QLD", ctx, prices)
        assert "추적을 중단" in message
        assert "QLD" not in await repository.run(repository.tracked_symbols, ctx.user_id)

    async def test_untrack_keeps_settings(self, ctx, prices):
        await run("/config QLD 300 30000 30", ctx, prices)
        await run("/untrack QLD", ctx, prices)
        state = await repository.run(repository.get_state, ctx.user_id, "QLD")
        assert state.enabled is False
        assert state.round_unit == 300.0

    async def test_track_requires_symbol(self, ctx, prices):
        assert "사용법" in await run("/track", ctx, prices)


class TestMisc:
    async def test_debug_masks_app_key(self, ctx, prices):
        message = await run("/debug", ctx, prices)
        assert "APPK...0000" in message
        assert "APPKEY0000" not in message

    async def test_unknown_command_is_ignored(self, ctx, prices):
        await commands.handle_command("/없는명령", ctx, prices)
        assert ctx.reply.sent == []

    async def test_plain_text_is_ignored(self, ctx, prices):
        await commands.handle_command("오늘 시장 어때?", ctx, prices)
        assert ctx.reply.sent == []
