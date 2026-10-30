"""Exact loaded revision acknowledgement after authenticated startup."""
import json
import os
import time
from pathlib import Path

def ready(config):
    revision = getattr(config, 'console_revision', '')
    if not revision:
        return
    path = Path(config.db_path).parent / 'agentfleet-ready.json'
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        if os.name != 'nt':
            import grp
            os.chown(temp,-1,grp.getgrnam('tglistener').gr_gid)
        os.chmod(temp, 0o640 if os.name != 'nt' else 0o600)
        json.dump({'revision': revision, 'pid': os.getpid(), 'readyAt': time.time(), 'mode': config.mode}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
