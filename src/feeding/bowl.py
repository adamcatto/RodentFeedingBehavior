"""Bowl annotations and ellipse geometry.

A bowl is annotated with five clicks: its centre and the top, bottom, left and
right points of its rim (the rim's extremal points in image y and x).

The rim is the ellipse ``(p - m)^T A (p - m) = 1``. Writing ``Q = A^-1 =
[[p, r], [r, s]]``, an ellipse's topmost/bottommost points are ``m -/+ (r, s)/sqrt(s)``
and its leftmost/rightmost points ``m -/+ (p, r)/sqrt(p)``. Hence

- half-width  ``sqrt(p)`` = (right.x - left.x) / 2
- half-height ``sqrt(s)`` = (bottom.y - top.y) / 2
- ``r`` = half the x-shift between bottom and top times the half-height
  (and, equivalently, the y-shift between right and left times the half-width;
  both estimates are averaged)
- ``m`` = mean of the two midpoints (top/bottom and left/right)

so a slightly tilted / perspective-skewed bowl is captured exactly. (A
least-squares conic fit through the four points cannot do this: opposite points
give identical equations, leaving 2 constraints for 3 unknowns.)

Alternatively the rim can be a free-form **polygon** (``shape: polygon``), e.g.
when a bowl is partly hidden or not elliptical in the image. Either way, contact
uses ``rim_distance`` (< 0 inside): radial distance for the ellipse, Euclidean
distance to the nearest edge for the polygon.

The clicked centre is kept separately as the *feeding origin*: distances and
approach headings are measured to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from matplotlib.path import Path as MplPath
from pydantic import BaseModel, model_validator

RIM_POINTS = ("top", "bottom", "left", "right")
Point = tuple[float, float]


class BowlAnnotation(BaseModel):
    video: str
    shape: Literal["ellipse", "polygon"] = "ellipse"
    center: Point
    top: Point | None = None
    bottom: Point | None = None
    left: Point | None = None
    right: Point | None = None
    polygon: list[Point] | None = None
    frame_idx: int = 0
    annotated_at: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> "BowlAnnotation":
        if self.shape == "ellipse" and any(getattr(self, k) is None for k in RIM_POINTS):
            raise ValueError("an ellipse bowl needs top, bottom, left and right points")
        if self.shape == "polygon" and (not self.polygon or len(self.polygon) < 3):
            raise ValueError("a polygon bowl needs at least 3 vertices")
        return self

    def geometry(self) -> dict:
        """The fields that define the bowl (used for cache keys)."""
        keys = {"shape", "center", *(RIM_POINTS if self.shape == "ellipse" else ("polygon",))}
        return self.model_dump(mode="json", include=keys)

    def rim(self) -> "Ellipse | Polygon":
        if self.shape == "polygon":
            return Polygon(np.asarray(self.polygon, dtype=float))
        return Ellipse.from_extremes(self.top, self.bottom, self.left, self.right)


@dataclass(frozen=True)
class Ellipse:
    cx: float
    cy: float
    A: np.ndarray  # 2x2 symmetric positive-definite

    @classmethod
    def from_extremes(cls, top, bottom, left, right) -> "Ellipse":
        t, b, l, r_ = (np.asarray(p, dtype=float) for p in (top, bottom, left, right))
        w = abs(r_[0] - l[0]) / 2
        h = abs(b[1] - t[1]) / 2
        if w <= 0 or h <= 0:
            raise ValueError("bowl rim points must span a non-zero width and height")
        cross = ((b[0] - t[0]) / 2 * h + (r_[1] - l[1]) / 2 * w) / 2
        cross = float(np.clip(cross, -0.95 * w * h, 0.95 * w * h))  # keep positive-definite
        Q = np.array([[w * w, cross], [cross, h * h]])
        m = ((t + b) / 2 + (l + r_) / 2) / 2
        return cls(float(m[0]), float(m[1]), np.linalg.inv(Q))

    @property
    def axes(self) -> tuple[float, float, float]:
        """(semi-major, semi-minor, angle_rad of major axis from +x)."""
        w, v = np.linalg.eigh(self.A)  # ascending eigenvalues -> major axis first
        major, minor = 1 / np.sqrt(w[0]), 1 / np.sqrt(w[1])
        angle = float(np.arctan2(v[1, 0], v[0, 0]))
        return float(major), float(minor), angle

    @property
    def area(self) -> float:
        return float(np.pi / np.sqrt(np.linalg.det(self.A)))

    def rim_radius(self, theta: np.ndarray) -> np.ndarray:
        """Distance from centre to rim along direction(s) ``theta``."""
        u = np.stack([np.cos(theta), np.sin(theta)], axis=-1)
        q = np.einsum("...i,ij,...j->...", u, self.A, u)
        return 1 / np.sqrt(q)

    def rim_distance(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Signed radial distance (px) from the rim: < 0 inside, > 0 outside, NaN stays NaN."""
        dx, dy = np.asarray(x, float) - self.cx, np.asarray(y, float) - self.cy
        r = np.hypot(dx, dy)
        return r - self.rim_radius(np.arctan2(dy, dx))

    def contains(self, x, y, margin_px: float = 0.0) -> np.ndarray:
        """Boolean mask (NaN coordinates -> False)."""
        with np.errstate(invalid="ignore"):
            return self.rim_distance(x, y) <= margin_px

    def to_dict(self) -> dict:
        major, minor, angle = self.axes
        return {
            "type": "ellipse", "cx": self.cx, "cy": self.cy,
            "semi_major": major, "semi_minor": minor, "angle_rad": angle,
            "area_px2": self.area,
        }


