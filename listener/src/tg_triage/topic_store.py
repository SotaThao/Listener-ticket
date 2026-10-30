"""Durable, non-authoritative projection of proven case/group membership.

This registry never routes messages or grants permission to mutate a ticket. Original
accounting and follow-up operation journals remain authoritative. No model prose,
message text or private reasoning is copied into its immutable event journal.
"""
import hashlib
import json


def topic_id(chat_id, kind, source_key):
    digest = hashlib.sha256(f'{chat_id}:{kind}:{source_key}'.encode()).hexdigest()[:24]
    return f'topic:{chat_id}:{digest}'


def init_registry(connection):
    connection.executescript('''
        CREATE TABLE IF NOT EXISTS triage_topic_hydration (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_case_id INTEGER NOT NULL);
        INSERT OR IGNORE INTO triage_topic_hydration VALUES(1,0);
        CREATE TABLE IF NOT EXISTS triage_topics (
            topic_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
            case_key TEXT NOT NULL, group_key TEXT, source_message_id INTEGER NOT NULL,
            decision TEXT, issue_number INTEGER, issue_url TEXT,
            active INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL,
            membership_truncated INTEGER NOT NULL DEFAULT 0,
            split_partition_not_projected INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat_id,source_kind,source_key));
        CREATE INDEX IF NOT EXISTS idx_triage_topics_chat ON triage_topics(chat_id);
        CREATE TABLE IF NOT EXISTS triage_topic_messages (
            topic_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            role TEXT NOT NULL, provenance TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(topic_id,chat_id,message_id));
        CREATE INDEX IF NOT EXISTS idx_triage_topic_message ON triage_topic_messages(chat_id,message_id);
        CREATE TABLE IF NOT EXISTS triage_topic_events (
            topic_id TEXT NOT NULL, version INTEGER NOT NULL, recorded_at TEXT NOT NULL,
            event TEXT NOT NULL, metadata TEXT NOT NULL, PRIMARY KEY(topic_id,version));
        CREATE TRIGGER IF NOT EXISTS triage_topic_events_no_update
            BEFORE UPDATE ON triage_topic_events BEGIN SELECT RAISE(ABORT,'immutable topic event'); END;
        CREATE TRIGGER IF NOT EXISTS triage_topic_events_no_delete
            BEFORE DELETE ON triage_topic_events BEGIN SELECT RAISE(ABORT,'immutable topic event'); END;
        CREATE TABLE IF NOT EXISTS triage_current_topics(
            topic_id TEXT PRIMARY KEY,root_key TEXT NOT NULL,chat_id INTEGER NOT NULL,case_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,issue_number INTEGER,source_ids TEXT NOT NULL,
            human_bindings TEXT NOT NULL,unresolved_ids TEXT NOT NULL,scope_status TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_current_topic_root ON triage_current_topics(root_key,active);
        CREATE TABLE IF NOT EXISTS triage_current_topic_events(
            topic_id TEXT NOT NULL,version INTEGER NOT NULL,recorded_at TEXT NOT NULL,
            metadata TEXT NOT NULL,PRIMARY KEY(topic_id,version));
        CREATE TRIGGER IF NOT EXISTS current_topic_events_no_update BEFORE UPDATE ON triage_current_topic_events
            BEGIN SELECT RAISE(ABORT,'immutable current topic event'); END;
        CREATE TRIGGER IF NOT EXISTS current_topic_events_no_delete BEFORE DELETE ON triage_current_topic_events
            BEGIN SELECT RAISE(ABORT,'immutable current topic event'); END;
    ''')


