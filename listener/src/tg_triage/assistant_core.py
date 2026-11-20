"""Deterministic decide-only view of the canonical assistant pack."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import get_close_matches
from types import MappingProxyType
from collections.abc import Mapping

from .assistant_errors import RuleNotFound
from .assistant_pack import AssistantPackError, _hash, _normalize, _read


REFERENCE_FILES = ("README.md", "preflight.md")
RULE_SECTION_MAX_BYTES = 8192
RULE_LOOKUP_MAX_IDS = 4
_CORE_FILES = ("principles.md", "states.md", "channels.md", "questions.md")
_CORE_SECTIONS = (
    "facts:fields", "facts:generic_values", "facts:object", "facts:field_valid",
    "facts:code_facts", "facts:command_intent", "readiness:intent_resolve",
    "readiness:team_request", "readiness:image_split", "readiness:chat_only",
    "readiness:confidence",
)
_CORE_EXAMPLES = ("X11", "X18", "X5", "X9")
_FILTERS = dict(F1="prefilter", F2="team_request", F3="link_only",
                F4="chat_words", F5="banter_only", F6="reply_chat")
_TABLES = {
    "readiness": {
        "prefilter": ("Lọc trước", "nhận diện tin nhờ review"),
        "prefilter_words": ("Từ lọc trước", "kiểm cụm từ nhờ review"),
        "link_only": ("Tin chỉ có link", "phân biệt link với yêu cầu"),
        "chat_only": ("Tin trò chuyện", "kiểm điều kiện bỏ qua"),
        "chat_words": ("Từ xã giao", "nhận diện tin chỉ xã giao"),
        "banter_only": ("Tin đùa", "phân biệt đùa với báo lỗi"),
        "banter_signals": ("Dấu hiệu đùa", "đọc tín hiệu trong ngữ cảnh"),
        "reply_chat": ("Reply tin CHAT", "xét một câu trả lời vào tin trò chuyện"),
        "image_split": ("Gom ảnh", "chia nhóm khi tin có ảnh hoặc reply ảnh"),
        "team_request": ("Nhờ người trong nhóm", "phân biệt giao việc nội bộ"),
        "intent_resolve": ("Ý định", "phân biệt câu hỏi, yêu cầu và lệnh"),
        "never_blocks": ("Trường tùy chọn", "xem trường nào không cản tạo ticket"),
        "unseen_media": ("Ảnh chưa xem", "quyết định với ảnh chưa đọc"),
        "confidence": ("Độ chắc chắn", "hiểu cách dùng độ chắc chắn"),
    },
    "facts": {
        "fields": ("Định nghĩa dữ kiện", "chọn trường và dẫn nguồn"),
        "code_facts": ("Dữ kiện từ code", "đọc tín hiệu đầu vào"),
        "command_intent": ("Ý định theo lệnh", "nhận diện lệnh"),
        "field_valid": ("Dữ kiện hợp lệ", "kiểm giá trị có bằng chứng"),
        "normalize": ("Chuẩn hóa", "so sánh giá trị"),
        "generic_values": ("Giá trị chung chung", "loại trường mơ hồ"),
        "group_mood": ("Ngữ cảnh đùa", "phân biệt yêu cầu và câu đùa"),
        "limits": ("Giới hạn dữ kiện", "kiểm giới hạn trường và nhóm"),
    },
    "examples": {
        "examples": ("Ca chuẩn", "đối chiếu các quyết định R1–R13"),
        "source_followup_cases": ("Ca bổ sung", "xem ví dụ bổ sung dữ kiện"),
        "prefilter_cases": ("Ca lọc trước", "đối chiếu nhờ review"),
        "link_cases": ("Ca có link", "đối chiếu tin chứa link"),
        "chat_cases": ("Ca xã giao", "đối chiếu tin trò chuyện"),
        "banter_cases": ("Ca đùa", "đối chiếu ngữ cảnh đùa"),
        "reply_chat_cases": ("Ca reply CHAT", "đối chiếu reply vào tin CHAT"),
        "image_split_cases": ("Ca gom ảnh", "đối chiếu phân nhóm ảnh"),
        "team_cases": ("Ca giao việc", "đối chiếu yêu cầu nội bộ"),
        "normalize_cases": ("Ca chuẩn hóa", "đối chiếu trường mơ hồ"),
    },
    "lifecycle": {
        "disposition": ("Vòng đời ticket", "phân biệt ticket mở và đóng"),
        "explicit_evidence": ("Bằng chứng vòng đời", "hiểu điều kiện bổ sung"),
        "clarification": ("Câu hỏi vòng đời", "hiểu cách hỏi lại"),
    },
    "analysis-status": {"intent_routes": ("Tra trạng thái", "phân biệt STATUS và ANALYZE")},
    "preflight": {
        "gate_states": ("Trạng thái phát hành", "tra thuật ngữ GO"),
        "item_states": ("Trạng thái kiểm tra", "tra tiêu chí phát hành"),
        "checks": ("Kiểm trước phát hành", "tra điều kiện vận hành"),
        "halt": ("Dừng vận hành", "tra điều kiện dừng"),
    },
}


def _tables(text):
    matches = list(re.finditer(r"^## (?:(?:Bảng: ([\w_]+))[ \t]*$|[^\n]+)", text, re.M))
    return {match[1]: text[match.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)].rstrip()
            for i, match in enumerate(matches) if match[1]}


def _row(text, key):
    pattern = re.compile(r"^\|\s*`?" + re.escape(key) + r"`?\s*\|")
    return next(line for line in text.splitlines() if pattern.match(line))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_rule_id(rule_id):
    return rule_id.strip().casefold()


@dataclass(frozen=True)
class CorePack:
    fingerprint: str
    text: str
    sections: Mapping[str, str]

    def get_rule(self, rule_id):
        lookup = normalize_rule_id(rule_id) if isinstance(rule_id, str) else ""
        if len(lookup) > 120:
            lookup = ""
        elif lookup not in self.sections and lookup.upper() in self.sections:
            lookup = lookup.upper()
        text = self.sections.get(lookup)
        if text is None:
            candidates = get_close_matches(lookup, self.sections, n=4, cutoff=.35)
            raise RuleNotFound(candidates or ["readiness:decision", "facts:fields", "examples:bug"])
        encoded = text.encode("utf-8")
        return dict(rule_id=lookup, text=encoded[:RULE_SECTION_MAX_BYTES].decode("utf-8", errors="ignore"),
                    truncated=len(encoded) > RULE_SECTION_MAX_BYTES, max_bytes=RULE_SECTION_MAX_BYTES)


def build_core(pack, extras):
    """Fingerprint both prompt text and the complete reference sources and index."""
    extras = {name: _normalize(extras[name]) for name in REFERENCE_FILES}
    bodies = dict(pack.bodies, **extras)
    tables = {name.removesuffix(".md"): _tables(text) for name, text in bodies.items()}
    sections, index = {}, []

    def add(key, text, description):
        sections[key] = text
        index.append(" · ".join((key, *description)))

    try:
        for source, catalog in _TABLES.items():
            for table, description in catalog.items():
                add(f"{source}:{table}", tables[source][table], description)
        for heading in re.split(r"(?=^## \d+\. )", extras["README.md"], flags=re.M)[1:]:
            number, title = heading.splitlines()[0][3:].split(". ", 1)
            add(f"readme:{number}", heading.rstrip(), (title, "tra hướng dẫn nạp hoặc vận hành"))
        add("facts:object", _row(sections["facts:fields"], "object"),
            ("Đối tượng cụ thể", "xác định trường object"))
        add("preflight:image", _row(sections["preflight:checks"], "P9"),
            ("Cấu hình ảnh khi phát hành", "tra giới hạn ảnh ở bước kiểm cấu hình"))
        example_lines = sections["examples:examples"].splitlines()
        for intent, title in (("bug", "Ca lỗi"), ("task", "Ca yêu cầu"), ("feedback", "Ca góp ý")):
            rows = [line for line in example_lines if line.startswith("| X")
                    and line.split("|")[10].strip() == f"`{intent}`"]
            header = [line for line in example_lines if line.startswith(("| #", "|---"))]
            add(f"examples:{intent}", "\n".join([*header, *rows]), (title, "đối chiếu ví dụ cùng ý định"))
        decision = tables["readiness"]["decision"]
        sections["readiness:decision"] = decision
        for fid, table in _FILTERS.items():
            sections[fid] = tables["readiness"][table]
        for source, definitions in tables.items():
            for table, body in definitions.items():
                sections.setdefault(f"{source}:{table}", body)
                # Codes remain valid even when their defining rows are in the reference.
                for match in re.finditer(r"^\|\s*`?([A-Z]+[1-9][0-9]*)`?\s*\|.*$", body, re.M):
                    sections.setdefault(match[1], match[0])
        aliases = {}
        for key in sections:
            if ":" in key:
                aliases.setdefault(key.split(":", 1)[1], []).append(key)
        sections.update((alias, sections[keys[0]]) for alias, keys in aliases.items() if len(keys) == 1)
    except (KeyError, StopIteration, ValueError, IndexError) as error:
        raise AssistantPackError("missing or malformed assistant core section") from error

    core = "\n\n".join([
        *(bodies[name].rstrip() for name in _CORE_FILES),
        *(("## facts:object\n" if key == "facts:object" else "") + sections[key]
          for key in _CORE_SECTIONS),
        "Đọc bảng decision từ trên xuống. Dòng đầu tiên khớp mọi cột là kết quả.\n\n" + decision,
        "\n".join(["## Ví dụ ngắn", *header, *(_row(sections["examples:examples"], key)
                                               for key in _CORE_EXAMPLES)]),
        "Bảng lọc trước (code): " + ", ".join(_FILTERS.values()),
        "## Chỉ mục tra quy tắc (RULE INDEX)\n" + "\n".join(index),
    ])
    fingerprint = _hash(_json(dict(full=pack.fingerprint, core=core, extras=extras, sections=sections)))
    return CorePack(fingerprint, f"<!-- assistant core v1 · {fingerprint} -->\n" + core,
                    MappingProxyType(sections))


def render_core(pack, extras):
    extras = {name: _normalize(extras[name]) for name in REFERENCE_FILES}
    core = build_core(pack, extras)
    return _json(dict(fingerprint=core.fingerprint, text=core.text, references=extras)) + "\n"


def load_core(pack, path, rules_dir):
    try:
        saved = json.loads(_read(path))
        extras = saved["references"]
        supplied = {name: rules_dir / name for name in REFERENCE_FILES}
        if any(file.exists() for file in supplied.values()):
            for name, file in supplied.items():
                if _normalize(_read(file)) != extras[name]:
                    raise AssistantPackError(f"{file}: source drift (rebuild assistant core)")
        core = build_core(pack, extras)
        if saved != dict(fingerprint=core.fingerprint, text=core.text, references=extras):
            raise AssistantPackError("assistant core differs from rebuild")
        return core
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise AssistantPackError("invalid assistant core bundle") from error
