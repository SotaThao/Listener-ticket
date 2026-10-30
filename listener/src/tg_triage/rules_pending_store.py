"""Durable rules-intake attempts and cross-chat question bindings.

This helper never sends or retries external effects. Callers reserve first and
must treat an unfinished reservation as consumed, not as permission to resend.
"""
import json
from datetime import timedelta


class RulesHalt(RuntimeError):
    """An unresolved side effect requires Owner inspection before restart."""


def _recoverable_batch_groups(connection):
    """Classify only checkpoints whose completed work can be proved from ledgers."""
    rows=connection.execute("SELECT batch_id,ordinal,plan,status FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS')").fetchall()
    recover=[]
    for batch_id,ordinal,raw,status in rows:
        if status == 'AMBIGUOUS': raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
        try:
            plan=json.loads(raw); state=plan['state']; keys=plan['source_keys']
            if not keys or not all(isinstance(k,list) and len(k)==2 and all(type(x) is int for x in k) for k in keys): raise ValueError()
            topic=plan.get('pending_topic') or f'{keys[0][0]}:{min(k[1] for k in keys)}'
            if state == 'CHAT':
                current=connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
                if current and current[0]=='CLOSED':
                    complete=all(connection.execute("""SELECT 1 FROM messages m JOIN rules_sources s
                        ON s.chat_id=m.chat_id AND s.message_id=m.message_id
                        WHERE m.chat_id=? AND m.message_id=? AND s.topic_id=? AND m.decision='chat' AND m.reason=?""",(*key,topic,plan['rule'])).fetchone() for key in keys)
                    if not complete: raise ValueError()
                    recover.append((batch_id,ordinal,'DONE')); continue
                if current and current[0]!='OPEN': raise ValueError()
                recover.append((batch_id,ordinal,'PLANNED')); continue
            if state == 'MEDIA_ONLY':
                recover.append((batch_id,ordinal,'PLANNED')); continue
            if state == 'READY':
                root=connection.execute('SELECT chat_id,message_id FROM rules_sources WHERE topic_id=? AND ref=1',(topic,)).fetchone()
                if not root: raise ValueError()
                issue=connection.execute("SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND decision='issue' AND github_called_at IS NOT NULL AND issue_number>0 AND issue_url IS NOT NULL",root).fetchone()
                notice=connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='other' AND status='sent' AND notice_id>0",(topic,)).fetchone()
                if issue and notice:
                    recover.append((batch_id,ordinal,'DONE')); continue
            if state == 'APPEND':
                targets=plan['targets']
                if not isinstance(targets,list) or not targets: raise ValueError()
                target=targets[0]
                number=target['number']
                if type(number) is not int or number<=0: raise ValueError()
                comment=connection.execute("SELECT repo FROM rules_comments WHERE chat_id=? AND message_id=? AND issue_number=? AND status='done' AND result>0",(target['chat_id'],target['reply_message_id'],number)).fetchone()
                if not comment: raise ValueError()
                repo=comment[0]
                for proof in targets:
                    chat=proof['chat_id']; reply=proof['reply_message_id']; parent=proof['reply_to_id']
                    if proof['number']!=number or proof['state']!='open' or [chat,reply] not in keys: raise ValueError()
                    source=connection.execute('SELECT reply_to_id FROM messages WHERE chat_id=? AND message_id=?',(chat,reply)).fetchone()
                    root=connection.execute("SELECT issue_number,issue_url FROM messages WHERE chat_id=? AND message_id=? AND decision IN ('issue','followup')",(chat,parent)).fetchone()
                    if not source or source[0]!=parent or not root or root[0]!=number or root[1]!=f'https://github.com/{repo}/issues/{number}': raise ValueError()
                notice=connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='other' AND status='sent' AND notice_id>0",(topic,)).fetchone()
                if comment and notice:
                    recover.append((batch_id,ordinal,'DONE')); continue
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            pass
        raise RulesHalt('batch checkpoint requires Owner inspection')
    return recover


