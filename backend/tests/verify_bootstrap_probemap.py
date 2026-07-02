#!/usr/bin/env python3
"""Verify assemble_deps() produces a non-empty probe_map for Capability.AI_CODER.

Run: python -m tests.verify_bootstrap_probemap

This is NOT a pytest test — it's a standalone verification script that prints
real output confirming the fix. It exercises the actual assemble_deps() code path
(with no real API keys), verifying that when OPENROUTER_API_KEY is set, the
HealthMonitor is constructed with a probe_map that includes Capability.AI_CODER.

The probe_map is populated in assemble_deps() regardless of what coding tool is
chosen (_create_coding_tool() runs after), so FORGE_USE_SANDBOX=never is used
to keep the test path simple and avoid Docker-related mocking complexity.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure the backend is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.runtime.models import Capability
from app.workflow.bootstrap import assemble_deps


def verify_probe_map():
    """Verify assemble_deps builds a HealthMonitor with a non-empty probe_map."""

    env = {
        "OPENROUTER_API_KEY": "sk-test-fake-key-for-verification",
        "FORGE_USE_SANDBOX": "never",  # Bypass sandbox path; probe_map is set before coding tool
        "FORGE_CONFIG_PATH": "/tmp/nonexistent-forge-config.json",
    }

    with patch.dict(os.environ, env, clear=True):
        # Mock persistence to avoid real DB requirements
        with patch("app.workflow.bootstrap._init_persistence", return_value=None):
            with patch("app.workflow.bootstrap.load_role_chain_config", return_value=None):
                deps = assemble_deps(config_dir="/tmp/nonexistent-config-dir")

    health_monitor = deps.health_monitor
    probe_map = getattr(health_monitor, "_probe_map", None)

    print("=" * 60)
    print("Bootstrap probe_map verification")
    print("=" * 60)
    print(f"probe_map contents: {probe_map}")
    print(f"probe_map keys: {list(probe_map.keys()) if probe_map else []}")
    print(f"Capability.AI_CODER in probe_map: {Capability.AI_CODER in probe_map if probe_map else False}")

    if probe_map and Capability.AI_CODER in probe_map:
        probe = probe_map[Capability.AI_CODER]
        print(f"Probe type: {type(probe).__name__}")
        print(f"Probe has health_check method: {hasattr(probe, 'health_check')}")
        print()
        print("✅ PASS: probe_map includes Capability.AI_CODER")
        return True
    else:
        print()
        print("❌ FAIL: probe_map is empty or missing Capability.AI_CODER")
        print("   This means HealthMonitor will incorrectly mark AI_CODER as unhealthy")
        print("   even when the OpenRouter provider is perfectly healthy.")
        return False


if __name__ == "__main__":
    result = verify_probe_map()
    sys.exit(0 if result else 1)