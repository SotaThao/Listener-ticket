"""Render the fixed Vietnamese issue template and neutralize user content."""

import re
import unicodedata
import html
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from .models import Classification, IncomingMessage, TICKET_TYPES, canonical_category, immutable_details
from .ticket_templates import active


MAX_QUOTED_MESSAGES = 10
TOTAL_QUOTE_BUDGET = 4000  # characters of quoted text shared by all messages in one body
MIN_QUOTE_PER_MESSAGE = 100  # floor, so a large group still shows something of every message
MAX_CLARIFY = 5
MAX_CLARIFY_CHARS = 200
NEEDS_CLARITY_LINE = "Chưa đủ thông tin để phân loại, cần người đọc xem lại."
NO_TITLE_LINE = "(không có tiêu đề)"
NO_SUMMARY_LINE = "(không có tóm tắt)"

# The classifier adds " [kèm ảnh]" to the text it sends the model, and the model sometimes copies
# it into title/summary. Variants: "[kèm ảnh]", "(có kèm ảnh)", "[có kèm ảnh]", bare "kèm ảnh" /
# "có kèm ảnh", any case, any run of spaces. A bracketed form is removed together with its brackets.
_IMAGE_WORDS = r"(?:có\s+)?kèm\s+ảnh"
_IMAGE_MARKER = re.compile(
    rf"\[\s*{_IMAGE_WORDS}\s*\]|\(\s*{_IMAGE_WORDS}\s*\)|(?<!\w){_IMAGE_WORDS}(?!\w)",
    re.IGNORECASE)
_EDGE_SEPARATORS = " ,;:.-–—·|/\\"
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {close: open_ for open_, close in _BRACKETS.items()}


# Quotes and fields redact contact data; code-built source-link lines retain the full URL per
# the TG-31 Owner decision of 2026-10-29. Redaction runs on text that
# `sanitize` already escaped, so the patterns accept its output: "@" is followed by U+200B, and
# "_" / "\\" are backslash-escaped. SQLite keeps the original text (internal data).
EMAIL_PLACEHOLDER = "[email đã ẩn]"
PHONE_PLACEHOLDER = "[số đã ẩn]"
_ZW = "\u200b"
# One character of an address as `sanitize` leaves it: word chars, . + % -, a backslash (escape of
# "_") and the zero-width space.
_EMAIL_CHAR = r"[\w.+%\-\\" + _ZW + "]"
_DOMAIN_CHAR = r"[\w\-\\" + _ZW + "]"
_EMAIL = re.compile(
    "(?<!" + _EMAIL_CHAR + ")" + _EMAIL_CHAR + r"*\w@" + _ZW + "?" + _DOMAIN_CHAR + "+"
    + r"(?:\." + _DOMAIN_CHAR + r"+)*\.[A-Za-z]{2,}")
# 9-11 digits, optionally led by "+" and grouped with single spaces or dashes: 0901234567,
# +84 901 234 567, 090-123-4567; also 090.123.4567. Longer digit runs (ids, card numbers) are left alone.
_PHONE = re.compile(
    r"(?<![\d+])(?:\+?\d(?:[ \-]?\d){8,10}|\d{3,4}\.\d{3}\.\d{3,4})(?!\d)(?!:\d)")
# US/Canada numbers with parentheses, including sanitize's Markdown escapes.
_PAREN_PHONE = re.compile(
    r"(?<![\w+])(?:\+?1[ .\-]*)?\\?\(\d{3}\\?\)[ .\-]*\d{3}[ .\-]*\d{4}(?!\d)")
_DATE_LIKE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-\d{2,4}")


def _phone(match) -> str:
    return match[0] if _DATE_LIKE.match(match[0].lstrip("+")) else PHONE_PLACEHOLDER


