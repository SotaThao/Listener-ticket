# THINKPAD: isolated services, cutover and rollback

Owner/controller commands only. Nothing below has been executed on production by the
agent. Use a reviewed split commit and finish the independent production gate first.
Keep configs, credentials, sessions, browser profiles and backups outside Git.

## Build stages from one commit

~~~sh
python deploy/build_wheel.py --service listener --commit <REVIEWED_COMMIT> --stage <LISTENER_STAGE>
python deploy/build_wheel.py --service nailhub --commit <REVIEWED_COMMIT> --stage <NAILHUB_STAGE>
~~~

Transfer the complete stage: wheel, build.json, source.tar and source/. The real staging
code includes repo-root config/systemd/.env.example fixtures used by the package tests,
from the same archive/commit as the package. Worktree edits are not production inputs.
Use the offline dependency wheel cache at /var/cache/agentfleet/wheels.

## 1. Pause legacy NailHub and record the browser version

The Owner sends /nailhub pause, then /nailhub status until paused=True and running=False.
Do not resume until the final listener deployment. Before moving any config, drop-in,
venv, profile or credentials, record the EXACT Playwright version from the legacy venv
and the matching cached Chromium executable/revision. This reads local metadata only:

~~~sh
PLAYWRIGHT_VERSION=$(/opt/agentfleet/listener/.venv/bin/python -c 'from importlib.metadata import version; print(version("playwright"))')
LEGACY_CHROMIUM=$(PLAYWRIGHT_BROWSERS_PATH=/var/lib/agentfleet/nailhub/browsers /opt/agentfleet/listener/.venv/bin/python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
PY
)
test -n "$PLAYWRIGHT_VERSION"
test -x "$LEGACY_CHROMIUM"
export PLAYWRIGHT_VERSION LEGACY_CHROMIUM
~~~

The executable must resolve under /var/lib/agentfleet/nailhub/browsers and its chromium
revision directory must already exist. Do not install a newer Playwright against an
older browser cache. Keep these recorded values with the private cutover backup.
Rollback step 1: no files moved yet; the Owner may resume the existing paused worker.

## 2. Remove the legacy NAILHUB_CONFIG drop-in, then restart triage

Do this BEFORE touching NailHub state ownership. The reviewed checkout below is at
/srv/agentfleet-split. Choose one private cutover directory:

~~~sh
umask 077
CUTOVER=/var/backups/agentfleet/cutover-<UTC_STAMP>
mkdir -m 0700 "$CUTOVER"
export CUTOVER
printf '%s\n' "$PLAYWRIGHT_VERSION" > "$CUTOVER/playwright-version.txt"
printf '%s\n' "$LEGACY_CHROMIUM" > "$CUTOVER/chromium-executable.txt"
python3 - <<'PY'
import json, os, shutil, subprocess
from pathlib import Path
backup=Path(os.environ['CUTOVER'])
paths=subprocess.check_output(['systemctl','show','tg-triage.service','-p','DropInPaths','--value'],text=True).split()
selected=[Path(p) for p in paths if 'NAILHUB_CONFIG' in Path(p).read_text()]
assert len(selected)==1, 'Locate the one dedicated NailHub drop-in before proceeding'
source=selected[0].resolve()
assert source.is_relative_to(Path('/etc/systemd/system/tg-triage.service.d')), 'Unexpected drop-in location'
info=source.stat()
(backup/'drop-in-origin.json').write_text(json.dumps({'path':str(source),'uid':info.st_uid,'gid':info.st_gid,'mode':info.st_mode & 0o777}))
shutil.move(source,backup/'nailhub-drop-in.conf')
PY
CUTOVER_SINCE=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
systemctl daemon-reload
systemctl restart tg-triage.service
~~~

If the drop-in also contains unrelated settings, preserve those in a separate listener
configuration before removal and record the change. Do not remove arbitrary other drop-ins.
Rollback step 2: restore nailhub-drop-in.conf to the path/ownership/mode in
 drop-in-origin.json, daemon-reload and restart ONLY tg-triage. No NailHub ownership
has changed yet; the Owner verifies the paused legacy worker before deciding to resume.

## 3. Prove the legacy worker is absent and triage is alive

