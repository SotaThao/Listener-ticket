"""Reproducible pure-Python wheels and commit-bound source stages, without setuptools."""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

SERVICES = {'listener': 'tg_triage', 'nailhub': 'nailhub'}
TEST_ROOT_FILES = {
    'listener': ('.env.example', 'config/listener.example.toml', 'systemd/tg-triage.service'),
    'nailhub': ('config/nailhub.example.toml', 'systemd/tg-nailhub.service'),
}
OPTIONAL_TEST_ROOT_FILES = ('systemd/agentfleet-tmpfiles.conf',)



def sha256(data):
    return hashlib.sha256(data).hexdigest()


def record_hash(data):
    return 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')


def inventory(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink():
            raise ValueError('Source symlink is not supported: ' + str(path))
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = path.read_bytes()
    return result


def wheel_bytes(source, service, commit):
    source = Path(source)
    project_data = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
    project = project_data['project']
    distribution = re.sub(r'[-_.]+', '_', project['name'])
    package = SERVICES[service]
    if distribution != package or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9._]*)', project['version']):
        raise ValueError('Unexpected distribution/version')
    version = project['version']
    dist_info = distribution + '-' + version + '.dist-info'
    files = {}
    for name, data in inventory(source / 'src').items():
        if name.split('/')[0] != package:
            raise ValueError('Source contains a different top-level package: ' + name)
        files[name] = data
    if package + '/__init__.py' not in files:
        raise ValueError('Package __init__.py missing')
    data_files = project_data.get('tool', {}).get('setuptools', {}).get('data-files', {})
    for target, patterns in data_files.items():
        if PurePosixPath(target).is_absolute() or '..' in PurePosixPath(target).parts:
            raise ValueError('Unsafe data-file destination')
        for pattern in patterns:
            if PurePosixPath(pattern).is_absolute() or '..' in PurePosixPath(pattern).parts:
                raise ValueError('Unsafe data-file source')
            matched = sorted(source.glob(pattern))
            if not matched:
                raise ValueError('Missing data file: ' + pattern)
            for item in matched:
                if item.is_symlink() or not item.is_file():
                    raise ValueError('Invalid data file')
                name = distribution + '-' + version + '.data/data/' + target + '/' + item.name
                if name in files:
                    raise ValueError('Duplicate data file')
                files[name] = item.read_bytes()
    headers = ['Metadata-Version: 2.1', 'Name: ' + project['name'], 'Version: ' + version,
               'Summary: ' + project.get('description', ''),
               'Requires-Python: ' + project['requires-python']]
    headers += ['Requires-Dist: ' + requirement for requirement in project.get('dependencies', [])]
    if any('\n' in header or '\r' in header for header in headers):
        raise ValueError('Invalid metadata header')
    files[dist_info + '/METADATA'] = ('\n'.join(headers) + '\n\n').encode()
    files[dist_info + '/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: agentfleet-stdlib\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    files[dist_info + '/top_level.txt'] = (package + '\n').encode()
    files[dist_info + '/agentfleet.json'] = (json.dumps({'commit': commit, 'service': service}, sort_keys=True) + '\n').encode()
    record = dist_info + '/RECORD'
    rows = [[name, record_hash(data), str(len(data))] for name, data in sorted(files.items())]
    rows.append([record, '', ''])
    stream = io.StringIO(newline='')
    csv.writer(stream, lineterminator='\n').writerows(rows)
    files[record] = stream.getvalue().encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as wheel:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            wheel.writestr(info, data)
    return distribution + '-' + version + '-py3-none-any.whl', buffer.getvalue()


def verify_record(wheel_data):
    with zipfile.ZipFile(io.BytesIO(wheel_data)) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate wheel entries')
        if any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts for name in names):
            raise ValueError('Unsafe wheel path')
        records = [name for name in names if name.endswith('.dist-info/RECORD')]
        if len(records) != 1:
            raise ValueError('One RECORD required')
        rows = list(csv.reader(io.StringIO(wheel.read(records[0]).decode())))
        if any(len(row) != 3 for row in rows) or len(rows) != len(names) or {row[0] for row in rows} != set(names):
            raise ValueError('RECORD inventory differs from wheel')
        for name, checksum, size in rows:
            if name == records[0]:
                if checksum or size:
                    raise ValueError('RECORD must have empty self hash/size')
            else:
                data = wheel.read(name)
                if checksum != record_hash(data) or size != str(len(data)):
                    raise ValueError('RECORD mismatch: ' + name)
    return True


