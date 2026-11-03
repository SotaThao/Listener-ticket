"""Narrow operator-authorized pending transition on an offline copy.

This does not adjudicate or retry unknown external operations. Only an explicitly
attested attachment with a durable pre-write error is permitted as history.
"""
import hashlib
import json
import sqlite3
from pathlib import Path

from .rules_pending_store import RulesHalt


def _digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def _sidecars(path):
    return any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal'))


def _reject_triggers(connection):
    safe={}
    for stem,table,message in (('autonomous_routes','autonomous_routes','immutable autonomous route'),
            ('triage_topic_events','triage_topic_events','immutable topic event'),
            ('current_topic_events','triage_current_topic_events','immutable current topic event')):
        for action in ('update','delete'):
            name=stem+'_no_'+action
            safe[name]=f"create trigger {name} before {action} on {table} begin select raise(abort,'{message}'); end"
    for name,sql in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"):
        if not isinstance(sql,str) or ' '.join(sql.split()).casefold()!=safe.get(name):
            raise RulesHalt('offline copy has unsupported triggers; manual schema reconciliation required')


def _stage_receipt(evidence_sha,attestation,original_state):
    payload=json.dumps(['legacy.stage/v2',evidence_sha,attestation,original_state],ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _source_evidence(connection,tables,source_key=None):
    """Hash all original source/attachment fields except the two staged fields."""
    columns=[r[1] for r in connection.execute('PRAGMA table_info(messages)') if r[1] not in ('decision','reason')]
    selected=','.join('"'+c.replace('"','""')+'"' for c in columns)
    message_rows=connection.execute('SELECT '+selected+' FROM messages ORDER BY chat_id,message_id').fetchall()
    payload={'message_columns':columns,'messages':message_rows}
    if 'triage_topic_hydration' in tables:
        payload['triage_topic_hydration']=connection.execute('SELECT * FROM triage_topic_hydration ORDER BY singleton').fetchall()
    if source_key is not None and len(message_rows)>1:
        payload['other_decisions']=connection.execute('''SELECT chat_id,message_id,decision,reason FROM messages
            WHERE NOT (chat_id=? AND message_id=?) ORDER BY chat_id,message_id''',source_key).fetchall()
    if 'attachments' in tables:
        attachment_columns=[r[1] for r in connection.execute('PRAGMA table_info(attachments)')]
        payload.update(attachment_columns=attachment_columns,attachments=connection.execute('SELECT * FROM attachments ORDER BY id').fetchall())
    def encode(value):
        if isinstance(value,bytes): return {'sqlite_blob_hex':value.hex()}
        raise TypeError('unsupported SQLite value')
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=encode).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _check_inert_messages(connection,source_key):
    """Keep unrelated, unlinked text-only CHAT/noise history without replay."""
    rows=connection.execute('''SELECT decision,github_called_at,case_key,group_key,has_image,has_document,
        reply_to_id,issue_number,issue_url FROM messages WHERE NOT (chat_id=? AND message_id=?)''',source_key).fetchall()
    if len(rows)>1000 or any(row[0] not in ('chat','noise') or any(row[1:]) for row in rows):
        raise RulesHalt('additional messages require separate reconciliation')


def _check_empty_hydration(connection,tables):
    if 'triage_topic_hydration' in tables and connection.execute('SELECT * FROM triage_topic_hydration').fetchall()!=[(1,0)]:
        raise RulesHalt('topic hydration history requires separate reconciliation')


def _check_prewrite_attachments(connection,tables,manifest,has_image,has_document):
    """Only the explicitly attested, durable pre-write outcome is eligible."""
    ids=manifest.get('prewrite_attachment_ids',[])
    if (not isinstance(ids,list) or any(type(i) is not int or i<=0 for i in ids)
            or ids!=sorted(set(ids))):
        raise RulesHalt('invalid prewrite attachment evidence')
    if 'attachments' not in tables:
        if ids or has_image or has_document: raise RulesHalt('attachment ledger unavailable')
        return
    columns={r[1] for r in connection.execute('PRAGMA table_info(attachments)')}
    if not {'id','chat_id','message_id','status'}<=columns:
        raise RulesHalt('attachment history cannot prove a prewrite outcome')
    rows=connection.execute('SELECT id,chat_id,message_id,status FROM attachments ORDER BY id').fetchall()
    if not rows:
        if ids or has_image or has_document: raise RulesHalt('attachment evidence missing')
        return
    if (len(rows)!=1 or not (bool(has_image) != bool(has_document)) or [r[0] for r in rows]!=ids
            or any(chat!=manifest['chat_id'] or mid!=manifest['message_id'] or status!='prewrite_error'
                   for _,chat,mid,status in rows)):
        raise RulesHalt('attachment outcome requires separate reconciliation')


