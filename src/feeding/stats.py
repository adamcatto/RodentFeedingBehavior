"""Group comparisons ("views") of bout features and session measures.

A *view* (``feeding.yaml: views``) names two or more groups, each a filter on
the video metadata (group, condition, context, chamber, animal, session, ...),
and the pairs of groups to test against each other. The *standard view* is
built from the project's groups, conditions and contexts:

- every pair of groups (e.g. Control vs Treated), within each context x condition
  (the first and the last analysed condition)
- first vs last condition (e.g. Pre vs Post), within each context x group

``analysis.unit`` selects the sample:

- ``animal`` (default): features are averaged per animal within each group, so
  each animal contributes one value per group. Two groups that share animals
  (e.g. Base vs Test of the same mice) are compared with a paired test over the
  shared animals; see ``View.paired``.
- ``bout``: every bout is a sample. Bouts from one animal are not
  independent, so p-values from this mode are optimistic.

Session measures are always compared per animal.

P-values are corrected across features within each pair (``p_adj``)
and across every pair x feature of the view (``p_adj_view``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .config import VIEW_FIELDS, AnalysisConfig, View, ViewGroup

META_COLS = ["video", "session", "part", "condition", "context", "animal", "chamber", "group", "bout", "start_frame"]


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in META_COLS and pd.api.types.is_numeric_dtype(df[c])]


# --------------------------------------------------------------------------- views


def analysis_conditions(conditions: list[str], df: pd.DataFrame) -> list[str]:
    """``analysis.conditions`` present in ``df``; if none are configured, every condition
    in ``df`` ordered by its earliest session number (then by name)."""
    present = set(df["condition"].dropna().astype(str)) - {"NA"} if "condition" in df else set()
    if conditions:
        return [c for c in conditions if c in present]

    def first_session(c):
        s = pd.to_numeric(df.loc[df["condition"] == c, "session"], errors="coerce") if "session" in df else pd.Series()
        return (s.min() if s.notna().any() else np.inf, c)

    return sorted(present, key=first_session)


def standard_view(conditions: list[str], groups: list[str], contexts: list[str]) -> View | None:
    """The standard comparisons as a view (None without two groups or two conditions)."""
    if len(groups) < 2 or len(conditions) < 2:
        return None
    base, test = conditions[0], conditions[-1]
    group_pairs = [(g1, g2) for i, g1 in enumerate(groups) for g2 in groups[i + 1:]]
    cells, pairs = [], []

    def cell(grp, cond, ctx):
        label = " ".join(x for x in (grp, cond, ctx) if x)
        if label not in [c.label for c in cells]:
            where = {"group": [grp], "condition": [cond]} | ({"context": [ctx]} if ctx else {})
            cells.append(ViewGroup(label=label, where=where))
        return label

    ctxs = contexts or [None]
    for ctx in ctxs:
        for cond in (base, test):
            for g1, g2 in group_pairs:
                pairs.append((cell(g1, cond, ctx), cell(g2, cond, ctx)))
    for ctx in ctxs:
        for grp in groups:
            pairs.append((cell(grp, base, ctx), cell(grp, test, ctx)))
    between = " vs ".join(groups) if len(groups) == 2 else "each pair of groups"
    return View(name="Standard", groups=cells, pairs=pairs,
                description=f"{between} within each context x condition, and {base} vs {test} within each "
                            "context x group.")


def select(df: pd.DataFrame, where: dict[str, list[str]]) -> pd.Series:
    """Rows matching every field of ``where`` (values compared as strings)."""
    m = pd.Series(True, index=df.index)
    for field, values in where.items():
        if field not in df.columns:
            return pd.Series(False, index=df.index)
        m &= df[field].astype(str).isin(values)
    return m


def group_data(df: pd.DataFrame, group: ViewGroup, unit: str) -> pd.DataFrame:
    """The samples of one group: rows (unit=bout) or per-animal means (unit=animal), indexed by animal."""
    sub = df[select(df, group.where)]
    feats = feature_columns(sub)
    if unit == "bout":
        return sub.set_index("animal", drop=False)[feats]
    agg = sub.groupby("animal")[feats].mean()
    agg["n_bouts" if "bout" in df.columns else "n_sessions"] = sub.groupby("animal").size()
    return agg


def is_paired(view: View, A: pd.DataFrame, B: pd.DataFrame, unit: str) -> bool:
    """``auto``: paired when unit=animal and at least half of each side's animals
    (and >= 2) appear on both sides -- e.g. the same mice before and after."""
    if unit != "animal" or view.paired == "no":
        return False
    shared = A.index.intersection(B.index)
    if view.paired == "yes":
        return len(shared) >= 2
    return len(shared) >= 2 and len(shared) >= 0.5 * max(1, min(len(A), len(B)))


# --------------------------------------------------------------------------- tests


_TEST_NAMES = {
    ("welch", False): "Welch t-test", ("student", False): "Student t-test", ("mannwhitney", False): "Mann-Whitney U",
    ("welch", True): "paired t-test", ("student", True): "paired t-test", ("mannwhitney", True): "Wilcoxon signed-rank",
}
_OMNIBUS_NAMES = {"welch": "Alexander-Govern", "student": "one-way ANOVA", "mannwhitney": "Kruskal-Wallis"}


def _test(a: pd.Series, b: pd.Series, paired: bool, method: str) -> tuple[float, float]:
    if paired:
        d = pd.concat([a, b], axis=1).dropna()
        if len(d) < 2:
            return np.nan, np.nan
        x, y = d.iloc[:, 0], d.iloc[:, 1]
        if np.allclose(x - y, 0):
            return np.nan, np.nan
        r = stats.wilcoxon(x, y) if method == "mannwhitney" else stats.ttest_rel(x, y)
    else:
        x, y = a.dropna(), b.dropna()
        if len(x) < 2 or len(y) < 2:
            return np.nan, np.nan
        if method == "mannwhitney":
            r = stats.mannwhitneyu(x, y, alternative="two-sided")
        else:
            r = stats.ttest_ind(x, y, equal_var=(method == "student"))
    return float(r.statistic), float(r.pvalue)


def _effect(a: pd.Series, b: pd.Series, paired: bool) -> float:
    """Hedges' g (b - a) for independent samples; d_z (mean difference / sd) for paired."""
    if paired:
        d = (b - a).dropna()
        return float(d.mean() / d.std(ddof=1)) if len(d) >= 2 and d.std(ddof=1) > 0 else np.nan
    x, y = a.dropna(), b.dropna()
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        return np.nan
    sp = np.sqrt(((n1 - 1) * x.var(ddof=1) + (n2 - 1) * y.var(ddof=1)) / (n1 + n2 - 2))
    if not sp > 0:
        return np.nan
    j = 1 - 3 / (4 * (n1 + n2) - 9)  # small-sample correction
    return float(j * (y.mean() - x.mean()) / sp)