~~~sh
systemctl is-active tg-triage.service
systemctl show tg-triage.service -p MainPID -p NRestarts
python3 - <<'PY'
import subprocess
from pathlib import Path
pid=int(subprocess.check_output(['systemctl','show','tg-triage.service','-p','MainPID','--value'],text=True))
assert pid>0
entries=Path('/proc/'+str(pid)+'/environ').read_bytes().split(b'\0')
assert not any(e.startswith(b'NAILHUB_CONFIG=') for e in entries), 'Legacy config still inherited'
PY
TRIAGE_PID=$(systemctl show tg-triage.service -p MainPID --value)
test "$TRIAGE_PID" -gt 0
journalctl -u tg-triage.service --since "$CUTOVER_SINCE" "_PID=$TRIAGE_PID" --no-pager --output=cat > "$CUTOVER/listener-after-restart.log"
if grep -qi NailHub "$CUTOVER/listener-after-restart.log"; then echo 'STOP: legacy NailHub still present'; exit 1; fi
~~~

The _PID filter selects the new MainPID, excluding shutdown lines from the previous
process. If MainPID changes, repeat the environment and journal checks for that PID.
Wait for a normal listener heartbeat or a processed-message log from an established
controlled test flow AFTER this restart, then capture/check the journal again. Confirm
triage is processing and no NailHub lines occur. If this cannot be established, stop the
cutover here; do not chown/move NailHub files. Check for remaining NAILHUB_CONFIG in an
EnvironmentFile or another active override without printing secrets.
Rollback step 3: restore the saved drop-in and restart tg-triage as in step 2. The
state/config/browser paths still have their original ownership.

## 4. Only now install the independent service

There is no legacy NailHub process now. Back up its config, DB via SQLite backup API,
profile and optional credential file, including original uid/gid/mode, under CUTOVER.
Use PRAGMA integrity_check on the DB copy. Example backup uses the configured paths:

~~~sh
python3 - <<'PY'
import json, os, shutil, sqlite3, tarfile, tomllib
from contextlib import closing
from pathlib import Path
backup=Path(os.environ['CUTOVER']);config=Path('/etc/tg-triage/nailhub.toml')
worker=tomllib.loads(config.read_text())['worker']
items=[config,Path(worker['db_path']),Path(worker['profile_path'])]
if worker.get('credential_file'):items.append(Path(worker['credential_file']))
(backup/'nailhub-ownership.json').write_text(json.dumps({str(p):{'uid':p.stat().st_uid,'gid':p.stat().st_gid,'mode':p.stat().st_mode & 0o777} for p in items},indent=2))
shutil.copy2(config,backup/'nailhub.toml')
with closing(sqlite3.connect(Path(worker['db_path']).as_uri()+'?mode=ro',uri=True)) as source:
    with closing(sqlite3.connect(backup/'jobs.sqlite3')) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
with tarfile.open(backup/'profile.tar','w') as archive:archive.add(worker['profile_path'],arcname='profile')
if worker.get('credential_file'):shutil.copy2(worker['credential_file'],backup/'credentials.json')
PY
getent group agentfleet || groupadd --system agentfleet
id tgnailhub || useradd --system --gid agentfleet --home-dir /var/lib/agentfleet/nailhub --shell /usr/sbin/nologin tgnailhub
usermod -a -G agentfleet tglistener
install -d -m 0755 /opt/agentfleet/nailhub /etc/agentfleet
install -d -m 0700 -o tgnailhub -g agentfleet /etc/nailhub /var/lib/agentfleet/nailhub
mv /etc/tg-triage/nailhub.toml /etc/nailhub/nailhub.toml
chown -R tgnailhub:agentfleet /var/lib/agentfleet/nailhub
chown tgnailhub:agentfleet /etc/nailhub/nailhub.toml
chmod 0600 /etc/nailhub/nailhub.toml
install -m 0644 /srv/agentfleet-split/systemd/agentfleet-tmpfiles.conf /etc/tmpfiles.d/agentfleet.conf
systemd-tmpfiles --create /etc/tmpfiles.d/agentfleet.conf
install -m 0644 /srv/agentfleet-split/systemd/tg-nailhub.service /etc/systemd/system/tg-nailhub.service
install -m 0644 /srv/agentfleet-split/systemd/tg-triage.service /etc/systemd/system/tg-triage.service
systemctl daemon-reload
~~~

Keep DB/profile under /var/lib/agentfleet/nailhub. Move any legacy credential outside
NailHub-owned paths to /var/lib/agentfleet/nailhub/credentials.json (after its backup),
set worker.credential_file to that path and set mode0600/tgnailhub:agentfleet. Keep
contract evidence, selectors, Owner binding and paused DB. Add the separate session:

~~~toml
[telegram]
session = "/var/lib/agentfleet/nailhub/nailhub.session"
~~~

Validate canonical resolved paths BEFORE launching anything:

~~~sh
python3 - <<'PY'
import subprocess, tomllib
from pathlib import Path
worker=tomllib.loads(Path('/etc/nailhub/nailhub.toml').read_text())['worker']
root=Path('/var/lib/agentfleet/nailhub').resolve()
profile=Path(worker['profile_path']).resolve()
assert profile.is_relative_to(root)
for flag in ('-r','-w'):subprocess.run(['runuser','-u','tgnailhub','--','test',flag,str(profile)],check=True)
if worker.get('credential_file'):
    credential=Path(worker['credential_file']).resolve();assert credential.is_relative_to(root)
    for flag in ('-r','-w'):subprocess.run(['runuser','-u','tgnailhub','--','test',flag,str(credential)],check=True)
PY
(umask 022; python3 -m venv /opt/agentfleet/nailhub/.venv)
(umask 022; /opt/agentfleet/nailhub/.venv/bin/python -m pip install --no-index --find-links /var/cache/agentfleet/wheels 'telethon>=1.45,<2' "playwright==$PLAYWRIGHT_VERSION")
~~~

Verify the new adapter resolves to the SAME already cached Chromium revision:

~~~sh
PLAYWRIGHT_BROWSERS_PATH=/var/lib/agentfleet/nailhub/browsers /opt/agentfleet/nailhub/.venv/bin/python - <<'PY'
import os
from importlib.metadata import version
from pathlib import Path
from playwright.sync_api import sync_playwright
assert version('playwright')==os.environ['PLAYWRIGHT_VERSION']
with sync_playwright() as p:
    executable=Path(p.chromium.executable_path).resolve()
root=Path('/var/lib/agentfleet/nailhub/browsers').resolve()
relative=executable.relative_to(root)
assert (root/relative.parts[0]).is_dir() and executable.is_file()
assert executable==Path(os.environ['LEGACY_CHROMIUM']).resolve()
PY
~~~

No browser download is needed; existing browsers stay at the shared configured cache.
Verify the stage and tests, then install only its wheel with readable root-created files:

~~~sh
/opt/agentfleet/nailhub/.venv/bin/python -c 'import sys; sys.path.insert(0,"/srv/agentfleet-split"); from deploy.build_wheel import verify_stage; print(verify_stage("<NAILHUB_STAGE>","nailhub"))'
(cd <NAILHUB_STAGE>/source/nailhub && PYTHONPATH=src /opt/agentfleet/nailhub/.venv/bin/python -m unittest discover -s tests -t .)
(umask 022; /opt/agentfleet/nailhub/.venv/bin/python -m pip install --no-index --no-deps <NAILHUB_STAGE>/nailhub-1.0.0-py3-none-any.whl)
~~~

Create root-owned mode0600 /etc/agentfleet/tg-nailhub.env containing only the existing
TG_API_ID/TG_API_HASH, filled by the Owner (examples: <API_ID>, <API_HASH>). The Owner
enters phone code/2FA interactively; never store a code in a file/env. Probe before enable:

~~~sh
set -a
. /etc/agentfleet/tg-nailhub.env
set +a
runuser --preserve-environment -u tgnailhub -- /opt/agentfleet/nailhub/.venv/bin/python -m nailhub login-telegram
unset TG_API_ID TG_API_HASH
runuser -u tgnailhub -- env PLAYWRIGHT_BROWSERS_PATH=/var/lib/agentfleet/nailhub/browsers /opt/agentfleet/nailhub/.venv/bin/python -m nailhub probe
NAILHUB_SINCE=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
systemctl enable --now tg-nailhub.service
sleep 30
systemctl is-active tg-nailhub.service
systemctl show tg-nailhub.service -p NRestarts
journalctl -u tg-nailhub.service --since "$NAILHUB_SINCE" --no-pager --output=cat
~~~

Require NailHub ready owner_verified=1 within 60s, NRestarts=0 after this manual start,
and no formatted ERROR/Traceback lines. Inspect manual notice INFO lines too. Keep paused
until listener cutover below. The independent process owns nailhub.lock BEFORE Telegram
connect/login; legacy tg-triage.lock is irrelevant after step 2 disabled its NailHub worker.
Rollback step 4: stop/disable ONLY tg-nailhub first. Preserve current data. To restore the
legacy worker, return config/credential references and original state/profile ownership
from the private backup, restore the saved drop-in, daemon-reload and restart tg-triage.
Only the Owner may explicitly choose to restore an older DB/profile, after stopping all
NailHub writers; never discard newer live data automatically. Verify paused before resume.

## 5. Deploy the listener later, with the new script

The guard refuses both unrecorded additions and recorded files the new wheel would remove
(including tg_triage/nailhub and model_gate.py). For this one migration, preserve the legacy
listener code/venv/config, stop only tg-triage, and replace its contaminated venv with a
clean listener venv. SQLite backup MUST follow the stop. NailHub stays running and paused.

