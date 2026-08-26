"""Workspace `.cube` LUTs, resolved into files the ffmpeg color chain can read.

The inspector stores only `{assetId, workspaceId, intensity}` on a clip's
adjust settings — never a path or URL, because a client-supplied path fed to
ffmpeg would read arbitrary server files. Rendering therefore starts here:
look the asset up, prove the caller may use it, parse the cube, bake the
intensity in, and hand back a private file path for `lut3d`.

Intensity is baked by re-writing the table blended toward identity rather
than by a split/blend filter graph: `build_adjust_filter_chain` returns a
flat list the keyframed exporter wraps per-slice with `enable=` — a branching
subgraph cannot be wrapped that way, a single `lut3d` can.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Industry cubes are 17/33/65; anything past 65³ is 2M+ rows of text and not
#: a grading LUT any tool ships.
MIN_CUBE_SIZE = 2
MAX_CUBE_SIZE = 65
MAX_CUBE_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 20


class LutError(ValueError):
    """A cube file that cannot be used — malformed, oversized, or missing."""


def parse_cube(text: str) -> dict[str, Any]:
    """Parse a `.cube` 3D LUT into `{size, domain_min, domain_max, table}`.

    The table is kept flat in file order (red fastest), values clamped to the
    domain rather than rejected — real-world cubes routinely poke a hair out
    of range and every grading tool quietly clamps them.
    """
    size = 0
    domain_min = [0.0, 0.0, 0.0]
    domain_max = [1.0, 1.0, 1.0]
    table: list[tuple[float, float, float]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("TITLE"):
            continue
        if upper.startswith("LUT_1D_SIZE"):
            raise LutError("1D LUTs are not supported; use a 3D .cube file.")
        if upper.startswith("LUT_3D_SIZE"):
            try:
                size = int(line.split()[1])
            except (IndexError, ValueError) as exc:
                raise LutError("Unreadable LUT_3D_SIZE line.") from exc
            if size < MIN_CUBE_SIZE or size > MAX_CUBE_SIZE:
                raise LutError(f"LUT size must be {MIN_CUBE_SIZE}–{MAX_CUBE_SIZE}, got {size}.")
            continue
        if upper.startswith("DOMAIN_MIN") or upper.startswith("DOMAIN_MAX"):
            parts = line.split()[1:4]
            try:
                values = [float(part) for part in parts]
            except ValueError as exc:
                raise LutError(f"Unreadable {upper.split()[0]} line.") from exc
            if len(values) != 3 or not all(math.isfinite(v) for v in values):
                raise LutError(f"Unreadable {upper.split()[0]} line.")
            if upper.startswith("DOMAIN_MIN"):
                domain_min = values
            else:
                domain_max = values
            continue
        parts = line.split()
        if len(parts) != 3:
            raise LutError(f"Expected three values per data row, got: {line[:40]!r}")
        try:
            r, g, b = (float(part) for part in parts)
        except ValueError as exc:
            raise LutError(f"Non-numeric data row: {line[:40]!r}") from exc
        if not all(math.isfinite(v) for v in (r, g, b)):
            raise LutError("LUT contains non-finite values.")
        table.append((r, g, b))
    if size == 0:
        raise LutError("Missing LUT_3D_SIZE — not a 3D .cube file.")
    if len(table) != size ** 3:
        raise LutError(f"Expected {size ** 3} rows for a {size}³ LUT, got {len(table)}.")
    if any(domain_max[i] - domain_min[i] <= 1e-9 for i in range(3)):
        raise LutError("Degenerate LUT domain.")
    clamped = [
        tuple(min(domain_max[i], max(domain_min[i], value[i])) for i in range(3))
        for value in table
    ]
    return {"size": size, "domain_min": domain_min, "domain_max": domain_max, "table": clamped}


def blend_with_identity(lut: dict[str, Any], alpha: float) -> dict[str, Any]:
    """The same LUT at `alpha` strength: each lattice point pulled toward the
    input coordinate it maps. alpha=1 is the LUT verbatim, alpha=0 identity."""
    alpha = max(0.0, min(1.0, float(alpha)))
    size = lut["size"]
    lo = lut["domain_min"]
    hi = lut["domain_max"]
    span = [hi[i] - lo[i] for i in range(3)]
    table = []
    for index, value in enumerate(lut["table"]):
        # .cube data order: red runs fastest, then green, then blue.
        coords = (index % size, (index // size) % size, index // (size * size))
        identity = tuple(lo[i] + (coords[i] / (size - 1)) * span[i] for i in range(3))
        table.append(tuple(
            identity[i] + (value[i] - identity[i]) * alpha for i in range(3)
        ))
    return {**lut, "table": table}


def write_cube(lut: dict[str, Any], path: Path) -> None:
    lines = [f"LUT_3D_SIZE {lut['size']}"]
    lo, hi = lut["domain_min"], lut["domain_max"]
    if lo != [0.0, 0.0, 0.0] or hi != [1.0, 1.0, 1.0]:
        lines.append("DOMAIN_MIN " + " ".join(f"{v:.6f}" for v in lo))
        lines.append("DOMAIN_MAX " + " ".join(f"{v:.6f}" for v in hi))
    for r, g, b in lut["table"]:
        lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    path.write_text("\n".join(lines) + "\n")


def _cache_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "editube_luts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _read_lut_bytes(file_url: str) -> str:
    """The cube's text, wherever the asset lives — local path or object store."""
    value = (file_url or "").strip()
    if value.startswith(("http://", "https://")):
        # Cloudflare fronts the public bucket and 403s the default
        # `Python-urllib` agent, so the request names itself honestly instead.
        request = urllib.request.Request(value, headers={"User-Agent": "editube-server/1.0"})
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310 - URL comes from our own asset row
            data = response.read(MAX_CUBE_BYTES + 1)
    else:
        candidate = Path(value)
        if value.startswith("/uploads/"):
            uploads = Path(os.environ.get("UPLOADS_DIR", "./uploads")).resolve()
            candidate = uploads / value.removeprefix("/uploads/")
        if not candidate.is_file():
            raise LutError("LUT file is missing from storage.")
        if candidate.stat().st_size > MAX_CUBE_BYTES:
            raise LutError("LUT file exceeds the 8MB limit.")
        data = candidate.read_bytes()
    if len(data) > MAX_CUBE_BYTES:
        raise LutError("LUT file exceeds the 8MB limit.")
    return data.decode("utf-8", "replace")


