"""Chat-scoped, read-only assistant data access. Returned text is untrusted data."""
from __future__ import annotations

import base64
import mimetypes
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from .assistant_errors import ToolDenied, ToolUnavailable

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_FILTER_SECTIONS = {
    "F1": "prefilter", "F2": "team_request", "F3": "link_only",
    "F4": "chat_only", "F5": "banter_only", "F6": "reply_chat",
}


def _cut_bytes(text: str) -> str:
    return text.encode("utf-8")[:4096].decode("utf-8", errors="ignore")


class LocalReader:
    """Read one chat; foreign/missing message ids (including around) are absent.

    Message results are chronological; default selects the latest messages and
    around selects the closest message ids to an existing same-chat anchor.
    The host supplies the trusted batch image mapping; DB attachment paths and
    model-supplied paths are never used. The mapping is copied at construction.
    """

    def __init__(self, db_path: Path, chat_id: int, *,
                 batch_message_ids: Iterable[int], images: Mapping[str, Path],
                 pack_text: str | None, core_pack=None):
        self._chat_id = chat_id
        self._batch_message_ids = frozenset(batch_message_ids)
        self._images = dict(images)
        self._pack_text = pack_text
        self._core_pack = core_pack
        self._connection = None
        try:
            self._connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA query_only=ON")
        except (OSError, ValueError, sqlite3.Error):
            self.close()
            raise ToolUnavailable("Local database unavailable") from None

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _query(self, sql, parameters=()):
        if self._connection is None:
            raise ToolUnavailable("Local database unavailable")
        try:
            return [dict(row) for row in self._connection.execute(sql, parameters)]
        except sqlite3.Error:
            raise ToolUnavailable("Local database unavailable") from None

    def get_topic(self, topic_id: str) -> dict:
        if not isinstance(topic_id, str):
            raise ToolDenied("Invalid topic id")
        rows = self._query("SELECT * FROM rules_topics WHERE topic_id=? AND source_chat_id=?",
                           (topic_id, self._chat_id))
        if not rows:
            raise ToolDenied("Topic outside this chat or unavailable")
        topic = rows[0]
        topic["sources"] = self._query(
            "SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=? AND chat_id=? ORDER BY ref",
            (topic_id, self._chat_id))
        return topic

    def list_open_topics(self) -> list[dict]:
        return self._query("""SELECT * FROM rules_topics WHERE source_chat_id=? AND status='OPEN'
            ORDER BY last_at DESC,topic_id DESC LIMIT 20""", (self._chat_id,))

    def get_messages(self, ids: Sequence[int] | None = None,
                     around: int | None = None, limit: int = 20) -> list[dict]:
        if type(limit) is not int or (around is not None and type(around) is not int):
            raise ToolDenied("Invalid message limit or anchor")
        if ids is not None and around is not None:
            raise ToolDenied("Choose message ids or an anchor")
        limit = max(0, min(limit, 40))
        select = """SELECT message_id,sender_name AS sender,received_at AS date,
            substr(text,1,2000) AS text,reply_to_id,has_image FROM messages WHERE chat_id=?"""
        if ids is not None:
            if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
                raise ToolDenied("Invalid message ids")
            if any(type(mid) is not int or not -(2**63) <= mid < 2**63 for mid in ids):
                raise ToolDenied("Invalid message ids")
            unique = list(dict.fromkeys(ids))
            rows = []
            # Stay below SQLite's variable limit even for a large explicit list.
            for start in range(0, len(unique), 400):
                chunk = unique[start:start + 400]
                rows.extend(self._query(select + " AND message_id IN (" + ",".join("?" for _ in chunk)
                                        + ") ORDER BY message_id DESC LIMIT ?",
                                        (self._chat_id, *chunk, limit)))
            rows = sorted(rows, key=lambda row: row["message_id"], reverse=True)[:limit]
        elif around is not None:
            if not -(2**63) <= around < 2**63:
                raise ToolDenied("Invalid message anchor")
            if not self._query("SELECT 1 FROM messages WHERE chat_id=? AND message_id=?",
                               (self._chat_id, around)):
                return []
            rows = self._query(select + " ORDER BY abs(CAST(message_id AS REAL)-?),message_id LIMIT ?",
                               (self._chat_id, around, limit))
        else:
            rows = self._query(select + " ORDER BY message_id DESC LIMIT ?", (self._chat_id, limit))
        for row in rows:
            row["has_image"] = bool(row["has_image"])
        return sorted(rows, key=lambda row: row["message_id"])

    def get_image(self, image_id: str) -> dict:
        if not isinstance(image_id, str) or image_id not in self._images:
            raise ToolDenied("Image outside this batch")
        path = self._images[image_id]
        try:
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise ToolDenied("Image exceeds size limit")
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise ToolUnavailable("Batch image unavailable") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolDenied("Image exceeds size limit")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return {"image_id": image_id, "mime": mime,
                "bytes_base64": base64.b64encode(data).decode("ascii")}

    def get_rule(self, rule_id: str) -> dict:
        if self._core_pack is not None:
            return self._core_pack.get_rule(rule_id)
        if self._pack_text is None:
            raise ToolUnavailable("Assistant pack unavailable")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[A-Z]+[1-9][0-9]*", rule_id):
            raise ToolDenied("Unknown rule id")
        # Rule references in prose/examples must not win over a defining row.
        for line in self._pack_text.splitlines():
            if re.match(r"^\|\s*`?" + re.escape(rule_id) + r"`?\s*\|", line):
                return {"rule_id": rule_id, "text": _cut_bytes(line)}
        # F ids name filters whose defining rows use P/T/W/C/B/K ids.
        section_name = _FILTER_SECTIONS.get(rule_id)
        if section_name:
            sections = re.split(r"(?=^#{1,6} )|(?=^<!-- (?:end-)?source:)", self._pack_text, flags=re.M)
            for section in sections:
                heading = section.split("\n", 1)[0]
                if re.fullmatch(r"## [^\n:]+:\s*" + section_name + r"\s*", heading):
                    if "`" + rule_id + "`" in section:
                        return {"rule_id": rule_id, "text": _cut_bytes(section.strip())}
        raise ToolDenied("Unknown rule id")
