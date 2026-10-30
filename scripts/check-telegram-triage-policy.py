"""Read-only documentation trace checker; a match is not proof of business semantics."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

POLICY = 'Docs/01-product/telegram-triage-noissue_business_261015_v1.0.0.md'
MARKER = 'telegram-triage-trace'

class PolicyError(ValueError):
    pass

def local_path(root, value):
    if not isinstance(value, str) or not value or '\\' in value or re.match(r'^[A-Za-z]:', value):
        raise PolicyError('invalid local path')
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise PolicyError('missing or escaping local path: ' + value)
    return target

def symbols(path):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    found = set()
    def walk(nodes, prefix=''):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                found.add(name)
                walk(node.body, name + '.')
    walk(tree.body)
    return found

def normalized_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()

def check(root, policy=POLICY):
    root = Path(root).resolve()
    doc = local_path(root, policy)
    text = doc.read_text(encoding='utf-8')
    blocks = re.findall(r'```json ' + MARKER + r'\n(.*?)\n```', text, re.S)
    if len(blocks) != 1:
        raise PolicyError('expected one embedded trace block')
    try:
        trace = json.loads(blocks[0])
    except ValueError as exc:
        raise PolicyError('invalid trace JSON') from exc
    if trace.get('schema') != 'telegram-triage-policy/v1' or trace.get('owner') != 'AlexChu':
        raise PolicyError('invalid schema or owner')
    expected = {f'TG-{n:02}' for n in range(1, 34)}
    headings = re.findall(r'^### (TG-\d{2})\b', text, re.M)
    rules = trace.get('rules', [])
    ids = [rule.get('id') for rule in rules]
    if len(headings) != len(set(headings)) or set(headings) != expected:
        raise PolicyError('duplicate or missing rule headings')
    if len(ids) != len(set(ids)) or set(ids) != expected:
        raise PolicyError('duplicate or missing trace rules')
    cache = {}
    for rule in rules:
        for kind in ('code', 'tests'):
            refs = rule.get(kind, [])
            if not refs:
                raise PolicyError('missing ' + kind + ' trace: ' + rule['id'])
            for ref in refs:
                path = local_path(root, ref['path'])
                if kind == 'tests' and (not ref['path'].startswith('listener/tests/test_') or not ref['symbol'].split('.')[-1].startswith('test_')):
                    raise PolicyError('test reference must target a real test method')
                if path.suffix != '.py':
                    raise PolicyError('symbol target must be Python')
                if path not in cache:
                    cache[path] = symbols(path)
                if ref['symbol'] not in cache[path]:
                    raise PolicyError('missing symbol: ' + ref['symbol'])
        for prompt in rule.get('prompts', []):
            local_path(root, prompt)
    inventory = trace.get('runtime_inventory', {})
    actual = {str(p.relative_to(root)).replace('\\', '/') for p in (root/'listener/src/tg_triage').glob('*.py')}
    actual |= {str(p.relative_to(root)).replace('\\', '/') for p in (root/'listener/prompts').glob('*.md')}
    actual |= {str(p.relative_to(root)).replace('\\', '/') for p in (root/'listener/templates').rglob('*.md')}
    if set(inventory) != actual:
        raise PolicyError('REVIEW_REQUIRED runtime inventory changed')
    for path, digest in inventory.items():
        if normalized_hash(local_path(root, path)) != digest:
            raise PolicyError('REVIEW_REQUIRED runtime hash mismatch: ' + path)
    for path in trace.get('legacy_documents', []):
        old = local_path(root, path).read_text(encoding='utf-8')
        if '<!-- telegram-triage-historical -->' not in old or Path(policy).name not in old:
            raise PolicyError('legacy triage status/link missing: ' + path)
    linked_docs = [doc] + [local_path(root, p) for p in trace.get('related_documents', [])]
    for linked in linked_docs:
        for value in re.findall(r'\]\(([^)]+)\)', linked.read_text(encoding='utf-8')):
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', value) or value.startswith('#'):
                continue
            value = value.split('#', 1)[0]
            if not value:
                continue
            local_path(root, (linked.parent/value).relative_to(root).as_posix())
    return {'status': 'PASS', 'rules': len(rules), 'runtime_files': len(inventory),
            'meaning': 'Trace, local links and reviewed bytes match; human semantic review remains required.'}

if __name__ == '__main__':
    try:
        print(json.dumps(check(Path(__file__).resolve().parents[1]), ensure_ascii=False))
    except (PolicyError, KeyError, TypeError, SyntaxError, ValueError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
