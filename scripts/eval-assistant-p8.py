#!/usr/bin/env python3
"""P8 canonical-example preflight against a real local router (no writable tools).

Reuses RulesSnapshot's parsed `examples` table and CodeFacts construction from
its self-check. Seen media use neutral valid PNGs plus canonical descriptions as
image-message captions (source.text), through production partition_and_extract's
images map and pixels_available flag. These are caption-based synthetic fixtures,
not screenshot/OCR evidence. Unseen media have neither pixels nor inferred
captions. The table's image-only example has no phantom text message. JSONL
records this fixture design and per-router-call diagnostics. No production DB
is opened. Default models come from AssistantConfig (flash-medium, then GPT).
Captions come ONLY from the narrative, never expected Dữ kiện facts. X20 has
seven photos without a visual description: their captions remain empty and its
text source retains the narrative POS report. X8 explicitly has no caption.
Other seen media lacking a narrative description are fixture errors with case ID.
"""
import argparse
import json
import re
import struct
from pathlib import Path
import sys
import tempfile
import time
import zlib
from dataclasses import replace
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'listener' / 'src'))

from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_extractor import AssistantExtractor
from tg_triage.config import (AssistantConfig, Config, ModelConfig, OllamaConfig,
                             ProxyConfig, TelegramConfig, _assistant_token, assistant_models)
from tg_triage.decision_rules import CodeFacts
from tg_triage.models import IncomingMessage
from tg_triage.router_transport import validate_base_url
from tg_triage.rules_extraction import partition_and_extract, parse_grouped_facts
from tg_triage.store import Store


def cases(snapshot):
    """Use the canonical parsed table, not a second Markdown parser or case list."""
    return tuple(snapshot.tables['examples']['examples'])


def code_facts(row):
    seen, unseen = int(row.cells['media_seen']), int(row.cells['media_unseen'])
    images = frozenset(range(2, 2 + seen + unseen))
    text_ids = frozenset({1}) if not images or row.one('text') == 'có' or row.one('command') != '∅' else frozenset()
    return CodeFacts(row.one('command'), row.one('command_payload'), row.one('text'),
        row.one('extraction_ok'), row.one('reply_target'), images | text_ids, images,
        frozenset(range(2, 2 + seen)), text_ids, frozenset())


def _png():
    """One neutral RGB pixel; actual description lives in the production caption."""
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xc0\xc0\xc0')) + chunk(b'IEND', b''))


def media_description(row):
    """Parse narrative media only; never expose expected extraction facts."""
    narrative = row.cells['Tin (rút gọn)'].replace('“', '"').replace('”', '"')
    # A plus inside a quoted caption/code span is not a source separator.
    parts = re.split(r'\s+\+\s+(?=(?:[^"`]*["`][^"`]*["`])*[^"`]*$)', narrative)
    media = next((p for p in parts if re.search(r'\bảnh\b', p, re.I)), '')
    description = re.sub(r'^\s*(?:(?:một|\d+)\s+)?ảnh\s*', '', media, flags=re.I)
    description = re.sub(r'\s*\([^)]*đã xem[^)]*\)\s*$', '', description).strip()
    if re.fullmatch(r',?\s*không chữ', description, re.I):
        return ''
    if description:
        return description.replace('`', '').replace('"', '').strip()
    if (row.cells['#'] == 'X20' and media.strip() == '7 ảnh (5 đã xem, 2 chưa xem)'
            and parts[0].strip() == '"Máy in POS in ra giấy trắng"'):
        return ''  # Canonical narrative specifies counts, not photo contents.
    raise FixtureError('P8 fixture ' + row.cells['#'] + ': seen image lacks narrative description')


