"""Deploy one AgentFleet service; guard and rollback never operate on the other unit."""
import argparse
import ast
import csv
import importlib.metadata
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import tomllib
import uuid
import zipfile
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from .build_wheel import SERVICES, sha256, verify_stage
except ImportError:
    from build_wheel import SERVICES, sha256, verify_stage


DEFAULTS = {
    'listener': dict(code_dir=Path('/opt/agentfleet/listener'), config=Path('/etc/tg-triage/listener.toml'),
                     state_dir=Path('/var/lib/agentfleet/tg'), unit='tg-triage', other_unit='tg-nailhub'),
    'nailhub': dict(code_dir=Path('/opt/agentfleet/nailhub'), config=Path('/etc/nailhub/nailhub.toml'),
                    state_dir=Path('/var/lib/agentfleet/nailhub'), unit='tg-nailhub', other_unit='tg-triage'),
}


class DeploymentError(RuntimeError):
    pass


class GuardError(DeploymentError):
    pass


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirement(value):
    match = re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([^]]+)\])?', value)
    if match is None:
        raise GuardError('Invalid declared requirement: ' + value)
    return normalize(match[1]), frozenset(x.strip() for x in (match[2] or '').split(',') if x.strip())


def marker_matches(value, extras):
    if ';' not in value:
        return True
    expression = value.split(';', 1)[1].strip()
    environment = {'python_version': platform.python_version_tuple()[0] + '.' + platform.python_version_tuple()[1],
                   'python_full_version': platform.python_version(), 'sys_platform': sys.platform,
                   'os_name': os.name, 'platform_machine': platform.machine(),
                   'platform_system': platform.system(), 'platform_release': platform.release(),
                   'platform_version': platform.version(), 'platform_python_implementation': platform.python_implementation(),
                   'implementation_name': sys.implementation.name, 'implementation_version': platform.python_version()}
    versions = {'python_version', 'python_full_version', 'implementation_version'}
    try:
        tree = ast.parse(expression, mode='eval')
        def evaluate(node, extra):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return extra if node.id == 'extra' else environment[node.id]
            if isinstance(node, ast.BoolOp):
                values = [bool(evaluate(child, extra)) for child in node.values]
                if isinstance(node.op, ast.And):
                    return all(values)
                if isinstance(node.op, ast.Or):
                    return any(values)
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                left, right = evaluate(node.left, extra), evaluate(node.comparators[0], extra)
                op = node.ops[0]
                if isinstance(op, ast.In): return left in right
                if isinstance(op, ast.NotIn): return left not in right
                if any(isinstance(n, ast.Name) and n.id in versions for n in (node.left, node.comparators[0])):
                    left, right = tuple(int(x) for x in left.split('.')), tuple(int(x) for x in right.split('.'))
                if isinstance(op, ast.Eq): return left == right
                if isinstance(op, ast.NotEq): return left != right
                if isinstance(op, ast.Lt): return left < right
                if isinstance(op, ast.LtE): return left <= right
                if isinstance(op, ast.Gt): return left > right
                if isinstance(op, ast.GtE): return left >= right
            raise ValueError('Unsupported marker')
        return any(evaluate(tree.body, extra) for extra in ('', *extras))
    except (SyntaxError, KeyError, ValueError, TypeError):
        raise GuardError('Cannot safely evaluate dependency marker: ' + expression) from None