def _adjust(p: np.ndarray, method: str) -> np.ndarray:
    out = np.full_like(p, np.nan, dtype=float)
    ok = np.isfinite(p)
    if ok.any():
        if method == "bonferroni":
            out[ok] = np.minimum(p[ok] * ok.sum(), 1.0)
        else:
            out[ok] = stats.false_discovery_control(p[ok], method="bh")
    return out


def stars(p: float) -> str:
    if p is None or not np.isfinite(p):
        return ""
    return "****" if p < 1e-4 else "***" if p < 1e-3 else "**" if p < 0.01 else "*" if p < 0.05 else "ns"


def compare_view(df: pd.DataFrame, view: View, cfg: AnalysisConfig, unit: str | None = None) -> pd.DataFrame:
    """Long table: one row per pair x feature."""
    unit = unit or cfg.unit
    data = {g.label: group_data(df, g, unit) for g in view.groups}
    feats = [c for c in feature_columns(df)]
    blocks = []
    for a, b in view.pair_list():
        A, B = data[a], data[b]
        paired = is_paired(view, A, B, unit)
        if paired:
            common = A.index.intersection(B.index)
            A, B = A.loc[common], B.loc[common]
        rows = []
        for f in feats:
            x, y = A.get(f, pd.Series(dtype=float)), B.get(f, pd.Series(dtype=float))
            stat, p = _test(x, y, paired, cfg.test)
            rows.append({
                "view": view.name, "pair": f"{a} vs {b}", "a": a, "b": b, "feature": f, "unit": unit,
                "n_a": int(x.notna().sum()), "n_b": int(y.notna().sum()),
                "mean_a": x.mean(), "mean_b": y.mean(), "sd_a": x.std(ddof=1), "sd_b": y.std(ddof=1),
                "median_a": x.median(), "median_b": y.median(), "diff": y.mean() - x.mean(),
                "effect_size": _effect(x, y, paired), "effect_measure": "d_z" if paired else "Hedges g",
                "paired": paired, "test": _TEST_NAMES[(cfg.test, paired)], "statistic": stat, "pvalue": p,
            })
        block = pd.DataFrame(rows)
        if len(block):
            block["p_adj"] = _adjust(block["pvalue"].to_numpy(), cfg.correction)
            blocks.append(block)
    if not blocks:
        return pd.DataFrame()
    out = pd.concat(blocks, ignore_index=True)
    out["p_adj_view"] = _adjust(out["pvalue"].to_numpy(), cfg.correction)
    out["correction"] = cfg.correction
    out["stars"] = [stars(p) for p in out["p_adj"]]
    return out


