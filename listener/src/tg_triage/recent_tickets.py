"""Local ticket context for assistant decide turns."""

import json
from datetime import timedelta

from agentfleet_common.ai_transport import redact as redact_provider

from .issue_format import redact, sanitize
from .store import _aware

WINDOW = timedelta(minutes=10)
MAX_CANDIDATES = 3
MAX_TITLE_CHARS = 200
MAX_EXCERPT_CHARS = 240

# Use the newest member of a completed group; only the authority source carries
# its create reservation or comment receipt. An effect with any future member
# is not historical evidence, even if its authority source is old enough.
_EFFECTS = """
    WITH scope AS (
      SELECT m.* FROM messages m
      WHERE m.chat_id=:chat_id AND m.issue_number IS NOT NULL AND m.issue_number>0
        AND m.issue_url=('https://github.com/' || :repo || '/issues/' || m.issue_number) COLLATE NOCASE
        AND m.message_id<:earliest_id
        AND julianday(m.received_at)<=julianday(:window_end)
        {source_filter}
    ), anchors AS (
      SELECT m.message_id, COALESCE(g.first_message_id, k.first_message_id, m.message_id) AS authority_id,
             'create' AS evidence
      FROM scope m
      LEFT JOIN groups g ON g.group_key=m.group_key AND g.chat_id=m.chat_id
        AND g.issue_number=m.issue_number AND g.decision=m.decision
      LEFT JOIN cases k ON m.group_key IS NULL AND k.case_key=m.case_key AND k.chat_id=m.chat_id
        AND k.issue_number=m.issue_number AND k.decision=m.decision
      WHERE m.decision IN ('issue','needs_human')
      UNION ALL
      SELECT m.message_id, m.message_id, 'append' FROM scope m WHERE m.decision='followup'
      UNION ALL
      SELECT m.message_id, a.message_id, 'append' FROM scope m
      CROSS JOIN rules_sources s ON s.chat_id=m.chat_id AND s.message_id=m.message_id
      CROSS JOIN rules_append_completions a ON a.topic_id=s.topic_id AND a.chat_id=m.chat_id AND a.status='done'
      WHERE m.decision='followup' AND a.message_id!=m.message_id
    ), effects AS (
      SELECT m.message_id AS source_message_id, m.received_at, m.issue_number AS number,
             m.title, p.title AS authority_title, m.text, m.group_key, m.case_key,
             p.message_id AS authority_message_id, anchors.evidence
      FROM scope m
      JOIN anchors ON anchors.message_id=m.message_id
      CROSS JOIN messages p ON p.chat_id=m.chat_id AND p.message_id=anchors.authority_id
        AND p.decision=m.decision AND p.issue_number=m.issue_number AND p.issue_url=m.issue_url COLLATE NOCASE
        AND p.message_id<:earliest_id AND julianday(p.received_at)<=julianday(:window_end)
      LEFT JOIN rules_comments c ON anchors.evidence='append' AND c.chat_id=p.chat_id AND c.message_id=p.message_id
        AND c.repo=:repo COLLATE NOCASE AND c.issue_number=m.issue_number AND c.status='done' AND c.result>0
      LEFT JOIN rules_append_completions receipt ON c.chat_id=receipt.chat_id AND c.message_id=receipt.message_id
      WHERE ((anchors.evidence='create' AND p.github_called_at IS NOT NULL)
             OR (anchors.evidence='append' AND c.message_id IS NOT NULL))
        AND NOT EXISTS (
          SELECT 1 FROM messages sibling INDEXED BY idx_messages_group
          WHERE anchors.evidence='create' AND sibling.group_key=m.group_key
            AND sibling.chat_id=m.chat_id AND sibling.issue_number=m.issue_number AND sibling.decision=m.decision
            AND (sibling.message_id>=:earliest_id OR julianday(sibling.received_at)>julianday(:window_end))
        )
        AND NOT EXISTS (
          SELECT 1 FROM messages sibling INDEXED BY idx_messages_case
          WHERE anchors.evidence='create' AND m.group_key IS NULL AND sibling.case_key=m.case_key
            AND sibling.chat_id=m.chat_id AND sibling.issue_number=m.issue_number AND sibling.decision=m.decision
            AND (sibling.message_id>=:earliest_id OR julianday(sibling.received_at)>julianday(:window_end))
        )
        AND NOT EXISTS (
          SELECT 1 FROM rules_sources s
          JOIN messages sibling ON sibling.chat_id=s.chat_id AND sibling.message_id=s.message_id
          WHERE s.topic_id=CASE anchors.evidence WHEN 'create' THEN p.case_key ELSE receipt.topic_id END
            AND ((sibling.chat_id=m.chat_id AND sibling.message_id>=:earliest_id) OR
                 julianday(sibling.received_at)>julianday(:window_end))
        )
    )
"""