def check_before_recovery(db_path):
    """Inspect existing side-effect ledgers read-only before Store can recover anything."""
    import sqlite3
    from pathlib import Path
    if str(db_path) == ':memory:': return
    path=Path(db_path)
    if not path.exists(): return
    connection=None
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks=(('rules_comments',"SELECT 1 FROM rules_comments WHERE status IN ('reserved','ambiguous') LIMIT 1"),
                ('rules_notices',"SELECT 1 FROM rules_notices WHERE status IN ('reserved','ambiguous') LIMIT 1"),
                ('rules_batch_groups',"SELECT 1 FROM rules_batch_groups WHERE status='AMBIGUOUS' LIMIT 1"))
        for table,query in checks:
            if table in tables and connection.execute(query).fetchone():
                raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
        if {'rules_sources','attachments'} <= tables:
            columns={row[1] for row in connection.execute('PRAGMA table_info(attachments)')}
            join=("(a.chat_id=s.chat_id AND a.message_id=s.message_id) OR (a.chat_id IS NULL AND a.case_key=s.topic_id AND a.message_id=s.message_id)"
                  if 'chat_id' in columns else "a.case_key=s.topic_id AND a.message_id=s.message_id")
            if connection.execute("SELECT 1 FROM attachments a JOIN rules_sources s ON " + join + " WHERE a.status IN ('uploading','ambiguous') LIMIT 1").fetchone():
                raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
        if 'rules_batch_groups' in tables: _recoverable_batch_groups(connection)
        if {'rules_sources','messages'} <= tables and connection.execute("""SELECT 1 FROM rules_sources s
            JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
            WHERE m.github_called_at IS NOT NULL AND m.decision IN ('pending','error') LIMIT 1""").fetchone():
            raise RulesHalt('unresolved ambiguous operation; Owner inspection required')
    except sqlite3.Error:
        raise RulesHalt('cannot inspect operation ledger safely; Owner inspection required') from None
    finally:
        if connection is not None: connection.close()