def project_chat(connection, chat_id, now, case_key):
    """Idempotent projection inside the caller's writable transaction; no network."""
    candidates = [dict(row, source_kind='group', source_key=row['group_key']) for row in
                  connection.execute('SELECT * FROM groups WHERE chat_id=? AND case_key=? LIMIT 1000', (chat_id,case_key))]
    candidates += [dict(row, source_kind='case', source_key=row['case_key'], group_key=None) for row in
                   connection.execute('''SELECT * FROM cases c WHERE chat_id=? AND case_key=? AND NOT EXISTS
                       (SELECT 1 FROM groups g WHERE g.case_key=c.case_key AND g.chat_id=c.chat_id)''', (chat_id,case_key))]
    current = set()
    for source in candidates:
        key = topic_id(chat_id, source['source_kind'], source['source_key'])
        current.add(key)
        column = 'group_key' if source['source_kind'] == 'group' else 'case_key'
        originals = connection.execute(f'''SELECT message_id FROM messages
            WHERE chat_id=? AND {column}=? AND decision!='followup' ORDER BY message_id LIMIT 1001''',
            (chat_id, source['source_key'])).fetchall()
        roots = connection.execute(f'''SELECT root_key,siblings,split_plan FROM followup_roots
            WHERE chat_id=? AND {column}=? LIMIT 21''', (chat_id, source['source_key'])).fetchall()
        truncated = len(originals)>1000 or len(roots)>20
        members = {row[0]: ('original', f'{source["source_kind"]}:{source["source_key"]}') for row in originals[:1000]}
        for root in roots[:20]:
            jobs = connection.execute('''SELECT j.id,j.message_id FROM followup_jobs j
                    JOIN messages m ON m.chat_id=j.chat_id AND m.message_id=j.message_id
                    WHERE j.chat_id=? AND j.root_key=? AND m.decision='followup' ORDER BY j.id DESC LIMIT 1001''', (chat_id, root[0])).fetchall()
            truncated = truncated or len(jobs)>1000
            for row in jobs[:1000]:
                members[row['message_id']] = ('followup', f'followup_job:{row["id"]}')
        state = dict(chat_id=chat_id, source_kind=source['source_kind'], source_key=source['source_key'],
                     case_key=source['case_key'], group_key=source.get('group_key'),
                     source_message_id=source['first_message_id'], decision=source['decision'],
                     issue_number=source['issue_number'], issue_url=source['issue_url'], active=1,
                     membership_truncated=int(truncated),
                     split_partition_not_projected=int(any(r['split_plan'] or r['siblings'] not in (None,'[]') for r in roots)))
        previous = connection.execute('SELECT * FROM triage_topics WHERE topic_id=?', (key,)).fetchone()
        old_members = {r['message_id']: (r['role'], r['provenance']) for r in connection.execute(
            'SELECT * FROM triage_topic_messages WHERE topic_id=? AND active=1 ORDER BY message_id DESC LIMIT 21000', (key,))}
        if previous is not None and all(previous[k] == v for k, v in state.items()) and old_members == members:
            continue
        version = previous['version'] + 1 if previous else 1
        connection.execute('''INSERT INTO triage_topics(topic_id,chat_id,source_kind,source_key,
            case_key,group_key,source_message_id,decision,issue_number,issue_url,active,version,membership_truncated,split_partition_not_projected)
            VALUES (:topic_id,:chat_id,:source_kind,:source_key,:case_key,:group_key,:source_message_id,
                    :decision,:issue_number,:issue_url,:active,:version,:membership_truncated,:split_partition_not_projected)
            ON CONFLICT(topic_id) DO UPDATE SET decision=excluded.decision,
                issue_number=excluded.issue_number,issue_url=excluded.issue_url,
                active=excluded.active,version=excluded.version,
                membership_truncated=excluded.membership_truncated,
                split_partition_not_projected=excluded.split_partition_not_projected''', dict(state, topic_id=key, version=version))
        connection.execute('UPDATE triage_topic_messages SET active=0 WHERE topic_id=?', (key,))
        for message_id, (role, provenance) in members.items():
            connection.execute('''INSERT INTO triage_topic_messages VALUES (?,?,?,?,?,1)
                ON CONFLICT(topic_id,chat_id,message_id) DO UPDATE SET
                    role=excluded.role,provenance=excluded.provenance,active=1''',
                (key, chat_id, message_id, role, provenance))
        metadata = dict(state, harness_owner='AlexChu', projection_scope='original_intake',
                        members=[dict(message_id=i, role=v[0], provenance=v[1]) for i, v in sorted(members.items())])
        connection.execute('INSERT INTO triage_topic_events VALUES (?,?,?,?,?)',
                           (key, version, now, 'projected', json.dumps(metadata, sort_keys=True)))
    for old in connection.execute('SELECT * FROM triage_topics WHERE chat_id=? AND case_key=? AND active=1', (chat_id,case_key)).fetchall():
        if old['topic_id'] not in current:
            version = old['version'] + 1
            connection.execute('UPDATE triage_topics SET active=0,version=? WHERE topic_id=?', (version, old['topic_id']))
            connection.execute('UPDATE triage_topic_messages SET active=0 WHERE topic_id=?', (old['topic_id'],))
            connection.execute('INSERT INTO triage_topic_events VALUES (?,?,?,?,?)',
                               (old['topic_id'], version, now, 'retired', '{"harness_owner":"AlexChu","reason_code":"partition_replaced"}'))
    project_current_topics(connection,chat_id,case_key,now)


