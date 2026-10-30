"""Chat collection deadlines and immutable live-ingress batch boundaries."""
from contextvars import ContextVar
from dataclasses import dataclass,field

# asyncio.to_thread copies this context into the synchronous pipeline. Direct tools/API
# callers leave None and retain their established immediate/backlog behavior.
RELEASED_SOURCES = ContextVar('telegram_released_sources',default=None)
BUSY_CHATS = ContextVar('telegram_collecting_or_ready_chats',default=frozenset())


@dataclass
class PendingChat:
    first: float
    last: float
    sequence: int
    messages: list=field(default_factory=list)


class ChatCollector:
    """Pure scheduling only; source membership is still decided by the model."""
    def __init__(self,quiet_seconds=30,max_wait_seconds=60):
        if not 30<=quiet_seconds<=max_wait_seconds<=60:
            raise ValueError('Expected 30 <= quiet seconds <= maximum wait <= 60')
        self.quiet_seconds=quiet_seconds
        self.max_wait_seconds=max_wait_seconds
        self.pending={}
        self._sequence=0

    def deadline(self,chat):
        return min(chat.last+self.quiet_seconds,chat.first+self.max_wait_seconds)

    def add(self,message,now):
        chat=self.pending.get(message.chat_id)
        if chat is None:
            self._sequence+=1
            chat=PendingChat(now,now,self._sequence)
            self.pending[message.chat_id]=chat
        chat.last=now
        chat.messages.append(message)

    def release_due(self,now):
        due=sorted((self.deadline(chat),chat.sequence,chat_id) for chat_id,chat in self.pending.items()
                   if self.deadline(chat)<=now)
        return [self.pending.pop(chat_id).messages for _,_,chat_id in due]

    def next_timeout(self,now):
        return max(0,min(self.deadline(chat) for chat in self.pending.values())-now) if self.pending else None

    def release_all(self):
        batches=[chat.messages for chat in sorted(self.pending.values(),key=lambda chat:chat.sequence)]
        self.pending.clear()
        return batches
