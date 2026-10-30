"""Package boundary is checked from syntax, without importing dependencies."""
import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'tg_triage'
FORBIDDEN = 'nailhub'


class IsolationTests(unittest.TestCase):
    def test_source_does_not_import_other_service(self):
        violations=[]
        for path in SOURCE.rglob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8'),filename=str(path))
            for node in ast.walk(tree):
                names=[]
                if isinstance(node,ast.Import):names=[alias.name for alias in node.names]
                elif isinstance(node,ast.ImportFrom):
                    names=[node.module or '']
                    if node.level:names += [alias.name for alias in node.names]
                elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='__import__':
                    names=[arg.value for arg in node.args[:1] if isinstance(arg,ast.Constant) and isinstance(arg.value,str)]
                if any(name.split('.')[0]==FORBIDDEN for name in names):
                    violations.append(f'{path.name}:{node.lineno}')
        self.assertEqual(violations,[])
    def test_listener_source_has_no_other_service_mentions(self):
        self.assertEqual([path.name for path in SOURCE.rglob('*.py')
                          if FORBIDDEN in path.read_text(encoding='utf-8').casefold()],[])

