"""Forge Runtime — entry point.

Run with: uvicorn main:app --host 0.0.0.0 --port 8000

Or for development with auto-reload:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os

from app.workflow.app import create_app

# Configure the app's own loggers (app.*) before create_app() runs. Without this,
# module-level `logging.getLogger(__name__)` calls throughout the codebase (e.g.
# "Coding tool: ...", "Commit/push failed for task ...") have no handler attached
# and are silently dropped — only uvicorn's own access/error logs show up.
# FORGE_LOG_LEVEL controls verbosity (default INFO); unrecognized values fall
# back to INFO rather than raising.
_LOG_LEVEL_NAME = os.environ.get("FORGE_LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("app").setLevel(_LOG_LEVEL)

app = create_app()