def redact(value: str) -> str:
    """Replace e-mail addresses and phone numbers in already-sanitized text."""
    value = _EMAIL.sub(EMAIL_PLACEHOLDER, value)
    value = _PAREN_PHONE.sub(PHONE_PLACEHOLDER, value)
    return _PHONE.sub(_phone, value)


@dataclass(frozen=True)
class Issue:
    title: str
    body: str
    labels: tuple[str, ...]


def sanitize(value: str, *, inline=False) -> str:
    # Keep line boundaries only inside the original-message blockquote. Inline
    # fields collapse whitespace before Cc removal so tabs cannot join words.
    if inline:
        value = " ".join(value.split())
    value = "".join(
        ch for ch in value
        if ((ch == "\n" and not inline) or unicodedata.category(ch) != "Cc")
        and not ("‪" <= ch <= "‮" or "⁦" <= ch <= "⁩"))
    value = html.escape(value, quote=False)
    value = value.replace("@", "@​")
    value = re.sub(r"#(?=\d)", "#​", value)
    value = re.sub(r"(?i)(gh-)(?=\d)", r"\1" + "​", value)
    value = re.sub(r"(?i)github\.com/", "github​.com/", value)
    value = value.replace("://", ":​//")
    value = re.sub(r"(?i)(www)(?=\.)", r"\1" + "​", value)
    # Escape Markdown in the original quote too: quoting alone permits images
    # and clickable links. The code-generated Telegram link bypasses sanitizing.
    value = re.sub(r"([\\`*_\[\]()!|])", r"\\\1", value)
    return value


def telegram_link(message: IncomingMessage) -> str | None:
    if message.chat_username and re.fullmatch(r"[A-Za-z0-9_]+", message.chat_username):
        return f"https://t.me/{message.chat_username}/{message.message_id}"
    chat = str(message.chat_id)
    if chat.startswith("-100") and chat[4:].isdigit() and chat[4:]:
        return f"https://t.me/c/{chat[4:]}/{message.message_id}"
    return None


_BLOCK_START = re.compile(
    r"( {0,3})(?:(?P<heading>#{1,6})(?=\s|$)|(?P<bullet>[-+])(?=\s|$)|(?P<ordered>\d{1,9})(?P<dot>[.)])(?=\s|$)"
    r"|(?P<quote>>)|(?P<fence>~{3,}))")
_RULE_LINE = re.compile(r"[-=+~ ]+")   # setext underline / thematic break made of these


def line_safe(value: str) -> str:
    """Neutralize Markdown block syntax at the start of each line of already sanitized text.

    `sanitize` escapes inline syntax but not what only matters at the start of a line: "# x",
    "- x", "+ x", "1. x", "> x", "---", "===", "~~~" and a 4-space indent. A backslash (or an
    entity for the indent) makes them literal; text that is not block syntax is returned as is.
    Call it AFTER sanitize/redact, on the text (never on a marker the code writes itself).
    """
    lines = []
    for line in value.split("\n"):
        if line.strip() and _RULE_LINE.fullmatch(line):
            line = "".join(ch if ch == " " else "\\" + ch for ch in line)
        elif line.startswith("    "):
            line = "&#32;" + line[1:]
        else:
            match = _BLOCK_START.match(line)
            if match:
                lead, end = match.end(1), match.end()
                if match["fence"]:
                    line = line[:lead] + "".join("\\" + ch for ch in match["fence"]) + line[end:]
                elif match["ordered"]:
                    line = line[:end - 1] + "\\" + line[end - 1:]
                else:
                    line = line[:lead] + "\\" + line[lead:]
        lines.append(line)
    return "\n".join(lines)


