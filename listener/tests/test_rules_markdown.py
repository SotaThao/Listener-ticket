"""Self-check for listener/rules/*.md (rules/README.md §4, K1-K11).

Reads markdown only and never imports tg_triage: this is the reference
evaluator the future rules loader must agree with.
"""

from __future__ import annotations

import re
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "rules"
TEMPLATES = ROOT / "templates" / "github-ticket"

HEADER = re.compile(
    r"^<!-- rule-file: (?P<name>[a-z_]+) · version (?P<version>\d+) · "
    r"status (?P<status>DRAFT|ACTIVE) · audience: (?P<audience>.+?) -->$"
)
AUDIENCES = {"code", "model + code", "người + agent"}
TABLE = re.compile(r"^## Bảng: (?P<name>[a-z_]+)\b")
TICK = re.compile(r"`([^`]+)`")
ANY = "*"
NONE = "∅"
EMPTY = "—"
PRONOUNS = {"em", "anh", "chị", "mình", "bạn"}
EDGE_PUNCT = ".,!?;:…\"'“”‘’()[]-"
MARK = re.compile(r"\s*\[(?P<mark>[a-zà-ỹ-]+)\]\s*$")
INVALID_MARKS = {"ảnh-chưa-xem", "ngoài-chủ-đề"}

REQUIRED = {
    "states": {
        "states": ["Mã", "Nghĩa", "Ticket", "Group gốc", "Group xử lý", "Pending", "Rule nghiệp vụ"],
        "pending_states": ["Từ", "Sự kiện", "Sang"],
        "limits": ["Khóa", "Giá trị", "Nghĩa"],
    },
    "facts": {
        "fields": ["Trường", "Kiểu", "Ý nghĩa", "Ví dụ"],
        "code_facts": ["Trường", "Giá trị", "Cách tính"],
        "command_intent": ["Command", "intent"],
        "field_valid": ["#", "Điều kiện"],
        "normalize": ["#", "Bước"],
        "generic_values": ["Trường", "Cụm"],
        "limits": ["Khóa", "Giá trị", "Nghĩa"],
    },
    "readiness": {
        "intent_resolve": ["#", "command", "intent (model)", "cần CÓ", "intent dùng"],
        "decision": [
            "#", "command", "command_payload", "text", "media", "extraction_ok", "reply_target",
            "intent", "cần CÓ tất cả", "cần CÓ ít nhất một", "Kết quả", "Ghi chú",
        ],
        "never_blocks": ["Thứ không chặn"],
        "unseen_media": ["#", "Quy tắc"],
        "confidence": ["#", "Quy tắc"],
    },
    "questions": {
        "ask_order": ["Thứ tự", "intent", "Trường thiếu", "Khối template"],
        "fallback": ["Khi", "Khối template"],
        "answer_link": ["#", "Kênh", "Là Reply vào", "Pending", "Kết quả"],
        "limits": ["Khóa", "Giá trị", "Nghĩa"],
    },
    "channels": {
        "channels": ["Kênh", "Là gì", "Config", "Bot được gửi"],
        "outputs": ["Trạng thái", "Group gốc", "Group xử lý", "Khối template"],
        "ops_message_fields": ["Trường", "Nguồn"],
        "blocks": ["Khối template", "File", "Có sẵn"],
    },
    "examples": {
        "examples": [
            "#", "Tin (rút gọn)", "command", "command_payload", "text", "media_seen", "media_unseen",
            "extraction_ok", "reply_target", "intent (model)", "Dữ kiện", "Kết quả", "Luật", "Hỏi",
        ],
        "normalize_cases": ["#", "Trường", "Giá trị", "Chung chung"],
    },
    "roles": {
        "permission_states": ["Mã", "Nghĩa"],
        "actors": ["Mã", "Vai trò", "Cách xác định"],
        "actions": ["#", "Việc", "`OWNER`", "`OPERATOR`", "`DEV`", "`AGENT`", "`MODEL`"],
        "stop_and_ask": ["#", "Điều kiện"],
    },
    "preflight": {
        "gate_states": ["Mã", "Nghĩa"],
        "item_states": ["Mã", "Nghĩa"],
        "checks": ["#", "Kiểm", "Cách kiểm / bằng chứng", "Ai ký", "Miễn được?"],
        "halt": ["#", "Điều kiện dừng"],
    },
}


