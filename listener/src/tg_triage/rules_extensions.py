"""Validated snapshots for proposed lifecycle and read-only intent rules.

This module does not infer evidence, send messages, or grant issue authority.
"""
import hashlib
import re
from pathlib import Path
from types import MappingProxyType

from .decision_rules import RulesError


class ExtensionRules:
    """Load the approved policy exactly once; source DRAFTs fail closed."""

    __slots__ = ('lifecycle', 'intents', 'explicit_evidence', 'clarification', 'fingerprint', '_frozen')

    def __init__(self, directory):
        directory = Path(directory)
        lifecycle = self._table(directory / 'lifecycle.md', 'lifecycle', 'disposition',
                                ('Bằng chứng host đã kiểm', 'Quyết định'))
        intents = self._table(directory / 'analysis-status.md', 'analysis_status', 'intent_routes',
                              ('Intent model đã kiểm nguồn', 'Quyết định'))
        # Supported safety semantics are constrained until the implementation
        # is deliberately changed and reviewed for additional outcomes.
        if lifecycle[1] != {'evidence': 'APPEND_CLOSED', 'recurrence': 'RELATED',
                            'reused_success': 'QUESTION', 'unclear': 'QUESTION',
                            'unavailable': 'HOLD'}:
            raise RulesError('lifecycle.md: unsupported disposition table')
        if intents[1] != {'analyze': 'ANALYZE', 'status': 'STATUS'}:
            raise RulesError('analysis-status.md: unsupported intent routes')
        self.lifecycle = MappingProxyType(lifecycle[1])
        self.intents = MappingProxyType(intents[1])
        lines=lifecycle[0].splitlines()
        heading='## Bảng: explicit_evidence'
        if lines.count(heading)!=1:
            raise RulesError('lifecycle.md: missing explicit evidence table')
        start=lines.index(heading)+1
        while start<len(lines) and not lines[start].strip(): start+=1
        if lines[start:start+2]!=['| Xác nhận trực tiếp | Bằng chứng |','|---|---|']:
            raise RulesError('lifecycle.md: invalid explicit evidence table')
        confirmations=[]
        for line in lines[start+2:]:
            if not line.startswith('|'): break
            row=re.fullmatch(r'\| `([^`\r\n]{1,120})` \| `evidence` \|',line)
            if not row or row[1]!=row[1].strip() or row[1] in confirmations:
                raise RulesError('lifecycle.md: invalid explicit evidence')
            confirmations.append(row[1])
        if not confirmations: raise RulesError('lifecycle.md: empty explicit evidence')
        self.explicit_evidence=frozenset(confirmations)
        heading='## Bảng: clarification'
        if lines.count(heading)!=1: raise RulesError('lifecycle.md: missing clarification table')
        start=lines.index(heading)+1
        while start<len(lines) and not lines[start].strip(): start+=1
        if lines[start:start+2]!=['| Khóa | Văn bản |','|---|---|']:
            raise RulesError('lifecycle.md: invalid clarification table')
        clarification={}
        for line in lines[start+2:]:
            if not line.startswith('|'): break
            row=re.fullmatch(r'\| `([a-z_]+)` \| `([^`\r\n]{1,500})` \|',line)
            if not row or row[1] in clarification: raise RulesError('lifecycle.md: invalid clarification row')
            clarification[row[1]]=row[2]
        if (set(clarification)!={'question','evidence_answer','recurrence_answer','evidence_context_prefix'}
                or clarification['evidence_answer']==clarification['recurrence_answer']
                or any(clarification[k]!=clarification[k].strip() for k in clarification)):
            raise RulesError('lifecycle.md: invalid clarification values')
        self.clarification=MappingProxyType(clarification)
        digest = hashlib.sha256()
        for name, text in (('lifecycle.md', lifecycle[0]), ('analysis-status.md', intents[0])):
            digest.update(name.encode('utf8'))
            digest.update(text.encode('utf8'))
        self.fingerprint = digest.hexdigest()
        self._frozen = True

    def __setattr__(self, name, value):
        if getattr(self, '_frozen', False):
            raise AttributeError('rules snapshot is immutable')
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if getattr(self, '_frozen', False):
            raise AttributeError('rules snapshot is immutable')
        object.__delattr__(self, name)

    @staticmethod
    def _table(path, name, table, columns):
        try:
            text = path.read_text(encoding='utf8').replace('\r\n', '\n').replace('\r', '\n')
        except (OSError, UnicodeError):
            raise RulesError(f'{path}: cannot read rules') from None
        lines = text.splitlines()
        header = re.fullmatch(r'<!-- rule-file: ([a-z_]+) · version ([1-9][0-9]*) · status (DRAFT|ACTIVE) · audience: code -->', lines[0] if lines else '')
        if not header or header[1] != name or header[3] != 'ACTIVE':
            raise RulesError(f'{path}:1: rules must have an ACTIVE header')
        heading = '## Bảng: ' + table
        if lines.count(heading) != 1:
            raise RulesError(f'{path}: missing or duplicate table {table}')
        start = lines.index(heading) + 1
        while start < len(lines) and not lines[start].strip():
            start += 1
        expected = '| ' + ' | '.join(columns) + ' |'
        if start + 1 >= len(lines) or lines[start] != expected:
            raise RulesError(f'{path}: invalid table columns')
        if not re.fullmatch(r'\|---\|---\|', lines[start + 1]):
            raise RulesError(f'{path}: invalid table separator')
        rows = {}
        for line in lines[start + 2:]:
            if not line.startswith('|'):
                break
            row = re.fullmatch(r'\| `([a-z_]+)` \| `([A-Z_]+)` \|', line)
            if not row or row[1] in rows:
                raise RulesError(f'{path}: invalid or duplicate row')
            rows[row[1]] = row[2]
        return text, rows

    def disposition(self, evidence):
        return self.lifecycle.get(evidence, self.lifecycle['unavailable'])

    def route(self, intent):
        return self.intents.get(intent)