def source_links(case_messages) -> list[str]:
    """Render validated links from source messages in time order."""
    lines, seen = [], set()
    for item in sorted(case_messages, key=lambda msg: (msg.received_at, msg.message_id)):
        for match in re.finditer(r"https?://[^\s<>\"'`]+", item.text or "", re.IGNORECASE):
            url = match.group()
            while url:
                if url[-1] in ".,;:!?":
                    url = url[:-1]
                elif url[-1] in _CLOSERS and url.count(url[-1]) > url.count(_CLOSERS[url[-1]]):
                    url = url[:-1]
                else:
                    break
            try:
                parts = urlsplit(url)
                host = parts.hostname
                if (len(url) > 2000 or parts.scheme.lower() not in ("http", "https")
                        or not host or parts.username is not None or parts.password is not None):
                    continue
                host = host.encode('idna').decode('ascii')
                authority = parts.netloc
                if not parts.hostname.isascii():
                    suffix = authority[authority.rfind(':'):] if ':' in authority else ''
                    authority = host + suffix
                    url_target = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
                else:
                    url_target = url
                target_parts = urlsplit(url_target)
                safe = "!#$%&*+,-./:;=?@^_{|}~"
                target = urlunsplit((target_parts.scheme, quote(target_parts.netloc, safe=safe + "[]"),
                                     quote(target_parts.path, safe=safe), quote(target_parts.query, safe=safe),
                                     quote(target_parts.fragment, safe=safe)))
                key = (parts.scheme.lower(), host.lower(), parts.port, parts.path, parts.query, parts.fragment)
            except (ValueError, UnicodeError):
                continue
            if key in seen:
                continue
            seen.add(key)
            authority = parts.netloc
            if authority.startswith('['):
                display_host = '[' + host + ']' + authority.partition(']')[2]
            else:
                display_host = host + (':' + authority.rpartition(':')[2] if ':' in authority else '')
            display = display_host + parts.path + ("?" + parts.query if parts.query else "") + ("#" + parts.fragment if parts.fragment else "")
            if len(display) > 80:
                display = display[:79] + "…"
            display = line_safe(sanitize(display, inline=True))
            lines.append(f"- 🔗 [{display}]({target})")
            if len(lines) == 10:
                return lines
    return lines


def _quote(text: str, limit: int) -> str:
    clean = redact(sanitize(text))   # before the cut, so a cut cannot leave half an address
    quoted = "\n".join("> " + line_safe(line) for line in clean[:limit].split("\n"))
    if len(clean) > limit:
        quoted += "\n> (đã cắt)"
    return quoted


def _trim_edges(value: str) -> str:
    """Drop separators and unbalanced brackets left hanging at either end."""
    value = value.strip()
    while value:
        before = value
        value = value.strip(_EDGE_SEPARATORS)
        if value and value[-1] in _BRACKETS:
            value = value[:-1]
        elif value and value[0] in _CLOSERS:
            value = value[1:]
        elif value and value[-1] in _CLOSERS and value.count(value[-1]) > value.count(_CLOSERS[value[-1]]):
            value = value[:-1]
        elif value and value[0] in _BRACKETS and value.count(value[0]) > value.count(_BRACKETS[value[0]]):
            value = value[1:]
        if value == before:
            break
    return value


def strip_image_marker(value) -> str:
    """Remove the system's image marker from a model-written title/summary (raw, pre-sanitize).

    Text without the marker is returned unchanged; only when something was removed are the
    whitespace and the edge punctuation tidied, so ordinary titles are never rewritten.
    """
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFC", value)
    cleaned, removed = _IMAGE_MARKER.subn(" ", value)
    if not removed:
        return value
    return _trim_edges(" ".join(cleaned.split()))


def _first_message_line(messages) -> str:
    """First non-empty original-message text (marker removed), for a title the model left empty."""
    for item in messages:
        text = _clean_line(strip_image_marker(item.text))
        if text:
            return text
    return ""


MAX_DOCUMENT_LABEL = 80
_RAW_NAME_LIMIT = 1000
_PLACEHOLDER = re.compile("(" + re.escape(EMAIL_PLACEHOLDER) + "|" + re.escape(PHONE_PLACEHOLDER) + ")")
_MARKDOWN_MARKS = re.compile(r"[\\`*_\[\]()!|<>~#{}^]")