class RulesPendingStore:
    def __init__(self, store):
        self.store = store
        with store._lock, store.connection:
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_topics (
                topic_id TEXT PRIMARY KEY, source_chat_id INTEGER NOT NULL,
                last_at TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                attempt_reserved INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'NEW', asked TEXT NOT NULL DEFAULT '{}',
                fallback_asked INTEGER NOT NULL DEFAULT 0)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_sources (
                topic_id TEXT NOT NULL, ref INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, PRIMARY KEY(topic_id,ref),
                UNIQUE(topic_id,chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_ops_replies (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, topic_id TEXT NOT NULL,
                ref INTEGER NOT NULL, PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_inflight_topics (
                topic_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_deferred_ops (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, topic_id TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_notices (
                topic_id TEXT NOT NULL, sequence INTEGER NOT NULL, ops_chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, notice_id INTEGER,
                PRIMARY KEY(topic_id,sequence), UNIQUE(ops_chat_id,notice_id))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
                state TEXT NOT NULL, intent_rule TEXT NOT NULL, decision_rule TEXT NOT NULL,
                fingerprint TEXT NOT NULL, present_sources TEXT NOT NULL, confidence REAL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_batches (
                batch_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, recorded_at TEXT NOT NULL)''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_batch_groups (
                batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(batch_id,ordinal))''')
            store.connection.execute('''CREATE TABLE IF NOT EXISTS rules_comments (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL, status TEXT NOT NULL, result INTEGER,
                PRIMARY KEY(chat_id,message_id))''')
            # An interrupted model call consumed its attempt; no operation may be replayed.
            store.connection.execute('UPDATE rules_topics SET attempt_reserved=0 WHERE attempt_reserved=1')
            store.connection.execute("UPDATE rules_comments SET status='ambiguous' WHERE status='reserved'")
            store.connection.execute("UPDATE rules_notices SET status='ambiguous' WHERE status='reserved'")
            try:
                recoverable=_recoverable_batch_groups(store.connection)
            except RulesHalt:
                recoverable=None
            if recoverable is not None:
                for batch_id,ordinal,status in recoverable:
                    store.connection.execute("UPDATE rules_batch_groups SET status=? WHERE batch_id=? AND ordinal=? AND status='RESERVED'",(status,batch_id,ordinal))
            # Internal claims have no external effect of their own.  Reconcile
            # only after proving that every actual write reservation is resolved.
            if not self.unresolved_ambiguity():
                store.connection.execute("""UPDATE rules_topics SET status='CLOSED'
                    WHERE status='OPEN' AND topic_id IN (
                        SELECT i.topic_id FROM rules_inflight_topics i
                        JOIN rules_sources s ON s.topic_id=i.topic_id AND s.ref=1
                        JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                        WHERE m.decision='issue' AND m.github_called_at IS NOT NULL
                          AND m.issue_number > 0 AND m.issue_url IS NOT NULL)""")
                store.connection.execute('DELETE FROM rules_inflight_topics')

    def unresolved_ambiguity(self):
        with self.store._lock:
            for query in ("SELECT 1 FROM rules_comments WHERE status IN ('reserved','ambiguous') LIMIT 1",
                          "SELECT 1 FROM rules_notices WHERE status IN ('reserved','ambiguous') LIMIT 1",
                          "SELECT 1 FROM rules_batch_groups WHERE status IN ('RESERVED','AMBIGUOUS') LIMIT 1",
                          """SELECT 1 FROM attachments a JOIN rules_sources s
                             ON (a.chat_id=s.chat_id AND a.message_id=s.message_id) OR (a.chat_id IS NULL AND a.case_key=s.topic_id AND a.message_id=s.message_id)
                             WHERE a.status IN ('uploading','ambiguous') LIMIT 1""",
                          """SELECT 1 FROM rules_sources s JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
                             WHERE m.github_called_at IS NOT NULL AND m.decision IN ('pending','error') LIMIT 1"""):
                if self.store.connection.execute(query).fetchone(): return True
            return False

    def reserve_comment(self, chat_id, message_id, repo, issue_number):
        with self.store._lock,self.store.connection:
            return self.store.connection.execute("INSERT OR IGNORE INTO rules_comments VALUES(?,?,?,?,'reserved',NULL)", (chat_id,message_id,repo,issue_number)).rowcount == 1

    def finish_comment(self, chat_id, message_id, comment_id):
        status='done' if type(comment_id) is int and comment_id > 0 else 'ambiguous'
        with self.store._lock,self.store.connection:
            return self.store.connection.execute("UPDATE rules_comments SET status=?,result=? WHERE chat_id=? AND message_id=? AND status='reserved'", (status,comment_id if status=='done' else None,chat_id,message_id)).rowcount == 1

    def comment_status(self, chat_id, message_id):
        with self.store._lock:
            row=self.store.connection.execute('SELECT status FROM rules_comments WHERE chat_id=? AND message_id=?',(chat_id,message_id)).fetchone()
            return row['status'] if row else None

    def create(self, topic_id, chat_id, message_id):
        with self.store._lock, self.store.connection:
            now = self.store.clock().isoformat()
            added = self.store.connection.execute('INSERT OR IGNORE INTO rules_topics(topic_id,source_chat_id,last_at,status) VALUES(?,?,?,?)',
                                                  (topic_id, chat_id, now, 'OPEN')).rowcount
            if added:
                self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic_id, 1, chat_id, message_id))
            return bool(added)

    def add_source(self, topic_id, chat_id, message_id):
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN': return None
            found = self.store.connection.execute('SELECT ref FROM rules_sources WHERE topic_id=? AND chat_id=? AND message_id=?', (topic_id,chat_id,message_id)).fetchone()
            if found: return found['ref']
            ref = self.store.connection.execute('SELECT COALESCE(MAX(ref),0)+1 FROM rules_sources WHERE topic_id=?', (topic_id,)).fetchone()[0]
            self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic_id,ref,chat_id,message_id))
            self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(), topic_id))
            return ref

    def sources(self, topic_id):
        with self.store._lock:
            return {r['ref']: (r['chat_id'],r['message_id']) for r in self.store.connection.execute('SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=?', (topic_id,))}

    def topic_snapshot(self, topic_id):
        with self.store._lock:
            row = self.store.connection.execute("SELECT status,last_at,asked,fallback_asked FROM rules_topics WHERE topic_id=?", (topic_id,)).fetchone()
            if row is None: return None
            sources = [[r['ref'],r['chat_id'],r['message_id']] for r in self.store.connection.execute(
                "SELECT ref,chat_id,message_id FROM rules_sources WHERE topic_id=? ORDER BY ref", (topic_id,))]
            return dict(status=row['status'],last_at=row['last_at'],asked=row['asked'],fallback_asked=row['fallback_asked'],sources=sources)

    def is_bound_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            return self.store.connection.execute('SELECT 1 FROM rules_ops_replies WHERE chat_id=? AND message_id=? UNION ALL SELECT 1 FROM rules_deferred_ops WHERE chat_id=? AND message_id=?', (chat_id,message_id,chat_id,message_id)).fetchone() is not None

    def is_deferred_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            return self.store.connection.execute('SELECT 1 FROM rules_deferred_ops WHERE chat_id=? AND message_id=?', (chat_id,message_id)).fetchone() is not None

    def topic_for_ops_reply(self, chat_id, message_id):
        with self.store._lock:
            row = self.store.connection.execute('''SELECT t.topic_id,t.status,t.asked FROM rules_ops_replies r
                JOIN rules_topics t ON t.topic_id=r.topic_id WHERE r.chat_id=? AND r.message_id=?''', (chat_id,message_id)).fetchone()
            return (row['topic_id'],json.loads(row['asked'])) if row and row['status'] == 'OPEN' else None

    def topic_for_source_reply(self, chat_id, reply_to_id, expiry_days):
        self.expire_due(expiry_days)
        with self.store._lock:
            rows=self.store.connection.execute('''SELECT t.topic_id,t.asked FROM rules_sources s JOIN rules_topics t ON t.topic_id=s.topic_id
                WHERE s.chat_id=? AND s.message_id=? AND t.source_chat_id=? AND t.status='OPEN' AND t.topic_id NOT LIKE 'batch:%' ''',
                (chat_id,reply_to_id,chat_id)).fetchall()
            return (rows[0]['topic_id'],json.loads(rows[0]['asked'])) if len(rows)==1 else None

    def record_decision(self, topic_id, decision, confidence=None):
        import math
        if confidence is not None and (type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0 <= confidence <= 1): raise ValueError('invalid confidence')
        with self.store._lock, self.store.connection:
            refs = self.sources(topic_id)
            provenance = {}
            for name, field in decision.present.items():
                if not field.source_ids or any(ref not in refs for ref in field.source_ids): raise ValueError('unknown source ref')
                provenance[name] = [{'chat_id': refs[ref][0], 'message_id': refs[ref][1]} for ref in field.source_ids]
            self.store.connection.execute('''INSERT INTO rules_decisions(topic_id,recorded_at,state,intent_rule,decision_rule,fingerprint,present_sources,confidence)
                VALUES(?,?,?,?,?,?,?,?)''', (topic_id,self.store.clock().isoformat(),decision.state,decision.intent_rule,decision.rule,
                                            decision.fingerprint,json.dumps(provenance,separators=(',',':')),confidence))

    def close(self, topic_id, status):
        if status not in ('CLOSED','EXPIRED','ESCALATED'): raise ValueError('invalid terminal state')
        with self.store._lock, self.store.connection:
            return self.store.connection.execute("UPDATE rules_topics SET status=? WHERE topic_id=? AND status='OPEN'", (status,topic_id)).rowcount == 1

    def save_batch(self, batch_id, fingerprint, plans, pending_updates=()):
        with self.store._lock, self.store.connection:
            if self.store.connection.execute('SELECT 1 FROM rules_batches WHERE batch_id=?', (batch_id,)).fetchone(): return False
            for ordinal,topic,expected,additions in pending_updates:
                if self.topic_snapshot(topic) != expected: return False
            for ordinal,topic,expected,additions in pending_updates:
                for ref,chat_id,message_id in additions:
                    self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (topic,ref,chat_id,message_id))
                if additions:
                    self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(),topic))
                plans[ordinal]['pending_snapshot'] = self.topic_snapshot(topic)
            inserted = self.store.connection.execute('INSERT OR IGNORE INTO rules_batches VALUES(?,?,?)', (batch_id,fingerprint,self.store.clock().isoformat())).rowcount
            if not inserted: return False
            for ordinal,plan in enumerate(plans):
                self.store.connection.execute("INSERT INTO rules_batch_groups VALUES(?,?,?,'PLANNED')", (batch_id,ordinal,json.dumps(plan,ensure_ascii=False,separators=(',',':'))))
            return True

    def batch(self, batch_id):
        with self.store._lock:
            header=self.store.connection.execute('SELECT fingerprint FROM rules_batches WHERE batch_id=?',(batch_id,)).fetchone()
            if header is None: return None
            rows=self.store.connection.execute('SELECT ordinal,plan,status FROM rules_batch_groups WHERE batch_id=? ORDER BY ordinal',(batch_id,)).fetchall()
            return header['fingerprint'], [(r['ordinal'],json.loads(r['plan']),r['status']) for r in rows]

    def claim_topic(self, topic_id, snapshot):
        with self.store._lock, self.store.connection:
            if self.topic_snapshot(topic_id) != snapshot: return False
            return self.store.connection.execute('INSERT OR IGNORE INTO rules_inflight_topics VALUES(?,?,?)',
                                                 (topic_id,'single:'+topic_id,0)).rowcount == 1

    def release_topic(self, topic_id):
        with self.store._lock, self.store.connection:
            return self.store.connection.execute('DELETE FROM rules_inflight_topics WHERE topic_id=? AND batch_id=?',
                                                 (topic_id,'single:'+topic_id)).rowcount == 1

    def transition_group(self, batch_id, ordinal, old, new, *, topic=None, snapshot=None):
        if (old,new) not in {('PLANNED','RESERVED'),('RESERVED','DONE'),('RESERVED','AMBIGUOUS')}:
            raise ValueError('invalid batch transition')
        with self.store._lock,self.store.connection:
            if topic is not None and self.topic_snapshot(topic) != snapshot: return False
            changed = self.store.connection.execute('UPDATE rules_batch_groups SET status=? WHERE batch_id=? AND ordinal=? AND status=?',(new,batch_id,ordinal,old)).rowcount == 1
            if changed and topic is not None:
                self.store.connection.execute('INSERT INTO rules_inflight_topics VALUES(?,?,?)', (topic,batch_id,ordinal))
            if changed and new == 'DONE':
                self.store.connection.execute('DELETE FROM rules_inflight_topics WHERE batch_id=? AND ordinal=?', (batch_id,ordinal))
            return changed

    def reserve_attempt(self, topic_id, max_retries):
        if type(max_retries) is not int or max_retries < 0: raise ValueError('invalid retry limit')
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT attempts,status,attempt_reserved FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN' or row['attempt_reserved'] or row['attempts'] >= max_retries + 1: return None
            attempt = row['attempts'] + 1
            self.store.connection.execute("UPDATE rules_topics SET attempts=?,attempt_reserved=1,phase='HOLD' WHERE topic_id=?", (attempt,topic_id))
            return attempt

    def finish_attempt(self, topic_id, attempt, *, success=False):
        if type(success) is not bool: raise ValueError('invalid attempt result')
        with self.store._lock, self.store.connection:
            return self.store.connection.execute("UPDATE rules_topics SET attempt_reserved=0,attempts=CASE WHEN ? THEN 0 ELSE attempts END,phase=CASE WHEN ? THEN 'WAITING' ELSE 'HOLD' END WHERE topic_id=? AND attempts=? AND attempt_reserved=1", (success,success,topic_id,attempt)).rowcount == 1

    def hold_topics(self):
        with self.store._lock:
            return [row['topic_id'] for row in self.store.connection.execute("SELECT topic_id FROM rules_topics WHERE status='OPEN' AND phase='HOLD' AND attempt_reserved=0 ORDER BY last_at")]

    def expire_due(self, expiry_days):
        from datetime import datetime
        from .store import _aware
        with self.store._lock, self.store.connection:
            rows = self.store.connection.execute("SELECT topic_id,last_at FROM rules_topics WHERE status='OPEN'").fetchall()
            expired = [r['topic_id'] for r in rows if self.store.clock() - _aware(datetime.fromisoformat(r['last_at'])) >= timedelta(days=expiry_days)]
            for topic in expired:
                self.store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=? AND status='OPEN'", (topic,))
            return len(expired)

    def reserve_notice(self, topic_id, ops_chat_id, kind, fields=(), *, max_asks_per_field=1):
        if kind not in ('question','escalation','other','error'): raise ValueError('invalid notice kind')
        if type(max_asks_per_field) is not int or max_asks_per_field <= 0: raise ValueError('invalid ask limit')
        fields = tuple(fields)
        if any(not isinstance(f,str) or not f for f in fields) or len(fields) != len(set(fields)): raise ValueError('invalid question fields')
        with self.store._lock, self.store.connection:
            row = self.store.connection.execute('SELECT * FROM rules_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row is None or row['status'] != 'OPEN': return None
            asked = json.loads(row['asked'])
            if kind == 'question' and ((not fields and row['fallback_asked']) or any(asked.get(f,0) >= max_asks_per_field for f in fields)): return None
            if kind == 'escalation' and self.store.connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='escalation'", (topic_id,)).fetchone(): return None
            if kind == 'error' and self.store.connection.execute("SELECT 1 FROM rules_notices WHERE topic_id=? AND kind='error'", (topic_id,)).fetchone(): return None
            sequence = self.store.connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM rules_notices WHERE topic_id=?', (topic_id,)).fetchone()[0]
            self.store.connection.execute("INSERT INTO rules_notices VALUES(?,?,?,?,'reserved',NULL)", (topic_id,sequence,ops_chat_id,kind))
            if kind == 'question':
                for field in fields: asked[field] = asked.get(field, 0) + 1
                self.store.connection.execute('UPDATE rules_topics SET asked=?,fallback_asked=? WHERE topic_id=?', (json.dumps(asked), int(not fields or row['fallback_asked']), topic_id))
            return sequence

    def finish_notice(self, topic_id, sequence, notice_id):
        status = 'sent' if type(notice_id) is int and notice_id > 0 else 'ambiguous'
        with self.store._lock, self.store.connection:
            return self.store.connection.execute('UPDATE rules_notices SET status=?,notice_id=? WHERE topic_id=? AND sequence=? AND status=?', (status,notice_id if status == 'sent' else None,topic_id,sequence,'reserved')).rowcount == 1

    def bind_reply(self, ops_chat_id, reply_to, message_id, expiry_days):
        if type(reply_to) is not int or type(message_id) is not int or reply_to <= 0 or message_id <= 0: return None
        if type(expiry_days) is not int or expiry_days <= 0: raise ValueError('invalid expiry')
        with self.store._lock, self.store.connection:
            return self._bind_reply(ops_chat_id, reply_to, message_id, expiry_days)

    def accept_ops_message(self, message, expiry_days):
        """Atomically persist an actual question reply and its original Telegram identity."""
        if type(message.reply_to_id) is not int or message.reply_to_id <= 0 or type(message.message_id) is not int or message.message_id <= 0: return False
        if type(expiry_days) is not int or expiry_days <= 0: raise ValueError('invalid expiry')
        with self.store._lock, self.store.connection:
            if self.store.get(message.chat_id, message.message_id) is not None: return False
            binding = self._bind_reply(message.chat_id, message.reply_to_id, message.message_id, expiry_days)
            return bool(binding and self.store._enqueue_sql(message))

    def _bind_reply(self, ops_chat_id, reply_to, message_id, expiry_days):
            row = self.store.connection.execute('''SELECT t.topic_id,t.last_at,t.status FROM rules_notices n
                JOIN rules_topics t ON t.topic_id=n.topic_id
                WHERE n.ops_chat_id=? AND n.notice_id=? AND n.kind='question' AND n.status='sent' ''', (ops_chat_id,reply_to)).fetchone()
            if row is None: return None
            if self.store.connection.execute('SELECT 1 FROM rules_inflight_topics WHERE topic_id=?', (row['topic_id'],)).fetchone():
                self.store.connection.execute('INSERT OR IGNORE INTO rules_deferred_ops VALUES(?,?,?)', (ops_chat_id,message_id,row['topic_id']))
                return row['topic_id'],0
            if row['status'] != 'OPEN': return None
            from .store import _aware
            from datetime import datetime
            if self.store.clock() - _aware(datetime.fromisoformat(row['last_at'])) >= timedelta(days=expiry_days):
                self.store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=?", (row['topic_id'],))
                return None
            prior = self.store.connection.execute('SELECT topic_id,ref FROM rules_ops_replies WHERE chat_id=? AND message_id=?', (ops_chat_id,message_id)).fetchone()
            if prior:
                return (prior['topic_id'],prior['ref']) if prior['topic_id'] == row['topic_id'] else None
            ref = self.store.connection.execute('SELECT COALESCE(MAX(ref),0)+1 FROM rules_sources WHERE topic_id=?', (row['topic_id'],)).fetchone()[0]
            self.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)', (row['topic_id'],ref,ops_chat_id,message_id))
            self.store.connection.execute('INSERT INTO rules_ops_replies VALUES(?,?,?,?)', (ops_chat_id,message_id,row['topic_id'],ref))
            self.store.connection.execute('UPDATE rules_topics SET last_at=? WHERE topic_id=?', (self.store.clock().isoformat(),row['topic_id']))
            return row['topic_id'],ref
