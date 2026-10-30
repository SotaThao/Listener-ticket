"""Durable host-authorized ingress routing; model identifiers never become SQL authority."""
import json


class AutonomyStoreMixin:
    def enable_autonomy(self):
        with self._lock:
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS autonomous_routes(
                    chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,recorded_at TEXT NOT NULL,
                    action TEXT NOT NULL,candidate_key TEXT,case_key TEXT,reason_code TEXT NOT NULL,
                    metadata TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
                CREATE TRIGGER IF NOT EXISTS autonomous_routes_no_update BEFORE UPDATE ON autonomous_routes
                    BEGIN SELECT RAISE(ABORT,'immutable autonomous route'); END;
                CREATE TRIGGER IF NOT EXISTS autonomous_routes_no_delete BEFORE DELETE ON autonomous_routes
                    BEGIN SELECT RAISE(ABORT,'immutable autonomous route'); END;
                CREATE TABLE IF NOT EXISTS autonomous_confirmations(
                    chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,issue_number INTEGER NOT NULL,
                    status TEXT NOT NULL,notice_message_id INTEGER,recorded_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,message_id,issue_number));
                CREATE TABLE IF NOT EXISTS autonomous_outages(
                    chat_id INTEGER NOT NULL,cycle INTEGER NOT NULL,anchor_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL,started_at TEXT NOT NULL,closed_at TEXT,
                    PRIMARY KEY(chat_id,cycle));
                CREATE UNIQUE INDEX IF NOT EXISTS autonomous_outage_open ON autonomous_outages(chat_id)
                    WHERE status='open';
                CREATE TABLE IF NOT EXISTS autonomous_clarifications(
                    case_key TEXT NOT NULL,question_key TEXT NOT NULL,question TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,recorded_at TEXT NOT NULL,
                    PRIMARY KEY(case_key,question_key));
                CREATE TABLE IF NOT EXISTS autonomous_plans(
                    chat_id INTEGER NOT NULL,anchor_id INTEGER NOT NULL,source_ids TEXT NOT NULL,
                    case_key TEXT NOT NULL,plan TEXT NOT NULL,recorded_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,anchor_id));
            ''')
            self.connection.execute("UPDATE autonomous_confirmations SET status='ambiguous' WHERE status='reserved'")
            self._autonomy_enabled=True

    def autonomous_read_context(self,sources):
        """Recent stored excerpts only; no target or routing authority is returned."""
        from .store import _to_message
        from .store import _aware
        if not sources:
            return []
        first=min(sources,key=lambda m:m.message_id)
        cutoff=min(_aware(m.received_at) for m in sources)
        with self._lock:
            rows=self.connection.execute("SELECT * FROM messages WHERE chat_id=? AND message_id<? ORDER BY message_id DESC LIMIT 40",
                (first.chat_id,first.message_id)).fetchall()
            messages=[_to_message(row) for row in rows]
            recent=list(reversed([m for m in messages if _aware(m.received_at)<=cutoff][:8]))
            exact=[]
            for source in sources[:6]:
                if source.reply_to_id is None:
                    continue
                target=self.group_notice_reply_target(source.chat_id,source.reply_to_id)
                row=self.get(source.chat_id,target)
                if row is not None and row['message_id']<first.message_id:
                    old=_to_message(row)
                    if _aware(old.received_at)<=cutoff and old.message_id not in {m.message_id for m in exact}:
                        exact.append(old)
            # Exact no-ticket replies are evidence, never candidates or write authority.
            return ([m for m in recent if m.message_id not in {old.message_id for old in exact}]+exact)[-8:]

    def reserve_autonomous_outage(self,message):
        """One technical notice per chat outage, unrelated to semantic topic membership."""
        with self._lock,self.connection:
            if self.connection.execute("SELECT 1 FROM autonomous_outages WHERE chat_id=? AND status='open'",
                                       (message.chat_id,)).fetchone():
                return False
            cycle=self.connection.execute('SELECT COALESCE(MAX(cycle),0)+1 FROM autonomous_outages WHERE chat_id=?',
                                          (message.chat_id,)).fetchone()[0]
            self.connection.execute("INSERT INTO autonomous_outages VALUES(?,?,?,'open',?,NULL)",
                (message.chat_id,cycle,message.message_id,self.clock().isoformat()))
            return True

    def close_autonomous_outage(self,chat_id):
        """Only host-validated model routing ends the incident; preserve its history."""
        with self._lock,self.connection:
            self.connection.execute("UPDATE autonomous_outages SET status='closed',closed_at=? WHERE chat_id=? AND status='open'",
                                    (self.clock().isoformat(),chat_id))

    def reserve_autonomous_clarification(self,case_key,message,question):
        """Semantic-topic reservation precedes the existing transport reservation.

        A crash or unknown transport outcome must not cause another question attempt.
        Different focused questions and different proven topics retain independent slots.
        """
        import hashlib
        import unicodedata
        normalized=' '.join(unicodedata.normalize('NFKC',question).casefold().split()).rstrip(' ?.!')
        digest=hashlib.sha256(normalized.encode('utf-8')).hexdigest()
        with self._lock,self.connection:
            for previous in self.autonomous_clarification_context(case_key):
                prior=' '.join(unicodedata.normalize('NFKC',previous['question']).casefold().split()).rstrip(' ?.!')
                if prior==normalized:
                    return False
            return self.connection.execute('INSERT OR IGNORE INTO autonomous_clarifications VALUES(?,?,?,?,?,?)',
                (case_key,digest,question,message.chat_id,message.message_id,self.clock().isoformat())).rowcount==1

    def autonomous_clarification_context(self,case_key):
        with self._lock:
            rows=self.connection.execute("""SELECT q.question,q.message_id,COALESCE(n.status,'reserved') AS status,
                n.notice_message_id FROM autonomous_clarifications q LEFT JOIN group_notices n
                ON n.chat_id=q.chat_id AND n.message_id=q.message_id
                WHERE q.case_key=? ORDER BY q.recorded_at DESC,q.message_id DESC LIMIT 3""",(case_key,)).fetchall()
            result=[dict(question=r['question'],source_message_id=r['message_id'],
                         delivery_status=r['status'],notice_message_id=r['notice_message_id']) for r in rows]
            # Upgrade existing pending topics without discarding their already-attempted question.
            if not result:
                for old in self.connection.execute("""SELECT p.plan,n.message_id,n.status,n.notice_message_id
                    FROM autonomous_plans p JOIN group_notices n ON n.chat_id=p.chat_id AND n.message_id=p.anchor_id
                    WHERE p.case_key=? AND n.kind='needs_human' ORDER BY p.recorded_at DESC LIMIT 3""",(case_key,)):
                    from . import group_notices
                    plan=json.loads(old['plan'])
                    missing=plan.get('analysis',{}).get('missing_info',[])
                    question=plan.get('question') or (missing[0] if missing else '') or (group_notices.TECHNICAL_CONTEXT_QUESTION
                        if plan.get('host_batch_notice_anchor') is not None else group_notices.LEGACY_PENDING_QUESTION)
                    result.append(dict(question=group_notices.address_question(question),source_message_id=old['message_id'],
                        delivery_status=old['status'],notice_message_id=old['notice_message_id']))
            return result


    def save_autonomous_plans(self,sources,groups,repo):
        """Checkpoint validated application decisions before any case or network mutation."""
        from dataclasses import asdict
        with self._lock,self.connection:
            for group in groups:
                candidate=group.get('candidate')
                analysis=group['analysis']
                wire=dict(verdict=analysis.verdict,category=analysis.category,actionable=analysis.actionable,
                    reason=analysis.reason,missing_info=list(analysis.missing_info),key_message_ids=list(analysis.key_message_ids),
                    details=dict(analysis.details))
                if analysis.classification:
                    wire.update(asdict(analysis.classification))
                value={k:v for k,v in group.items() if k not in ('analysis','candidate')}
                value['analysis']=wire
                value['host_repo']=repo
                if candidate:
                    value['host_candidate']={k:v for k,v in candidate.items() if k!='messages'}
                    value['host_candidate']['source_ids']=[m.message_id for m in candidate['messages']]
                anchor=min(group['message_ids']); target=candidate['source']['case_key'] if candidate else f'{sources[0].chat_id}:{anchor}'
                self.connection.execute('INSERT OR IGNORE INTO autonomous_plans VALUES(?,?,?,?,?,?)',
                    (sources[0].chat_id,anchor,json.dumps(group['message_ids']),target,
                     json.dumps(value,ensure_ascii=False),self.clock().isoformat()))

    def autonomous_plan(self,chat_id,message_id=None,case_key=None):
        from .classifier import parse_case_analysis
        from .store import _to_message
        with self._lock:
            if case_key is not None:
                row=self.connection.execute('SELECT * FROM autonomous_plans WHERE chat_id=? AND case_key=? ORDER BY anchor_id DESC LIMIT 1',(chat_id,case_key)).fetchone()
            else:
                row=self.connection.execute('''SELECT p.* FROM autonomous_plans p,json_each(p.source_ids) s
                    WHERE p.chat_id=? AND s.value=? LIMIT 1''',(chat_id,message_id)).fetchone()
            if not row:
                return None
            value=json.loads(row['plan']); value['analysis']=parse_case_analysis(json.dumps(value['analysis'],ensure_ascii=False))
            candidate=value.pop('host_candidate',None)
            if candidate:
                candidate['messages']=[_to_message(self.get(chat_id,i)) for i in candidate.pop('source_ids')]
            value['candidate']=candidate
            return value

    def reserve_autonomous_confirmation(self,message,number):
        with self._lock,self.connection:
            prior=self.connection.execute("SELECT * FROM group_notices WHERE chat_id=? AND message_id=? AND status='sent' AND kind='needs_human'",
                (message.chat_id,message.message_id)).fetchone()
            if not prior:
                return False
            return self.connection.execute("INSERT OR IGNORE INTO autonomous_confirmations VALUES(?,?,?,'reserved',NULL,?)",
                (message.chat_id,message.message_id,number,self.clock().isoformat())).rowcount==1

    def finish_autonomous_confirmation(self,message,number,notice_id):
        with self._lock,self.connection:
            sent=type(notice_id) is int and notice_id>0
            self.connection.execute("UPDATE autonomous_confirmations SET status=?,notice_message_id=? WHERE chat_id=? AND message_id=? AND issue_number=? AND status='reserved'",
                ('sent' if sent else 'ambiguous',notice_id if sent else None,message.chat_id,message.message_id,number))

    def autonomy_notice_reply_target(self,chat_id,message_id):
        with self._lock:
            row=self.connection.execute("SELECT message_id FROM autonomous_confirmations WHERE chat_id=? AND notice_message_id=? AND status='sent'",
                                        (chat_id,message_id)).fetchone()
            return row[0] if row else message_id

    def autonomy_candidates(self, message, repo):
        """Exact reply first regardless of age, then bounded recent same-chat topics."""
        from .store import _to_message
        with self._lock:
            direct_source=self.followup_source(message)
            mapped=self.autonomy_notice_reply_target(message.chat_id,message.reply_to_id) if message.reply_to_id else None
            if mapped!=message.reply_to_id:
                from dataclasses import replace
                direct_source=self.followup_source(replace(message,reply_to_id=mapped))
            target = self.group_notice_reply_target(message.chat_id,mapped) if mapped else None
            target = target or message.reply_to_id
            direct = self.get(message.chat_id,target) if target else None
            keys = []
            if direct_source:
                keys.append(direct_source['root_key'])
            elif direct and direct['case_key']:
                keys.append(direct['group_key'] or direct['case_key'])
            for episode in self.connection.execute('''SELECT e.root_key FROM followup_episode_sources e
                JOIN messages m ON m.chat_id=e.chat_id AND m.message_id=e.message_id
                WHERE e.chat_id=? ORDER BY m.received_at DESC,m.message_id DESC LIMIT 20''',(message.chat_id,)):
                if episode['root_key'] not in keys:
                    keys.append(episode['root_key'])
            recent = self.connection.execute('''SELECT group_key,case_key FROM messages WHERE chat_id=?
                AND case_key IS NOT NULL AND decision IN ('issue','needs_human','pending_review','followup')
                ORDER BY received_at DESC,message_id DESC LIMIT 80''',(message.chat_id,)).fetchall()
            for row in recent:
                key = row['group_key'] or row['case_key']
                if key not in keys:
                    keys.append(key)
            result=[]
            for key in keys:
                selected=direct_source if direct_source and key==direct_source['root_key'] else None
                episode=self.connection.execute('SELECT source_ids FROM followup_episodes WHERE root_key=?',(key,)).fetchone()
                if episode:
                    rows=[self.get(message.chat_id,i) for i in json.loads(episode[0])]
                    rows=[r for r in rows if r is not None]
                else:
                    member_key=(selected['group_key'] or selected['case_key']) if selected else key
                    rows=self.connection.execute('''SELECT * FROM messages WHERE chat_id=?
                    AND (group_key=? OR (group_key IS NULL AND case_key=?))
                    AND decision!='followup' ORDER BY message_id LIMIT 21''',
                    (message.chat_id,member_key,member_key)).fetchall()
                if not rows or len(rows)>20:
                    continue
                if result and sum(len(c['messages']) for c in result)+len(rows)>20:
                    continue
                first=rows[0]
                pending=all(r['decision']=='pending_review' and r['issue_number'] is None for r in rows)
                issue=first['issue_number']
                if not pending and (type(issue) is not int or first['issue_url']!=f'https://github.com/{repo}/issues/{issue}'):
                    continue
                root_key=selected['root_key'] if selected else key if episode else f"{message.chat_id}:{key}"
                root=self.connection.execute('SELECT * FROM followup_roots WHERE root_key=?',(root_key,)).fetchone()
                source=dict(root) if root else dict(root_key=root_key,chat_id=message.chat_id,
                    source_message_id=first['message_id'],group_key=first['group_key'],case_key=first['case_key'],
                    issue_number=issue,siblings='[]')
                if selected:
                    source=selected
                if any(c['source']['root_key']==source['root_key'] for c in result):
                    continue
                # Retain recent exchange context ('app hay web?' -> 'cả 2'), not just intake.
                history=self.connection.execute('''SELECT m.* FROM followup_jobs j
                    LEFT JOIN followup_job_sources s ON s.job_id=j.id
                    JOIN messages m ON m.chat_id=j.chat_id AND m.message_id=COALESCE(s.message_id,j.message_id)
                    LEFT JOIN followup_episode_sources e ON e.chat_id=m.chat_id AND e.message_id=m.message_id
                    WHERE j.root_key=? AND (e.root_key IS NULL OR e.root_key=j.root_key)
                    ORDER BY j.id DESC,m.message_id DESC LIMIT 5''',(source['root_key'],)).fetchall()
                known={r['message_id'] for r in rows}
                for old in reversed(history):
                    if old['message_id'] not in known and len(rows)<20:
                        rows=list(rows)+[old]; known.add(old['message_id'])
                pending_questions=[]
                for job in self.connection.execute('SELECT plan FROM followup_jobs WHERE root_key=? AND plan IS NOT NULL ORDER BY id DESC LIMIT 3',(source['root_key'],)):
                    plan=json.loads(job['plan'])
                    if plan.get('action')=='clarify':
                        pending_questions.extend(str(q)[:180] for q in plan.get('questions',[])[:3])
                clarification_outcomes=self.autonomous_clarification_context(source['case_key'])
                pending_questions=[v['question'] for v in clarification_outcomes]+pending_questions
                result.append(dict(candidate_id=f'c{len(result)+1}',key=key,pending=pending,
                    exact_reply=bool(selected or not direct_source and direct and (direct['group_key'] or direct['case_key'])==key),
                    split=bool(source.get('split_plan') or source.get('siblings') not in (None,'[]')),
                    current_scopes=self.current_topic_context(source['root_key']),
                    pending_questions=list(dict.fromkeys(pending_questions))[:3],
                    clarification_outcomes=[{k:v for k,v in item.items() if k!='question'} for item in clarification_outcomes],
                    source=source,messages=[_to_message(r) for r in rows],
                    title=first['title'] or '',summary=first['summary'] or ''))
                if len(result)==6:
                    break
            return result

    def autonomy_route(self,messages,action,candidate,case_key,reason):
        from .thinking import CURRENT
        turn=CURRENT.get()
        with self._lock,self.connection:
            for message in messages:
                prior=self.connection.execute('SELECT action,candidate_key,case_key,reason_code FROM autonomous_routes WHERE chat_id=? AND message_id=?',
                    (message.chat_id,message.message_id)).fetchone()
                signature=(action,candidate['key'] if candidate else None,case_key,reason)
                if prior:
                    if tuple(prior)!=signature:
                        raise ValueError('Conflicting autonomous replay')
                    continue
                self.connection.execute('INSERT INTO autonomous_routes VALUES(?,?,?,?,?,?,?,?)',
                    (message.chat_id,message.message_id,self.clock().isoformat(),action,
                     candidate['key'] if candidate else None,case_key,reason,
                     json.dumps(dict(harness_owner='AlexChu',source_ids=[m.message_id for m in messages],
                        decision_turn_key=turn.key if turn else None,
                        exact_reply=bool(candidate and candidate['exact_reply'])),sort_keys=True)))

    def autonomy_case(self,messages,candidate=None):
        """Assign a validated partition atomically. Only no-ticket pending cases can reopen."""
        with self._lock,self.connection:
            first=min(messages,key=lambda m:m.message_id)
            key=candidate['source']['case_key'] if candidate else f'{first.chat_id}:{first.message_id}'
            group=candidate['source']['group_key'] if candidate else key
            if candidate:
                case=self._case(key)
                if not candidate['pending'] or case is None or case['status']!='pending_review':
                    raise ValueError('Pending candidate changed')
                self.connection.execute("UPDATE cases SET status='analyzing',decision=NULL,closed_at=NULL WHERE case_key=?",(key,))
                self.connection.execute("UPDATE groups SET status='planned',decision=NULL WHERE group_key=?",(group,))
                self.connection.execute("UPDATE messages SET decision='in_case' WHERE chat_id=? AND case_key=? AND decision='pending_review'",
                                        (first.chat_id,key))
            else:
                self.connection.execute('''INSERT INTO cases(case_key,chat_id,first_message_id,status,
                    created_at,last_message_at) VALUES(?,?,?,'analyzing',?,?)''',
                    (key,first.chat_id,first.message_id,first.received_at.isoformat(),first.received_at.isoformat()))
                self.connection.execute('''INSERT INTO groups(group_key,case_key,chat_id,first_message_id,
                    message_count,status,created_at) VALUES(?,?,?,?,0,'planned',?)''',
                    (group,key,first.chat_id,first.message_id,self.clock().isoformat()))
            for message in messages:
                changed=self.connection.execute("""UPDATE messages SET case_key=?,group_key=?,decision='in_case'
                    WHERE chat_id=? AND message_id=? AND case_key IS NULL AND decision='classifying'""",
                    (key,group,message.chat_id,message.message_id))
                if changed.rowcount!=1:
                    raise ValueError('Ingress source already assigned')
            count=self.connection.execute('SELECT COUNT(*) FROM messages WHERE case_key=? AND decision!=?',(key,'followup')).fetchone()[0]
            self.connection.execute('UPDATE cases SET message_count=?,last_message_at=? WHERE case_key=?',
                (count,max(m.received_at for m in messages).isoformat(),key))
            self.connection.execute('UPDATE groups SET message_count=? WHERE group_key=?',(count,group))
            self.project_topics(first.chat_id,key)
            return key,group,self.case_messages(key)
