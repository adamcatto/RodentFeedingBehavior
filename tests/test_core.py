import numpy as np
import pandas as pd
import pytest

from feeding.bouts import detect_bouts
from feeding.bowl import BowlAnnotation, Ellipse
from feeding.config import BoutConfig, Config
from feeding.features import bout_features, heading_angles
from feeding.naming import parse_video_name
from feeding.tracks import quality_mask, runs, segment_ids


# --------------------------------------------------------------------------- naming

@pytest.mark.parametrize("name,expected", [
    ("33_Post_A_12", ("33", None, "Post", "A", "12")),
    ("11_1_Pre_B_3.mp4", ("11", "1", "Pre", "B", "3")),
])
def test_parse_video_name(name, expected):
    i = parse_video_name(name)
    assert (i.session, i.part, i.condition, i.context, i.animal) == expected


def test_parse_video_name_rejects_quadrant_names():
    with pytest.raises(ValueError):
        parse_video_name("33_Post_A_11_12_13_14")


# --------------------------------------------------------------------------- bowl

def test_circle_fit():
    e = Ellipse.from_extremes((100, 40), (100, 60), (90, 50), (110, 50))
    major, minor, _ = e.axes
    assert major == pytest.approx(10) and minor == pytest.approx(10)
    assert e.area == pytest.approx(np.pi * 100)
    assert e.contains(105, 50) and not e.contains(111, 50)
    assert e.contains(111, 50, margin_px=1.5)
    assert e.rim_distance(np.array([120.0]), np.array([50.0]))[0] == pytest.approx(10)


def test_axis_aligned_ellipse_and_nan():
    ann = BowlAnnotation(video="v", center=(0, 0), top=(0, -5), bottom=(0, 5), left=(-20, 0), right=(20, 0))
    e = ann.rim()
    major, minor, angle = e.axes
    assert (major, minor) == pytest.approx((20, 5))
    assert abs(np.cos(angle)) == pytest.approx(1)
    assert e.rim_radius(np.array([0.0, np.pi / 2])) == pytest.approx([20, 5])
    assert not e.contains(np.nan, np.nan)


def test_rotated_extremal_points_recover_ellipse():
    # The top/bottom/left/right clicks are the rim's extremal points. For a rotated
    # ellipse these lie off the axes through the centre, which identifies rotation.
    a, b, th = 15.0, 9.0, np.radians(25)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    A_true = R @ np.diag([1 / a**2, 1 / b**2]) @ R.T
    Q = np.linalg.inv(A_true)
    top = -Q[:, 1] / np.sqrt(Q[1, 1])   # argmin y on the rim
    left = -Q[:, 0] / np.sqrt(Q[0, 0])  # argmin x on the rim
    e = Ellipse.from_extremes(top, -top, left, -left)
    assert e.A == pytest.approx(A_true, rel=1e-9)
    assert e.axes[:2] == pytest.approx((a, b))
    assert e.axes[2] % np.pi == pytest.approx(th)
    assert (e.cx, e.cy) == pytest.approx((0, 0))


# --------------------------------------------------------------------------- tracks

def test_runs_and_segments():
    m = np.array([0, 1, 1, 0, 1, 1, 1, 0], bool)
    assert runs(m) == [(1, 3), (4, 7)]
    assert segment_ids(m, 3).tolist() == [-1, -1, -1, -1, 0, 0, 0, -1]


def test_quality_mask_window_edges():
    df = pd.DataFrame({"A_score": np.ones(20), "B_score": np.r_[np.ones(10), np.zeros(10)]})
    m = quality_mask(df, ["A", "B"], window=4, min_score=0.5)
    assert not m[0] and not m[-1]  # incomplete windows are invalid
    assert m[5] and not m[15]


# --------------------------------------------------------------------------- bouts

def _tracks(in_bowl, valid=None):
    n = len(in_bowl)
    valid = np.ones(n, bool) if valid is None else valid
    return pd.DataFrame({"in_bowl": np.asarray(in_bowl, bool) & valid, "segment": np.where(valid, 0, -1)})


def test_bout_merging_and_flanks():
    cfg = BoutConfig(min_contact_frames=5, merge_gap_frames=10, flank_frames=8)
    x = np.zeros(100, bool)
    x[20:30] = True   # episode 1
    x[35:37] = True   # return after a 5-frame gap -> same bout
    x[60:62] = True   # isolated short touch -> dropped (< min_contact_frames)
    b = detect_bouts(_tracks(x), cfg)
    assert len(b) == 1
    r = b.iloc[0]
    assert (r.window_start, r.contact_start, r.contact_end, r.window_end) == (12, 20, 37, 45)
    assert r.n_episodes == 2 and r.contact_frames == 12


def test_bout_requires_tracked_flanks():
    cfg = BoutConfig(min_contact_frames=5, merge_gap_frames=10, flank_frames=8)
    x = np.zeros(50, bool)
    x[5:15] = True  # only 5 frames before -> no approach window
    assert len(detect_bouts(_tracks(x), cfg)) == 0


def test_bouts_not_duplicated():
    # Two long episodes in one bout used to be emitted twice (per episode) and again (append bug).
    cfg = BoutConfig(min_contact_frames=5, merge_gap_frames=20, flank_frames=10)
    x = np.zeros(120, bool)
    x[30:40] = True
    x[50:60] = True
    assert len(detect_bouts(_tracks(x), cfg)) == 1


# --------------------------------------------------------------------------- features

