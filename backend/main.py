"""Forge Runtime — entry point.

Run with: uvicorn main:app --host 0.0.0.0 --port 8000

Or for development with auto-reload:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from app.workflow.app import create_app

app = create_app()
