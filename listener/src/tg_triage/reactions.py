"""Reaction trên tin Telegram (không import telethon).

Hàm gửi thật (`send(chat_id, message_id, emoji)`, coroutine) được inject từ
`telegram_io.py`. Mọi lỗi chỉ log WARNING kèm tên lớp exception; không retry.
"""

import asyncio
import logging

LOG = logging.getLogger(__name__)

RECEIVED = "❤"  # ❤ — đã nhận lệnh (cả dry-run và live)
ISSUE_CREATED = "\U0001F44C"  # 👌 — đã tạo GitHub issue (chỉ live)
TIMEOUT_SECONDS = 10.0  # chặn Telethon tự ngủ FloodWait/retry làm kẹt worker


def _code(emoji):
    """Log emoji dưới dạng U+XXXX để console/journald không phải mã hoá ký tự."""
    return "+".join(f"U+{ord(ch):04X}" for ch in emoji)


async def react(send, chat_id, message_id, emoji, *, timeout=TIMEOUT_SECONDS) -> bool:
    """Thả một reaction; trả True nếu thành công. Không bao giờ ném lỗi thường."""
    if send is None:
        return False
    try:
        await asyncio.wait_for(send(chat_id, message_id, emoji), timeout)
    except Exception as error:
        # Không log nội dung exception/tin: chỉ định danh tin, emoji và tên lớp lỗi.
        LOG.warning("Reaction chat_id=%s message_id=%s emoji=%s result=error exception=%s",
                    chat_id, message_id, _code(emoji), type(error).__name__)
        return False
    LOG.info("Reaction chat_id=%s message_id=%s emoji=%s result=ok",
             chat_id, message_id, _code(emoji))
    return True
