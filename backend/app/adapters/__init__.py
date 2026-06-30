"""Adapter Layer - AI provider, coding tool, VCS, storage, vector, verifier, doc writer adapters."""

from app.adapters.openrouter import OpenRouterProvider
from app.adapters.github_vcs import GitHubVCS
from app.adapters.aider_tool import AiderTool
from app.adapters.sandboxed_aider import SandboxedAiderTool

__all__ = [
    "OpenRouterProvider",
    "GitHubVCS",
    "AiderTool",
    "SandboxedAiderTool",
]