def project_current_topics(connection,chat_id,case_key,now):
    """Current semantic scopes from host-validated journals; never rewrite intake membership."""
    for root in connection.execute('SELECT * FROM followup_roots WHERE chat_id=? AND case_key=? LIMIT 100',
                                   (chat_id,case_key)).fetchall():
        state=json.loads(root['context_state']); bindings=state.get('bindings',{})
        column='group_key' if root['group_key'] else 'case_key'
        rows=connection.execute(f"SELECT message_id FROM messages WHERE chat_id=? AND {column}=? AND decision!='followup' ORDER BY message_id LIMIT 1001",
                                (chat_id,root[column])).fetchall()
        ids={r[0] for r in rows}
        episode=connection.execute('SELECT source_ids FROM followup_episodes WHERE root_key=?',(root['root_key'],)).fetchone()
        if episode:
            ids={i for i in json.loads(episode[0]) if type(i) is int and connection.execute(
                'SELECT 1 FROM messages WHERE chat_id=? AND message_id=? AND case_key=?',(chat_id,i,case_key)).fetchone()}
        numbers=[root['issue_number']]+json.loads(root['siblings'])
        numbers=[n if type(n) is int and n>0 else None for n in numbers]
        plan=json.loads(root['split_plan']) if root['split_plan'] else None
        if not plan and not episode:
            for job in connection.execute('SELECT id,message_id FROM followup_jobs WHERE root_key=? ORDER BY id DESC LIMIT 100',(root['root_key'],)):
                members=connection.execute('SELECT message_id FROM followup_job_sources WHERE job_id=?',(job['id'],)).fetchall()
                for mid in [r[0] for r in members] or [job['message_id']]:
                    other=connection.execute('SELECT root_key FROM followup_episode_sources WHERE chat_id=? AND message_id=?',(chat_id,mid)).fetchone()
                    if not other or other['root_key']==root['root_key']:
                        ids.add(mid)
        groups=plan.get('groups',[]) if isinstance(plan,dict) else []
        assigned=[i for g in groups for i in g.get('message_ids',[]) if isinstance(g,dict)]
        unresolved=plan.get('unresolved_message_ids',[]) if plan else []
        valid=bool(groups) and all(type(i) is int for i in assigned+unresolved) and len(set(assigned+unresolved))==len(assigned+unresolved) and set(assigned+unresolved)==ids
        if valid:
            positions={i:ordinal for ordinal,g in enumerate(groups) for i in g['message_ids']}
            if any(positions.get(int(k))!=v for k,v in bindings.items() if str(k).isdigit()):
                valid=False
        if valid:
            scopes=[(ordinal,g['message_ids'],numbers[ordinal] if ordinal<len(numbers) else None,
                     'verified_scope' if ordinal<len(numbers) and numbers[ordinal] else 'awaiting_issue') for ordinal,g in enumerate(groups)]
        elif plan or len(numbers)>1:
            unresolved=sorted(ids); scopes=[(0,[],numbers[0] if numbers else None,'unresolved_partition')]
        else:
            unresolved=[]; scopes=[(0,sorted(ids),numbers[0] if numbers else None,'verified_scope')]
        proven_bindings={str(i):ordinal for i,ordinal in ((int(k),v) for k,v in bindings.items() if str(k).isdigit())
                         if i in ids and type(ordinal) is int and 0<=ordinal<len(scopes)}
        active=set()
        for ordinal,members,number,status in scopes:
            key=topic_id(chat_id,'current',f"{root['root_key']}:{ordinal}"); active.add(key)
            value=dict(root_key=root['root_key'],chat_id=chat_id,case_key=case_key,ordinal=ordinal,issue_number=number,
                source_ids=json.dumps(sorted(members)),human_bindings=json.dumps(proven_bindings,sort_keys=True),
                unresolved_ids=json.dumps(sorted(unresolved)),scope_status=status,active=1)
            prior=connection.execute('SELECT * FROM triage_current_topics WHERE topic_id=?',(key,)).fetchone()
            if prior and all(prior[k]==v for k,v in value.items()):
                continue
            version=prior['version']+1 if prior else 1
            connection.execute('''INSERT INTO triage_current_topics VALUES(:topic_id,:root_key,:chat_id,:case_key,
                :ordinal,:issue_number,:source_ids,:human_bindings,:unresolved_ids,:scope_status,:active,:version)
                ON CONFLICT(topic_id) DO UPDATE SET issue_number=excluded.issue_number,source_ids=excluded.source_ids,
                human_bindings=excluded.human_bindings,unresolved_ids=excluded.unresolved_ids,scope_status=excluded.scope_status,
                active=1,version=excluded.version''',dict(value,topic_id=key,version=version))
            connection.execute('INSERT INTO triage_current_topic_events VALUES(?,?,?,?)',
                (key,version,now,json.dumps(dict(value,harness_owner='AlexChu'),sort_keys=True)))
        for old in connection.execute('SELECT topic_id,version FROM triage_current_topics WHERE root_key=? AND active=1',(root['root_key'],)).fetchall():
            if old['topic_id'] not in active:
                version=old['version']+1
                connection.execute('UPDATE triage_current_topics SET active=0,version=? WHERE topic_id=?',(version,old['topic_id']))
                connection.execute('INSERT INTO triage_current_topic_events VALUES(?,?,?,?)',
                    (old['topic_id'],version,now,'{"harness_owner":"AlexChu","active":0}'))