def build_sources(row, index, directory):
    """Return production-shaped sources and seen image paths for one example."""
    facts = code_facts(row)
    narrative = row.cells['Tin (rút gọn)'].replace('“', '"').replace('”', '"')
    source = IncomingMessage(chat_id=-10000-index, message_id=1,
        received_at=datetime.now(timezone.utc), sender_id=1,
        sender_name='P8 synthetic example', text=narrative)
    if not facts.image_ids:
        return [source], {}, dict(kind='text_only', seen_ids=[], unseen_ids=[])
    parts = re.split(r'\s+\+\s+(?=(?:[^"`]*["`][^"`]*["`])*[^"`]*$)', narrative)
    text_parts = [p for p in parts if not re.search(r'\bảnh\b', p, re.I)]
    text = ' '.join(match for p in text_parts for match in re.findall(r'"([^"]+)"', p))
    if not text and facts.command != '∅':
        text = facts.command
    sources = [replace(source, text=text)] if 1 in facts.topic_ids else []
    description = media_description(row) if facts.seen_image_ids else ''
    images = {}
    for mid in sorted(facts.image_ids):
        seen = mid in facts.seen_image_ids
        sources.append(replace(source, message_id=mid, text=description if seen else '', has_image=True))
        if seen:
            path = Path(directory) / f'{mid}.png'
            path.write_bytes(_png())
            images[mid] = path
    return sources, images, dict(kind='synthetic_neutral_png_with_canonical_caption',
        seen_ids=sorted(images), unseen_ids=sorted(facts.image_ids - facts.seen_image_ids),
        seen_caption=description, limitation='caption fixture; not screenshot/OCR evidence')


def template(state, intent):
    return intent + '.md' if state == 'READY' and intent in ('bug', 'task', 'feedback') else None