~~~sh
LISTENER_LEGACY="$CUTOVER/listener-legacy"
mkdir -m 0700 "$LISTENER_LEGACY"
systemctl stop tg-triage.service
tar --acls --xattrs -cpf "$LISTENER_LEGACY/code.tar" -C /opt/agentfleet listener
cp -p /etc/tg-triage/listener.toml "$LISTENER_LEGACY/listener.toml"
export LISTENER_LEGACY
python3 - <<'PY'
import os,sqlite3
from contextlib import closing
from pathlib import Path
with closing(sqlite3.connect('file:/var/lib/agentfleet/tg/triage.sqlite3?mode=ro',uri=True)) as source:
    with closing(sqlite3.connect(Path(os.environ['LISTENER_LEGACY'])/'triage.sqlite3')) as target:
        source.backup(target);assert target.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
PY
mv /opt/agentfleet/listener/.venv "$LISTENER_LEGACY/venv"
(umask 022; python3 -m venv /opt/agentfleet/listener/.venv)
(umask 022; /opt/agentfleet/listener/.venv/bin/python -m pip install --no-index --find-links /var/cache/agentfleet/wheels 'telethon>=1.45,<2')
/opt/agentfleet/listener/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service listener --stage <LISTENER_STAGE>
~~~

Before this deploy, set telegram.ignored_command_prefixes=["/nailhub"] in listener.toml.
No group command from that namespace enters triage. After listener health verification,
the Owner may send /nailhub resume privately. Other-unit restart changes are printed and
stored as warnings for inspection; they do not roll back a healthy target.
Rollback step 5: stop only tg-triage and restore saved legacy code/venv/config, keeping the
NAILHUB_CONFIG drop-in REMOVED so the restored listener cannot start another NailHub.
Keep the live listener DB if start was already issued. An explicit older DB restoration
requires Owner choice and no live writer. Independent tg-nailhub stays running. A return
to the combined architecture requires step 4 rollback first, then step 2 rollback.

## Subsequent deployment and recovery

~~~sh
/opt/agentfleet/listener/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service listener --stage <LISTENER_STAGE>
/opt/agentfleet/nailhub/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service nailhub --stage <NAILHUB_STAGE>
~~~

Production units/code/config/state paths are bound to the selected service. The target
interpreter runs its real staged tests; guards pass before stop. Stop, backup code/config
and SQLite, install --no-index --no-deps --force-reinstall, verify bytes, start, wait >=30s,
then require active/NRestarts=0. NailHub additionally needs the ready log within 60s.

For reviewed intentional removals, pass --allow-removed once per exact installed RECORD
path. No wildcard, directory prefix or unrecorded/unknown-package exception is granted.
A recorded .pyc whose source .py is listed is also allowed, including __pycache__
entries for each Python/cache tag. List the source, not every generated bytecode path;
both source and bytecode removals are printed and recorded. For example, listing
tg_triage/retired.py also permits tg_triage/__pycache__/retired.cpython-312.pyc.
All other missing RECORD paths still abort before stop/install. Accepted removals print as
ALLOW-REMOVED and appear in deployment.json (allowed_removed and guard.allowed_removed),
including the backup's copy. Review the list against the staged commit before deployment:

~~~sh
/opt/agentfleet/listener/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service listener --stage <LISTENER_STAGE> --allow-removed tg_triage/retired.py --allow-removed tg_triage/retired_data.json
~~~

Journal readiness is read at ANY priority. Listener logging writes to stdout; NailHub
logging writes to stderr. Both use formatted INFO/ERROR lines, and journald does not infer
Python severity from either stream: priority=err filters can miss errors.
The script scans ALL lines since start for ' ERROR ' or 'Traceback'. deployment.json records
commit, wheel SHA256, backup, start-issued boundary, health results and other-unit warnings.

Automatic rollback restores code+config. DB is restored ONLY for failure before start was
issued. After start, keep live DB/WAL/SHM intact. Manual rollback also preserves DB by default:

~~~sh
/opt/agentfleet/listener/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service listener --stage <LISTENER_STAGE> --rollback <LISTENER_BACKUP>
/opt/agentfleet/nailhub/.venv/bin/python /srv/agentfleet-split/deploy/agentfleet_deploy.py --service nailhub --stage <NAILHUB_STAGE> --rollback <NAILHUB_BACKUP>
~~~

Only when the Owner explicitly elects older data, add --restore-db to the chosen manual
rollback command. It validates backup integrity and restores DB with all writers stopped.
If restoration reports failure, preserve backups/failed code and perform manual recovery.
The controller independently verifies real login/browser/systemd and monitors both services
for 24h after cutover; offline tests do not establish those production facts.
