"""Keeps every test away from the user's real GenFill files.

Imported first by each test module: points ``GENFILL_CONFIG_DIR`` (config.json with keys and
OAuth tokens) and ``GENFILL_MODELS_CACHE`` (live model discovery cache) at a fresh temp dir,
so results never depend on what is in ``~/.nuke/genfill``. Modules imported earlier are
patched too.
"""
import atexit
import os
import shutil
import sys
import tempfile

if not os.environ.get("GENFILL_TEST_ISOLATED"):
    _DIR = tempfile.mkdtemp(prefix="genfill_test_cfg_")
    atexit.register(shutil.rmtree, _DIR, True)
    os.environ["GENFILL_TEST_ISOLATED"] = _DIR
    os.environ["GENFILL_CONFIG_DIR"] = _DIR
    os.environ["GENFILL_MODELS_CACHE"] = os.path.join(_DIR, "models_cache.json")

CONFIG_DIR = os.environ["GENFILL_CONFIG_DIR"]

_config = sys.modules.get("genfill.config")
if _config is not None and _config.CONFIG_DIR != CONFIG_DIR:
    _config.CONFIG_DIR = CONFIG_DIR
    _config.CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
_discovery = sys.modules.get("genfill.backends.discovery")
if _discovery is not None:
    _discovery.reset()