def document_label(name, fallback="tài liệu") -> str:
    """Display text of a document link (12.14): the sender's file name made safe for Markdown.

    The name goes through `sanitize` and `redact` (e-mail and phone are replaced) and then every
    Markdown-significant character is dropped (an underscore becomes a space), so the name cannot
    open a link, an image, emphasis or HTML. Placeholders keep their brackets, escaped. The result
    is capped at MAX_DOCUMENT_LABEL characters without cutting a placeholder; empty becomes `fallback`.
    """
    base = re.split(r"[\\/]", name)[-1] if isinstance(name, str) else ""
    cleaned = redact(sanitize(base[:_RAW_NAME_LIMIT], inline=True))
    pieces = []
    for part in _PLACEHOLDER.split(cleaned):
        if part in (EMAIL_PLACEHOLDER, PHONE_PLACEHOLDER):
            pieces.append(part.replace("[", "\\[").replace("]", "\\]"))
        else:
            pieces.append(_MARKDOWN_MARKS.sub(lambda mark: " " if mark[0] == "_" else "", part))
    label, used = "", 0
    for piece in pieces:
        if used + len(piece) > MAX_DOCUMENT_LABEL:
            if not piece.startswith("\\["):
                label += piece[:MAX_DOCUMENT_LABEL - used]
            break
        label += piece
        used += len(piece)
    label = " ".join(label.split())
    return label or fallback


def _clean_line(value) -> str:
    return redact(sanitize(value, inline=True)) if isinstance(value, str) else ""


TIME_FORMAT = "%d/%m %H:%M"
MAX_DETAIL_CHARS = 500
MAX_DETAIL_ITEMS = 10
MAX_DETAIL_ITEM_CHARS = 200


def _evidence_text(*parts):
    return "\n\n".join(part for part in parts if part)


def _template_values(category, details, media, notes, links_from_source):
    source = dict(immutable_details(details))
    template = active()
    def text(key):
        return line_safe(_clean_line(strip_image_marker(source.get(key)))[:MAX_DETAIL_CHARS])
    def items(key):
        return [line_safe(line) for line in (
            _clean_line(strip_image_marker(entry))[:MAX_DETAIL_ITEM_CHARS]
            for entry in source.get(key, ())[:MAX_DETAIL_ITEMS]) if line]
    def question(name):
        return template.render(category + '.md', 'question.' + name)
    questions = []
    if category == 'bug':
        values = {key: text(key) for key in ('environment', 'expected', 'actual')}
        values['steps'] = items('steps')
        values['evidence'] = _evidence_text(text('error_text'), *media, links_from_source)
        values['notes'] = notes if '\n'.join(notes) else []
        if not values['environment']: questions.append(question('environment'))
        if not values['steps']: questions.append(question('steps'))
        if not values['expected'] or not values['actual']: questions.append(question('expected_actual'))
        if not values['evidence']: questions.append(question('evidence'))
    elif category == 'feedback':
        values = {key: text(key) for key in ('problem', 'user_story', 'proposal', 'alternatives')}
        values['acceptance'] = items('acceptance')
        values['context'] = _evidence_text(text('context'), *media, '\n'.join(notes), links_from_source)
        if not values['problem']: questions.append(question('problem'))
        if not values['user_story']: questions.append(question('user_story'))
    else:
        values = {key: text(key) for key in ('work', 'scope_in', 'scope_out', 'due')}
        values['reason'] = text('reason_context')
        values['done'] = items('done')
        links = '\n'.join('- ' + line for line in items('links'))
        values['task_evidence'] = _evidence_text(*media, '\n'.join(notes), links, links_from_source)
        if not values['work']: questions.append(question('work'))
    return values, questions


def _sender(item) -> str:
    return _clean_line(item.sender_name) or active().render('_common.md', 'original.unknown_sender')


