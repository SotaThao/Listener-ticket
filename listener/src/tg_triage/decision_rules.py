"""Validated, immutable snapshot of the decision rules (not wired into intake)."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

FILES = ("states", "facts", "readiness", "questions", "channels", "examples")
SCHEMA = {
    "states": {"states": "Mã|Nghĩa|Ticket|Group gốc|Group xử lý|Pending|Rule nghiệp vụ", "pending_states": "Từ|Sự kiện|Sang", "limits": "Khóa|Giá trị|Nghĩa"},
    "facts": {"fields": "Trường|Kiểu|Ý nghĩa|Ví dụ", "code_facts": "Trường|Giá trị|Cách tính", "command_intent": "Command|intent", "field_valid": "#|Điều kiện", "normalize": "#|Bước", "generic_values": "Trường|Cụm", "limits": "Khóa|Giá trị|Nghĩa"},
    "readiness": {"intent_resolve": "#|command|intent (model)|cần CÓ|intent dùng", "decision": "#|command|command_payload|text|media|extraction_ok|reply_target|intent|cần CÓ tất cả|cần CÓ ít nhất một|Kết quả|Ghi chú", "never_blocks": "Thứ không chặn", "unseen_media": "#|Quy tắc", "confidence": "#|Quy tắc"},
    "questions": {"ask_order": "Thứ tự|intent|Trường thiếu|Khối template", "fallback": "Khi|Khối template", "answer_link": "#|Kênh|Là Reply vào|Pending|Kết quả", "limits": "Khóa|Giá trị|Nghĩa"},
    "channels": {"channels": "Kênh|Là gì|Config|Bot được gửi", "outputs": "Trạng thái|Group gốc|Group xử lý|Khối template", "ops_message_fields": "Trường|Nguồn", "blocks": "Khối template|File|Có sẵn"},
    "examples": {"examples": "#|Tin (rút gọn)|command|command_payload|text|media_seen|media_unseen|extraction_ok|reply_target|intent (model)|Dữ kiện|Kết quả|Luật|Hỏi", "normalize_cases": "#|Trường|Giá trị|Chung chung"},
}
HEADER = re.compile(r"^<!-- rule-file: ([a-z_]+) · version ([1-9][0-9]*) · status (DRAFT|ACTIVE) · audience: (code|model \+ code|người \+ agent) -->$")
TABLE = re.compile(r"^## Bảng: ([a-z_]+)$")
TICKS = re.compile(r"`([^`]+)`")
EDGE = ".,!?;:…\"'“”‘’()[]-"


class RulesError(ValueError):
    """Rule file error, including source location."""


def normalize(value: str) -> str:
    value = " ".join(unicodedata.normalize("NFC", value).casefold().split()).strip(EDGE).strip()
    words = value.split(" ")
    return " ".join(words[1:]) if len(words) > 1 and words[0] in {"em", "anh", "chị", "mình", "bạn"} else value


@dataclass(frozen=True)
class Row:
    path: str
    line: int
    cells: Mapping[str, str]

    def fail(self, message: str):
        raise RulesError(f"{self.path}:{self.line}: {message}")

    def one(self, key: str) -> str:
        vals = self.values(key)
        if len(vals) != 1:
            self.fail(f"{key}: expected one quoted value")
        return vals[0]

    def values(self, key: str) -> tuple[str, ...]:
        cell = self.cells[key]
        vals = TICKS.findall(cell)
        if not vals or TICKS.sub("", cell).strip():
            self.fail(f"{key}: invalid quoted values")
        if len(vals) != len(set(vals)):
            self.fail(f"{key}: duplicate value")
        return tuple(vals)


def _parse(path: Path, name: str):
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        raise RulesError(f"{path}:1: {exc}") from exc
    lines = text.splitlines()
    header = HEADER.fullmatch(lines[0]) if lines else None
    if not header or header[1] != name or header[4] != ("model + code" if name == "facts" else "code"):
        raise RulesError(f"{path}:1: invalid rule header")
    if header[3] != "ACTIVE":
        raise RulesError(f"{path}:1: rules must be ACTIVE")
    if "CHỜ OWNER CHỐT" in text:
        raise RulesError(f"{path}:{next(i for i, line in enumerate(lines, 1) if 'CHỜ OWNER CHỐT' in line)}: unresolved owner decision")
    tables = {}
    for i, line in enumerate(lines):
        match = TABLE.fullmatch(line)
        if not match:
            if line.startswith("## Bảng:"):
                raise RulesError(f"{path}:{i+1}: malformed table heading")
            continue
        table = match[1]
        if table in tables or table not in SCHEMA[name]:
            raise RulesError(f"{path}:{i+1}: duplicate or unknown table {table}")
        j = i + 1
        while j < len(lines) and not lines[j].startswith("|") and not lines[j].startswith("#"):
            j += 1
        def cells(s):
            return [v.strip() for v in s.strip().strip("|").split("|")]
        expected = SCHEMA[name][table].split("|")
        if j + 1 >= len(lines) or cells(lines[j]) != expected:
            raise RulesError(f"{path}:{j+1}: invalid columns in {table}")
        sep = cells(lines[j+1])
        if len(sep) != len(expected) or any(not re.fullmatch(r":?-{3,}:?", x) for x in sep):
            raise RulesError(f"{path}:{j+2}: invalid table separator")
        rows = []
        j += 2
        while j < len(lines) and lines[j].startswith("|"):
            values = cells(lines[j])
            if len(values) != len(expected):
                raise RulesError(f"{path}:{j+1}: wrong number of cells")
            rows.append(Row(str(path), j+1, MappingProxyType(dict(zip(expected, values)))))
            j += 1
        if not rows:
            raise RulesError(f"{path}:{i+1}: empty table {table}")
        tables[table] = tuple(rows)
    if set(tables) != set(SCHEMA[name]):
        raise RulesError(f"{path}:1: missing required tables {set(SCHEMA[name]) - set(tables)}")
    return text, MappingProxyType(tables)


@dataclass(frozen=True)
class Field:
    value: str
    source_ids: tuple[int, ...]


@dataclass(frozen=True)
class CodeFacts:
    command: str
    command_payload: str
    text: str
    extraction_ok: str
    reply_target: str
    topic_ids: frozenset[int]
    image_ids: frozenset[int]
    seen_image_ids: frozenset[int]
    text_source_ids: frozenset[int]
    document_ids: frozenset[int]


@dataclass(frozen=True)
class Decision:
    state: str
    intent: str
    intent_rule: str
    rule: str
    present: Mapping[str, Field]
    questions: tuple[str, ...]
    asked_fields: tuple[str, ...]
    fingerprint: str


class RulesSnapshot:
    """Load once; never mutate or read rule files during evaluation."""
    __slots__ = ('tables','fingerprint','_instructions','text_fields','intents','commands',
                 'domains','generic','limits','model_fields','model_instructions','_frozen')
    def __init__(self, rules_dir: Path, templates_dir: Path):
        tables, texts = {}, {}
        for name in FILES:
            texts[name], tables[name] = _parse(Path(rules_dir) / f"{name}.md", name)
        self.tables = MappingProxyType(tables)
        digest = hashlib.sha256()
        for name in sorted(FILES):
            digest.update((name + ".md").encode()); digest.update(texts[name].encode("utf-8"))
        self.fingerprint = digest.hexdigest()
        self._instructions = self._extract_instructions(texts['facts'], Path(rules_dir) / 'facts.md')
        self._validate(Path(templates_dir))
        self._self_check()
        object.__setattr__(self, '_frozen', True)

    def __setattr__(self, key, value):
        if getattr(self, '_frozen', False): raise AttributeError('rules snapshot is immutable')
        object.__setattr__(self, key, value)

    def __delattr__(self, key):
        if getattr(self, '_frozen', False): raise AttributeError('rules snapshot is immutable')
        object.__delattr__(self, key)

    @staticmethod
    def _extract_instructions(text, path):
        match = re.search(r'(?ms)^Cách model ghi giá trị:\s*\n(.*?)\n## Bảng:', text)
        if not match: raise RulesError(f'{path}:1: missing model extraction instructions')
        return match.group(1).strip()

    def _validate(self, templates: Path):
        t = self.tables
        def unique(rows, column):
            result = {}
            for r in rows:
                key = r.one(column)
                if key in result: r.fail(f"duplicate {column}: {key}")
                result[key] = r
            return result
        states = unique(t['states']['states'], 'Mã')
        if set(states) != {'READY', 'APPEND', 'NEED_INFO', 'MEDIA_ONLY', 'CHAT', 'HOLD'}:
            t['states']['states'][0].fail('unsupported states')
        fields = unique(t['facts']['fields'], 'Trường')
        if set(fields) != {'intent', 'object', 'observed', 'expected', 'work', 'problem', 'proposal', 'media_ids'}:
            t['facts']['fields'][0].fail('unsupported fields')
        self.text_fields = frozenset(fields) - {'intent', 'media_ids'}
        self.intents = frozenset(fields['intent'].values('Kiểu')) if fields['intent'].cells['Kiểu'].startswith('`') else frozenset(TICKS.findall(fields['intent'].cells['Kiểu']))
        if self.intents != {'bug', 'task', 'feedback', 'question', 'chat', 'unknown'}: fields['intent'].fail('unsupported intents')
        for f in self.text_fields:
            if fields[f].cells['Kiểu'] != 'chuỗi': fields[f].fail('unsupported field type')
        if fields['media_ids'].cells['Kiểu'] != 'danh sách ID tin': fields['media_ids'].fail('unsupported field type')
        cf = unique(t['facts']['code_facts'], 'Trường')
        domains = {k: frozenset(TICKS.findall(cf[k].cells['Giá trị'])) for k in ('command_payload','text','media','extraction_ok','reply_target') if k in cf}
        expected_domains = {'command_payload': {'none','text','media','empty'}, 'text': {'có','không'}, 'media': {'có','không'}, 'extraction_ok': {'true','false'}, 'reply_target': {'none','ticket_open','bot_question'}}
        if set(cf) != {'command','command_payload','text','media','media_seen','media_unseen','extraction_ok','reply_target'} or domains != expected_domains:
            t['facts']['code_facts'][0].fail('unsupported code facts')
        commands = unique(t['facts']['command_intent'], 'Command')
        self.commands = MappingProxyType({k: r.one('intent') for k,r in commands.items()})
        if any(v not in self.intents for v in self.commands.values()): t['facts']['command_intent'][0].fail('unknown command intent')
        domains.update(command=frozenset(self.commands) | {'∅'}, intent=self.intents)
        self.domains = MappingProxyType(domains)
        generic = {f: set() for f in self.text_fields}
        for r in t['facts']['generic_values']:
            f, v = r.one('Trường'), normalize(r.one('Cụm'))
            if f not in generic or not v or v in generic[f]: r.fail('invalid or duplicate generic value')
            generic[f].add(v)
        self.generic = MappingProxyType({k: frozenset(v) for k,v in generic.items()})
        for name, codes in [('field_valid', ['V1','V2','V3','V4']), ('normalize', ['N1','N2','N3','N4','N5'])]:
            if [r.cells['#'] for r in t['facts'][name]] != codes: t['facts'][name][0].fail('unsupported algorithm sequence')
            # These prose algorithms are implemented explicitly, not interpreted as code.
            # Reject edits until their semantics have been implemented and reviewed.
            supported = {'field_valid': 'b3d848a75ece489b46112480bef7f61bfd0d38a5acf5756d6796d49b7dc0dad7', 'normalize': '2be762e508c7820cb7f60f3ed7619f19485c68a867f7506d3300f91ccc428c53'}
            encoded = '\n'.join('| ' + ' | '.join(r.cells.values()) + ' |' for r in t['facts'][name]).encode('utf8')
            if hashlib.sha256(encoded).hexdigest() != supported[name]: t['facts'][name][0].fail('unsupported algorithm definition')
        limits = {}
        for file, expected in [('states', {'pending_expiry_days','hold_max_retries'}), ('facts', {'max_value_chars'}), ('questions', {'max_fields_per_ask','max_asks_per_field'})]:
            items = unique(t[file]['limits'], 'Khóa')
            if set(items) != expected: t[file]['limits'][0].fail('invalid limits')
            for k,r in items.items():
                v = r.one('Giá trị')
                if not re.fullmatch(r'[1-9][0-9]*', v): r.fail('invalid positive limit')
                limits[k] = int(v)
        self.limits = MappingProxyType(limits)
        def check_condition(r, col, domain, special='*'):
            if r.cells[col] == special: return
            if not set(r.values(col)) <= set(domain): r.fail(f'{col}: unknown value')
        for table, prefix in [('intent_resolve','I'),('decision','R')]:
            rows = t['readiness'][table]
            if [r.cells['#'] for r in rows] != [f'{prefix}{i}' for i in range(1, len(rows)+1)]: rows[0].fail('nonconsecutive rule codes')
            for r in rows:
                cols = ('command','intent (model)') if table == 'intent_resolve' else tuple(domains)
                for col in cols: check_condition(r, col, domains['intent' if col == 'intent (model)' else col])
                for col in (('cần CÓ',) if table == 'intent_resolve' else ('cần CÓ tất cả','cần CÓ ít nhất một')):
                    check_condition(r, col, self.text_fields, '—')
                if table == 'intent_resolve':
                    if r.one('intent dùng') not in self.intents | {'=model','=command'}: r.fail('invalid intent output')
                elif r.one('Kết quả') not in states: r.fail('unknown state')
        last = t['readiness']['decision'][-1]
        if any(last.cells[c] != '*' for c in domains) or any(last.cells[c] != '—' for c in ('cần CÓ tất cả','cần CÓ ít nhất một')): last.fail('invalid catchall')
        # Every supported combination must resolve to an intent, including absent fields.
        names = tuple(sorted(self.text_fields))
        for command in domains['command']:
            for model_intent in self.intents:
                for mask in range(1 << len(names)):
                    present = {name for i, name in enumerate(names) if mask & (1 << i)}
                    for r in t['readiness']['intent_resolve']:
                        if self._matches(r, 'command', command) and self._matches(r, 'intent (model)', model_intent) and self._all(r, 'cần CÓ', present):
                            target = r.one('intent dùng')
                            resolved = model_intent if target == '=model' else self.commands.get(command) if target == '=command' else target
                            if resolved not in self.intents: r.fail('intent resolution yields invalid intent')
                            break
                    else: t['readiness']['intent_resolve'][-1].fail('intent resolution is not exhaustive')
        outputs = unique(t['channels']['outputs'], 'Trạng thái')
        if set(outputs) != set(states): t['channels']['outputs'][0].fail('outputs must cover each state once')
        blocks = unique(t['channels']['blocks'], 'Khối template')
        used = set()
        order = set()
        for r in t['questions']['ask_order']:
            intent, field, block = r.one('intent'), r.one('Trường thiếu'), r.one('Khối template')
            if intent not in self.intents or field not in self.text_fields or not re.fullmatch(r'[1-9][0-9]*', r.cells['Thứ tự']) or (intent,field) in order: r.fail('invalid ask order')
            order.add((intent,field)); used.add(block)
        for r in t['questions']['fallback']: used.add(r.one('Khối template'))
        for r in outputs.values():
            if r.cells['Khối template'] != '—': used.update(r.values('Khối template'))
        if used != set(blocks): t['channels']['blocks'][0].fail('template block registry mismatch')
        for block,r in blocks.items():
            filename, status = r.one('File'), r.one('Có sẵn')
            if Path(filename).name != filename or status not in {'có','cần thêm'}: r.fail('invalid template reference')
            try: source = (templates / filename).read_text(encoding='utf-8')
            except (OSError, UnicodeError) as e: r.fail(f'template unavailable: {e}')
            if ('{{#block ' + block + '}}' in source) != (status == 'có'): r.fail('template block status mismatch')
        self.model_fields = tuple(r.cells for r in t['facts']['fields'])
        self.model_instructions = self._instructions

    def model_context(self):
        return {'fields': [dict(row) for row in self.model_fields], 'instructions': self.model_instructions,
                'examples': [{k: r.cells[k] for k in ('Tin (rút gọn)', 'intent (model)', 'Dữ kiện')} for r in self.tables['examples']['examples']]}

    def extraction_schema(self):
        field = {'type': 'object', 'properties': {'value': {'type': 'string'}, 'source_ids': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['value', 'source_ids'], 'additionalProperties': False}
        properties = {name: field for name in self.text_fields | {'intent'}}
        properties['media_ids'] = {'type': 'object', 'properties': {'value': {'type': 'array', 'items': {'type': 'integer'}}, 'source_ids': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['value', 'source_ids'], 'additionalProperties': False}
        return {'type': 'object', 'properties': properties, 'additionalProperties': False}

    def evaluate(self, model: Mapping, facts: CodeFacts, asked_counts: Mapping[str, int] | None = None) -> Decision:
        if not isinstance(model, Mapping) or not isinstance(facts, CodeFacts): raise ValueError('invalid input')
        for ids in (facts.topic_ids, facts.image_ids, facts.seen_image_ids, facts.text_source_ids, facts.document_ids):
            if not isinstance(ids, frozenset) or any(type(i) is not int or i <= 0 for i in ids): raise ValueError('invalid source IDs')
        if not facts.seen_image_ids <= facts.image_ids <= facts.topic_ids or not facts.text_source_ids <= facts.topic_ids or not facts.document_ids <= facts.topic_ids: raise ValueError('invalid source boundaries')
        code = {'command': facts.command, 'command_payload': facts.command_payload, 'text': facts.text,
                'media': 'có' if facts.image_ids or facts.document_ids else 'không', 'extraction_ok': facts.extraction_ok, 'reply_target': facts.reply_target}
        for key, value in code.items():
            if value not in self.domains[key]: raise ValueError(f'invalid code fact {key}')
        if (facts.command == '∅') != (facts.command_payload == 'none'): raise ValueError('inconsistent command payload')
        counts = {} if asked_counts is None else asked_counts
        if not isinstance(counts, Mapping) or any(k not in self.text_fields or type(v) is not int or v < 0 for k,v in counts.items()): raise ValueError('invalid asked counts')
        parsed = {}
        for key in self.text_fields | {'intent', 'media_ids'}:
            if key not in model: continue
            item = model[key]
            if not isinstance(item, Mapping) or set(item) != {'value', 'source_ids'} or not isinstance(item['source_ids'], (list, tuple)) or any(type(i) is not int or i <= 0 for i in item['source_ids']): raise ValueError(f'invalid model field {key}')
            value, ids = item['value'], tuple(item['source_ids'])
            if key == 'media_ids':
                if not isinstance(value, (list, tuple)) or any(type(i) is not int or i <= 0 for i in value): raise ValueError('invalid media IDs')
                continue
            if not isinstance(value, str): raise ValueError(f'invalid model value {key}')
            # An unrelated source invalidates the entire field; unseen image-only evidence cannot establish it.
            if not ids or not set(ids) <= facts.topic_ids or not (set(ids) & (facts.text_source_ids | facts.seen_image_ids)): continue
            if key == 'intent': parsed[key] = value if value in self.intents else 'unknown'; continue
            value = value[:self.limits['max_value_chars']]
            normal = normalize(value)
            if normal and normal not in self.generic[key]: parsed[key] = Field(value, ids)
        present = frozenset(parsed) & self.text_fields
        model_intent = parsed.get('intent', 'unknown')
        intent = None
        for r in self.tables['readiness']['intent_resolve']:
            if not self._matches(r, 'command', facts.command) or not self._matches(r, 'intent (model)', model_intent) or not self._all(r, 'cần CÓ', present): continue
            target = r.one('intent dùng')
            intent = model_intent if target == '=model' else self.commands.get(facts.command) if target == '=command' else target
            intent_rule = r.cells['#']; break
        if intent not in self.intents: raise ValueError('no valid intent resolution')
        code['intent'] = intent
        for r in self.tables['readiness']['decision']:
            if not all(self._matches(r, k, v) for k,v in code.items()): continue
            if not self._all(r, 'cần CÓ tất cả', present): continue
            if r.cells['cần CÓ ít nhất một'] != '—' and not present.intersection(r.values('cần CÓ ít nhất một')): continue
            state = r.one('Kết quả'); break
        else: raise ValueError('no decision matched')
        questions, asked = [], []
        if state == 'NEED_INFO':
            for q in sorted(self.tables['questions']['ask_order'], key=lambda q: int(q.cells['Thứ tự'])):
                f, block = q.one('Trường thiếu'), q.one('Khối template')
                if q.one('intent') == intent and f not in present and counts.get(f, 0) < self.limits['max_asks_per_field'] and block not in questions:
                    questions.append(block); asked.append(f)
                    if len(questions) >= self.limits['max_fields_per_ask']: break
            if not questions: questions = [q.one('Khối template') for q in self.tables['questions']['fallback']]
        return Decision(state, intent, intent_rule, r.cells['#'], MappingProxyType({k:parsed[k] for k in present}), tuple(questions), tuple(asked), self.fingerprint)

    @staticmethod
    def _matches(row, col, value):
        return row.cells[col] == '*' or value in row.values(col)

    @staticmethod
    def _all(row, col, present):
        return row.cells[col] == '—' or set(row.values(col)) <= present

    def _self_check(self):
        coverage = set()
        for row in self.tables['examples']['examples']:
            try:
                seen, unseen = int(row.cells['media_seen']), int(row.cells['media_unseen'])
                if seen < 0 or unseen < 0: raise ValueError('negative media count')
                image = frozenset(range(2, 2 + seen + unseen)); viewed = frozenset(range(2, 2 + seen))
                facts = CodeFacts(row.one('command'), row.one('command_payload'), row.one('text'), row.one('extraction_ok'), row.one('reply_target'), image | {1}, image, viewed, frozenset({1}), frozenset())
                model = {'intent': {'value': row.one('intent (model)'), 'source_ids': [1]}}
                cell = row.cells['Dữ kiện']
                if cell not in {'∅', '—'}:
                    for part in cell.split(';'):
                        key, sep, value = part.partition('=')
                        key = key.strip().strip('`'); value = value.strip()
                        if not sep or key not in self.text_fields or key in model: raise ValueError('invalid example field')
                        match = re.search(r'\s*\[([^]]+)\]$', value)
                        source = 1
                        if match:
                            value = value[:match.start()]; mark = match[1]
                            if mark == 'ảnh-đã-xem' and viewed: source = min(viewed)
                            elif mark == 'ảnh-chưa-xem' and image - viewed: source = min(image - viewed)
                            elif mark == 'ngoài-chủ-đề': source = 99999999
                            else: raise ValueError('invalid example source')
                        model[key] = {'value': value, 'source_ids': [source]}
                decision = self.evaluate(model, facts)
                expected_asks = () if row.cells['Hỏi'] == '—' else row.values('Hỏi')
                if (decision.state, decision.rule, decision.questions) != (row.one('Kết quả'), row.cells['Luật'], expected_asks): raise ValueError('example result mismatch')
                coverage.add(decision.rule)
            except (ValueError, TypeError) as exc: row.fail(f'example self-check: {exc}')
        if coverage != {r.cells['#'] for r in self.tables['readiness']['decision']}: self.tables['examples']['examples'][0].fail('decision coverage incomplete')
        for row in self.tables['examples']['normalize_cases']:
            field, value, expected = row.one('Trường'), row.one('Giá trị'), row.one('Chung chung')
            if field not in self.text_fields or expected not in {'có','không'} or (normalize(value) in self.generic[field]) != (expected == 'có'): row.fail('normalization self-check failed')