class _EvalExtractor(AssistantExtractor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        chat = self.chat
        self.calls = []
        def measured(model, messages, **kw):
            started = time.monotonic()
            call = dict(link=('gemini', 'gpt')[self.settings.models.index(model)],
                        model=model, timeout_s=kw['timeout'], error_class=None, finish_reason=None)
            try:
                reply = chat(model, messages, **kw)
                call['finish_reason'] = reply.finish_reason
                if reply.finish_reason == 'length':
                    call['error_class'] = 'truncated'
                elif not reply.tool_calls:
                    from tg_triage.assistant import router_proposal_content, schema_rejection_kind
                    if parse_grouped_facts(router_proposal_content(reply.content), self.diagnostic_snapshot, self.diagnostic_ids) is None:
                        call['error_class'] = 'schema'
                        call['schema_error_kind'] = schema_rejection_kind(reply.content)
                return reply
            except Exception as error:
                from tg_triage.assistant import _kind
                call['error_class'] = _kind(error)
                if call['error_class'] == 'schema': call.setdefault('schema_error_kind', 'unavailable')
                raise
            finally:
                call['latency_ms'] = max(0, round((time.monotonic() - started) * 1000))
                self.calls.append(call)
        self.chat = measured

    def _acquire(self):
        # Preflight can wait; the foreground runtime never sleeps on admission.
        while not self.limiter.try_acquire():
            time.sleep(.1)


class _BodyProvider:
    settings = type('Settings', (), {'model': 'assistant-preflight'})()
    think = False
    request_budget_bytes = 24000
    def _ask(self, *args, **kwargs):
        raise AssertionError('preflight must only use the assistant')


class FixtureError(ValueError):
    """Canonical case cannot be represented faithfully; fixed message has ID only."""


def evaluate_case(runtime, snapshot, row, index):
    # Paths are consumed synchronously by runtime.run, and removed afterwards.
    runtime.calls.clear()
    with tempfile.TemporaryDirectory(prefix='p8-media-') as directory:
        return _evaluate_case(runtime, snapshot, row, index, directory)


def _evaluate_case(runtime, snapshot, row, index, directory):
    facts = code_facts(row)
    sources, images, fixture = build_sources(row, index, directory)
    for message in sources:
        runtime.store.enqueue(message)
    provider, bodies = _BodyProvider(), []
    partition_and_extract(provider, snapshot, sources, images=images, body_out=bodies)
    ids = [m.message_id for m in sources]
    job = dict(provider=provider, snapshot=snapshot, sources=sources, body=bodies[0],
               parse=lambda raw: parse_grouped_facts(raw, snapshot, ids),
               data=dict(images=images, documents=[], read_context=()))
    runtime.calls.clear()
    runtime.diagnostic_snapshot, runtime.diagnostic_ids = snapshot, ids
    result = runtime.run(job)
    links = {}
    for call in runtime.calls:
        item = links.setdefault(call['link'], dict(model=call['model'], error_class=None, fallback_reason=None))
        if call['error_class']:
            item['error_class'] = call['error_class']
            item['fallback_reason'] = item['fallback_reason'] or call['error_class']
    if result and result.link:
        item = links.setdefault(result.link, dict(model=result.model, error_class=None, fallback_reason=None))
        item['error_class'] = result.error_class or item['error_class']
    proposed = (result.proposal or {}).get('groups', []) if result else []
    # Examples describe one subject; multiple groups fail preflight explicitly.
    decision = snapshot.evaluate(proposed[0]['facts'] if len(proposed) == 1 else {},
                                facts if result and result.outcome == 'ok' else replace(facts, extraction_ok='false'))
    expected = row.one('Kết quả')
    # Same intent selection as the canonical self-check (commands override model intent).
    reference_intent = {'R9': 'bug', 'R10': 'task', 'R11': 'feedback'}.get(row.cells['Luật'], row.one('intent (model)'))
    expected_template = template(expected, reference_intent)
    actual_template = template(decision.state, decision.intent)
    return dict(case=row.cells['#'], expected_state=expected, actual_state=decision.state,
        expected_template=expected_template, actual_template=actual_template,
        passed=(decision.state == expected and actual_template == expected_template and len(proposed) <= 1),
        decision_rule=decision.rule, model=result.model if result else None,
        model_calls=result.model_calls if result else 0,
        error_class=result.error_class if result else 'unavailable',
        proposal_link=result.link if result and result.proposal else None,
        fallback_reason=result.fallback_reason if result else None,
        calls=list(runtime.calls), links=links, media_fixture=fixture,
        pack_fingerprint=runtime.store.assistant_pack_fingerprint)


def run_eval(*, router_url, key_file, output, models=None, chat=None):
    validate_base_url(router_url)
    key_path = Path(key_file)
    if not key_path.is_absolute():
        raise ValueError('key file must be absolute')
    key = _assistant_token(str(key_path))
    if not key:
        raise ValueError('router key unavailable')
    snapshot, pack = _load_decision_rules(), _load_assistant_pack()
    selected = assistant_models(list(models or AssistantConfig.models))
    rows = []
    with tempfile.TemporaryDirectory(prefix='assistant-p8-') as directory:
        db = str(Path(directory) / 'examples.db')
        store = Store(db)
        try:
            store.assistant_pack_fingerprint = pack.fingerprint
            config = Config('dry-run', TelegramConfig('unused', (), 0, ProxyConfig()),
                OllamaConfig('http://127.0.0.1:11434', 'unused', 1, .7), 1, db, 1, '0'*32,
                model=ModelConfig(fallback='none'),
                assistant=AssistantConfig(enabled=True, mode='shadow', router_base_url=router_url,
                    router_key_file=str(key_path), router_key=key, models=selected))
            runtime = _EvalExtractor(config, store, pack.text, chat=chat)
            with Path(output).open('w', encoding='utf-8') as artifact:
                for index, row in enumerate(cases(snapshot), 1):
                    try:
                        result = evaluate_case(runtime, snapshot, row, index)
                    except FixtureError:
                        raise
                    except Exception as error:
                        result = dict(case=row.cells['#'], expected_state=row.one('Kết quả'),
                            actual_state='ERROR', expected_template=None, actual_template=None,
                            passed=False, error_class=type(error).__name__, model=None,
                            proposal_link=None, fallback_reason=None, calls=list(runtime.calls),
                            links={}, media_fixture={'kind': 'fixture_or_turn_error'})
                    rows.append(result)
                    artifact.write(json.dumps(result, ensure_ascii=False) + '\n')
                    artifact.flush()
                    print(f"{result['case']}: expected {result['expected_state']}/{result['expected_template']} "
                          f"actual {result['actual_state']}/{result['actual_template']} "
                          f"{'PASS' if result['passed'] else 'FAIL'}")
        finally:
            store.close()
    total = sum(row['passed'] for row in rows)
    print(f'{total}/{len(rows)}')
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--router-url', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--output', required=True, help='JSONL artifact path')
    parser.add_argument('--models', nargs=2, metavar=('PRIMARY', 'SECONDARY'))
    args = parser.parse_args(argv)
    try:
        rows = run_eval(router_url=args.router_url, key_file=args.key_file,
                        output=args.output, models=args.models)
        return 0 if all(row['passed'] for row in rows) else 1
    except FixtureError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('P8 failed: ' + type(error).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