def candidates(store, sources, repo):
    """Use only operations already proven for sources that precede the whole batch."""
    parameters = _bounds(sources, repo)
    parameters['limit'] = MAX_CANDIDATES
    source_filter = 'AND julianday(m.received_at)>=julianday(:window_start)'
    with store._lock:
        rows = store.connection.execute(f"""
            {_EFFECTS.format(source_filter=source_filter)}, ranked AS (
              SELECT *, ROW_NUMBER() OVER (
                PARTITION BY number ORDER BY julianday(received_at) DESC, source_message_id DESC,
                                             authority_message_id DESC
              ) AS rank FROM effects
            )
            SELECT ranked.*, r.context_state FROM ranked
            LEFT JOIN followup_roots r
              ON r.root_key=(:chat_id || ':' || COALESCE(ranked.group_key,ranked.case_key))
              AND r.chat_id=:chat_id AND r.issue_number=ranked.number
            WHERE rank=1
            ORDER BY julianday(received_at) DESC, source_message_id DESC
            LIMIT :limit
            """, parameters).fetchall()
    return [dict(number=row['number'], repo=repo, chat_id=parameters['chat_id'],
                 title=_title(row, repo), source_message_id=row['source_message_id'],
                 authority_message_id=row['authority_message_id'],
                 source_excerpt=_text(row['text'], MAX_EXCERPT_CHARS),
                 received_at=row['received_at'], evidence=row['evidence']) for row in rows]


def _title(row, repo):
    try:
        state = json.loads(row['context_state'] or '{}')
        snapshot = state.get('managed_snapshot') or {}
        title = (state.get('created_title') if snapshot.get('issue_number') == row['number']
                 and str(snapshot.get('repo', '')).casefold() == repo.casefold() else None)
    except (ValueError, TypeError, AttributeError):
        title = None
    return _text(title or row['authority_title'] or row['title'], MAX_TITLE_CHARS)


def _text(value, limit):
    if not isinstance(value, str):
        return None
    cleaned = redact(redact_provider(value))
    return redact(sanitize(cleaned, inline=True))[:limit] or None


def still_proven(store, proof, members, repo):
    """Recheck the specific source and effect before using a saved plan."""
    parameters = _bounds(members, repo)
    if proof.get('chat_id') != parameters['chat_id'] or proof.get('repo') != repo:
        return False
    parameters.update({key: proof[key] for key in ('source_message_id', 'authority_message_id',
                                                   'number', 'received_at', 'evidence')})
    with store._lock:
        row = store.connection.execute(f"""
            {_EFFECTS.format(source_filter='AND m.message_id=:source_message_id')}
            SELECT 1 FROM effects WHERE authority_message_id=:authority_message_id
              AND number=:number AND received_at=:received_at AND evidence=:evidence LIMIT 1
            """, parameters).fetchone()
    return row is not None


def _bounds(sources, repo):
    anchor_time = min(_aware(source.received_at) for source in sources)
    return dict(chat_id=sources[0].chat_id, repo=repo, earliest_id=min(s.message_id for s in sources),
                window_start=(anchor_time - WINDOW).isoformat(), window_end=anchor_time.isoformat())