def test_heading_angles():
    # Moving straight at a bowl at the origin -> 0 deg; straight away -> 180 deg.
    x = np.array([-10.0, -9, -8])
    assert heading_angles(x, np.zeros(3), 0, 0) == pytest.approx([0, 0])
    assert heading_angles(-x, np.zeros(3) + 0, 0, 0) == pytest.approx([0, 0])
    assert heading_angles(np.array([1.0, 2, 3]), np.zeros(3), 0, 0) == pytest.approx([180, 180])


def test_bout_features_shape():
    cfg = Config(skeleton={"nodes": ["Snout", "LeftEar", "RightEar", "StartTail", "TipTail", "MidBack"]},
                 quality={"drop_nodes": ["TipTail"]})
    n = 100
    t = np.arange(n, dtype=float)
    tracks = pd.DataFrame({f"{node}_{c}": t for node in cfg.skeleton.nodes for c in ("x", "y")})
    bouts = pd.DataFrame([{"bout": 0, "window_start": 10, "contact_start": 30, "contact_end": 60,
                           "window_end": 80, "n_episodes": 3, "contact_frames": 20}])
    f = bout_features(tracks, bouts, 0.0, 0.0, 30.0, cfg)
    assert f.loc[0, "num_returns"] == 2
    assert f.loc[0, "num_interaction_frames"] == 30
    assert f.loc[0, "Snout_approach_speed_mean"] == pytest.approx(np.sqrt(2))


def test_polygon_rim():
    from feeding.bowl import Polygon

    sq = Polygon(np.array([[0, 0], [10, 0], [10, 10], [0, 10]], float))
    assert sq.area == pytest.approx(100) and sq.centroid == pytest.approx((5, 5))
    d = sq.rim_distance(np.array([5.0, 12.0, 1.0, np.nan]), np.array([5.0, 5.0, 5.0, 1.0]))
    assert d[:3] == pytest.approx([-5, 2, -1]) and np.isnan(d[3])
    assert sq.contains(np.array([11.0]), np.array([5.0]), margin_px=1.5).all()
    assert not sq.contains(np.array([np.nan]), np.array([5.0])).any()


def test_polygon_annotation_validation():
    ok = BowlAnnotation(video="v", shape="polygon", center=(1, 1), polygon=[(0, 0), (2, 0), (2, 2)])
    assert ok.rim().to_dict()["type"] == "polygon"
    assert "polygon" in ok.geometry() and "top" not in ok.geometry()
    with pytest.raises(ValueError):
        BowlAnnotation(video="v", shape="polygon", center=(1, 1), polygon=[(0, 0), (2, 0)])
    with pytest.raises(ValueError):
        BowlAnnotation(video="v", center=(1, 1), top=(1, 0))  # ellipse missing rim points


def test_combine_parts_merges_split_sessions():
    import pandas as pd

    from feeding.views import combine_parts

    base = {"animal": "1", "session": "11", "condition": "Pre", "context": "B", "chamber": "c", "group": "Control"}
    rows = pd.DataFrame([
        {**base, "video": "11_1_Pre_B_1", "part": "1", "n_frames": 300, "valid_frames": 200, "in_bowl_frames": 0,
         "in_bowl_time_s": 0.0, "n_bouts": 0, "latency_to_first_contact_s": float("nan"), "mean_bout_frames": float("nan"),
         "valid_fraction": 2 / 3, "in_bowl_fraction_of_valid": 0.0, "bouts_per_valid_min": 0.0, "MidBack_speed_mean": 1.0},
        {**base, "video": "11_2_Pre_B_1", "part": "2", "n_frames": 600, "valid_frames": 600, "in_bowl_frames": 60,
         "in_bowl_time_s": 2.0, "n_bouts": 2, "latency_to_first_contact_s": 1.0, "mean_bout_frames": 30.0,
         "valid_fraction": 1.0, "in_bowl_fraction_of_valid": 0.1, "bouts_per_valid_min": 6.0, "MidBack_speed_mean": 2.0},
        {**base, "session": "16", "context": "A", "video": "16_Pre_A_1", "part": None, "n_frames": 900, "valid_frames": 900,
         "in_bowl_frames": 30, "in_bowl_time_s": 1.0, "n_bouts": 1, "latency_to_first_contact_s": 3.0, "mean_bout_frames": 30.0,
         "valid_fraction": 1.0, "in_bowl_fraction_of_valid": 1 / 30, "bouts_per_valid_min": 2.0, "MidBack_speed_mean": 1.5},
    ])
    c = combine_parts(rows).set_index("session")
    assert len(c) == 2 and c.loc["16", "video"] == "16_Pre_A_1"
    m = c.loc["11"]
    assert m["video"] == "11_1_Pre_B_1+11_2_Pre_B_1"
    assert m["n_frames"] == 900 and m["valid_frames"] == 800 and m["n_bouts"] == 2
    assert m["valid_fraction"] == pytest.approx(800 / 900)
    assert m["in_bowl_fraction_of_valid"] == pytest.approx(60 / 800)
    assert m["MidBack_speed_mean"] == pytest.approx((1.0 * 200 + 2.0 * 600) / 800)
    # fps = 30: contact 1 s into part 2, which starts 300 frames (10 s) in
    assert m["latency_to_first_contact_s"] == pytest.approx(11.0)


def test_quadrant_outputs():
    from pathlib import Path

    from feeding.video import quadrant_outputs

    out = quadrant_outputs(Path("3_Post_A_m1_m2_m3_m4.mpg"), Path("o"))
    assert [p.name for p in out] == ["3_Post_A_m1.mp4", "3_Post_A_m2.mp4", "3_Post_A_m3.mp4", "3_Post_A_m4.mp4"]
    with pytest.raises(ValueError):
        quadrant_outputs(Path("m1_m2_m3.mpg"), Path("o"))