@dataclass(frozen=True)
class Polygon:
    vertices: np.ndarray  # (n, 2), closed implicitly

    def __post_init__(self):
        v = np.asarray(self.vertices, dtype=float)
        if v.ndim != 2 or v.shape[1] != 2 or len(v) < 3:
            raise ValueError("polygon needs at least 3 (x, y) vertices")
        if self.area <= 0:
            raise ValueError("polygon has zero area")

    @property
    def area(self) -> float:
        x, y = self.vertices[:, 0], self.vertices[:, 1]
        return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)

    @property
    def centroid(self) -> tuple[float, float]:
        x, y = self.vertices[:, 0], self.vertices[:, 1]
        cross = x * np.roll(y, -1) - np.roll(x, -1) * y
        a = cross.sum() / 2
        return float(((x + np.roll(x, -1)) * cross).sum() / (6 * a)), float(((y + np.roll(y, -1)) * cross).sum() / (6 * a))

    def rim_distance(self, x: np.ndarray, y: np.ndarray, chunk: int = 20_000) -> np.ndarray:
        """Signed Euclidean distance (px) to the polygon boundary: < 0 inside; NaN stays NaN."""
        x, y = np.atleast_1d(np.asarray(x, float)), np.atleast_1d(np.asarray(y, float))
        out = np.full(x.shape, np.nan)
        ok = np.isfinite(x) & np.isfinite(y)
        P = np.column_stack([x[ok], y[ok]])
        a, b = self.vertices, np.roll(self.vertices, -1, axis=0)
        ab = b - a
        L2 = (ab ** 2).sum(1)
        d = np.empty(len(P))
        for i in range(0, len(P), chunk):  # bounded memory for long videos
            ap = P[i:i + chunk, None, :] - a[None]
            t = np.clip((ap * ab[None]).sum(-1) / L2[None], 0, 1)
            d[i:i + chunk] = np.sqrt(((ap - t[..., None] * ab[None]) ** 2).sum(-1)).min(1)
        inside = MplPath(self.vertices).contains_points(P)
        out[ok] = np.where(inside, -d, d)
        return out

    def contains(self, x, y, margin_px: float = 0.0) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return self.rim_distance(x, y) <= margin_px

    def to_dict(self) -> dict:
        cx, cy = self.centroid
        return {"type": "polygon", "points": self.vertices.round(2).tolist(), "cx": cx, "cy": cy, "area_px2": self.area}


def shift_rim(rim: dict, dx: float, dy: float) -> dict:
    """A rim dict (``to_dict()``) translated by (dx, dy)."""
    out = {**rim, "cx": rim["cx"] + dx, "cy": rim["cy"] + dy}
    if rim.get("type") == "polygon":
        out["points"] = [[px + dx, py + dy] for px, py in rim["points"]]
    return out


# --------------------------------------------------------------------------- storage


def bowl_path(bowls_dir: Path, video: str) -> Path:
    return Path(bowls_dir) / f"{video}.json"


def load_bowl(bowls_dir: Path, video: str) -> BowlAnnotation | None:
    p = bowl_path(bowls_dir, video)
    if not p.exists():
        return None
    return BowlAnnotation.model_validate_json(p.read_text())


def save_bowl(bowls_dir: Path, ann: BowlAnnotation) -> Path:
    ann = ann.model_copy(update={"annotated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    p = bowl_path(bowls_dir, ann.video)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ann.model_dump(mode="json"), indent=2) + "\n")
    return p


def delete_bowl(bowls_dir: Path, video: str) -> None:
    bowl_path(bowls_dir, video).unlink(missing_ok=True)
