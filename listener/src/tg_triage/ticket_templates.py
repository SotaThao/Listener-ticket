"""Parse and validate the shipped ticket templates before rendering any content."""

import hashlib
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class TemplateError(ValueError):
    pass


def _spec(required, optional=""):
    slots = {}
    for item in required.split():
        name, kind = item.split(":")
        slots[name] = (kind, True)
    for item in optional.split():
        name, kind = item.split(":")
        slots[name] = (kind, False)
    return slots


CATALOG = {
    "bug.md": {"body": _spec("summary:T evidence:B notes:L questions:L original:B footer:B", "environment:T steps:L expected:T actual:T"),
               **{f"question.{x}": {} for x in ("environment", "steps", "expected_actual", "evidence")}},
    "feedback.md": {"body": _spec("summary:T context:B questions:L original:B footer:B", "problem:T user_story:T proposal:T alternatives:T acceptance:L"),
                    "question.problem": {}, "question.user_story": {}},
    "task.md": {"body": _spec("summary:T task_evidence:B questions:L original:B footer:B", "work:T reason:T scope_in:T scope_out:T due:T done:L"), "question.work": {}},
    "needs-human.md": {"body": _spec("summary:T evidence:B questions:L original:B footer:B")},
    "_common.md": {"original": _spec("count:T senders:T time:T messages:B"), "footer": _spec("provenance:T"),
                   "provenance.manual": {}, "provenance.rule": {}, "provenance.model": _spec("model:T confidence:T"),
                   "provenance.case": _spec("count:T"), "confidence.none": {}, "original.open_link": {},
                   "original.key_mark": {}, "original.hidden": _spec("count:T"), "original.image_only": {},
                   "original.document_only": {}, "original.unknown_sender": {}, "document.fallback_label": _spec("index:T"),
                   "fallback.summary": {}, "fallback.summary_needs_human": {}, "fallback.needs_clarity": {},
                   "note.unseen_images": _spec("count:T")},
    "comments.md": {"enrichment": _spec("summary:B reply_quote:B", "reply_link:T"),
                    "scope_confirmation": _spec("source:T ordinal:T", "source_link:T image_urls:L document_urls:L"),
                    "scope_correction": _spec("title:T summary:B message_ids:T questions:L unresolved:L unresolved_image_urls:L unresolved_document_urls:L", "has_unresolved:T"),
                    **{f"append.{x}": _spec("url:T") for x in ("source", "recurrence", "related")}},
    "group-replies.md": {"notice.issue": _spec("issue_link:T"), "notice.needs_human": _spec("questions:L", "issue_link:T"),
                         **{x: {} for x in ("notice.error", "notice.rate_limited", "notice.context_pending", "question.fallback", "question.legacy_pending", "question.technical_context", "followup.updated", "followup.split", "followup.recurrence")}},
}
NAME = r"[a-z][a-z0-9_.]*"
TOKEN = re.compile(r"{{(.*?)}}")
ONE_LINE = {"_common.md": set(CATALOG["_common.md"]) - {"original", "footer"},
            "group-replies.md": set(CATALOG["group-replies.md"]) - {"notice.needs_human"},
            "comments.md": {"append.source", "append.recurrence", "append.related"}}
for _file in ("bug.md", "feedback.md", "task.md"):
    ONE_LINE[_file] = set(CATALOG[_file]) - {"body"}


@dataclass(frozen=True)
class Block:
    nodes: tuple


def _error(filename, line, message):
    raise TemplateError(f"{filename}:{line}: {message}")


def parse(text, filename):
    """Parse a normalized file into blocks; values are never parsed as template text."""
    if filename not in CATALOG:
        _error(filename, 1, "unknown file")
    if '\r' in text:
        _error(filename, text[:text.index('\r')].count('\n') + 1, "lone carriage return")
    blocks, name, body, start = {}, None, [], 0
    for line_no, line in enumerate(text.split('\n'), 1):
        if not unicodedata.is_normalized('NFC', line):
            _error(filename, line_no, 'text must be NFC')
        if len(line) > 300 or line.rstrip() != line or any(unicodedata.category(c) == 'Cc' and c != '\t' for c in line):
            _error(filename, line_no, "invalid line length, whitespace or control character")
        opened = re.fullmatch(r'{{#block (' + NAME + r')}}', line)
        if opened:
            if name is not None or opened[1] in blocks:
                _error(filename, line_no, "nested or duplicate block")
            name, body, start = opened[1], [], line_no
        elif line == '{{/block}}':
            if name is None:
                _error(filename, line_no, "unexpected block close")
            if name not in CATALOG[filename]:
                _error(filename, start, "unknown block")
            if len('\n'.join(body)) > 4000:
                _error(filename, start, "block too long")
            if name in ONE_LINE.get(filename, set()) and len(body) != 1:
                _error(filename, start, "block must be one line")
            blocks[name] = _parse_body(filename, start + 1, body, CATALOG[filename][name])
            name = None
        elif name is None:
            if '{{' in line or '}}' in line:
                _error(filename, line_no, "directive outside block")
        else:
            body.append(line)
    if name is not None:
        _error(filename, start, "unclosed block")
    missing = set(CATALOG[filename]) - set(blocks)
    if missing:
        _error(filename, 1, 'missing blocks: ' + ', '.join(sorted(missing)))
    return blocks