def guard_environment(site_paths, package, declared, *, new_wheel=None, prefix=None, allow_removed=()):
    """Inspect RECORD ownership and active transitive requirements, without importing packages."""
    sites = [Path(p) for p in site_paths]
    distributions = {}
    for dist in importlib.metadata.distributions(path=[str(p) for p in sites]):
        name = normalize(dist.metadata['Name'])
        if name in distributions:
            raise GuardError('Duplicate installed distribution: ' + name)
        distributions[name] = dist
    primary = normalize(package)
    allowed = {primary, 'pip', 'setuptools'}
    pending = [requirement(value) for value in declared if marker_matches(value, ())]
    visited = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        if name not in distributions:
            raise GuardError('Missing declared dependency: ' + name)
        allowed.add(name)
        pending += [requirement(value) for value in distributions[name].requires or () if marker_matches(value, extras)]
    records = set()
    primary_records = set()
    allowed_top = set()
    for name, dist in distributions.items():
        if name not in allowed:
            continue
        if dist.files is None:
            raise GuardError('Installed distribution has no RECORD: ' + name)
        for relative in dist.files:
            candidate = Path(dist.locate_file(relative)).resolve()
            records.add(candidate)
            if name == primary:
                primary_records.add(candidate)
            for site in sites:
                if candidate.is_relative_to(site.resolve()):
                    parts = candidate.relative_to(site.resolve()).parts
                    if parts:
                        allowed_top.add(parts[0])
    findings = []
    allowed_removed = []
    removal_allowlist = frozenset(allow_removed)  # Exact RECORD paths; no globs or directory prefixes.
    if new_wheel is not None and primary in distributions:
        if prefix is None:
            raise GuardError('Target prefix required to compare installed RECORD with new wheel')
        expected = set()
        info_files = set()
        with zipfile.ZipFile(new_wheel) as wheel:
            for name in wheel.namelist():
                parts = PurePosixPath(name).parts
                if parts[0].endswith('.data'):
                    if parts[1] != 'data':
                        raise GuardError('Unsupported wheel data scheme')
                    expected.add(Path(prefix).joinpath(*parts[2:]).resolve())
                elif parts[0].endswith('.dist-info'):
                    info_files.add(PurePosixPath(*parts[1:]).as_posix())
                else:
                    expected.update(site.joinpath(*parts).resolve() for site in sites)
        dist = distributions[primary]
        allowed_sources = {Path(dist.locate_file(value)).resolve() for value in removal_allowlist
                           if PurePosixPath(value).suffix == '.py'}
        for relative in dist.files:
            parts = PurePosixPath(str(relative).replace('\\', '/')).parts
            candidate = Path(dist.locate_file(relative)).resolve()
            if parts[0].endswith('.dist-info'):
                name = PurePosixPath(*parts[1:]).as_posix()
                # pip owns these installation receipts, not the wheel payload.
                if name in info_files or name in ('INSTALLER', 'REQUESTED', 'direct_url.json'):
                    continue
            if candidate in expected:
                continue
            if candidate.suffix == '.pyc':
                try:
                    source = Path(importlib.util.source_from_cache(str(candidate)))
                except ValueError:
                    source = candidate.with_suffix('.py')
                if source.resolve() in expected:
                    continue  # pip regenerates bytecode for a retained source file.
                if source.resolve() in allowed_sources:
                    allowed_removed.append(str(relative))
                    continue
            if str(relative) in removal_allowlist:
                allowed_removed.append(str(relative))
            else:
                findings.append('recorded file absent from new wheel: ' + str(relative))
    for site in sites:
        package_dir = site / package
        if package_dir.is_symlink():
            findings.append('symlinked package root: ' + str(package_dir))
        if package_dir.exists():
            for item in package_dir.rglob('*'):
                if item.is_symlink() or (item.is_file() and item.resolve() not in primary_records):
                    findings.append('unrecorded package file: ' + str(item))
                elif item.is_dir() and not any(p.is_relative_to(item.resolve()) for p in primary_records):
                    findings.append('unrecorded package directory: ' + str(item))
        for item in site.iterdir():
            name = item.name
            if name == '__pycache__' or name.endswith(('.dist-info', '.egg-info', '.data')):
                continue
            importable = ((item.is_dir() and name.isidentifier()) or
                          (item.is_file() and item.suffix in ('.py', '.pyw', '.so', '.pyd') and name.split('.')[0].isidentifier()))
            # A .pth file can load arbitrary code at interpreter startup too.
            if item.is_symlink() and (name.isidentifier() or importable):
                findings.append('symlinked top-level importable: ' + str(item))
            if (importable or item.suffix == '.pth') and name not in allowed_top:
                findings.append('unknown top-level importable: ' + str(item))
    if findings:
        raise GuardError('Venv guard refused deployment:\n' + '\n'.join(sorted(set(findings))))
    return {'distributions': sorted(name for name in distributions if name in allowed), 'findings': [],
            'allowed_removed': sorted(set(allowed_removed))}


