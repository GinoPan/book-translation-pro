"""Vendor-neutral runtime capability profiles for file-backed work queues."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .artifacts import BTPError, project_lock


ADAPTERS: dict[str, dict[str, Any]] = {
    "sequential": {"parallel": False, "isolated_workers": False, "external_queue": False},
    "local_parallel": {"parallel": True, "isolated_workers": True, "external_queue": False},
    "host_workers": {"parallel": True, "isolated_workers": True, "external_queue": False},
    "external_queue": {"parallel": True, "isolated_workers": True, "external_queue": True},
}


def resolve_adapter(name: str, max_parallel: int, host_parallel: bool = False) -> dict[str, Any]:
    aliases = {"parallel": "local_parallel"}
    if name == "auto":
        name = "host_workers" if host_parallel else "sequential"
    name = aliases.get(name, name)
    if name not in ADAPTERS:
        raise BTPError(f"Unknown runtime adapter: {name}")
    capabilities = dict(ADAPTERS[name])
    concurrency = max(1, max_parallel if capabilities["parallel"] else 1)
    return {
        "contract_version": 1,
        "adapter": name,
        "concurrency": concurrency,
        "capabilities": capabilities,
        "input_contract": "show-unit-v1",
        "output_contracts": ["capsule-v1", "draft-markdown", "edit-markdown", "observation-v1"],
        "shared_state_writer": "coordinator",
        "stable_commit_order": True,
    }


def conformance_cases(max_parallel: int = 4) -> dict[str, Any]:
    profiles = [resolve_adapter(name, max_parallel) for name in ADAPTERS]
    common = {
        (profile["input_contract"], tuple(profile["output_contracts"]), profile["shared_state_writer"], profile["stable_commit_order"])
        for profile in profiles
    }
    lock = cross_process_lock_probe()
    return {
        "contract_version": 1,
        "passed": len(profiles) == 4 and len(common) == 1 and lock["passed"],
        "profiles": profiles,
        "cross_process_lock": lock,
    }


def cross_process_lock_probe() -> dict[str, Any]:
    """Prove on this host that a second process cannot take a held project lock.

    Parallel adapters rely on this lock to serialize Run State writes.
    """
    package_root = Path(__file__).resolve().parents[1]
    child = (
        "import sys; from pathlib import Path; from book_translation_pro.artifacts import BTPError, project_lock\n"
        "try:\n"
        "    with project_lock(Path(sys.argv[1]), timeout=0.3):\n"
        "        pass\n"
        "except BTPError:\n"
        "    sys.exit(3)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(package_root), os.environ.get("PYTHONPATH")]))}

    def attempt(project: Path) -> int:
        return subprocess.run([sys.executable, "-c", child, str(project)], env=env, timeout=60).returncode

    with tempfile.TemporaryDirectory() as temp:
        project = Path(temp)
        with project_lock(project):
            while_held = attempt(project)
        after_release = attempt(project)
    return {
        "passed": while_held == 3 and after_release == 0,
        "blocked_while_held": while_held == 3,
        "acquired_after_release": after_release == 0,
    }