def _parse_body(filename, start, lines, spec):
    root, stack, used = [], [], set()
    current = root
    for offset, line in enumerate(lines):
        n = start + offset
        cond = re.fullmatch(r'{{#if (' + NAME + r'(?: ' + NAME + r')*)}}', line)
        if cond:
            names = cond[1].split()
            if len(stack) >= 3 or any(x not in spec for x in names):
                _error(filename, n, 'invalid condition or nesting')
            yes, no = [], []
            current.append(('if', names, yes, no))
            stack.append((current, no, False)); current = yes
            continue
        if line == '{{#else}}':
            if not stack or stack[-1][2]:
                _error(filename, n, 'unexpected else')
            parent, no, _ = stack[-1]; stack[-1] = (parent, no, True); current = no
            continue
        if line == '{{/if}}':
            if not stack:
                _error(filename, n, 'unexpected if close')
            current = stack.pop()[0]
            continue
        pieces, cursor, list_name = [], 0, None
        for match in TOKEN.finditer(line):
            pieces.append(('static', line[cursor:match.start()]))
            token = match[1]
            slot, sep, fallback = token.partition('|')
            if sep and (any(c in fallback for c in '{}|') or '\n' in fallback):
                _error(filename, n, 'invalid fallback')
            is_list = slot.startswith('@')
            key = slot[1:] if is_list else slot
            if key == 'index' and is_list:
                if sep:
                    _error(filename, n, 'index cannot have a fallback')
                pieces.append(('index',)); cursor = match.end(); continue
            if key not in spec or not re.fullmatch(NAME, key):
                _error(filename, n, 'unknown slot or directive')
            kind = spec[key][0]
            if is_list:
                if kind != 'L' or sep or list_name is not None:
                    _error(filename, n, 'invalid list slot')
                list_name = key
            elif kind == 'L' or (kind == 'B' and line != match[0]):
                _error(filename, n, 'invalid slot position')
            used.add(key); pieces.append(('value', key, fallback if sep else None, is_list)); cursor = match.end()
        pieces.append(('static', line[cursor:]))
        static = ''.join(x[1] if x[0] == 'static' else x[2] or '' if x[0] == 'value' else '' for x in pieces)
        if '{{' in static or '}}' in static or any(x[0] == 'index' for x in pieces) and list_name is None:
            _error(filename, n, 'unbalanced token or index without list')
        if filename == 'group-replies.md':
            bad = re.search(r'@|://|www\.|<', static, re.I)
        else:
            stripped = re.sub(r'</?(?:details|summary|sub)>', '', static)
            bad = re.search(r'@\w|://|www\.|\]\(|!\[|```|#\d|<!--|<', stripped, re.I)
        if bad:
            _error(filename, n, 'forbidden static text')
        current.append(('line', tuple(pieces), list_name))
    if stack:
        _error(filename, start + len(lines), 'unclosed if')
    missing = {key for key, (_, required) in spec.items() if required and key not in used}
    if missing:
        _error(filename, start, 'missing required slots: ' + ', '.join(sorted(missing)))
    return Block(tuple(root))


def render(block, values):
    def walk(nodes):
        result = []
        for node in nodes:
            if node[0] == 'if':
                result.extend(walk(node[2] if any(bool(values.get(key, '')) for key in node[1]) else node[3]))
                continue
            _, pieces, list_name = node
            entries = values.get(list_name, ()) if list_name else (None,)
            for index, entry in enumerate(entries, 1):
                line = []
                for part in pieces:
                    if part[0] == 'static': line.append(part[1])
                    elif part[0] == 'index': line.append(str(index))
                    else:
                        value = entry if part[3] else values.get(part[1], '')
                        line.append(part[2] if value == '' and part[2] is not None else value)
                result.append(''.join(line))
        return result
    return '\n'.join(walk(block.nodes))


@dataclass(frozen=True)
class Templates:
    blocks: dict
    fingerprint: str

    def render(self, filename, block, values=None):
        return render(self.blocks[filename][block], values or {})


def validate(directory):
    directory = Path(directory)
    blocks, digest = {}, hashlib.sha256()
    if directory.is_dir():
        extras = {p.name for p in directory.glob('*.md')} - set(CATALOG)
        if extras:
            _error(sorted(extras)[0], 1, 'unexpected template file')
    for filename in sorted(CATALOG):
        path = directory / filename
        try:
            text = path.read_bytes().decode('utf-8-sig').replace('\r\n', '\n')
        except (OSError, UnicodeError) as exc:
            raise TemplateError(f'{filename}:1: {type(exc).__name__}') from exc
        blocks[filename] = parse(text, filename)
        digest.update(filename.encode('utf-8') + b'\0' + text.encode('utf-8') + b'\0')
    return Templates(blocks, digest.hexdigest())


def load_templates():
    global _active
    source = Path(__file__).resolve().parents[2] / 'templates' / 'github-ticket'
    installed = Path(sys.prefix) / 'share' / 'tg-triage' / 'templates' / 'github-ticket'
    _active = validate(source if source.is_dir() else installed)
    return _active


_active = None


def active():
    global _active
    if _active is None:
        _active = load_templates()
    return _active