@dataclass(frozen=True)
class Settings:
    service: str
    stage: Path
    code_dir: Path
    config: Path
    state_dir: Path
    backup_root: Path
    python: str
    site_paths: tuple
    prefix: Path
    unit: str
    other_unit: str
    db_path: Path | None = None
    systemctl: str = 'systemctl'
    journalctl: str = 'journalctl'
    health_wait: float = 30
    ready_timeout: float = 60
    ready_poll: float = 1
    test_paths: bool = False
    allow_removed: tuple[str, ...] = ()

    @property
    def package(self):
        return SERVICES[self.service]

    def database(self):
        if self.db_path is not None:
            return self.db_path
        if self.service == 'listener':
            return self.state_dir / 'triage.sqlite3'
        data = tomllib.loads(self.config.read_text(encoding='utf-8'))
        return Path(data['worker']['db_path'])

    def validate(self):
        if not self.test_paths:
            for name,value in DEFAULTS[self.service].items():
                if getattr(self,name) != value:
                    raise DeploymentError('Unit/path overrides require --test-paths: ' + name)
            if self.prefix != self.code_dir / '.venv' or self.db_path is not None:
                raise DeploymentError('Venv/database overrides require --test-paths')
            if self.backup_root != Path('/var/backups/agentfleet') or self.systemctl != 'systemctl' or self.journalctl != 'journalctl':
                raise DeploymentError('Command/backup overrides require --test-paths')
            if self.health_wait < 30 or self.ready_timeout != 60:
                raise DeploymentError('Production health wait must be >=30s and readiness timeout 60s')
        if self.unit == self.other_unit:
            raise DeploymentError('Target and other unit must differ')
        if self.code_dir.resolve() == Path(self.code_dir.anchor):
            raise DeploymentError('Target code cannot be a filesystem root')
        if not self.code_dir.is_absolute() or self.code_dir.is_symlink() or not self.code_dir.is_dir():
            raise DeploymentError('Existing absolute target code directory required')
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise DeploymentError('Existing non-symlink service state directory required')
        if self.state_dir.resolve().is_relative_to(self.code_dir.resolve()) or self.code_dir.resolve().is_relative_to(self.state_dir.resolve()):
            raise DeploymentError('State and code trees must be separate for safe code-only rollback')
        if not self.config.is_file() or self.config.is_symlink():
            raise DeploymentError('Existing regular target configuration required')
        if self.stage.resolve().is_relative_to(self.code_dir.resolve()) or self.backup_root.resolve().is_relative_to(self.code_dir.resolve()):
            raise DeploymentError('Stage/backups must be outside the code being backed up')
        if self.code_dir.resolve().is_relative_to(self.backup_root.resolve()):
            raise DeploymentError('Code must be outside the backup tree')
        if not self.prefix.resolve().is_relative_to(self.code_dir.resolve()):
            raise DeploymentError('Target venv must be inside target code directory')
        for site in self.site_paths:
            if not Path(site).resolve().is_relative_to(self.prefix.resolve()):
                raise DeploymentError('Site-packages must belong to target venv')
        if self.health_wait < 0 or self.ready_timeout <= 0 or self.ready_poll <= 0:
            raise DeploymentError('Invalid health wait')


def command(args, *, cwd=None, env=None):
    return subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True)


def owner_info(path):
    info = path.stat()
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode & 0o777}


