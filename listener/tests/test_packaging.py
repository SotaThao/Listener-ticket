"""Installed runtime assets and shared model-lock protocol must match the package."""
import hashlib
import re
import tomllib
import unittest
from pathlib import Path
import agentfleet_common
from agentfleet_common import ai_transport, model_lock

ROOT = Path(__file__).resolve().parents[1]

# Any change to agentfleet-common requires a version bump AND updating these
# constants in a reviewed listener PR. This staged test binds the target venv's
# transport/lock bytes to reviewed code before the bot is stopped.
REVIEWED_COMMON_SHA256 = {
    'ai_transport': 'b1d61881efc7437d9b4d77a2767b1935434e905905da66841229f9263a5c8c4b',
    'model_lock': '768041570feda133e227fa07bc1a8b6b4229dbdfb6a763718c411d6c901459ca',
}



class PackagingTests(unittest.TestCase):
    def test_runtime_markdown_is_packaged(self):
        metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf8'))
        data_files = metadata['tool']['setuptools']['data-files']
        for directory, excluded in (('prompts', set()), ('templates/github-ticket', set()),
                                    ('rules', {'README.md', 'roles.md', 'preflight.md'})):
            listed = set(data_files['share/tg-triage/' + directory])
            for path in (ROOT / directory).glob('*.md'):
                with self.subTest(path=path.name, directory=directory):
                    self.assertTrue(path.relative_to(ROOT).as_posix() in listed or path.name in excluded)

    def test_common_version_and_protocol_match_dependency(self):
        metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf8'))
        pins = [re.fullmatch(r'agentfleet-common==(.+)', dependency)
                for dependency in metadata['project']['dependencies']]
        versions = [match.group(1) for match in pins if match]
        self.assertEqual(versions, [agentfleet_common.__version__])
        self.assertEqual(agentfleet_common.PROTOCOL, 1)

    def test_common_installed_bytes_match_reviewed_package(self):
        for module in (ai_transport, model_lock):
            name = module.__name__.rsplit('.', 1)[1]
            with self.subTest(module=name):
                source = Path(module.__file__).read_bytes().replace(b'\r\n', b'\n')
                self.assertEqual(hashlib.sha256(source).hexdigest(),
                                 REVIEWED_COMMON_SHA256[name])
