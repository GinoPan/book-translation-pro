"""Vendor-neutral runtime capability profiles for file-backed work queues."""

from __future__ import annotations

from typing import Any

from .artifacts import BTPError


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
    return {
        "contract_version": 1,
        "passed": len(profiles) == 4 and len(common) == 1,
        "profiles": profiles,
    }
