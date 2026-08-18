"""텔레그램 명령 파싱과 처리.

파싱은 순수 함수(`parse_command`, `resolve_symbol`)로 분리해 두고, 실제 처리
핸들러는 `CommandContext.reply`를 통해서만 사용자에게 응답한다.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import config
import messages
import repository
import signals
from prices import PriceFetchError, PriceService
from signals import Signal

logger = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[bool]]

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


@dataclass(slots=True)
class CommandContext:
    user_id: int
    user_name: str
    app_key: str | None
    reply: Reply


def parse_command(text: str) -> tuple[str, list[str]]:
    """`/signal@my_bot TQQQ` -> `("signal", ["TQQQ"])`. 명령이 아니면 빈 문자열."""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []

    command = parts[0][1:].split("@", 1)[0].lower()
    return command, parts[1:]


def is_ticker(token: str) -> bool:
    return bool(_TICKER_RE.match(token))


def resolve_symbol(
    args: Sequence[str],
    tracked: Sequence[str],
) -> tuple[str | None, list[str]]:
    """인자 앞머리의 종목명을 떼어낸다.

    반환값은 `(종목, 나머지 인자)`이며, 종목명이 생략되면 추적 목록의 첫 종목을 쓴다.
    티커처럼 보이지만 추적 목록에 없으면 `(None, ...)`을 돌려준다.
    """
    default = tracked[0] if tracked else None
    if not args:
        return default, []

    head = args[0]
    if is_ticker(head):
        symbol = head.upper()
        if symbol not in tracked:
            return None, list(args[1:])
        return symbol, list(args[1:])

    # 숫자로 시작하면 종목 생략으로 본다: `/config 150 18000 120`
    return default, list(args)


async def handle_command(text: str, ctx: CommandContext, prices: PriceService) -> None:
    command, args = parse_command(text)
    if not command:
        return

    handler = _HANDLERS.get(command)
    if handler is None:
        return

    await handler(args, ctx, prices)


# --- 개별 핸들러 -------------------------------------------------------------


async def _tracked(ctx: CommandContext) -> list[str]:
    """추적 종목 목록. 처음 말을 건 사용자에게는 기본 종목을 자동으로 등록해준다."""
    return await repository.run(repository.tracked_or_seed, ctx.user_id)


async def _cmd_start(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    await repository.run(repository.ensure_default_states, ctx.user_id)
    await ctx.reply(messages.start(await _tracked(ctx)))


async def _signal_for(symbol: str, ctx: CommandContext, prices: PriceService) -> Signal:
    state = await repository.run(repository.ensure_state, ctx.user_id, symbol)
    closes = await prices.daily_closes(symbol)
    return signals.calculate_signal(symbol, closes, state)


async def _cmd_signal(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    tracked = await _tracked(ctx)
    symbol, _ = resolve_symbol(args, tracked)
    if not symbol:
        await ctx.reply(messages.unknown_symbol(tracked))
        return

    try:
        signal = await _signal_for(symbol, ctx, prices)
    except (PriceFetchError, ValueError) as exc:
        await ctx.reply(messages.failure(f"{symbol} 시세 조회 실패", exc))
        return
    await ctx.reply(messages.signal_detail(signal))


async def _cmd_signals(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    tracked = await _tracked(ctx)
    if not tracked:
        await ctx.reply(messages.signals_summary([], []))
        return

    results: list[Signal] = []
    errors: list[tuple[str, str]] = []
    for symbol in tracked:
        try:
            results.append(await _signal_for(symbol, ctx, prices))
        except (PriceFetchError, ValueError) as exc:
            logger.warning("%s 요약 계산 실패: %s", symbol, exc)
            errors.append((symbol, str(exc)[:120]))
    await ctx.reply(messages.signals_summary(results, errors))


async def _cmd_config(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    tracked = await _tracked(ctx)
    symbol, rest = resolve_symbol(args, tracked)
    if not symbol:
        await ctx.reply(messages.unknown_symbol(tracked))
        return

    if not rest:
        state = await repository.run(repository.ensure_state, ctx.user_id, symbol)
        await ctx.reply(messages.strategy(state))
        return

    if len(rest) < 3:
        await ctx.reply(messages.usage_config(symbol))
        return

    try:
        round_unit = float(rest[0])
        total_budget = float(rest[1])
        sell_splits = int(float(rest[2]))
    except ValueError:
        await ctx.reply(messages.invalid_number())
        return

    if round_unit <= 0 or total_budget <= 0:
        await ctx.reply("1회 매수금액과 총 예수금은 0보다 커야 합니다.")
        return

    state = await repository.run(
        repository.set_strategy,
        ctx.user_id,
        symbol,
        round_unit=round_unit,
        total_budget=total_budget,
        sell_splits=sell_splits,
    )
    await ctx.reply(messages.strategy_updated(state))


async def _cmd_update(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    tracked = await _tracked(ctx)
    symbol, rest = resolve_symbol(args, tracked)
    if not symbol:
        await ctx.reply(messages.unknown_symbol(tracked))
        return

    if len(rest) < 3:
        await ctx.reply(messages.usage_update(symbol))
        return

    try:
        avg_price = float(rest[0])
        shares = int(float(rest[1]))
        current_round = int(float(rest[2]))
    except ValueError:
        await ctx.reply(messages.invalid_number())
        return

    if avg_price < 0 or shares < 0 or current_round < 1:
        await ctx.reply("평단가·보유주수는 0 이상, 회차는 1 이상이어야 합니다.")
        return

    state = await repository.run(
        repository.set_holdings,
        ctx.user_id,
        symbol,
        avg_price=avg_price,
        shares=shares,
        current_round=current_round,
    )
    await ctx.reply(messages.holdings_updated(state))


async def _cmd_track(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    if not args or not is_ticker(args[0]):
        await ctx.reply("사용법: <code>/track 종목</code>\n예시: <code>/track SOXL</code>")
        return

    symbol = args[0].upper()
    state = await repository.run(repository.set_enabled, ctx.user_id, symbol, True)
    await ctx.reply(messages.tracking_changed(state))


async def _cmd_untrack(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    tracked = await _tracked(ctx)
    symbol, _ = resolve_symbol(args, tracked)
    if not symbol:
        await ctx.reply(messages.unknown_symbol(tracked))
        return

    state = await repository.run(repository.set_enabled, ctx.user_id, symbol, False)
    await ctx.reply(messages.tracking_changed(state))


async def _cmd_debug(args: Sequence[str], ctx: CommandContext, prices: PriceService) -> None:
    states = await repository.run(repository.list_states, ctx.user_id)
    notes = [f"DB: {config.DB_URL.rsplit('@', 1)[-1]}"]
    await ctx.reply(messages.debug(ctx.user_name, ctx.app_key, states, notes))


_HANDLERS: dict[str, Callable[[Sequence[str], CommandContext, PriceService], Awaitable[None]]] = {
    "start": _cmd_start,
    "help": _cmd_start,
    "signal": _cmd_signal,
    "status": _cmd_signal,
    "signals": _cmd_signals,
    "config": _cmd_config,
    "update": _cmd_update,
    "track": _cmd_track,
    "untrack": _cmd_untrack,
    "debug": _cmd_debug,
}

KNOWN_COMMANDS = tuple(sorted(_HANDLERS))
