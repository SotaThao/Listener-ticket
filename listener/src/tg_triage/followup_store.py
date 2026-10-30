"""Additive durable conversation jobs; no mutation of original case/group accounting."""
import json
import threading
from datetime import timedelta


def _locked(method):
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class FollowupStoreMixin:
    def _project_followup_topic(self, job):
        root=self.connection.execute('SELECT chat_id,case_key FROM followup_roots WHERE root_key=?',
                                     (job['root_key'],)).fetchone()
        if root is not None:
            self.project_topics(root['chat_id'],root['case_key'])

    def init_followups(self, recover):
        self._followup_execution_lock = threading.RLock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS followup_roots (
                root_key TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL, group_key TEXT, case_key TEXT NOT NULL,
                issue_number INTEGER, siblings TEXT NOT NULL DEFAULT '[]', split_plan TEXT);
            CREATE TABLE IF NOT EXISTS followup_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, root_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', plan TEXT, result TEXT,
                UNIQUE(chat_id,message_id));
            CREATE TABLE IF NOT EXISTS followup_analysis_retries (
                job_id INTEGER PRIMARY KEY, original_plan TEXT NOT NULL, original_result TEXT,
                notice_message_id INTEGER NOT NULL, authorized_at TEXT NOT NULL,
                edit_status TEXT NOT NULL DEFAULT 'planned', edit_reserved_at TEXT);
            CREATE TABLE IF NOT EXISTS followup_operations (
                job_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
                reserved_at TEXT, result INTEGER, PRIMARY KEY(job_id,ordinal));
            CREATE TABLE IF NOT EXISTS followup_episodes (
                root_key TEXT PRIMARY KEY, parent_root_key TEXT NOT NULL,
                job_id INTEGER NOT NULL UNIQUE, chat_id INTEGER NOT NULL,
                anchor_message_id INTEGER NOT NULL, issue_number INTEGER NOT NULL,
                repo TEXT NOT NULL, source_ids TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(chat_id,anchor_message_id));
            CREATE TABLE IF NOT EXISTS followup_episode_sources (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, root_key TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id));
            CREATE TABLE IF NOT EXISTS followup_job_sources (
                job_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
                PRIMARY KEY(chat_id,message_id));
        """)
        existing = {row[1] for row in self.connection.execute('PRAGMA table_info(followup_roots)')}
        if 'split_plan' not in existing:
            self.connection.execute('ALTER TABLE followup_roots ADD COLUMN split_plan TEXT')
        if 'context_state' not in existing:
            self.connection.execute("ALTER TABLE followup_roots ADD COLUMN context_state TEXT NOT NULL DEFAULT '{}'")
        if recover:
            self.connection.execute("UPDATE followup_analysis_retries SET edit_status='ambiguous' WHERE edit_status='reserved'")
            self.connection.execute("UPDATE followup_operations SET status='ambiguous' WHERE status='reserved'")
            self.connection.execute("""UPDATE followup_jobs SET status='ambiguous'
                WHERE id IN (SELECT job_id FROM followup_operations WHERE status='ambiguous')""")

    def followup_source(self, message):
        """Only same-chat sent notice records or their proven mapped source may resolve a root."""
        if message.reply_to_id is None:
            return None
        with self._lock:
            confirmation=None
            if (getattr(self,'_autonomy_enabled',False) or self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='autonomous_confirmations'").fetchone()):
                confirmation=self.connection.execute('''SELECT message_id,issue_number
                    FROM autonomous_confirmations WHERE chat_id=? AND notice_message_id=? AND status='sent' ''',
                    (message.chat_id,message.reply_to_id)).fetchone()
                if confirmation is not None:
                    source=self.get(message.chat_id,confirmation['message_id'])
                    number=confirmation['issue_number']
                    nested=self.connection.execute('''SELECT 1 FROM autonomous_confirmations
                        WHERE chat_id=? AND notice_message_id=? LIMIT 1''',
                        (message.chat_id,confirmation['message_id'])).fetchone()
                    if (confirmation['message_id']==message.reply_to_id or nested is not None or source is None
                            or source['case_key'] is None or type(number) is not int or number<=0
                            or source['issue_number']!=number or source['decision'] not in ('issue','needs_human')):
                        return None
                    import re
                    if (not isinstance(source['issue_url'],str) or not re.fullmatch(
                            r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/'+str(number),source['issue_url'])):
                        return None
                    # Normalize only this lookup. The stored Telegram edge remains
                    # the actual reply to the sent confirmation, never rewritten.
                    from dataclasses import replace
                    message=replace(message,reply_to_id=confirmation['message_id'])
            resolved=self._followup_source_original(message)
            if confirmation is not None:
                if not resolved or resolved['issue_number']!=confirmation['issue_number']:
                    return None
                root=self.connection.execute('SELECT chat_id,issue_number FROM followup_roots WHERE root_key=?',
                                             (resolved['root_key'],)).fetchone()
                if root is not None and (root['chat_id']!=message.chat_id or root['issue_number']!=confirmation['issue_number']):
                    return None
            return resolved

    def _followup_source_original(self,message):
        """Resolve original source/group/episode notices; never chase confirmation aliases."""
        with self._lock:
            episode_notice=self.connection.execute('''SELECT r.* FROM followup_episodes e
                JOIN followup_jobs j ON j.id=e.job_id JOIN group_notices n
                ON n.chat_id=j.chat_id AND n.message_id=j.message_id JOIN followup_roots r ON r.root_key=e.root_key
                WHERE n.chat_id=? AND n.notice_message_id=? AND n.status='sent' LIMIT 1''',
                (message.chat_id,message.reply_to_id)).fetchone()
            if episode_notice is not None:return dict(episode_notice)
            episode=self.connection.execute("""SELECT r.* FROM followup_episode_sources e
                JOIN followup_roots r ON r.root_key=e.root_key
                WHERE e.chat_id=? AND (e.message_id=? OR e.message_id IN
                    (SELECT message_id FROM group_notices WHERE chat_id=? AND notice_message_id=? AND status='sent'))
                LIMIT 1""",
                (message.chat_id,message.reply_to_id,message.chat_id,message.reply_to_id)).fetchone()
            if episode is not None:
                return dict(episode)
            member_job=self.connection.execute('''SELECT r.* FROM followup_job_sources s
                JOIN followup_jobs j ON j.id=s.job_id JOIN followup_roots r ON r.root_key=j.root_key
                WHERE s.chat_id=? AND s.message_id=?''',(message.chat_id,message.reply_to_id)).fetchone()
            if member_job is not None:
                return dict(member_job)
            notice = self.connection.execute("""SELECT message_id FROM group_notices
                WHERE chat_id=? AND status='sent' AND kind!='context_pending' AND (notice_message_id=? OR message_id=?)
                ORDER BY reserved_at DESC LIMIT 1""",
                (message.chat_id, message.reply_to_id, message.reply_to_id)).fetchone()
            if notice is None:
                # A direct reply to ANY original member may resolve only through a sent
                # notice on that exact stored topic and its proven issue association.
                member=self.get(message.chat_id,message.reply_to_id)
                if (member is None or member['decision']=='followup' or member['case_key'] is None
                        or member['issue_number'] is None):
                    return None
                column='group_key' if member['group_key'] else 'case_key'
                notice=self.connection.execute(f"""SELECT n.message_id FROM group_notices n
                    JOIN messages m ON m.chat_id=n.chat_id AND m.message_id=n.message_id
                    WHERE n.chat_id=? AND n.status='sent' AND m.{column}=?
                    AND m.issue_number=? AND m.issue_url=? AND m.decision!='followup'
                    ORDER BY n.reserved_at LIMIT 1""",
                    (message.chat_id,member[column],member['issue_number'],member['issue_url'])).fetchone()
                if notice is None:
                    return None
            job = self.connection.execute("SELECT root_key FROM followup_jobs WHERE chat_id=? AND message_id=?",
                                          (message.chat_id, notice[0])).fetchone()
            if job:
                return dict(self.connection.execute('SELECT * FROM followup_roots WHERE root_key=?',
                                                    (job[0],)).fetchone())
            source = self.get(message.chat_id, notice[0])
            if source is None or source['case_key'] is None:
                return None
            return dict(root_key=f"{message.chat_id}:{source['group_key'] or source['case_key']}",
                        chat_id=message.chat_id, source_message_id=notice[0], group_key=source['group_key'],
                        case_key=source['case_key'], issue_number=source['issue_number'], siblings='[]')

    def enqueue_followup(self, message, source):
        with self._lock, self.connection:
            self.connection.execute("""INSERT OR IGNORE INTO followup_roots
                (root_key,chat_id,source_message_id,group_key,case_key,issue_number)
                VALUES (:root_key,:chat_id,:source_message_id,:group_key,:case_key,:issue_number)""", source)
            self.connection.execute("""INSERT OR IGNORE INTO followup_jobs(chat_id,message_id,root_key)
                VALUES (?,?,?)""", (message.chat_id,message.message_id,source['root_key']))
            self.connection.execute("""UPDATE messages SET decision='followup',tier='none',case_key=?,group_key=?,
                issue_number=?,issue_url=?,processed_at=? WHERE chat_id=? AND message_id=?""",
                (source['case_key'],source['group_key'],source['issue_number'],
                 self.get(source['chat_id'],source['source_message_id'])['issue_url'],self.clock().isoformat(),
                 message.chat_id,message.message_id))

            self.project_topics(message.chat_id, source['case_key'])

    def enqueue_followup_batch(self, messages, source):
        """Commit all context members and one anchored job together, or commit nothing."""
        messages=list(messages)
        if (not messages or len(messages)>20 or len({m.message_id for m in messages})!=len(messages)
                or any(m.chat_id!=source['chat_id'] for m in messages)):
            raise ValueError('Invalid followup context batch')
        anchor=messages[0]
        with self._lock,self.connection:
            prior_job=self.connection.execute('SELECT * FROM followup_jobs WHERE chat_id=? AND message_id=?',
                                              (anchor.chat_id,anchor.message_id)).fetchone()
            if prior_job is not None:
                prior_ids={r[0] for r in self.connection.execute('SELECT message_id FROM followup_job_sources WHERE job_id=?',(prior_job['id'],))}
                if not prior_ids:prior_ids={prior_job['message_id']}
                if prior_job['root_key']!=source['root_key'] or prior_ids!={m.message_id for m in messages}:
                    raise ValueError('Existing followup batch membership differs')
                return prior_job['id']  # Preserve proven recurrence mappings and operation history.
            for m in messages:
                prior=self.connection.execute('SELECT job_id FROM followup_job_sources WHERE chat_id=? AND message_id=?',
                                              (m.chat_id,m.message_id)).fetchone()
                if prior is not None:
                    job=self.connection.execute('SELECT message_id,root_key FROM followup_jobs WHERE id=?',(prior[0],)).fetchone()
                    if job['message_id']!=anchor.message_id or job['root_key']!=source['root_key']:
                        raise ValueError('Followup batch already belongs to another job')
            # Inline the single-message writes to avoid nested connection-context commits.
            self.connection.execute('''INSERT OR IGNORE INTO followup_roots
                (root_key,chat_id,source_message_id,group_key,case_key,issue_number)
                VALUES (:root_key,:chat_id,:source_message_id,:group_key,:case_key,:issue_number)''',source)
            self.connection.execute('INSERT OR IGNORE INTO followup_jobs(chat_id,message_id,root_key) VALUES(?,?,?)',
                                    (anchor.chat_id,anchor.message_id,source['root_key']))
            job=self.connection.execute('SELECT * FROM followup_jobs WHERE chat_id=? AND message_id=?',
                                        (anchor.chat_id,anchor.message_id)).fetchone()
            if job['root_key']!=source['root_key']:
                raise ValueError('Conflicting followup anchor')
            origin=self.get(source['chat_id'],source['source_message_id'])
            for m in messages:
                if self.get(m.chat_id,m.message_id) is None:
                    raise ValueError('Followup batch source is missing')
                self.connection.execute('INSERT OR IGNORE INTO followup_job_sources VALUES(?,?,?)',
                                        (job['id'],m.chat_id,m.message_id))
                self.connection.execute('''UPDATE messages SET decision='followup',tier='none',case_key=?,group_key=?,
                    issue_number=?,issue_url=?,processed_at=? WHERE chat_id=? AND message_id=?''',
                    (source['case_key'],source['group_key'],source['issue_number'],origin['issue_url'],self.clock().isoformat(),m.chat_id,m.message_id))
            self.project_topics(anchor.chat_id,source['case_key'])
            return job['id']

    @_locked
    def followup_job_messages(self, job):
        from .store import _to_message
        rows=self.connection.execute('''SELECT m.* FROM followup_job_sources s JOIN messages m
            ON m.chat_id=s.chat_id AND m.message_id=s.message_id WHERE s.job_id=? ORDER BY m.message_id''',(job['id'],)).fetchall()
        return [_to_message(r) for r in rows] if rows else [_to_message(self.get(job['chat_id'],job['message_id']))]

    @_locked
    def latest_related_episode(self, root_key):
        row=self.connection.execute('SELECT * FROM followup_episodes WHERE parent_root_key=? ORDER BY anchor_message_id DESC LIMIT 1',
                                    (root_key,)).fetchone()
        return dict(row) if row is not None else None

    def pending_followups(self):
        with self._lock:
            return [dict(row) for row in self.connection.execute("""SELECT * FROM followup_jobs
                WHERE status IN ('queued','planned','ambiguous') ORDER BY id""")]

    def followup_context(self, job):
        from .store import _to_message
        with self._lock:
            root = dict(self.connection.execute('SELECT * FROM followup_roots WHERE root_key=?',
                                               (job['root_key'],)).fetchone())
            column = 'group_key' if root['group_key'] else 'case_key'
            rows = self.connection.execute(f"""SELECT * FROM messages WHERE chat_id=? AND {column}=?
                AND decision != 'followup' ORDER BY message_id""",
                (root['chat_id'],root[column])).fetchall()
            episode=self.connection.execute('SELECT source_ids FROM followup_episodes WHERE root_key=?',(root['root_key'],)).fetchone()
            if episode is not None:
                rows=[self.get(root['chat_id'],mid) for mid in json.loads(episode[0])]
                rows=[row for row in rows if row is not None]
            reply = _to_message(self.get(job['chat_id'],job['message_id']))
            present={r['message_id'] for r in rows}
            for m in self.followup_job_messages(job):
                if m.message_id!=reply.message_id and m.message_id not in present:
                    rows.append(self.get(m.chat_id,m.message_id));present.add(m.message_id)
            history = [dict(r) for r in self.connection.execute("""SELECT m.message_id,m.reply_to_id,m.sender_id,m.sender_name,m.received_at,
                       m.has_image,m.has_document,m.text,j.plan,j.result
                FROM followup_jobs j JOIN messages m ON m.chat_id=j.chat_id AND m.message_id=j.message_id
                WHERE j.root_key=? AND j.id<? ORDER BY m.received_at DESC,m.message_id DESC LIMIT 5""", (job['root_key'],job['id']))]
            history.reverse()
            return root, [_to_message(row) for row in rows], reply, history

    def save_followup_plan(self, job, plan, operations):
        with self._lock, self.connection:
            changed = self.connection.execute("UPDATE followup_jobs SET plan=?,status='planned' WHERE id=? AND status='queued'",
                                              (json.dumps(plan,ensure_ascii=False),job['id']))
            if changed.rowcount != 1:
                raise RuntimeError('Followup already planned')
            if plan.get('action')=='split':
                self.connection.execute('UPDATE followup_roots SET split_plan=? WHERE root_key=?',
                                        (json.dumps(plan,ensure_ascii=False),job['root_key']))
            for ordinal,(kind,payload) in enumerate(operations):
                self.connection.execute("""INSERT INTO followup_operations(job_id,ordinal,kind,payload)
                    VALUES (?,?,?,?)""", (job['id'],ordinal,kind,json.dumps(payload,ensure_ascii=False)))
            self._project_followup_topic(job)

    @_locked
    def followup_operations(self, job):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM followup_operations WHERE job_id=? ORDER BY ordinal', (job['id'],))]

    def reserve_followup_operation(self, job, ordinal, limit):
        with self._lock:
            try:
                self.connection.execute('BEGIN IMMEDIATE')
                row=self.connection.execute('SELECT * FROM followup_operations WHERE job_id=? AND ordinal=?',
                                            (job['id'],ordinal)).fetchone()
                if row['status'] != 'planned':
                    self.connection.rollback(); return False
                if row['kind'] in ('create','related_create') and self.count_recent_issues() >= limit:
                    self.connection.rollback(); return False
                self.connection.execute("UPDATE followup_operations SET status='reserved',reserved_at=? WHERE job_id=? AND ordinal=?",
                                        (self.clock().isoformat(),job['id'],ordinal))
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    def finish_followup_operation(self, job, op, number=None):
        with self._lock, self.connection:
            changed=self.connection.execute("""UPDATE followup_operations SET status='done',result=?
                WHERE job_id=? AND ordinal=? AND status='reserved'""", (number,job['id'],op['ordinal']))
            if changed.rowcount != 1:
                return
            if op['kind']=='related_create' and op.get('readback_verified') is True:
                self._register_related_episode(job,op,number)
            if op['kind']=='refresh':
                payload=json.loads(op['payload'])
                root=self.connection.execute('SELECT context_state FROM followup_roots WHERE root_key=?',(job['root_key'],)).fetchone()
                state=json.loads(root[0]); state['managed_snapshot']=payload['new_snapshot']
                self.connection.execute('UPDATE followup_roots SET context_state=? WHERE root_key=?',
                                        (json.dumps(state,ensure_ascii=False),job['root_key']))
            if op['kind']=='create':
                root=self.connection.execute('SELECT siblings FROM followup_roots WHERE root_key=?', (job['root_key'],)).fetchone()
                siblings=json.loads(root[0]); siblings.append(number)
                self.connection.execute('UPDATE followup_roots SET siblings=? WHERE root_key=?',
                                        (json.dumps(siblings),job['root_key']))
            self._project_followup_topic(job)

    def remember_followup_result(self, job, op, number):
        """Best-effort DB recovery for a proven response; never repeat the external POST."""
        with self._lock, self.connection:
            row=self.connection.execute('SELECT result FROM followup_operations WHERE job_id=? AND ordinal=?',
                                        (job['id'],op['ordinal'])).fetchone()
            if row is None or row[0] is not None:
                return
            self.connection.execute('UPDATE followup_operations SET result=? WHERE job_id=? AND ordinal=?',
                                    (number,job['id'],op['ordinal']))
            if op['kind']=='related_create' and op.get('readback_verified') is True:
                self._register_related_episode(job,op,number)
            if op['kind']=='create':
                root=self.connection.execute('SELECT siblings FROM followup_roots WHERE root_key=?',
                                             (job['root_key'],)).fetchone()
                siblings=json.loads(root[0])
                if number not in siblings:
                    siblings.append(number)
                    self.connection.execute('UPDATE followup_roots SET siblings=? WHERE root_key=?',
                                            (json.dumps(siblings),job['root_key']))

    def finish_followup(self, job, status, result):
        with self._lock, self.connection:
            self.connection.execute('UPDATE followup_jobs SET status=?,result=? WHERE id=?', (status,result,job['id']))
            self._project_followup_topic(job)

    @_locked
    def followup_split_attempted(self, root_key, before_id):
        rows=self.connection.execute('SELECT plan FROM followup_jobs WHERE root_key=? AND id<? AND plan IS NOT NULL AND status != "dryrun"',
                                     (root_key,before_id)).fetchall()
        return any(json.loads(row[0]).get('action')=='split' for row in rows)

    @_locked
    def followup_create_count(self, now):
        return self.connection.execute("""SELECT COUNT(*) FROM followup_operations WHERE kind IN ('create','related_create')
            AND reserved_at IS NOT NULL AND julianday(reserved_at)>=julianday(?)""",
            ((now-timedelta(minutes=60)).isoformat(),)).fetchone()[0]

    def _register_related_episode(self, job, op, number):
        """Same transaction as proven POST result; never changes original case accounting."""
        payload=json.loads(op['payload'])
        root=self.connection.execute('SELECT * FROM followup_roots WHERE root_key=?',(job['root_key'],)).fetchone()
        key=f"episode:{job['chat_id']}:{job['message_id']}"
        episode_anchor=min(payload['source_ids'])
        snapshot=payload['new_snapshot']; snapshot['issue_number']=number
        state=json.dumps({'managed_snapshot':snapshot},ensure_ascii=False)
        self.connection.execute("""INSERT OR IGNORE INTO followup_roots
            (root_key,chat_id,source_message_id,case_key,issue_number,context_state)
            VALUES (?,?,?,?,?,?)""",(key,job['chat_id'],episode_anchor,root['case_key'],number,state))
        self.connection.execute("""INSERT OR IGNORE INTO followup_episodes
            (root_key,parent_root_key,job_id,chat_id,anchor_message_id,issue_number,repo,source_ids,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",(key,job['root_key'],job['id'],job['chat_id'],episode_anchor,number,
                snapshot['repo'],json.dumps(payload['source_ids']),self.clock().isoformat()))
        for mid in payload['source_ids']:
            self.connection.execute('INSERT OR IGNORE INTO followup_episode_sources VALUES(?,?,?)',
                (job['chat_id'],mid,key))
        # Only the new episode's followup message mapping changes. Original messages/cases remain intact.
        for mid in payload['source_ids']:
            self.connection.execute('UPDATE messages SET issue_number=?,issue_url=? WHERE chat_id=? AND message_id=?',
                (number,f"https://github.com/{snapshot['repo']}/issues/{number}",job['chat_id'],mid))

    @_locked
    def followup_media(self, root, messages, repo):
        """Reuse only proven original uploads; never reupload media while correcting a ticket."""
        import re
        from .attachments import release_tag
        images,documents=[],[]
        for item in messages:
            rows=self.connection.execute("""SELECT asset_name FROM attachments
                WHERE case_key=? AND message_id=? AND status='uploaded'""",
                (root['group_key'] or root['case_key'],item.message_id)).fetchall()
            for row in rows:
                asset=row[0]
                if not re.fullmatch(r'[0-9]+-[0-9]+\.[a-z0-9]+',asset):
                    continue
                url=f"https://github.com/{repo}/releases/download/{release_tag(root['group_key'] or root['case_key'])}/{asset}"
                if item.has_image:
                    images.append(('Ảnh',url))
                elif item.has_document:
                    documents.append((item.document_name,url))
        return images,documents

    def ensure_followup_job(self, chat_id, message_id):
        """Explicit one-message legacy replay only, never a global backfill or case rearm."""
        from .store import _to_message
        with self._lock:
            row=self.get(chat_id,message_id)
            if row is None or row['decision']!='followup':
                return False
            existing=self.connection.execute('SELECT 1 FROM followup_jobs WHERE chat_id=? AND message_id=?',
                                             (chat_id,message_id)).fetchone()
            if existing:
                return False
            message=_to_message(row)
            source=self.followup_source(message)
            if source is None:
                return False
            self.enqueue_followup(message,source)
            return True

    def save_followup_context_state(self, root_key, state):
        with self._lock, self.connection:
            self.connection.execute('UPDATE followup_roots SET context_state=? WHERE root_key=?',
                                    (json.dumps(state,ensure_ascii=False),root_key))

    @_locked
    def followup_binding_history(self, job):
        """Bounded legacy reconstruction, beyond the short model history, on this root only."""
        return [dict(row) for row in self.connection.execute("""SELECT m.* FROM followup_jobs j
            JOIN messages m ON m.chat_id=j.chat_id AND m.message_id=j.message_id
            WHERE j.root_key=? AND j.id<? ORDER BY m.received_at,m.message_id LIMIT 100""",
            (job['root_key'],job['id']))]

    def retry_followup_analysis(self, chat_id, message_id, repo, notice_message_id):
        """One explicit recovery for a failed analysis that never planned ANY root operation."""
        with self._followup_execution_lock, self._lock:
            try:
                self.connection.execute('BEGIN IMMEDIATE')
                job=self.connection.execute('SELECT * FROM followup_jobs WHERE chat_id=? AND message_id=?',
                                            (chat_id,message_id)).fetchone()
                if job is None or job['status']!='done':
                    self.connection.rollback(); return False
                plan=json.loads(job['plan'] or '{}')
                root=self.connection.execute('SELECT * FROM followup_roots WHERE root_key=?',(job['root_key'],)).fetchone()
                notice=self.connection.execute("SELECT * FROM group_notices WHERE chat_id=? AND message_id=? AND status='sent'",
                                               (chat_id,message_id)).fetchone()
                source=self.get(chat_id,root['source_message_id'])
                operations=self.connection.execute("""SELECT count(*) FROM followup_operations o JOIN followup_jobs j ON j.id=o.job_id
                    WHERE j.root_key=?""",(job['root_key'],)).fetchone()[0]
                active=self.connection.execute("""SELECT count(*) FROM followup_jobs WHERE root_key=? AND id!=?
                    AND status IN ('queued','planned','ambiguous')""",(job['root_key'],job['id'])).fetchone()[0]
                prior=self.connection.execute('SELECT 1 FROM followup_analysis_retries WHERE job_id=?',(job['id'],)).fetchone()
                valid=(plan.get('action')=='clarify' and plan.get('context_reason')=='provider_unavailable'
                       and not operations and not active and not prior and json.loads(root['siblings'])==[]
                       and root['split_plan'] is None and type(notice_message_id) is int and notice_message_id>0
                       and notice is not None and notice['notice_message_id']==notice_message_id
                       and source is not None and type(root['issue_number']) is int and root['issue_number']>0
                       and source['issue_number']==root['issue_number'] and isinstance(source['issue_url'],str)
                       and source['issue_url'].casefold()==f"https://github.com/{repo}/issues/{root['issue_number']}".casefold())
                if not valid:
                    self.connection.rollback(); return False
                self.connection.execute("""INSERT INTO followup_analysis_retries
                    (job_id,original_plan,original_result,notice_message_id,authorized_at) VALUES (?,?,?,?,?)""",
                    (job['id'],job['plan'],job['result'],notice_message_id,self.clock().isoformat()))
                self.connection.execute("UPDATE followup_jobs SET status='queued',plan=NULL,result=NULL WHERE id=?",(job['id'],))
                self.connection.commit()
                return True
            except Exception:
                self.connection.rollback()
                raise

    @_locked
    def followup_analysis_retry(self, job_id):
        row=self.connection.execute('SELECT * FROM followup_analysis_retries WHERE job_id=?',(job_id,)).fetchone()
        return dict(row) if row else None

    def reserve_followup_notice_edit(self, job_id):
        with self._lock, self.connection:
            changed=self.connection.execute("""UPDATE followup_analysis_retries SET edit_status='reserved',edit_reserved_at=?
                WHERE job_id=? AND edit_status='planned'""",(self.clock().isoformat(),job_id))
            return changed.rowcount==1

    def finish_followup_notice_edit(self, job_id, success):
        with self._lock, self.connection:
            self.connection.execute("UPDATE followup_analysis_retries SET edit_status=? WHERE job_id=? AND edit_status='reserved'",
                                    ('done' if success else 'ambiguous',job_id))

    @_locked
    def pending_followup_notice_edits(self):
        return [dict(row) for row in self.connection.execute("""SELECT j.* FROM followup_analysis_retries r
            JOIN followup_jobs j ON j.id=r.job_id WHERE r.edit_status='planned' AND j.status IN ('done','blocked')
            AND j.result IS NOT NULL ORDER BY j.id""")]

    def register_managed_issue(self, first, group_key, case_key, number, repo, issue, analysis, source_ids, images, documents):
        """Only the actual successful initial renderer/create caller can seed a managed snapshot."""
        import hashlib
        from .models import utf8_valid
        if not utf8_valid(issue.body) or not utf8_valid(issue.title):
            return False  # injected transports cannot prove an unencodable rendered issue
        root_key=f"{first.chat_id}:{group_key or case_key}"
        snapshot=dict(issue_number=number,repo=repo,title=issue.title,body_sha=hashlib.sha256(issue.body.encode('utf-8')).hexdigest(),
                      source_ids=list(source_ids),analysis=analysis,images=list(images),documents=list(documents),owned_labels=list(issue.labels))
        with self._lock,self.connection:
            self.connection.execute("""INSERT OR IGNORE INTO followup_roots
                (root_key,chat_id,source_message_id,group_key,case_key,issue_number) VALUES (?,?,?,?,?,?)""",
                (root_key,first.chat_id,first.message_id,group_key,case_key,number))
            row=self.connection.execute('SELECT context_state FROM followup_roots WHERE root_key=?',(root_key,)).fetchone()
            state=json.loads(row[0])
            if 'managed_snapshot' not in state:
                state['managed_snapshot']=snapshot
                self.connection.execute('UPDATE followup_roots SET context_state=? WHERE root_key=?',
                                        (json.dumps(state,ensure_ascii=False),root_key))
            self.project_topics(first.chat_id,case_key)

    def materialize_followup_operation(self, job, op, payload):
        with self._lock,self.connection:
            changed=self.connection.execute("UPDATE followup_operations SET payload=? WHERE job_id=? AND ordinal=? AND status='planned'",
                (json.dumps(payload,ensure_ascii=False),job['id'],op['ordinal']))
            if changed.rowcount!=1:
                raise RuntimeError('Followup materialization already reserved')

    @_locked
    def followup_attachment_rows(self, root, message_id):
        return [dict(row) for row in self.connection.execute('SELECT * FROM attachments WHERE case_key=? AND message_id=? ORDER BY id',
                (root['group_key'] or root['case_key'],message_id))]

    def record_followup_evidence(self, root_key, message_id, images, documents, available):
        with self._lock,self.connection:
            row=self.connection.execute('SELECT context_state FROM followup_roots WHERE root_key=?',(root_key,)).fetchone()
            state=json.loads(row[0]); evidence=state.setdefault('media_evidence',{})
            evidence[str(message_id)]=dict(images=list(images),documents=list(documents),available=available)
            self.connection.execute('UPDATE followup_roots SET context_state=? WHERE root_key=?',
                                    (json.dumps(state,ensure_ascii=False),root_key))
