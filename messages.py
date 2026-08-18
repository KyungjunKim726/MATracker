"""텔레그램 HTML 메시지 조립.

`parse_mode=HTML`로 보내므로 동적으로 끼워넣는 값(예외 메시지, 시세 소스명 등)은
반드시 `esc()`로 이스케이프한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape as _escape

import config
from models import SymbolState
from signals import BUY, HOLD, SELL, Signal


def esc(value: object) -> str:
    return _escape(str(value), quote=False)


def money(value: float) -> str:
    return f"{value:,.2f}"


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "비어 있음"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def start(symbols: Sequence[str]) -> str:
    symbol_text = esc(", ".join(symbols)) if symbols else "없음"
    return (
        f"🤖 <b>{config.SMA_WINDOW}일선 신호봇</b>\n\n"
        f"추적 종목: <b>{symbol_text}</b>\n"
        f"자동 알림: 평일 {config.SIGNAL_CRON_HOUR:02d}:{config.SIGNAL_CRON_MINUTE:02d} KST "
        f"(직전 미국장 종가 기준)\n\n"
        "<b>명령어</b>\n"
        "/signal [종목] - 단일 종목 신호\n"
        "/signals - 추적 종목 전체 요약\n"
        "/config [종목] [1회매수금액] [총예수금] [매도분할수]\n"
        "/update [종목] [평단가] [보유주수] [회차]\n"
        "/track [종목] / /untrack [종목]\n"
        "/debug - 설정 진단"
    )


def signal_detail(signal: Signal) -> str:
    direction = "위" if signal.is_above_sma else "아래"
    header = (
        f"📈 <b>{esc(signal.symbol)} {signal.window}일선 신호</b>\n"
        f"기준 일자: {esc(signal.market_date)} (미국장 종가)\n"
        f"시세 소스: <b>{esc(signal.price_source or 'unknown')}</b>\n"
        f"종가: <b>${signal.close:.2f}</b>\n"
        f"{signal.window}일선: <b>${signal.sma:.2f}</b>\n"
        f"이격도: <b>{signal.deviation_pct:+.2f}%</b> ({direction})\n"
        f"판정: <b>{signal.action_label}</b>"
    )

    if signal.action == BUY:
        return (
            f"{header}\n\n"
            f"원칙: 종가가 {signal.window}일선 아래이면 1회치 분할매수\n"
            f"권장 1회 매수금액: <b>${money(signal.round_unit)}</b>\n"
            f"예상 매수수량: <b>{signal.estimated_buy_shares}주</b>\n"
            f"주문 기준: {signal.window}일선 근처 LOC 종가 매수"
        )

    if signal.action == SELL:
        if signal.shares > 0:
            sell_note = (
                f"권장 1회 매도수량: <b>{signal.suggested_sell_shares}주</b>\n"
                f"기준: 보유수량 {signal.shares}주를 {signal.sell_splits}등분"
            )
        else:
            sell_note = "보유수량이 0주로 등록되어 있어 수량 제안은 생략합니다. (/update 로 갱신)"
        return (
            f"{header}\n\n"
            f"원칙: 이격도가 {signal.sell_threshold_pct:.0f}% 이상이면 1회치 분할매도\n"
            f"{sell_note}\n"
            f"주문 기준: 미국장 마감 기준 분할매도"
        )

    return (
        f"{header}\n\n"
        f"매수 조건: 종가가 {signal.window}일선 아래\n"
        f"매도 조건: 이격도 {signal.sell_threshold_pct:.0f}% 이상\n"
        "현재는 둘 다 아니므로 대기 구간입니다."
    )


def signals_summary(signals: Sequence[Signal], errors: Sequence[tuple[str, str]] = ()) -> str:
    lines = [f"📬 <b>{config.SMA_WINDOW}일선 신호 요약</b>"]
    if not signals and not errors:
        lines.append("추적 중인 종목이 없습니다. /track 종목 으로 추가해주세요.")
        return "\n".join(lines)

    for signal in signals:
        lines.append(
            f"{esc(signal.symbol)}: <b>{signal.action_label}</b> / "
            f"종가 ${signal.close:.2f} / {signal.window}일선 ${signal.sma:.2f} / "
            f"이격도 {signal.deviation_pct:+.2f}% / 소스 {esc(signal.price_source or 'unknown')}"
        )
    for symbol, reason in errors:
        lines.append(f"{esc(symbol)}: 조회 실패 ({esc(reason)})")
    return "\n".join(lines)


def strategy(state: SymbolState) -> str:
    return (
        f"⚙️ <b>{esc(state.symbol)} 전략 설정</b>\n"
        f"1회 매수금액: <b>${money(state.round_unit)}</b>\n"
        f"총 예수금 기준: <b>${money(state.total_budget)}</b>\n"
        f"매도 분할 수: <b>{state.sell_splits}</b>\n"
        f"보유수량: <b>{state.shares}</b>주 / 평단 <b>${money(state.avg_price)}</b>\n"
        f"현재 회차: <b>{state.current_round}</b>회차\n"
        f"추적 여부: <b>{'ON' if state.enabled else 'OFF'}</b>"
    )


def strategy_updated(state: SymbolState) -> str:
    return (
        f"✅ <b>{esc(state.symbol)} 전략 설정 업데이트</b>\n"
        f"1회 매수금액: <b>${money(state.round_unit)}</b>\n"
        f"총 예수금 기준: <b>${money(state.total_budget)}</b>\n"
        f"매도 분할 수: <b>{state.sell_splits}</b>"
    )


def holdings_updated(state: SymbolState) -> str:
    return (
        f"✅ <b>{esc(state.symbol)} 보유정보 업데이트 완료</b>\n"
        f"평단가: <b>${money(state.avg_price)}</b>\n"
        f"보유 주수: {state.shares}주\n"
        f"현재 회차: {state.current_round}회차\n"
        f"투자원금: <b>${money(state.total_invested)}</b>"
    )


def tracking_changed(state: SymbolState) -> str:
    if state.enabled:
        return f"✅ <b>{esc(state.symbol)}</b> 추적을 시작합니다."
    return f"⏸️ <b>{esc(state.symbol)}</b> 추적을 중단했습니다. 설정값은 유지됩니다."


def usage_update(symbol: str) -> str:
    return (
        "사용법: <code>/update 종목 평단가 보유주수 현재회차</code>\n"
        f"예시: <code>/update {esc(symbol)} 57.50 3 3</code>"
    )


def usage_config(symbol: str) -> str:
    return (
        "사용법: <code>/config 종목 1회매수금액 총예수금 매도분할수</code>\n"
        f"예시: <code>/config {esc(symbol)} 150 18000 120</code>"
    )


def unknown_symbol(symbols: Sequence[str]) -> str:
    available = esc(", ".join(symbols)) if symbols else "등록된 종목 없음"
    return (
        "지원하지 않는 종목입니다.\n"
        f"현재 추적 종목: <code>{available}</code>\n"
        "새 종목은 <code>/track 종목</code> 으로 추가할 수 있습니다."
    )


def invalid_number() -> str:
    return "숫자를 올바르게 입력해주세요."


def failure(title: str, exc: BaseException | str) -> str:
    return f"{title}\n<code>{esc(exc)}</code>\n\n잠시 후 다시 시도하거나 /debug 로 상태를 확인해주세요."


def debug(user_name: str, app_key: str | None, states: Sequence[SymbolState], notes: Sequence[str] = ()) -> str:
    lines = [
        "🧪 <b>봇 진단</b>",
        f"사용자: <b>{esc(user_name)}</b>",
        f"KIS app_key: {esc(mask(app_key))}",
        f"이동평균 기간: <b>{config.SMA_WINDOW}일</b>",
        f"자동 신호: 평일 {config.SIGNAL_CRON_HOUR:02d}:{config.SIGNAL_CRON_MINUTE:02d} KST",
        f"프록시 설정: {'있음' if config.PROXY_URL else '없음'}",
    ]
    if not states:
        lines.append("등록된 종목이 없습니다. /track 종목 으로 추가해주세요.")
    for state in states:
        lines.append(
            f"{esc(state.symbol)} [{'ON' if state.enabled else 'OFF'}] "
            f"매도기준 {config.sell_threshold_pct(state.symbol):.0f}% / "
            f"last={esc(state.last_signal_date or '-')} "
            f"{esc(state.last_signal_action or '-')} "
            f"소스={esc(state.last_price_source or '-')}"
        )
    lines.extend(esc(note) for note in notes)
    return "\n".join(lines)


ACTION_EMOJI = {BUY: "🟢", SELL: "🔴", HOLD: "⚪"}
