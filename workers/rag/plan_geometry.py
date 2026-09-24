"""Deterministic wall/door detection for simple architectural unit-plan drawings.

Walls in the LH unit plans are thick (~10px at 200dpi) gray bands, while furniture,
text and dimension lines are 1-2px strokes. The door is a gap in the wall band.
Vision models misread doors near corners (the open door leaf lies along the
adjacent wall), so the door wall is measured here instead of asked for.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

WALL_MAX_GRAY = 200
MIN_WALL_THICKNESS = 5
MAX_DOOR_GAP_PX = 120
MIN_DOOR_GAP_PX = 30


def wall_mask(gray: np.ndarray) -> np.ndarray:
    dark = gray < WALL_MAX_GRAY
    walls = ndimage.binary_opening(dark, structure=np.ones((MIN_WALL_THICKNESS, MIN_WALL_THICKNESS)))
    labels, count = ndimage.label(walls)
    if count == 0:
        return walls
    sizes = ndimage.sum(walls, labels, index=range(1, count + 1))
    return labels == (int(np.argmax(sizes)) + 1)


MIN_WALL_RUN_PX = 40


def _runs(values: np.ndarray) -> list[tuple[int, int]]:
    """Start/stop indices of consecutive True runs in a 1-D bool array."""
    padded = np.concatenate([[False], values, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _gaps(walls: np.ndarray, horizontal: bool) -> list[tuple[slice, slice]]:
    """Find breaks inside long straight wall bands.

    A wall band is a stack of rows (or columns) that each contain a long wall run.
    Only a break *within* such a band counts, so furniture drawn next to a wall
    cannot inflate or fake a gap, and wall-edge jitter thinner than the band is
    ignored.
    """
    grid = walls if horizontal else walls.T
    long_rows = [
        index
        for index, row in enumerate(grid)
        if any(stop - start >= MIN_WALL_RUN_PX for start, stop in _runs(row))
    ]
    bands: list[tuple[int, int]] = []
    for index in long_rows:
        if bands and index - bands[-1][1] <= 2:
            bands[-1] = (bands[-1][0], index + 1)
        else:
            bands.append((index, index + 1))

    bands = [(start, stop) for start, stop in bands if stop - start >= MIN_WALL_THICKNESS]
    if not bands:
        return []
    # A band much thinner than a real wall is an edge sliver, not a wall.
    typical = float(np.median([stop - start for start, stop in bands]))
    found = []
    for start, stop in bands:
        if stop - start < 0.7 * typical:
            continue
        coverage = grid[start:stop].mean(axis=0) > 0.5
        segments = [(a, b) for a, b in _runs(coverage) if b - a >= 8]
        for (_, previous_end), (next_start, _) in zip(segments, segments[1:]):
            if MIN_DOOR_GAP_PX <= next_start - previous_end <= MAX_DOOR_GAP_PX:
                band_slice, gap_slice = slice(start, stop), slice(previous_end, next_start)
                found.append((band_slice, gap_slice) if horizontal else (gap_slice, band_slice))
    return found


def detect_door(image_path: Path) -> dict | None:
    gray = np.array(Image.open(image_path).convert("L"))
    walls = wall_mask(gray)
    candidates = [(region, True) for region in _gaps(walls, horizontal=True)]
    candidates += [(region, False) for region in _gaps(walls, horizontal=False)]
    if not candidates:
        return None

    # Close every candidate gap, then everything not reachable from the image
    # border (and not a wall) is the room interior.
    sealed = walls.copy()
    for region, _ in candidates:
        sealed[region] = True
    interior = ndimage.binary_fill_holes(sealed) & ~sealed

    ys, xs = np.nonzero(walls)
    top, bottom, left, right = ys.min(), ys.max(), xs.min(), xs.max()

    doors = []
    for region, horizontal in candidates:
        y0, y1 = region[0].start, region[0].stop
        x0, x1 = region[1].start, region[1].stop
        ymid, xmid = (y0 + y1) // 2, (x0 + x1) // 2
        probe = 8
        if horizontal:
            above = interior[max(y0 - probe, 0), xmid]
            below = interior[min(y1 + probe, gray.shape[0] - 1), xmid]
            if above == below:
                continue
            wall = "top" if below else "bottom"
            position = (xmid - left) / max(right - left, 1)
            end = "left" if position < 0.4 else "right" if position > 0.6 else "middle"
        else:
            left_side = interior[ymid, max(x0 - probe, 0)]
            right_side = interior[ymid, min(x1 + probe, gray.shape[1] - 1)]
            if left_side == right_side:
                continue
            wall = "left" if right_side else "right"
            position = (ymid - top) / max(bottom - top, 1)
            end = "top" if position < 0.4 else "bottom" if position > 0.6 else "middle"
        doors.append({"wall": wall, "gap_near_end": end, "gap_box": [int(x0), int(y0), int(x1), int(y1)]})

    if len(doors) != 1:
        return {"ambiguous": doors} if doors else None
    return doors[0]
