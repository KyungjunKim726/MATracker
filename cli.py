"""커맨드라인 진입점.

    python main.py run                 # 봇 서비스 실행 (기본)
    python main.py initdb              # symbol_state 테이블 생성
    python main.py users               # DB의 사용자 목록 확인
    python main.py signal TQQQ QLD     # 텔레그램 없이 신호만 계산해 출력
    python main.py broadcast           # 일일 신호 발송을 즉시 1회 실행
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import config
import db
import repository
import signals
import timeutil
from app import MATrackerService, setup_logging
from prices import PriceFetchError, PriceService


@dataclass(slots=True)
class _PlainStrategy:
    """DB 없이 신호만 볼 때 쓰는 기본 전략값."""

    round_unit: float = config.DEFAULT_ROUND_UNIT
    shares: int = 0
    sell_splits: int = config.DEFAULT_SELL_SPLITS


async def _cmd_run() -> int:
    await MATrackerService().run()
    return 0


async def _cmd_initdb() -> int:
    db.init_db()
    print(f"OK: {config.DB_URL.rsplit('@', 1)[-1]} 에 symbol_state 준비 완료")
    return 0


async def _cmd_users() -> int:
    users = await repository.run(repository.list_users)
    if not users:
        print("user 테이블이 비어 있습니다.")
        return 1

    for user in users:
        states = await repository.run(repository.list_states, user.user_id)
        tracked = ", ".join(f"{s.symbol}{'' if s.enabled else '(off)'}" for s in states) or "-"
        notify = "OK" if user.can_notify else "텔레그램 설정 없음"
        print(f"[{user.user_id}] {user.display_name} / 알림 {notify} / 종목 {tracked}")
    return 0


async def _cmd_signal(symbols: list[str], user_id: int | None) -> int:
    prices = PriceService()
    targets = [symbol.upper() for symbol in symbols] or list(config.DEFAULT_TRACKED_SYMBOLS)
    exit_code = 0

    for symbol in targets:
        strategy: object = _PlainStrategy()
        if user_id is not None:
            strategy = await repository.run(repository.ensure_state, user_id, symbol)

        try:
            closes = await prices.daily_closes(symbol)
            signal = signals.calculate_signal(symbol, closes, strategy)  # type: ignore[arg-type]
        except (PriceFetchError, ValueError) as exc:
            print(f"{symbol}: 실패 - {exc}")
            exit_code = 1
            continue

        print(
            f"{symbol}: {signal.action_label}({signal.action}) "
            f"종가 ${signal.close:.2f} / {signal.window}일선 ${signal.sma:.2f} / "
            f"이격도 {signal.deviation_pct:+.2f}% / 기준일 {signal.market_date} / 소스 {signal.price_source}"
        )
    return exit_code


async def _cmd_broadcast() -> int:
    db.init_db()
    await MATrackerService().broadcast_signals()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description=f"{config.SMA_WINDOW}일 이동평균선 신호 서비스")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="봇 서비스 실행")
    sub.add_parser("initdb", help="symbol_state 테이블 생성")
    sub.add_parser("users", help="사용자 목록 출력")
    sub.add_parser("broadcast", help="일일 신호 발송 1회 실행")

    signal_parser = sub.add_parser("signal", help="신호 계산 결과만 출력")
    signal_parser.add_argument("symbols", nargs="*", help="예: TQQQ QLD")
    signal_parser.add_argument("--user-id", type=int, default=None, help="해당 사용자의 전략값 사용")

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    command = args.command or "run"
    if command == "run":
        return await _cmd_run()
    if command == "initdb":
        return await _cmd_initdb()
    if command == "users":
        return await _cmd_users()
    if command == "broadcast":
        return await _cmd_broadcast()
    if command == "signal":
        return await _cmd_signal(args.symbols, args.user_id)
    raise SystemExit(f"알 수 없는 명령: {command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print(f"\n중단됨 ({timeutil.format_kst()} KST)")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
