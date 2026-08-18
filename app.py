"""서비스 본체.

구성 요소는 두 개다.

1) 스케줄러: 평일 17:00 KST에 사용자별 추적 종목의 120일선 신호를 계산해 발송.
   기준일이 오래된 시세(`is_stale_market_date`)와 이미 보낸 신호
   (`is_duplicate_signal`)는 건너뛰고, 발송에 성공한 뒤에만 DB에 기록한다.
2) 롱폴링: 텔레그램 토큰 단위로 `getUpdates`를 돌며 명령을 처리. 토큰 목록은
   주기적으로 DB에서 다시 읽어 사용자 추가/삭제를 반영한다.

발신자 검증은 (토큰, 챗ID) 조합이 `user` 테이블에 있는지로만 판단한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import messages
import repository
import signals
import timeutil
from commands import CommandContext, handle_command
from models import User
from prices import PriceFetchError, PriceService
from signals import Signal
from telegram_api import TelegramClient, chat_id_of, message_text

logger = logging.getLogger(__name__)


class MATrackerService:
    def __init__(self, prices: PriceService | None = None) -> None:
        self.prices = prices or PriceService()
        self._clients: dict[str, TelegramClient] = {}
        self._pollers: dict[str, asyncio.Task[None]] = {}

    # --- 텔레그램 클라이언트 ------------------------------------------------

    def client_for(self, token: str) -> TelegramClient:
        client = self._clients.get(token)
        if client is None:
            client = TelegramClient(token)
            self._clients[token] = client
        return client

    async def _notify(self, user: User, text: str) -> bool:
        if not user.can_notify:
            logger.warning("텔레그램 설정이 없어 발송을 건너뜁니다: user=%s", user.user_id)
            return False
        return await self.client_for(user.telegram_token).send(user.telegram_chat_id, text)

    # --- 신호 발송 ----------------------------------------------------------

    async def signal_for_state(self, symbol: str, state) -> Signal:
        closes = await self.prices.daily_closes(symbol)
        return signals.calculate_signal(symbol, closes, state)

    async def broadcast_signals(self) -> None:
        """스케줄러가 호출하는 일일 신호 발송."""
        self.prices.clear()  # 마감 직후이므로 캐시를 버리고 새로 받는다
        market_today = timeutil.market_today()

        users = await repository.run(repository.list_notifiable_users)
        if not users:
            logger.warning("알림 대상 사용자가 없습니다. user 테이블의 telegram 설정을 확인하세요.")
            return

        sent_count = 0
        for user in users:
            states = await repository.run(repository.list_states, user.user_id, enabled_only=True)
            for state in states:
                try:
                    signal = await self.signal_for_state(state.symbol, state)
                except (PriceFetchError, ValueError) as exc:
                    logger.warning("user=%s %s 신호 계산 실패: %s", user.user_id, state.symbol, exc)
                    continue

                if signals.is_stale_market_date(signal.market_date, today=market_today):
                    logger.warning(
                        "user=%s %s 시세가 오래되어 건너뜁니다: %s",
                        user.user_id,
                        state.symbol,
                        signal.market_date,
                    )
                    continue

                if signals.is_duplicate_signal(
                    signal,
                    last_signal_hash=state.last_signal_hash,
                    last_signal_date=state.last_signal_date,
                    last_signal_action=state.last_signal_action,
                ):
                    logger.info(
                        "user=%s %s 중복 신호 건너뜀: %s %s",
                        user.user_id,
                        state.symbol,
                        signal.market_date,
                        signal.action,
                    )
                    continue

                if not await self._notify(user, messages.signal_detail(signal)):
                    logger.warning("user=%s %s 발송 실패로 기록을 남기지 않습니다.", user.user_id, state.symbol)
                    continue

                await repository.run(repository.record_sent_signal, user.user_id, signal)
                sent_count += 1

        logger.info("일일 신호 발송 완료: %s건", sent_count)

    # --- 명령 처리 ----------------------------------------------------------

    async def dispatch_update(self, token: str, update: dict) -> None:
        text = message_text(update).strip()
        if not text:
            return

        chat_id = chat_id_of(update)
        user = await repository.run(repository.find_user_by_chat, token, chat_id)
        if user is None:
            logger.warning("등록되지 않은 chat_id의 명령을 무시합니다: %s", chat_id or "unknown")
            return

        client = self.client_for(token)

        async def reply(message: str) -> bool:
            return await client.send(chat_id, message)

        ctx = CommandContext(
            user_id=user.user_id,
            user_name=user.display_name,
            app_key=user.app_key,
            reply=reply,
        )

        try:
            await handle_command(text, ctx, self.prices)
        except Exception as exc:
            logger.exception("명령 처리 오류: user=%s text=%r", user.user_id, text[:100])
            await reply(messages.failure("명령 처리 중 오류가 발생했습니다.", exc))

    async def _poll_loop(self, token: str) -> None:
        client = self.client_for(token)
        logger.info("롱폴링 시작: token=%s", messages.mask(token, 6))
        while True:
            try:
                updates = await client.poll()
                for update in updates:
                    try:
                        await self.dispatch_update(token, update)
                    except Exception:
                        logger.exception("업데이트 처리 실패: %s", update.get("update_id"))
                    finally:
                        client.ack(update)
            except asyncio.CancelledError:
                logger.info("롱폴링 종료: token=%s", messages.mask(token, 6))
                raise
            except Exception:
                logger.exception("롱폴링 루프 오류")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def sync_pollers(self) -> set[str]:
        """DB의 사용자 목록에 맞춰 토큰별 폴링 태스크를 맞춘다."""
        users = await repository.run(repository.list_notifiable_users)
        tokens = {user.telegram_token for user in users if user.telegram_token}

        for token in tokens - self._pollers.keys():
            self._pollers[token] = asyncio.create_task(
                self._poll_loop(token), name=f"poll-{messages.mask(token, 6)}"
            )

        for token in self._pollers.keys() - tokens:
            task = self._pollers.pop(token)
            task.cancel()

        # 예외로 죽은 태스크는 다음 주기에 다시 만들어지도록 정리한다.
        for token, task in list(self._pollers.items()):
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    logger.error("폴링 태스크가 종료됐습니다: %s", exc)
                self._pollers.pop(token, None)

        return tokens

    # --- 부팅 --------------------------------------------------------------

    async def announce_startup(self) -> None:
        users = await repository.run(repository.list_notifiable_users)
        for user in users:
            await repository.run(repository.ensure_default_states, user.user_id)
            if not config.SEND_STARTUP_NOTICE:
                continue
            symbols = await repository.run(repository.tracked_symbols, user.user_id)
            await self._notify(
                user,
                f"✅ <b>{config.SMA_WINDOW}일선 신호봇 시작</b>\n\n" + messages.start(symbols),
            )

    def build_scheduler(self) -> AsyncIOScheduler:
        scheduler = AsyncIOScheduler(timezone=timeutil.KST)
        if config.ENABLE_SCHEDULED_SIGNAL:
            scheduler.add_job(
                self.broadcast_signals,
                "cron",
                day_of_week=config.SIGNAL_CRON_DAYS,
                hour=config.SIGNAL_CRON_HOUR,
                minute=config.SIGNAL_CRON_MINUTE,
                timezone=timeutil.KST,
                id="daily-signal",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
        return scheduler

    async def run(self) -> None:
        db.init_db()
        await self.announce_startup()

        scheduler = self.build_scheduler()
        scheduler.start()
        logger.info(
            "서비스 시작: %s일선 / 자동 신호 %s",
            config.SMA_WINDOW,
            "ON" if config.ENABLE_SCHEDULED_SIGNAL else "OFF",
        )

        try:
            while True:
                await self.sync_pollers()
                await asyncio.sleep(config.USER_REFRESH_SECONDS)
        finally:
            for task in self._pollers.values():
                task.cancel()
            await asyncio.gather(*self._pollers.values(), return_exceptions=True)
            self._pollers.clear()
            scheduler.shutdown(wait=False)
            db.dispose()


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or config.LOG_LEVEL), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    await MATrackerService().run()


AsyncMain = Callable[[], Awaitable[None]]