def transition_pristine(path, manifest):
    """Stage one legacy message for manual review; never start the bot.

    The caller must supply an offline copy and an explicit, content-free
    attestation. A database digest is compared under a SQLite write lock.
    """
    path=Path(path)
    required={'offline_copy','sha256','chat_id','message_id','decision','operator','reason'}
    if (not isinstance(manifest,dict) or not required<=set(manifest) or set(manifest)-required-{'prewrite_attachment_ids','reviewed_context'} or manifest['offline_copy'] is not True
            or ('reviewed_context' in manifest and manifest['reviewed_context'] is not True)
            or type(manifest['chat_id']) is not int or type(manifest['message_id']) is not int
            or manifest['message_id']<=0 or manifest['decision'] not in ('queued','pending','pending_review')
            or not isinstance(manifest['sha256'],str) or len(manifest['sha256'])!=64
            or not isinstance(manifest['operator'],str) or not manifest['operator'].strip()
            or not isinstance(manifest['reason'],str) or not manifest['reason'].strip()
            or len(manifest['operator'])>120 or len(manifest['reason'])>500):
        raise RulesHalt('invalid offline transition attestation')
    if not path.is_file() or path.is_symlink():
        raise RulesHalt('offline copy missing or is a symbolic link')
    if _sidecars(path):
        raise RulesHalt('offline copy has SQLite sidecars; create a consistent offline copy')
    connection=None
    try:
        if _digest(path)!=manifest['sha256']:
            raise RulesHalt('offline copy digest changed')
        connection=sqlite3.connect(path,timeout=0,isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        if _digest(path)!=manifest['sha256']:
            raise RulesHalt('offline copy digest changed')
        if _sidecars(path): raise RulesHalt('offline copy acquired SQLite sidecars')
        _reject_triggers(connection)
        if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RulesHalt('offline copy failed integrity check')
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns={r[1] for r in connection.execute('PRAGMA table_info(messages)')}
        required_columns={'chat_id','message_id','decision','github_called_at','case_key','group_key',
                          'has_image','has_document','received_at','reason','reply_to_id'}
        if not required_columns<=columns:
            raise RulesHalt('legacy message schema cannot prove a pristine source')
        # This initial transition intentionally supports only clean inventories.
        # Historical ledgers require a separate evidence-specific adjudicator.
        _check_empty_hydration(connection,tables)
        for table in tables-{'messages','sqlite_sequence','attachments','triage_topic_hydration'}:
            quoted='"'+table.replace('"','""')+'"'
            if connection.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():
                raise RulesHalt('history requires evidence-specific reconciliation')
        rows=connection.execute('SELECT chat_id,message_id,decision,github_called_at,case_key,group_key,has_image,has_document,received_at,reply_to_id '
            "FROM messages WHERE decision IN ('queued','classifying','in_case','pending','pending_review','error')").fetchall()
        expected=(manifest['chat_id'],manifest['message_id'],manifest['decision'])
        if (len(rows)!=1
                or rows[0][:3]!=expected or any(rows[0][3:6]) or rows[0][9] is not None):
            raise RulesHalt('pending source has unproven work or does not match attestation')
        source_key=(manifest['chat_id'],manifest['message_id'])
        _check_inert_messages(connection,source_key)
        if connection.execute('SELECT count(*) FROM messages').fetchone()[0]>1 and manifest.get('reviewed_context') is not True:
            raise RulesHalt('operator must review retained context before staging')
        _check_prewrite_attachments(connection,tables,manifest,rows[0][6],rows[0][7])
        original=connection.execute('SELECT decision,reason FROM messages WHERE chat_id=? AND message_id=?',source_key).fetchone()
        if original is None or not isinstance(original[0],str) or original[1] is not None and not isinstance(original[1],str):
            raise RulesHalt('original source state cannot be archived')
        original_state=json.dumps(dict(version=1,decision=original[0],reason=original[1]),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        evidence_sha=_source_evidence(connection,tables,source_key)
        # No terminal message may hide a write reservation either.
        if connection.execute('SELECT 1 FROM messages WHERE github_called_at IS NOT NULL OR issue_number IS NOT NULL OR issue_url IS NOT NULL LIMIT 1').fetchone():
            raise RulesHalt('message ledger contains external-write evidence')
        connection.execute('''CREATE TABLE IF NOT EXISTS rules_topics (
            topic_id TEXT PRIMARY KEY, source_chat_id INTEGER NOT NULL,last_at TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,attempt_reserved INTEGER NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'NEW',asked TEXT NOT NULL DEFAULT '{}',fallback_asked INTEGER NOT NULL DEFAULT 0)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS rules_sources (
            topic_id TEXT NOT NULL,ref INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
            PRIMARY KEY(topic_id,ref),UNIQUE(topic_id,chat_id,message_id))''')
        connection.execute('''CREATE TABLE IF NOT EXISTS rules_legacy_transitions (
            chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,source_sha256 TEXT NOT NULL,
            attestation TEXT NOT NULL,source_evidence_sha256 TEXT NOT NULL,original_state TEXT NOT NULL,PRIMARY KEY(chat_id,message_id))''')
        if not {'source_evidence_sha256','original_state'} <= {r[1] for r in connection.execute('PRAGMA table_info(rules_legacy_transitions)')}:
            raise RulesHalt('legacy transition ledger lacks source evidence')
        topic=f"{manifest['chat_id']}:{manifest['message_id']}"
        if connection.execute("INSERT INTO rules_topics(topic_id,source_chat_id,last_at,status,phase) VALUES(?,?,?,'OPEN','LEGACY_REVIEW')",
                              (topic,manifest['chat_id'],rows[0][8])).rowcount!=1:
            raise RulesHalt('staging topic was not persisted')
        if connection.execute('INSERT INTO rules_sources VALUES(?,1,?,?)',(topic,manifest['chat_id'],manifest['message_id'])).rowcount!=1:
            raise RulesHalt('staging source was not persisted')
        if connection.execute("UPDATE messages SET decision='pending_review',reason='legacy_reconciled_pending' WHERE chat_id=? AND message_id=?",
                              (manifest['chat_id'],manifest['message_id'])).rowcount!=1:
            raise RulesHalt('staging message was not persisted')
        audit=json.dumps(manifest,ensure_ascii=False,sort_keys=True)
        if connection.execute('INSERT INTO rules_legacy_transitions VALUES(?,?,?,?,?,?)',
            (manifest['chat_id'],manifest['message_id'],manifest['sha256'],audit,evidence_sha,original_state)).rowcount!=1:
            raise RulesHalt('staging attestation was not persisted')
        stored=connection.execute('''SELECT t.source_chat_id,t.status,t.phase,s.ref,s.chat_id,s.message_id,m.decision,m.reason,
            l.source_sha256,l.attestation,l.source_evidence_sha256,l.original_state FROM rules_topics t JOIN rules_sources s ON s.topic_id=t.topic_id
            JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id JOIN rules_legacy_transitions l
            ON l.chat_id=m.chat_id AND l.message_id=m.message_id WHERE t.topic_id=?''',(topic,)).fetchone()
        expected=(manifest['chat_id'],'OPEN','LEGACY_REVIEW',1,manifest['chat_id'],manifest['message_id'],
                  'pending_review','legacy_reconciled_pending',manifest['sha256'],audit,evidence_sha,original_state)
        staged_topic=connection.execute('SELECT source_chat_id,last_at,status,attempts,attempt_reserved,phase,asked,fallback_asked FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
        if (stored!=expected or staged_topic!=(manifest['chat_id'],rows[0][8],'OPEN',0,0,'LEGACY_REVIEW','{}',0) or
                _source_evidence(connection,tables,source_key)!=evidence_sha or
                connection.execute('SELECT count(*) FROM rules_topics').fetchone()[0]!=1 or
                connection.execute('SELECT count(*) FROM rules_sources').fetchone()[0]!=1 or
                connection.execute('SELECT count(*) FROM rules_legacy_transitions').fetchone()[0]!=1):
            raise RulesHalt('staging evidence was not persisted')
        connection.execute('COMMIT')
        return {'transitioned':True,'outcome':'STAGED_REVIEW_ONLY','functional_verified':False,'stage_receipt_version':'legacy.stage/v2','topic_id':topic,'stage_receipt_sha256':_stage_receipt(evidence_sha,audit,original_state),'external_effects_attempted':False}
    except (sqlite3.Error,OSError) as error:
        raise RulesHalt('offline transition failed; no replay authorized') from error
    finally:
        if connection is not None:
            if connection.in_transaction: connection.execute('ROLLBACK')
            connection.close()


def release_pristine(path, manifest):
    """Operator explicitly releases a staged source for normal rules processing."""
    from datetime import datetime, timezone
    path=Path(path)
    required={'offline_copy','sha256','chat_id','message_id','operator','reason','release_for_rules','stage_receipt_sha256'}
    if (not isinstance(manifest,dict) or not required<=set(manifest) or set(manifest)-required-{'reviewed_context'} or manifest['offline_copy'] is not True
            or ('reviewed_context' in manifest and manifest['reviewed_context'] is not True)
            or manifest['release_for_rules'] is not True or type(manifest['chat_id']) is not int
            or type(manifest['message_id']) is not int or manifest['message_id']<=0
            or not isinstance(manifest['sha256'],str) or len(manifest['sha256'])!=64
            or not isinstance(manifest['stage_receipt_sha256'],str) or len(manifest['stage_receipt_sha256'])!=64
            or not isinstance(manifest['operator'],str) or not manifest['operator'].strip() or len(manifest['operator'])>120
            or not isinstance(manifest['reason'],str) or not manifest['reason'].strip() or len(manifest['reason'])>500):
        raise RulesHalt('invalid offline release attestation')
    if not path.is_file() or path.is_symlink() or _sidecars(path):
        raise RulesHalt('offline copy missing or has SQLite sidecars')
    connection=None
    try:
        if _digest(path)!=manifest['sha256']: raise RulesHalt('offline copy digest changed')
        connection=sqlite3.connect(path,timeout=0,isolation_level=None)
        connection.execute('BEGIN IMMEDIATE')
        if _digest(path)!=manifest['sha256']: raise RulesHalt('offline copy digest changed')
        if _sidecars(path): raise RulesHalt('offline copy acquired SQLite sidecars')
        _reject_triggers(connection)
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _check_empty_hydration(connection,tables)
        for table in tables-{'messages','sqlite_sequence','attachments','triage_topic_hydration','rules_topics','rules_sources','rules_legacy_transitions','rules_legacy_releases'}:
            quoted='"'+table.replace('"','""')+'"'
            if connection.execute('SELECT 1 FROM '+quoted+' LIMIT 1').fetchone():
                raise RulesHalt('history changed; release refused')
        if (connection.execute('SELECT count(*) FROM rules_topics').fetchone()[0]!=1
                or connection.execute('SELECT count(*) FROM rules_sources').fetchone()[0]!=1
                or connection.execute('SELECT 1 FROM messages WHERE github_called_at IS NOT NULL OR issue_number IS NOT NULL OR issue_url IS NOT NULL LIMIT 1').fetchone()):
            raise RulesHalt('source history changed; release refused')
        source_key=(manifest['chat_id'],manifest['message_id'])
        _check_inert_messages(connection,source_key)
        if connection.execute('SELECT count(*) FROM messages').fetchone()[0]>1 and manifest.get('reviewed_context') is not True:
            raise RulesHalt('operator must review retained context before release')
        pending=connection.execute("SELECT chat_id,message_id,decision FROM messages WHERE decision IN ('queued','classifying','in_case','pending','pending_review','error')").fetchall()
        if pending!=[(manifest['chat_id'],manifest['message_id'],'pending_review')]:
            raise RulesHalt('other pending sources need reconciliation before release')
        topic=f"{manifest['chat_id']}:{manifest['message_id']}"
        row=connection.execute('''SELECT t.status,t.phase,m.decision,m.github_called_at,m.case_key,m.group_key,m.reply_to_id
            FROM rules_topics t JOIN rules_sources s ON s.topic_id=t.topic_id AND s.ref=1
            JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
            JOIN rules_legacy_transitions l ON l.chat_id=s.chat_id AND l.message_id=s.message_id
            WHERE t.topic_id=? AND s.chat_id=? AND s.message_id=?''',(topic,manifest['chat_id'],manifest['message_id'])).fetchone()
        if row!=('OPEN','LEGACY_REVIEW','pending_review',None,None,None,None):
            raise RulesHalt('staged source changed; release refused')
        initial_topic=connection.execute('''SELECT source_chat_id,last_at,status,attempts,attempt_reserved,phase,asked,fallback_asked
            FROM rules_topics WHERE topic_id=?''',(topic,)).fetchone()
        received_at=connection.execute('SELECT received_at FROM messages WHERE chat_id=? AND message_id=?',source_key).fetchone()
        if not received_at or initial_topic!=(manifest['chat_id'],received_at[0],'OPEN',0,0,'LEGACY_REVIEW','{}',0):
            raise RulesHalt('staged topic state changed; release refused')
        if 'original_state' not in {r[1] for r in connection.execute('PRAGMA table_info(rules_legacy_transitions)')}:
            raise RulesHalt('original source archive missing; manual reconciliation required')
        try:
            stored=connection.execute('SELECT attestation,source_evidence_sha256,source_sha256,original_state FROM rules_legacy_transitions WHERE chat_id=? AND message_id=?',(manifest['chat_id'],manifest['message_id'])).fetchone()
            attestation=json.loads(stored[0])
            archive=json.loads(stored[3])
            if (not isinstance(archive,dict) or set(archive)!={'version','decision','reason'} or archive['version']!=1
                    or archive['decision']!=attestation.get('decision')
                    or archive['reason'] is not None and not isinstance(archive['reason'],str)):
                raise RulesHalt('original source archive invalid; manual reconciliation required')
        except (TypeError,ValueError,IndexError):
            raise RulesHalt('staged attestation unavailable') from None
        if not isinstance(attestation,dict) or attestation.get('sha256')!=stored[2] or _stage_receipt(stored[1],stored[0],stored[3])!=manifest['stage_receipt_sha256']:
            raise RulesHalt('external staged evidence receipt does not match')
        attachment_flags=connection.execute('SELECT has_image,has_document FROM messages WHERE chat_id=? AND message_id=?',(manifest['chat_id'],manifest['message_id'])).fetchone()
        _check_prewrite_attachments(connection,tables,attestation,*attachment_flags)
        if any(attachment_flags):
            raise RulesHalt('attachment bytes lack immutable evidence; manual reconciliation required')
        if _source_evidence(connection,tables,source_key)!=stored[1]:
            raise RulesHalt('staged source evidence changed; release refused')
        connection.execute('''CREATE TABLE IF NOT EXISTS rules_legacy_releases (
            chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,attestation TEXT NOT NULL,
            PRIMARY KEY(chat_id,message_id))''')
        audit=json.dumps(manifest,ensure_ascii=False,sort_keys=True)
        if connection.execute('INSERT INTO rules_legacy_releases VALUES(?,?,?)',
            (manifest['chat_id'],manifest['message_id'],audit)).rowcount!=1:
            raise RulesHalt('release attestation was not persisted')
        now=datetime.now(timezone.utc).isoformat()
        if connection.execute("UPDATE rules_topics SET phase='HOLD',last_at=? WHERE topic_id=? AND phase='LEGACY_REVIEW'",
                              (now,topic)).rowcount!=1:
            raise RulesHalt('release state was not persisted')
        state=connection.execute('SELECT source_chat_id,last_at,status,attempts,attempt_reserved,phase,asked,fallback_asked FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()
        recorded=connection.execute('SELECT attestation FROM rules_legacy_releases WHERE chat_id=? AND message_id=?',(manifest['chat_id'],manifest['message_id'])).fetchone()
        final_source=connection.execute('SELECT decision,reason FROM messages WHERE chat_id=? AND message_id=?',source_key).fetchone()
        final_binding=connection.execute('SELECT topic_id,ref,chat_id,message_id FROM rules_sources').fetchall()
        final_transition=connection.execute('SELECT attestation,source_evidence_sha256,source_sha256,original_state FROM rules_legacy_transitions WHERE chat_id=? AND message_id=?',source_key).fetchone()
        if (state!=(manifest['chat_id'],now,'OPEN',0,0,'HOLD','{}',0) or recorded!=(audit,) or
                final_source!=('pending_review','legacy_reconciled_pending') or
                final_binding!=[(topic,1,*source_key)] or final_transition!=stored or
                connection.execute('SELECT count(*) FROM rules_topics').fetchone()[0]!=1 or
                connection.execute('SELECT count(*) FROM rules_legacy_transitions').fetchone()[0]!=1 or
                connection.execute('SELECT count(*) FROM rules_legacy_releases').fetchone()[0]!=1 or
                _source_evidence(connection,tables,source_key)!=stored[1]):
            raise RulesHalt('release evidence was not persisted')
        connection.execute('COMMIT')
        return {'released':True,'outcome':'ARMED_HOLD_ONLY','functional_verified':False,'topic_id':topic,'external_effects_attempted':False}
    except (sqlite3.Error,OSError) as error:
        raise RulesHalt('offline release failed; no replay authorized') from error
    finally:
        if connection is not None:
            if connection.in_transaction: connection.execute('ROLLBACK')
            connection.close()