def restore_owner(path, info):
    path.chmod(info['mode'])
    if os.name == 'posix':
        os.chown(path, info['uid'], info['gid'])


class Deployment:
    def __init__(self, settings, *, runner=command, sleep=time.sleep, clock=time.monotonic):
        self.settings = settings
        self.runner = runner
        self.sleep = sleep
        self.clock = clock

    def system(self, verb):
        return self.runner([self.settings.systemctl, verb, self.settings.unit])

    def snapshot(self, unit):
        result = self.runner([self.settings.systemctl, 'show', unit, '--property=MainPID',
                              '--property=NRestarts', '--property=ActiveState'])
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        try:
            return {'MainPID': int(properties['MainPID']), 'NRestarts': int(properties['NRestarts']),
                    'ActiveState': properties['ActiveState']}
        except (KeyError, ValueError):
            raise DeploymentError('Invalid systemctl show result for ' + unit) from None

    def test_package(self):
        env = os.environ.copy()
        env['PYTHONPATH'] = 'src'
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        with tempfile.TemporaryDirectory(prefix='agentfleet-deploy-tests-') as directory:
            env['AGENTFLEET_LOCK_DIR'] = directory
            result = self.runner([self.settings.python, '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'],
                                 cwd=self.settings.stage / 'source' / self.settings.service, env=env)
        return {'passed': True, 'tail': (result.stderr or result.stdout).strip().splitlines()[-3:]}

    def backup(self):
        settings = self.settings
        if self.snapshot(settings.unit)['ActiveState'] not in ('inactive', 'failed'):
            raise DeploymentError('Stop the target unit before taking its SQLite backup')
        database = settings.database()
        if not database.is_absolute() or not database.resolve().is_relative_to(settings.state_dir.resolve()):
            raise DeploymentError('SQLite path must belong to this service state directory')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        destination = settings.backup_root / (settings.service + '-' + stamp)
        destination.mkdir(parents=True, mode=0o700)
        destination.chmod(0o700)
        metadata = {'service': settings.service, 'code_dir': str(settings.code_dir.resolve()),
                    'config_path': str(settings.config.resolve()), 'config_sha256': sha256(settings.config.read_bytes()),
                    'config': owner_info(settings.config), 'database': str(database), 'database_present': database.exists()}
        with tarfile.open(destination / 'code.tar', 'w') as archive:
            archive.add(settings.code_dir, arcname='code', recursive=True)
        shutil.copy2(settings.config, destination / 'config.toml')
        (destination / 'config.toml').chmod(0o600)
        if database.exists():
            metadata['database_owner'] = owner_info(database)
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(destination / 'sqlite.backup')) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise DeploymentError('SQLite backup integrity_check failed')
            (destination / 'sqlite.backup').chmod(0o600)
            metadata['database_sha256'] = sha256((destination / 'sqlite.backup').read_bytes())
        metadata['code_sha256'] = sha256((destination / 'code.tar').read_bytes())
        (destination / 'backup.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        return destination

    def verify_installed(self, manifest):
        settings = self.settings
        wheel = settings.stage / manifest['wheel']
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.endswith('.dist-info/RECORD'):
                    continue  # pip rewrites RECORD with installed paths and generated bytecode.
                parts = PurePosixPath(name).parts
                if '.data' in parts[0]:
                    if len(parts) < 3 or parts[1] != 'data':
                        raise DeploymentError('Unsupported wheel data scheme')
                    installed = settings.prefix.joinpath(*parts[2:])
                else:
                    matches = [Path(site).joinpath(*parts) for site in settings.site_paths if Path(site).joinpath(*parts).is_file()]
                    if len(matches) != 1:
                        raise DeploymentError('Installed file missing/ambiguous: ' + name)
                    installed = matches[0]
                if installed.is_symlink() or not installed.is_file() or installed.read_bytes() != archive.read(name):
                    raise DeploymentError('Installed bytes differ from staged wheel: ' + name)
        return {'bytes_match': True}

    def rollback(self, backup, before, *, restore_db=False):
        settings = self.settings
        metadata = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
        if metadata['service'] != settings.service or metadata['code_dir'] != str(settings.code_dir.resolve()) or metadata['config_path'] != str(settings.config.resolve()):
            raise DeploymentError('Backup belongs to a different service or target paths')
        database = Path(metadata['database'])
        if not database.resolve().is_relative_to(settings.state_dir.resolve()):
            raise DeploymentError('Backup SQLite belongs to a different state directory')
        if sha256((backup / 'config.toml').read_bytes()) != metadata['config_sha256']:
            raise DeploymentError('Backup configuration checksum mismatch')
        if restore_db and metadata['database_present'] and sha256((backup / 'sqlite.backup').read_bytes()) != metadata['database_sha256']:
            raise DeploymentError('Backup SQLite checksum mismatch')
        archive_bytes = (backup / 'code.tar').read_bytes()
        if sha256(archive_bytes) != metadata['code_sha256']:
            raise DeploymentError('Backup archive checksum mismatch')
        self.system('stop')  # Never replace a running process's state.
        with tempfile.TemporaryDirectory(prefix='agentfleet-restore-', dir=settings.code_dir.parent) as directory:
            with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
                for member in archive.getmembers():
                    parts = PurePosixPath(member.name).parts
                    if not parts or parts[0] != 'code' or '..' in parts or PurePosixPath(member.name).is_absolute():
                        raise DeploymentError('Unsafe backup archive')
                    if member.islnk() and (PurePosixPath(member.linkname).is_absolute() or '..' in PurePosixPath(member.linkname).parts):
                        raise DeploymentError('Unsafe backup hardlink')
                # This hash-checked local backup must retain venv symlinks to /usr/bin/python.
                archive.extractall(directory, filter='fully_trusted')
            failed = settings.code_dir.with_name(settings.code_dir.name + '.failed-' + uuid.uuid4().hex)
            settings.code_dir.rename(failed)
            (Path(directory) / 'code').rename(settings.code_dir)
        shutil.copy2(backup / 'config.toml', settings.config)
        restore_owner(settings.config, metadata['config'])
        if restore_db:
            database = Path(metadata['database'])
            for suffix in ('', '-wal', '-shm'):
                current = Path(str(database) + suffix)
                if current.exists():
                    current.rename(current.with_name(current.name + '.failed-' + uuid.uuid4().hex))
            if metadata['database_present']:
                shutil.copy2(backup / 'sqlite.backup', database)
                restore_owner(database, metadata['database_owner'])
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as restored:
                    if restored.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise DeploymentError('Restored SQLite integrity_check failed')
        if before['ActiveState'] == 'active':
            self.system('start')
            if self.snapshot(settings.unit)['ActiveState'] != 'active':
                raise DeploymentError('Rollback unit is not active')
        return {'restored': True, 'database_restored': restore_db, 'failed_code_preserved': str(failed)}

    def save_result(self, result, backup):
        text = json.dumps(result, indent=2, sort_keys=True) + '\n'
        for directory in (self.settings.stage, backup):
            if directory is not None:
                (directory / 'deployment.json').write_text(text, encoding='utf-8')

    def journal_since(self, started):
        return self.runner([self.settings.journalctl,'-u',self.settings.unit,'--since',started,
                            '--no-pager','--output=cat']).stdout.splitlines()

    def check_journal(self, lines, result):
        # Python stderr logging is not mapped to journald PRIORITY; inspect formatted severity.
        errors=[line for line in lines if ' ERROR ' in line or 'Traceback' in line]
        result['journal_error_lines']=len(errors)
        if errors:
            raise DeploymentError('Target unit has error journal lines since start')
        return any('NailHub ready owner_verified=1' in line for line in lines)

    def report_other(self, result):
        try:
            after=self.snapshot(self.settings.other_unit)
        except Exception as error:
            after={'query_error': type(error).__name__}
        result['other_after']=after
        if after != result['other_before']:
            warning='Other unit MainPID/NRestarts/ActiveState changed; target is not rolled back for this warning'
            result.setdefault('warnings',[]).append(warning)
            print('WARNING: '+warning)

    def deploy(self):
        settings=self.settings
        settings.validate()
        manifest=verify_stage(settings.stage,settings.service)
        source=settings.stage/'source'/settings.service
        declared=tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'))['project']['dependencies']
        tests=self.test_package()
        guard=guard_environment(settings.site_paths,settings.package,declared,
                                new_wheel=settings.stage/manifest['wheel'],prefix=settings.prefix,allow_removed=settings.allow_removed)
        for relative in guard['allowed_removed']:
            print('ALLOW-REMOVED: ' + relative)
        before=self.snapshot(settings.unit)
        other_before=self.snapshot(settings.other_unit)
        database=settings.database()
        if not database.is_absolute() or not database.resolve().is_relative_to(settings.state_dir.resolve()):
            raise DeploymentError('SQLite path must belong to this service state directory')
        result={'service':settings.service,'commit':manifest['commit'],'wheel_sha256':manifest['wheel_sha256'],
                'backup_path':None,'tests':tests,'guard':guard,'before':before,'other_before':other_before,
                'warnings':[],'success':False,'start_issued':False,'allowed_removed':guard['allowed_removed']}
        backup=None
        stopped=False
        try:
            self.system('stop')
            stopped=True
            backup=self.backup()  # SQLite is consistent with the final stopped state.
            result['backup_path']=str(backup)
            previous_umask=os.umask(0o022)
            try:
                self.runner([settings.python,'-m','pip','install','--no-index','--no-deps','--force-reinstall',
                             str(settings.stage/manifest['wheel'])])
            finally:
                os.umask(previous_umask)
            result['installed']=self.verify_installed(manifest)
            result['installed_guard']=guard_environment(settings.site_paths,settings.package,declared,
                                    new_wheel=settings.stage/manifest['wheel'],prefix=settings.prefix)
            started=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f UTC')
            deadline=self.clock()+settings.ready_timeout
            result['start_issued']=True  # Even a failed start request may have launched a writer.
            self.system('start')
            self.sleep(settings.health_wait)
            if settings.service=='nailhub':
                while True:
                    if self.check_journal(self.journal_since(started),result):
                        result['owner_ready']=True
                        break
                    remaining=deadline-self.clock()
                    if remaining<=0:
                        raise DeploymentError('NailHub ready owner_verified=1 missing within 60s')
                    self.sleep(min(settings.ready_poll,remaining))
            self.check_journal(self.journal_since(started),result)
            result['after']=self.snapshot(settings.unit)
            own=result['after']
            if own['ActiveState']!='active' or own['MainPID']<=0 or own['NRestarts']!=0:
                raise DeploymentError('Target unit inactive or NRestarts changed (must be 0 after manual start)')
            self.report_other(result)
            result['success']=True
            self.save_result(result,backup)
        except Exception as error:
            result['success']=False
            result['error']=type(error).__name__+': '+str(error)
            try:
                if backup is not None:
                    result['rollback']=self.rollback(backup,before,restore_db=not result['start_issued'])
                elif stopped and before['ActiveState']=='active':
                    self.system('start')  # Backup failed before any installation; original files are intact.
                    result['rollback']={'restored':True,'original_install_unchanged':True,'database_restored':False}
            except Exception as rollback_error:
                result['rollback']={'restored':False,'error':type(rollback_error).__name__+': '+str(rollback_error)}
            self.report_other(result)
            self.save_result(result,backup)
            raise DeploymentError(json.dumps(result,indent=2,sort_keys=True)) from error
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, choices=SERVICES)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--allow-removed', action='append', default=[], metavar='RECORD_PATH',
                        help='Permit exactly this installed RECORD path to be absent from the wheel (repeatable)')
    parser.add_argument('--restore-db', action='store_true', help='Explicitly restore DB with a manual rollback')
    parser.add_argument('--test-paths', action='store_true', help='Allow TEMP paths/units and short health waits for tests only')
    parser.add_argument('--rollback', type=Path, help='Restore this service from a validated deploy backup')
    parser.add_argument('--code-dir', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--db-path', type=Path)
    parser.add_argument('--backup-root', type=Path, default=Path('/var/backups/agentfleet'))
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--site-packages', action='append', type=Path)
    parser.add_argument('--prefix', type=Path, default=Path(sys.prefix))
    parser.add_argument('--unit')
    parser.add_argument('--other-unit')
    parser.add_argument('--systemctl', default='systemctl')
    parser.add_argument('--journalctl', default='journalctl')
    parser.add_argument('--health-wait', type=float, default=30)
    args = parser.parse_args(argv)
    if args.restore_db and args.rollback is None:
        parser.error('--restore-db requires --rollback')
    if not args.test_paths:
        defaults=DEFAULTS[args.service]
        for field in ('unit','other_unit','code_dir','config','state_dir'):
            value=getattr(args,field)
            if value is not None and value!=defaults[field]:
                parser.error('Overriding '+field+' requires --test-paths')
        if args.db_path is not None or args.systemctl!='systemctl' or args.journalctl!='journalctl':
            parser.error('Database/command overrides require --test-paths')
        if args.health_wait<30:
            parser.error('Production health wait must be >=30s')
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('Run as root on THINKPAD with the target venv python')
    if Path(args.python).absolute() != Path(sys.executable).absolute():
        parser.error('--python must be the interpreter running this command')
    if args.prefix.resolve() != Path(sys.prefix).resolve():
        parser.error('--prefix must be the running target interpreter prefix')
    actual_sites = {Path(sysconfig.get_path('purelib')).resolve(), Path(sysconfig.get_path('platlib')).resolve()}
    if args.site_packages and {site.resolve() for site in args.site_packages} != actual_sites:
        parser.error('--site-packages must match the running target interpreter')
    service = args.service
    config = Path('/etc/tg-triage/listener.toml' if service == 'listener' else '/etc/nailhub/nailhub.toml')
    site_paths = args.site_packages or sorted({Path(sysconfig.get_path('purelib')), Path(sysconfig.get_path('platlib'))})
    settings = Settings(service, args.stage.resolve(), args.code_dir or Path('/opt/agentfleet') / service,
                        args.config or config, args.state_dir or Path('/var/lib/agentfleet') / ('tg' if service == 'listener' else 'nailhub'),
                        args.backup_root, args.python, tuple(site_paths), args.prefix,
                        args.unit or ('tg-triage' if service == 'listener' else 'tg-nailhub'),
                        args.other_unit or ('tg-nailhub' if service == 'listener' else 'tg-triage'),
                        args.db_path, args.systemctl, args.journalctl, args.health_wait, test_paths=args.test_paths, allow_removed=tuple(args.allow_removed))
    os.umask(0o077)
    try:
        deployer = Deployment(settings)
        if args.rollback is not None:
            settings.validate()
            other_before = deployer.snapshot(settings.other_unit)
            restored = deployer.rollback(args.rollback.resolve(), {'ActiveState': 'active'}, restore_db=args.restore_db)
            other_after = deployer.snapshot(settings.other_unit)
            result = {'service': service, 'rollback': restored, 'other_before': other_before, 'other_after': other_after}
            if other_after != other_before:
                result['warning']='Other unit changed during manual rollback'
                print('WARNING: '+result['warning'])
            (args.rollback / 'rollback.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        else:
            result = deployer.deploy()
    except (DeploymentError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