def archive_sources(archive, service, commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Full commit SHA required')
    result = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
        if tar.pax_headers.get('comment') != commit:
            raise ValueError('Git archive commit differs from recorded commit')
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            allowed_root = member.name in (*TEST_ROOT_FILES[service], *OPTIONAL_TEST_ROOT_FILES)
            allowed_directory = member.isdir() and member.name.rstrip('/') in ('config', 'systemd')
            if (name.is_absolute() or '..' in name.parts or '\\' in member.name or not name.parts
                    or (name.parts[0] != service and not allowed_root and not allowed_directory)):
                raise ValueError('Unexpected source archive path')
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError('Non-regular source archive member')
            relative = name.as_posix()
            if relative in result:
                raise ValueError('Duplicate source archive entry')
            result[relative] = tar.extractfile(member).read()
    if service + '/pyproject.toml' not in result:
        raise ValueError('Package absent from commit')
    return result


def stage_archive(service, stage, archive, commit):
    files = archive_sources(archive, service, commit)
    stage = Path(stage)
    if stage.exists() and any(stage.iterdir()):
        raise ValueError('Stage must be empty')
    source_root = stage / 'source'
    source_root.mkdir(parents=True)
    source = source_root / service
    for name, data in files.items():
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    filename, data = wheel_bytes(source, service, commit)
    verify_record(data)
    (stage / filename).write_bytes(data)
    (stage / 'source.tar').write_bytes(archive)
    manifest = {'service': service, 'commit': commit, 'wheel': filename,
                'wheel_sha256': sha256(data), 'source_archive_sha256': sha256(archive),
                'source_files': {name: sha256(content) for name, content in sorted(files.items())}}
    (stage / 'build.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return manifest


def build_stage(service, stage, *, repo=None, commit='HEAD'):
    repo = Path(repo or Path(__file__).resolve().parents[1])
    resolved = subprocess.run(['git', '-c', 'core.attributesfile=' + os.devnull, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', '-C', str(repo), 'rev-parse', commit + '^{commit}'],
                              check=True, capture_output=True, text=True).stdout.strip()
    env = os.environ.copy()
    env['GIT_ATTR_NOSYSTEM'] = '1'
    candidates = (*TEST_ROOT_FILES[service], *OPTIONAL_TEST_ROOT_FILES)
    present = subprocess.run(['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', resolved, *candidates],
                             check=True, capture_output=True, text=True).stdout.splitlines()
    if any(name not in present for name in TEST_ROOT_FILES[service]):
        raise ValueError('Commit is missing required package test fixtures')
    archive = subprocess.run(['git', '-c', 'core.attributesfile=' + os.devnull, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf', '-C', str(repo), 'archive', '--format=tar', resolved, service, *present],
                             check=True, capture_output=True, env=env).stdout
    return stage_archive(service, stage, archive, resolved)


def verify_stage(stage, service):
    stage = Path(stage)
    manifest = json.loads((stage / 'build.json').read_text(encoding='utf-8'))
    if manifest['service'] != service:
        raise ValueError('Stage service mismatch')
    archive = (stage / 'source.tar').read_bytes()
    if sha256(archive) != manifest['source_archive_sha256']:
        raise ValueError('Source archive checksum mismatch')
    archived = archive_sources(archive, service, manifest['commit'])
    source = stage / 'source' / service
    staged = inventory(stage / 'source')
    hashes = {name: sha256(content) for name, content in sorted(staged.items())}
    if archived != staged or hashes != manifest['source_files']:
        raise ValueError('Staged source differs from recorded git archive')
    filename, expected = wheel_bytes(source, service, manifest['commit'])
    if manifest['wheel'] != filename:
        raise ValueError('Wheel name differs from source metadata')
    data = (stage / filename).read_bytes()
    verify_record(data)
    if data != expected or sha256(data) != manifest['wheel_sha256']:
        raise ValueError('Wheel differs from staged source/commit')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=SERVICES)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--commit', default='HEAD', help='Committed sources only; ignores working-tree edits')
    parser.add_argument('--repo', type=Path)
    args = parser.parse_args(argv)
    manifest = build_stage(args.service, args.stage, repo=args.repo, commit=args.commit)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