def _time(item) -> str:
    return item.received_at.astimezone().strftime(TIME_FORMAT)


def _sender_list(case_messages) -> str:
    """Distinct senders in time order: up to two names, then "+K" for the rest."""
    names = list(dict.fromkeys(_sender(item) for item in case_messages))
    if len(names) >= 3:
        return f"{names[0]}, {names[1]} +{len(names) - 2}"
    return ", ".join(names)


def _original_text(item, limit) -> str:
    if not item.text.strip():
        if item.has_image:
            return "> " + active().render('_common.md', 'original.image_only')
        if item.has_document:
            return "> " + active().render('_common.md', 'original.document_only')
    return _quote(item.text, limit)


def format_issue(message: IncomingMessage, classification: Classification | None,
                 tier: str, needs_human: bool, model: str = "qwen3.5:9b", *,
                 case_messages=None, key_message_ids=None, images=(), documents=(), notes=(),
                 clarify=None, text=None, max_quoted=None, details=None) -> Issue:
    """Render one issue. `message` is the case's (or topic group's) first message.

    `details=None` preserves the legacy body; an empty mapping enables the type template
    for trusted tickets, including typed needs-human tickets (12.15).

    All `case_messages` are quoted in time order; `key_message_ids` (Assistant
    definition 12.2) only marks the messages the model called key. `images` ((alt, url) pairs) and `notes` are written by code
    (12.8), never taken from the model, so they do not pass through `sanitize`. `documents` ((file name,
    url) pairs, 12.14) are listed as plain links, never embedded; the sender's file name is cleaned by
    `document_label` here, so no caller can pass a raw name into the body.

    Topic groups (12.9): `clarify` (None = single-case rendering) is the group's missing_info and
    adds the "Cần làm rõ" section; `text` is the model's (title, summary) for a needs-human group;
    `max_quoted` raises the quoted-message cap so a whole group is rendered. A needs-human issue
    keeps its type label only when the type is trusted (explicit command, or a confident
    ticket that still lacks information): then `classification` is passed and is valid.
    """
    if not needs_human and (classification is None or not classification.is_valid()
                            or classification.type not in TICKET_TYPES):
        raise ValueError("An issue requires a valid non-noise classification")
    typed = not needs_human
    fallback_messages = sorted(case_messages or [message],
                               key=lambda item: (item.received_at, item.message_id))
    fallback_messages = [message] + [item for item in fallback_messages if item is not message]
    if needs_human:
        typed = (clarify is not None and classification is not None and classification.is_valid()
                 and classification.type in TICKET_TYPES)
        given_title = _clean_line(strip_image_marker(text[0])) if text else ""
        given_summary = _clean_line(strip_image_marker(text[1])) if text else ""
        prefix = f"[{classification.type}] " if typed else "[cần phân loại] "
        title = prefix + (given_title or _first_message_line(fallback_messages) or NO_TITLE_LINE)
        summary = given_summary or active().render('_common.md', 'fallback.summary_needs_human')
        labels = ((classification.type, "triage", "needs-human", "source:telegram") if typed
                  else ("needs-human", "source:telegram"))
    else:
        title = f"[{classification.type}] " + (
            _clean_line(strip_image_marker(classification.title))
            or _first_message_line(fallback_messages) or NO_TITLE_LINE)
        summary = _clean_line(strip_image_marker(classification.summary)) or active().render('_common.md', 'fallback.summary')
        labels = (classification.type, "triage", "source:telegram")
    title = title.replace("\n", " ")[:80]
    case_messages = sorted(case_messages or [message],
                           key=lambda item: (item.received_at, item.message_id))
    # Every message of the case is rendered; the model's key ids only add a marker.
    # Ids that are not in this case are ignored, so they can never mark or add anything.
    present = {item.message_id for item in case_messages}
    key_ids = {value for value in (key_message_ids or ())
               if type(value) is int and value in present}
    shown = case_messages[:MAX_QUOTED_MESSAGES if max_quoted is None else max(1, max_quoted)]
    hidden = len(case_messages) - len(shown)
    limit = TOTAL_QUOTE_BUDGET // len(shown)
    if max_quoted is not None:
        limit = max(limit, MIN_QUOTE_PER_MESSAGE)
    blocks = []
    for item in shown:
        link = telegram_link(item)
        link_text = f" · [{active().render('_common.md', 'original.open_link')}]({link})" if link else ""
        mark = " · " + active().render('_common.md', 'original.key_mark') if item.message_id in key_ids else ""
        blocks.append(f"**{_sender(item)} · {_time(item)}**{link_text}{mark}\n"
                      f"{_original_text(item, limit)}")
    if hidden:
        blocks.append(active().render('_common.md', 'original.hidden', {'count': str(hidden)}))
    if tier == "manual":
        provenance = active().render('_common.md', 'provenance.manual')
    elif tier == "rule":
        provenance = active().render('_common.md', 'provenance.rule')
    else:
        confidence = (f"{classification.confidence:.2f}"
                      if classification is not None and classification.is_valid() else active().render('_common.md', 'confidence.none'))
        provenance = active().render('_common.md', 'provenance.model', {'model': sanitize(model, inline=True), 'confidence': confidence})
    if len(case_messages) > 1:
        provenance += " · " + active().render('_common.md', 'provenance.case', {'count': str(len(case_messages))})

    questions = []
    if clarify is not None and (needs_human or clarify):
        questions = [_clean_line(entry)[:MAX_CLARIFY_CHARS] for entry in clarify
                     if isinstance(entry, str) and entry.strip()][:MAX_CLARIFY]
        questions = questions or [active().render('_common.md', 'fallback.needs_clarity')]
    media = []
    if images:
        media.append("\n".join(f"![{alt}]({url})" for alt, url in images))
    if documents:
        media.append("\n".join(
            f"- 📎 [{document_label(name, active().render('_common.md', 'document.fallback_label', {'index': str(index)}))}]({url})"
            for index, (name, url) in enumerate(documents, 1)))
    clean_notes = [" ".join(note.split()) for note in notes]
    links_from_source = "\n".join(source_links(case_messages))
    category = canonical_category(classification.type) if typed else None
    template_name = 'needs-human.md'
    values = {}
    if details is not None and category in ("bug", "feedback", "task"):
        template_name = category + '.md'
        values, auto_questions = _template_values(category, details, media, clean_notes, links_from_source)
        media, clean_notes = [], []
        questions = []
        seen = set()
        for entry in clarify or ():
            clean = _clean_line(strip_image_marker(entry))[:MAX_CLARIFY_CHARS]
            if clean and clean not in seen:
                seen.add(clean)
                questions.append(clean)
        for question in auto_questions:
            key = _clean_line(question)
            if key not in seen:
                seen.add(key)
                questions.append(question)
        questions = questions[:MAX_CLARIFY]
        if not questions and needs_human and clarify is not None:
            questions = [active().render('_common.md', 'fallback.needs_clarity')]
    extra_evidence = _evidence_text(*media, "\n".join(clean_notes), links_from_source)
    if template_name == 'needs-human.md':
        values['evidence'] = extra_evidence
    original = active().render('_common.md', 'original', {'count': str(len(case_messages)),
        'senders': _sender_list(case_messages), 'time': _time(case_messages[0]), 'messages': '\n\n'.join(blocks)})
    footer = active().render('_common.md', 'footer', {'provenance': provenance})
    values.update(summary=line_safe(summary), questions=[line_safe(q) for q in questions], original=original, footer=footer)
    body = f"<!-- tg:{message.chat_id}:{message.message_id} -->\n" + active().render(template_name, 'body', values)
    return Issue(title, body, labels)
