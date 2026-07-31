from __future__ import annotations

from typing import Any


STATUS_RANK = {"unknown": 0, "does_not_fit": 1, "tight": 2, "fits": 3}


def component_memory(rig: dict[str, Any], kind: str) -> int:
    return sum(
        max(0, int(component.get("memory_bytes") or 0))
        * max(1, int(component.get("quantity") or 1))
        for component in rig.get("components") or []
        if component.get("kind") == kind
    )


def evaluate_weight(weight: dict[str, Any], rig: dict[str, Any]) -> dict[str, Any]:
    size = max(0, int(weight.get("total_bytes") or 0))
    required = (size * 120 + 99) // 100 if size else 0
    weight_format = str(weight.get("format") or "").lower()
    capacities = {
        "Apple unified memory": component_memory(rig, "apple_silicon"),
        "aggregate GPU VRAM": component_memory(rig, "gpu"),
        "system RAM": component_memory(rig, "cpu"),
    }
    if weight_format == "mlx":
        candidates = [("Apple unified memory", capacities["Apple unified memory"])]
    elif weight_format == "gguf":
        candidates = list(capacities.items())
    else:
        candidates = []

    evaluations = []
    for target, available in candidates:
        if not size or available <= 0:
            status = "unknown"
        elif required <= available:
            status = "fits"
        elif size <= available:
            status = "tight"
        else:
            status = "does_not_fit"
        evaluations.append(
            {
                "target": target,
                "status": status,
                "available_bytes": available,
            }
        )
    useful = [item for item in evaluations if item["available_bytes"] > 0]
    best = max(
        useful or evaluations or [{"target": None, "status": "unknown", "available_bytes": 0}],
        key=lambda item: (STATUS_RANK[item["status"]], item["available_bytes"]),
    )
    return {
        "rig_id": rig["id"],
        "rig_name": rig["name"],
        "is_primary": bool(rig.get("is_primary")),
        "status": best["status"],
        "target": best["target"],
        "weight_bytes": size,
        "required_bytes": required,
        "available_bytes": best["available_bytes"],
        "headroom_percent": 20,
    }


def evaluate_weight_groups(
    groups: list[dict[str, Any]], rigs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            **group,
            "compatibility": [evaluate_weight(group, rig) for rig in rigs],
        }
        for group in groups
    ]
