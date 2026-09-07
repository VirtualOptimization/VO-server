"""Search-bound helpers for room layout optimization."""

from __future__ import annotations


def center_axis_bounds(container_size: float, item_size: float) -> tuple[float, float]:
    """Return a valid center range even when an item is larger than the room axis."""
    half_size = max(float(item_size), 0.0) / 2.0
    if half_size * 2.0 > container_size:
        center = container_size / 2.0
        return center, center
    return half_size, container_size - half_size