def derived_lut_path(db: Any, asset: Any, intensity: float) -> Path:
    """A ready-to-render cube for this asset at this strength, cached on disk.

    Always a derived file, even at full intensity: the rewrite normalises the
    source and guarantees ffmpeg sees a path with no characters that need
    filter-graph escaping.
    """
    alpha = max(0.0, min(1.0, float(intensity)))
    key = hashlib.sha1(
        f"{asset.id}:{asset.size_bytes}:{asset.created_at}:{alpha:.3f}".encode()
    ).hexdigest()
    target = _cache_dir() / f"lut_{key}.cube"
    if target.is_file() and target.stat().st_size > 0:
        return target
    lut = parse_cube(_read_lut_bytes(asset.file_url))
    if alpha < 0.999:
        lut = blend_with_identity(lut, alpha)
    write_cube(lut, target)
    return target


def resolve_adjust_lut(
    db: Any,
    settings: dict[str, Any] | None,
    *,
    user_id: int | None = None,
    allowed_workspace_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    """Replace a settings' `lut` reference with a rendered-file path, in place.

    Exactly one authorisation route must be supplied: `user_id` (the asset's
    workspace must have them as a member — the preview path) or
    `allowed_workspace_ids` (the export path, derived from the project).

    Failure never raises: a render missing its LUT beats a render that dies,
    so the reference is stripped and logged instead.
    """
    if not isinstance(settings, dict):
        return settings
    lut = settings.get("lut")
    if not isinstance(lut, dict):
        return settings

    def strip(reason: str) -> dict[str, Any]:
        logger.warning("Skipping LUT on render: %s (ref=%r)", reason, {k: lut.get(k) for k in ("assetId", "workspaceId")})
        settings.pop("lut", None)
        return settings

    try:
        asset_id = int(lut.get("assetId"))
    except (TypeError, ValueError):
        return strip("no usable assetId")
    intensity = lut.get("intensity")
    try:
        alpha = 1.0 if intensity is None else max(0.0, min(1.0, float(intensity) / 100.0))
    except (TypeError, ValueError):
        alpha = 1.0
    if alpha <= 0.001:
        settings.pop("lut", None)
        return settings

    from app.db.models import WorkspaceAsset, WorkspaceMember

    asset = db.query(WorkspaceAsset).filter(WorkspaceAsset.id == asset_id).first()
    if asset is None or (asset.category or "").lower() != "lut":
        return strip("asset missing or not a LUT")
    if allowed_workspace_ids is not None:
        if asset.workspace_id not in allowed_workspace_ids:
            return strip("asset belongs to another workspace")
    elif user_id is not None:
        member = (
            db.query(WorkspaceMember)
            .filter(
                WorkspaceMember.workspace_id == asset.workspace_id,
                WorkspaceMember.user_id == user_id,
            )
            .first()
        )
        if member is None:
            return strip("requester is not a member of the LUT's workspace")
    else:
        return strip("no authorisation context")

    try:
        path = derived_lut_path(db, asset, alpha)
    except (LutError, OSError, urllib.error.URLError) as exc:
        return strip(f"could not prepare cube: {exc}")
    settings["lut"] = {**lut, "path": str(path)}
    return settings


def video_workspace_ids(db: Any, video: Any) -> set[int]:
    """The workspace(s) a render of this video may pull LUT assets from."""
    if video is None or not getattr(video, "project_id", None):
        return set()
    from app.db.models import Project

    project = db.query(Project).filter(Project.id == video.project_id).first()
    if project is None or not project.workspace_id:
        return set()
    return {int(project.workspace_id)}