class TopicStoreMixin:
    def current_topic_context(self,root_key):
        with self._lock:
            return [dict(ordinal=r['ordinal'],issue_number=r['issue_number'],source_ids=json.loads(r['source_ids']),
                human_bindings=json.loads(r['human_bindings']),unresolved_ids=json.loads(r['unresolved_ids']),scope_status=r['scope_status'])
                for r in self.connection.execute('SELECT * FROM triage_current_topics WHERE root_key=? AND active=1 ORDER BY ordinal LIMIT 20',(root_key,))]

    def enable_topic_registry(self, *, hydration_batch=100):
        """Explicit normal-runtime migration/hydration. Read-only tools never call this."""
        if getattr(self, '_read_only', False):
            raise RuntimeError('Topic registry requires writable runtime')
        if type(hydration_batch) is not int or not 1 <= hydration_batch <= 1000:
            raise ValueError('Invalid topic hydration batch')
        with self._lock:
            init_registry(self.connection)
            with self.connection:
                cursor = self.connection.execute('SELECT last_case_id FROM triage_topic_hydration WHERE singleton=1').fetchone()[0]
                rows = self.connection.execute('SELECT id,chat_id,case_key FROM cases WHERE id>? ORDER BY id LIMIT ?', (cursor,hydration_batch)).fetchall()
                for row in rows:
                    project_chat(self.connection, row['chat_id'], self.clock().isoformat(), row['case_key'])
                if rows:
                    self.connection.execute('UPDATE triage_topic_hydration SET last_case_id=? WHERE singleton=1', (rows[-1]['id'],))
            self._topic_registry_enabled = True

    def project_topics(self, chat_id, case_key):
        if case_key and getattr(self, '_topic_registry_enabled', False):
            project_chat(self.connection, chat_id, self.clock().isoformat(), case_key)