def omnibus(df: pd.DataFrame, view: View, cfg: AnalysisConfig, unit: str | None = None) -> pd.DataFrame:
    """One test across all groups per feature (views with three or more groups).

    Friedman when every group has the same animals (unit=animal, repeated
    measures), otherwise Alexander-Govern / one-way ANOVA / Kruskal-Wallis
    following ``analysis.test``.
    """
    unit = unit or cfg.unit
    if len(view.groups) < 3:
        return pd.DataFrame()
    data = [group_data(df, g, unit) for g in view.groups]
    animal_sets = [set(d.index) for d in data]
    repeated = unit == "animal" and view.paired != "no" and all(s == animal_sets[0] for s in animal_sets) and len(animal_sets[0]) >= 2
    rows = []
    for f in feature_columns(df):
        try:
            if repeated:
                m = pd.concat([d[f] for d in data], axis=1).dropna()
                r = stats.friedmanchisquare(*[m.iloc[:, i] for i in range(m.shape[1])]) if len(m) >= 2 else None
                name = "Friedman"
            else:
                samples = [d[f].dropna() for d in data]
                if any(len(s) < 2 for s in samples):
                    r = None
                elif cfg.test == "mannwhitney":
                    r = stats.kruskal(*samples)
                elif cfg.test == "student":
                    r = stats.f_oneway(*samples)
                else:
                    r = stats.alexandergovern(*samples)
                name = _OMNIBUS_NAMES[cfg.test]
        except ValueError:
            r = None
        rows.append({"view": view.name, "feature": f, "unit": unit, "test": name if r else "n/a",
                     "statistic": float(r.statistic) if r else np.nan, "pvalue": float(r.pvalue) if r else np.nan})
    out = pd.DataFrame(rows)
    out["p_adj"] = _adjust(out["pvalue"].to_numpy(), cfg.correction)
    out["stars"] = [stars(p) for p in out["p_adj"]]
    return out


def group_sizes(df: pd.DataFrame, view: View) -> pd.DataFrame:
    """Per group (over a per-session table): animals, sessions and videos matched."""
    rows = []
    for g in view.groups:
        sub = df[select(df, g.where)]
        rows.append({"group": g.label, "where": "; ".join(f"{k} = {', '.join(v)}" for k, v in g.where.items()) or "everything",
                     "animals": sub["animal"].nunique(), "sessions": len(sub),
                     "videos": int(sub["video"].astype(str).str.count(r"\+").sum() + len(sub)),
                     "animal_ids": ", ".join(sorted(sub["animal"].astype(str).unique(), key=lambda s: (len(s), s)))})
    return pd.DataFrame(rows)


def metadata_values(meta: pd.DataFrame) -> dict[str, list[str]]:
    """Distinct values of each view field, for building views."""
    from .naming import natural_key

    out = {}
    for f in VIEW_FIELDS:
        if f in meta.columns:
            vals = sorted({str(v) for v in meta[f].dropna()}, key=natural_key)
            if f == "condition":  # experimental order (by first session), not alphabetical
                order = analysis_conditions([], meta)
                vals = order + [v for v in vals if v not in order]
            if vals:
                out[f] = vals
    return out


def write_excel(path, sheets: dict[str, pd.DataFrame]) -> None:
    """Workbook with one sheet per table (names truncated to Excel's 31 chars, made unique)."""
    used = set()
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, df in sheets.items():
            base = "".join(c for c in name if c not in "[]:*?/\\")[:31] or "sheet"
            sheet, k = base, 2
            while sheet in used:
                sheet = f"{base[:28]}~{k}"
                k += 1
            used.add(sheet)
            df.to_excel(w, sheet_name=sheet, index=False)
