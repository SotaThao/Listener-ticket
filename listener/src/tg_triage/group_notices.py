"""Bounded, plain-text ticket confirmations and owner clarification notices."""

import logging
import re
import unicodedata

from .issue_format import redact
from .models import utf8_valid
from .ticket_templates import active

LOG = logging.getLogger(__name__)
MAX_QUESTIONS = 3
MAX_QUESTION_CHARS = 180
MAX_NOTICE_CHARS = 1100
TIMEOUT_SECONDS = 15
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def clean_question(value):
    if not isinstance(value, str) or not utf8_valid(value):
        return ""
    # Bound processing as well as output. Remove invisible format/control characters,
    # then redact contact data before truncation and disable mentions / automatic links.
    value = " ".join(value[:4000].split())
    value = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    value = redact(value)
    value = re.sub(r"(?i)(?:https?://|www\.)\S+", "[liên kết đã ẩn]", value)
    value = value.replace("@", "＠")
    return value[:MAX_QUESTION_CHARS].strip()


def question(name):
    return active().render('group-replies.md', 'question.' + name)


def __getattr__(name):
    # Compatibility for existing callers while keeping template access lazy.
    names = {'FALLBACK_QUESTION': 'fallback', 'LEGACY_PENDING_QUESTION': 'legacy_pending',
             'TECHNICAL_CONTEXT_QUESTION': 'technical_context'}
    if name in names:
        return question(names[name])
    raise AttributeError(name)


def clean_analysis(answer):
    """Sanitize informational prose and choices without a scope prefix."""
    if not utf8_valid(answer):
        return ''
    value='\n'.join(' '.join(line.split()) for line in answer[:6000].splitlines())
    value=''.join(ch for ch in value if ch=='\n' or not unicodedata.category(ch).startswith('C'))
    value=redact(value)
    value=re.sub(r'(?i)(?:https?://|www\.)\S+', '[liên kết đã ẩn]', value)
    value=re.sub(r'(?i)(?:[a-z][a-z0-9+.-]*://\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)', '[liên kết đã ẩn]', value)
    value=value.replace('@','＠')
    return value.strip()


def analysis_text(answer):
    value=clean_analysis(answer)
    prefix='Em phân tích dựa trên phần tin được cung cấp; không phải toàn bộ lịch sử nhóm.\n'
    return (prefix+value)[:1500].strip()


ADDRESS_DEFAULT="Anh/Chị"
_PROTECTED=re.compile(r'“[^\n]*”|“[^\n]*$|"[^\n]*"|"[^\n]*$|https?://\S+|^>.*$',re.MULTILINE)


def _outside_quotes(text,replace):
    pieces=[]; last=0
    for match in _PROTECTED.finditer(text):
        pieces.extend((replace(text[last:match.start()]),match[0])); last=match.end()
    pieces.append(replace(text[last:]))
    return ''.join(pieces)


def address_question(value):
    """Normalize only the sanitized question prose, preserving quoted source text and URLs."""
    return _outside_quotes(clean_question(value),lambda part:re.sub(r'\b[Bb]ạn\b',ADDRESS_DEFAULT,part))


def personalize_notice(text,address=ADDRESS_DEFAULT):
    """Render trusted address slots; never rewrite quotes, source links or issue content."""
    address=address if address in ('Anh','Chị',ADDRESS_DEFAULT) else ADDRESS_DEFAULT
    return _outside_quotes(text,lambda part:part.replace(ADDRESS_DEFAULT,address))


def address_from_public_bio(bio):
    """Only explicit self-description: never infer gender from a name, avatar or unrelated words."""
    if not isinstance(bio,str):
        return ADDRESS_DEFAULT
    bio=bio[:4096].casefold()
    pronouns=re.findall(r'(?:^|[\n•|;,])\s*(?:(?:pronouns?|my pronouns(?: are)?|i use)\s*:?\s*)?(he\s*/\s*him|she\s*/\s*her)\s*(?=$|[\n•|;,])',bio)
    male=any(re.fullmatch(r'he\s*/\s*him',p) for p in pronouns)
    female=any(re.fullmatch(r'she\s*/\s*her',p) for p in pronouns)
    for match in re.finditer(r'(?m)^\s*(?:gender|giới tính|preferred address|xưng hô)\s*:\s*(male|female|nam|nữ|anh|chị)\s*(?:$|[;,.])',bio):
        male=male or match[1] in ('male','nam','anh')
        female=female or match[1] in ('female','nữ','chị')
    return 'Anh' if male and not female else 'Chị' if female and not male else ADDRESS_DEFAULT


def notice_text(result, missing_info, repo):
    issue_link = None
    if (isinstance(repo, str) and _REPO.fullmatch(repo)
            and type(result.issue_number) is int and result.issue_number > 0):
        issue_link = f"https://github.com/{repo}/issues/{result.issue_number}"
    if result.decision == "issue":
        if issue_link is None:
            return None
        text = active().render('group-replies.md', 'notice.issue', {'issue_link': issue_link})
    elif result.decision == "needs_human":
        questions = []
        if isinstance(missing_info, (tuple, list)):
            for value in missing_info[:20]:
                question = address_question(value)
                if question:
                    questions.append(question)
                    if len(questions) == MAX_QUESTIONS:
                        break
        if not questions:
            questions.append(active().render('group-replies.md', 'question.fallback'))
        text = active().render('group-replies.md', 'notice.needs_human',
                               {'issue_link': issue_link or '', 'questions': questions})
    elif result.decision == "error":
        text = active().render('group-replies.md', 'notice.error')
    elif result.decision == "rate_limited":
        text = active().render('group-replies.md', 'notice.rate_limited')
    elif result.decision == 'context_pending':
        text = active().render('group-replies.md', 'notice.context_pending')
    else:
        return None
    return text[:MAX_NOTICE_CHARS]


def notify(store, sender, first, result, missing_info, repo, *, text_override=None):
    """At most one send attempt per group; even ambiguous transport failures stay terminal.

    Reserve is committed before transport. A crash may lose a notice, deliberately:
    replay must never blindly resend an already delivered message.
    """
    if sender is None:
        return
    text = text_override if text_override is not None else notice_text(result, missing_info, repo)
    if text is None:
        return
    try:
        if not store.reserve_group_notice(first.chat_id, first.message_id, result.decision):
            return
    except Exception as error:
        LOG.warning("Group notice reservation failed exception=%s", type(error).__name__)
        return  # fail closed if durable state is unavailable
    status, notice_id = "failed", None
    try:
        sent = sender(first.chat_id, first.message_id, text)
        if type(sent) is int and sent > 0:
            status, notice_id = "sent", sent
    except Exception as error:
        LOG.warning("Group notice send failed exception=%s", type(error).__name__)
    try:
        store.finish_group_notice(first.chat_id, first.message_id, status, notice_id)
    except Exception as error:
        LOG.warning("Group notice state failed exception=%s", type(error).__name__)
