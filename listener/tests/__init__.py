"""Every unittest entry point gets a private model-lock directory, including single modules."""
import atexit
import os
import tempfile
from tg_triage.model_lock import LOCK_DIR_ENV

_model_lock_directory = tempfile.TemporaryDirectory(prefix='agentfleet-unittest-')
TEST_MODEL_LOCK_DIR = _model_lock_directory.name
os.environ[LOCK_DIR_ENV] = TEST_MODEL_LOCK_DIR
atexit.register(_model_lock_directory.cleanup)