def split_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    header = HEADER.match(lines[0]) if lines else None
    tables = {}
    index = 0
    while index < len(lines):
        found = TABLE.match(lines[index])
        index += 1
        if not found:
            continue
        while index < len(lines) and not lines[index].startswith("|"):
            if lines[index].startswith("#"):
                break
            index += 1
        rows = []
        while index < len(lines) and lines[index].startswith("|"):
            rows.append(split_row(lines[index]))
            index += 1
        if len(rows) < 2:
            raise AssertionError(f"{path.name}: bảng {found['name']} không có hàng")
        tables[found["name"]] = (rows[0], [row for row in rows[2:]])
    return header, tables


def ticks(cell):
    return TICK.findall(cell)


def one(cell):
    values = ticks(cell)
    if len(values) != 1:
        raise AssertionError(f"ô phải có đúng một giá trị: {cell!r}")
    return values[0]


def records(table):
    columns, rows = table
    return [dict(zip(columns, row)) for row in rows]


def normalize(value):
    text = unicodedata.normalize("NFC", value).casefold()
    text = " ".join(text.split())
    text = text.strip(EDGE_PUNCT).strip()
    words = text.split(" ")
    if len(words) > 1 and words[0] in PRONOUNS:
        text = " ".join(words[1:])
    return text


class RulesMarkdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = {}
        for path in sorted(RULES.glob("*.md")):
            if path.name == "README.md":
                continue
            cls.files[path.stem] = parse(path)
        cls.tables = {name: tables for name, (_, tables) in cls.files.items()}
        facts = cls.tables["facts"]
        cls.states = {one(r["Mã"]) for r in records(cls.tables["states"]["states"])}
        cls.fields = [one(r["Trường"]) for r in records(facts["fields"])]
        cls.text_fields = [f for f in cls.fields if f not in {"intent", "media_ids"}]
        intent_row = next(r for r in records(facts["fields"]) if one(r["Trường"]) == "intent")
        cls.intents = set(ticks(intent_row["Kiểu"]))
        cls.code_facts = {one(r["Trường"]): set(ticks(r["Giá trị"])) for r in records(facts["code_facts"])}
        cls.command_intent = {one(r["Command"]): one(r["intent"]) for r in records(facts["command_intent"])}
        cls.generic = {}
        for row in records(facts["generic_values"]):
            cls.generic.setdefault(one(row["Trường"]), set()).add(normalize(one(row["Cụm"])))
        cls.decision = records(cls.tables["readiness"]["decision"])
        cls.resolve = records(cls.tables["readiness"]["intent_resolve"])
        cls.ask_order = records(cls.tables["questions"]["ask_order"])
        cls.fallback = [one(r["Khối template"]) for r in records(cls.tables["questions"]["fallback"])]
        cls.question_limits = {one(r["Khóa"]): int(one(r["Giá trị"])) for r in records(cls.tables["questions"]["limits"])}

    # K1
    def test_headers(self):
        expected = set(REQUIRED)
        self.assertEqual(set(self.files), expected)
        for name, (header, _) in self.files.items():
            self.assertIsNotNone(header, f"{name}.md: dòng 1 phải là header rule-file")
            self.assertEqual(header["name"], name)
            self.assertIn(header["audience"], AUDIENCES)

    # K2
    def test_required_tables_and_columns(self):
        for name, required in REQUIRED.items():
            tables = self.tables[name]
            self.assertEqual(set(tables), set(required), f"{name}.md: danh sách bảng")
            for table, columns in required.items():
                self.assertEqual(tables[table][0], columns, f"{name}.md · {table}: cột")
                for row in tables[table][1]:
                    self.assertEqual(len(row), len(columns), f"{name}.md · {table}: {row}")

    # K3
    def test_states_closed(self):
        results = {one(r["Kết quả"]) for r in self.decision}
        self.assertLessEqual(results, self.states)
        outputs = {one(r["Trạng thái"]) for r in records(self.tables["channels"]["outputs"])}
        self.assertEqual(outputs, self.states, "channels.outputs phải có đúng mỗi trạng thái một dòng")

    # K4
    def test_field_references(self):
        self.assertLessEqual(set(self.generic), set(self.text_fields))
        for row in self.ask_order:
            self.assertIn(one(row["Trường thiếu"]), self.text_fields)
            self.assertIn(one(row["intent"]), self.intents)
        self.assertLessEqual(set(self.command_intent.values()), self.intents)

    # K5
    def test_decision_codes_and_default(self):
        codes = [r["#"] for r in self.decision]
        self.assertEqual(codes, [f"R{i}" for i in range(1, len(codes) + 1)])
        last = self.decision[-1]
        for column in REQUIRED["readiness"]["decision"][1:8]:
            self.assertEqual(last[column], ANY, f"dòng cuối phải là 'còn lại': {column}")
        resolve_codes = [r["#"] for r in self.resolve]
        self.assertEqual(resolve_codes, [f"I{i}" for i in range(1, len(resolve_codes) + 1)])

    # K6
    def test_template_blocks(self):
        registry = {one(r["Khối template"]): r for r in records(self.tables["channels"]["blocks"])}
        used = {one(r["Khối template"]) for r in self.ask_order} | set(self.fallback)
        for row in records(self.tables["channels"]["outputs"]):
            used |= set(ticks(row["Khối template"]))
        self.assertEqual(used, set(registry), "channels.blocks phải liệt kê đúng các khối được dùng")
        for block, row in registry.items():
            source = (TEMPLATES / one(row["File"])).read_text(encoding="utf-8")
            exists = "{{#block " + block + "}}" in source
            status = one(row["Có sẵn"])
            self.assertIn(status, {"có", "cần thêm"})
            self.assertEqual(exists, status == "có", f"khối {block}: cột Có sẵn sai")

    # K7
    def test_no_open_owner_decision_in_rule_files(self):
        for name in REQUIRED:
            text = (RULES / f"{name}.md").read_text(encoding="utf-8")
            self.assertNotIn("CHỜ OWNER CHỐT", text, f"{name}.md")

    # K10
    def test_decision_cells_closed(self):
        domains = {
            "command": {NONE} | set(self.command_intent),
            "command_payload": self.code_facts["command_payload"],
            "text": self.code_facts["text"],
            "media": self.code_facts["media"],
            "extraction_ok": self.code_facts["extraction_ok"],
            "reply_target": self.code_facts["reply_target"],
            "intent": self.intents,
        }
        for row in self.decision:
            for column, domain in domains.items():
                if row[column] != ANY:
                    self.assertLessEqual(set(ticks(row[column])), domain, f"{row['#']} · {column}")
                    self.assertTrue(ticks(row[column]), f"{row['#']} · {column}: ô rỗng")
            for column in ("cần CÓ tất cả", "cần CÓ ít nhất một"):
                if row[column] != EMPTY:
                    self.assertLessEqual(set(ticks(row[column])), set(self.text_fields), f"{row['#']} · {column}")

    def test_normalize_cases(self):
        rows = records(self.tables["facts"]["normalize"])
        self.assertEqual([r["#"] for r in rows], ["N1", "N2", "N3", "N4", "N5"])
        for row in records(self.tables["examples"]["normalize_cases"]):
            field = one(row["Trường"])
            generic = normalize(one(row["Giá trị"])) in self.generic.get(field, set())
            self.assertEqual(generic, one(row["Chung chung"]) == "có", f"{row['#']}")

    # K8, K9
    def test_examples(self):
        covered = set()
        for row in records(self.tables["examples"]["examples"]):
            with self.subTest(example=row["#"]):
                rule, result, asks = self.evaluate(row)
                self.assertEqual(result, one(row["Kết quả"]))
                self.assertEqual(rule, row["Luật"])
                expected_asks = ticks(row["Hỏi"])
                self.assertEqual(asks, expected_asks)
                covered.add(rule)
        self.assertEqual(covered, {r["#"] for r in self.decision}, "mỗi luật phải có ít nhất một ví dụ")

    def present_fields(self, cell):
        present = set()
        if cell in {NONE, EMPTY}:
            return present
        for part in cell.split(";"):
            field, _, value = part.partition("=")
            field = field.strip().strip("`")
            self.assertIn(field, self.text_fields, f"trường lạ trong Dữ kiện: {field}")
            mark = MARK.search(value)
            if mark:
                value = value[: mark.start()]
                if mark["mark"] in INVALID_MARKS:
                    continue
                self.assertEqual(mark["mark"], "ảnh-đã-xem")
            value = normalize(value)
            if value and value not in self.generic.get(field, set()):
                present.add(field)
        return present

    def evaluate(self, row):
        command = one(row["command"])
        model_intent = one(row["intent (model)"])
        if model_intent not in self.intents:
            model_intent = "unknown"
        present = self.present_fields(row["Dữ kiện"])
        media = "có" if int(row["media_seen"]) + int(row["media_unseen"]) > 0 else "không"
        intent = None
        for rule in self.resolve:
            if rule["command"] != ANY and command not in ticks(rule["command"]):
                continue
            if rule["intent (model)"] != ANY and model_intent not in ticks(rule["intent (model)"]):
                continue
            if rule["cần CÓ"] != EMPTY and not set(ticks(rule["cần CÓ"])) <= present:
                continue
            target = one(rule["intent dùng"])
            intent = {"=model": model_intent, "=command": self.command_intent.get(command)}.get(target, target)
            break
        self.assertIn(intent, self.intents)
        facts = {
            "command": command,
            "command_payload": one(row["command_payload"]),
            "text": one(row["text"]),
            "media": media,
            "extraction_ok": one(row["extraction_ok"]),
            "reply_target": one(row["reply_target"]),
            "intent": intent,
        }
        for rule in self.decision:
            if any(rule[c] != ANY and facts[c] not in ticks(rule[c]) for c in facts):
                continue
            need_all = rule["cần CÓ tất cả"]
            if need_all != EMPTY and not set(ticks(need_all)) <= present:
                continue
            need_any = rule["cần CÓ ít nhất một"]
            if need_any != EMPTY and not set(ticks(need_any)) & present:
                continue
            result = one(rule["Kết quả"])
            return rule["#"], result, self.asks(result, intent, present)
        raise AssertionError("không luật nào khớp")

    def asks(self, result, intent, present):
        if result != "NEED_INFO":
            return []
        rows = [r for r in self.ask_order if one(r["intent"]) == intent and one(r["Trường thiếu"]) not in present]
        rows.sort(key=lambda r: int(r["Thứ tự"]))
        blocks = []
        for row in rows:
            block = one(row["Khối template"])
            if block not in blocks:
                blocks.append(block)
        if not blocks:
            return list(self.fallback)
        return blocks[: self.question_limits["max_fields_per_ask"]]

    def test_roles_cells(self):
        actors = {one(r["Mã"]) for r in records(self.tables["roles"]["actors"])}
        allowed = {one(r["Mã"]) for r in records(self.tables["roles"]["permission_states"])}
        columns = self.tables["roles"]["actions"][0][2:]
        self.assertEqual({one(c) for c in columns}, actors)
        for row in records(self.tables["roles"]["actions"]):
            for column in columns:
                cell = row[column]
                if cell != EMPTY:
                    self.assertIn(one(cell), allowed, f"{row['#']} · {column}")

    def test_preflight_cells(self):
        actors = {one(r["Mã"]) for r in records(self.tables["roles"]["actors"])}
        for row in records(self.tables["preflight"]["checks"]):
            self.assertIn(one(row["Ai ký"]), actors, row["#"])
            self.assertIn(one(row["Miễn được?"]), {"có", "không"}, row["#"])

    def test_answer_link_cells(self):
        for row in records(self.tables["questions"]["answer_link"]):
            self.assertIn(one(row["Kết quả"]), {"LINK", "IGNORE"}, row["#"])


if __name__ == "__main__":
    unittest.main()
