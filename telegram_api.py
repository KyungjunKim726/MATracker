"""텔레그램 Bot API 최소 클라이언트.

사용자마다 자기 봇 토큰(`user.telegram_token`)을 가질 수 있으므로 토큰 단위로
클라이언트를 만들고 `getUpdates` 오프셋도 토큰별로 관리한다. 같은 토큰으로 두 곳에서
롱폴링하면 텔레그램이 409를 돌려주므로, 호출부에서 토큰을 중복 없이 묶어야 한다.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


class TelegramClient:
    def __init__(self, token: str, *, api_root: str = API_ROOT) -> None:
        if not token:
            raise ValueError("텔레그램 토큰이 비어 있습니다.")
        self._token = token
        self._base = f"{api_root}/bot{token}"
        self._offset = 0

    @property
    def token(self) -> str:
        return self._token

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            trust_env=True,
            follow_redirects=True,
            headers=config.DEFAULT_HTTP_HEADERS,
        )

    async def send(self, chat_id: str, text: str, *, parse_mode: str = "HTML") -> bool:
        if not chat_id:
            logger.error("chat_id가 비어 있어 메시지를 보내지 않습니다.")
            return False

        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            async with self._client(config.HTTP_TIMEOUT) as client:
                resp = await client.post(f"{self._base}/sendMessage", json=payload)
        except Exception as exc:
            logger.warning("텔레그램 전송 오류: %s", exc)
            return False

        if resp.status_code != 200:
            logger.error("텔레그램 전송 실패 chat_id=%s: %s", chat_id, resp.text[:500])
            return False
        return True

    async def poll(self) -> list[dict[str, Any]]:
        """롱폴링으로 새 업데이트를 받아온다. 실패 시 빈 리스트."""
        params = {"timeout": config.TELEGRAM_POLL_TIMEOUT, "offset": self._offset + 1}
        try:
            async with self._client(config.TELEGRAM_POLL_TIMEOUT + 5) as client:
                resp = await client.get(f"{self._base}/getUpdates", params=params)
                payload = resp.json()
        except Exception as exc:
            logger.warning("업데이트 조회 오류: %s", exc)
            return []

        if not payload.get("ok", False):
            logger.warning("업데이트 조회 실패: %s", str(payload)[:300])
            return []

        updates = [item for item in payload.get("result", []) if isinstance(item, dict)]
        for update in updates:
            try:
                self._offset = max(self._offset, int(update["update_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return updates

    def ack(self, update: dict[str, Any]) -> None:
        """처리 실패한 업데이트도 다시 받지 않도록 오프셋을 전진시킨다."""
        try:
            self._offset = max(self._offset, int(update["update_id"]))
        except (KeyError, TypeError, ValueError):
            pass


def message_text(update: dict[str, Any]) -> str:
    message = update.get("message")
    if isinstance(message, dict):
        return str(message.get("text") or "")
    return ""


def chat_id_of(update: dict[str, Any]) -> str:
    message = update.get("message")
    if not isinstance(message, dict):
        return ""

    chat = message.get("chat")
    if isinstance(chat, dict) and chat.get("id") is not None:
        return str(chat["id"])
    if message.get("chat_id") is not None:
        return str(message["chat_id"])
    return ""
