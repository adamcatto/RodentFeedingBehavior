"""Exploratory multivariate analyses of bout features: PCA / UMAP projections,
local same-condition clustering and a random-forest condition classifier.

All randomness is seeded from ``analysis.seed``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.decomposition import PCA
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from .stats import feature_columns

def pca_features(node: str = "Snout") -> list[str]:
    """The reduced feature set for the projections (``node`` = the bout node)."""
    return ["num_interaction_frames", "num_returns", "approach_indirectness", "withdrawal_indirectness",
            f"{node}_approach_speed_mean", f"{node}_withdrawal_speed_mean", f"{node}_interaction_speed_mean"]


def standardized(features: pd.DataFrame, cols: list[str]) -> tuple[np.ndarray, pd.DataFrame]:
    """Z-scored matrix over rows with no missing values in ``cols``, plus those rows."""
    sub = features.dropna(subset=cols)
    return StandardScaler().fit_transform(sub[cols].to_numpy()), sub


def project(features: pd.DataFrame, method: str, seed: int, cols: list[str] | None = None):
    cols = [c for c in (cols or pca_features()) if c in features.columns]
    X, sub = standardized(features, cols)
    if method == "PCA":
        pca = PCA(n_components=2, random_state=seed).fit(X)
        return pca.transform(X), sub, {"explained_variance_ratio": pca.explained_variance_ratio_.tolist(), "features": cols}
    import umap  # optional extra

    return umap.UMAP(random_state=seed).fit_transform(X), sub, {"features": cols}


def local_condition_clustering(coords: np.ndarray, meta: pd.DataFrame, label: str, k: int = 31) -> pd.DataFrame:
    """For each point, the number of its k nearest neighbours with condition ``label``,
    divided by the label's base-rate ratio. Computed per context."""
    out = []
    for ctx, idx in meta.groupby("context").indices.items():
        sub = meta.iloc[idx]
        is_lbl = (sub["condition"] == label).to_numpy()
        ratio = is_lbl.sum() / max((~is_lbl).sum(), 1)
        kk = min(k, len(idx) - 1)
        if kk < 1 or ratio == 0:
            continue
        _, nbrs = NearestNeighbors(n_neighbors=kk + 1).fit(coords[idx]).kneighbors(coords[idx])
        metric = is_lbl[nbrs[:, 1:]].sum(axis=1) / ratio
        out.append(pd.DataFrame({"context": ctx, "condition": sub["condition"].to_numpy(), "metric": metric}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["context", "condition", "metric"])


def _oversample(X: np.ndarray, y: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Random oversampling of minority classes to the majority count."""
    classes, counts = np.unique(y, return_counts=True)
    idx = [np.flatnonzero(y == c) for c in classes]
    idx = np.concatenate([np.r_[i, rng.choice(i, counts.max() - len(i), replace=True)] for i in idx])
    return X[idx], y[idx]


def condition_classifier(features: pd.DataFrame, conditions: list[str], seed: int) -> pd.DataFrame:
    """Random forest, first vs last of ``conditions``, per context: held-out ROC AUC + feature importances."""
    cols = [c for c in feature_columns(features)]
    rows = []
    for ctx, sub in features[features["condition"].isin(conditions)].groupby("context"):
        X, kept = standardized(sub, cols)
        y = (kept["condition"] == conditions[-1]).astype(int).to_numpy()
        if len(np.unique(y)) < 2 or min(np.bincount(y)) < 4:
            continue
        Xtr, Xte, ytr, yte = train_test_split(X, y, stratify=y, random_state=seed)
        Xtr, ytr = _oversample(Xtr, ytr, np.random.default_rng(seed))
        rf = RandomForestClassifier(n_estimators=500, max_depth=11, max_features="sqrt", max_samples=0.8,
                                    random_state=seed, n_jobs=-1).fit(Xtr, ytr)
        auc = roc_auc_score(yte, rf.predict_proba(Xte)[:, 1])
        for f, imp in zip(cols, rf.feature_importances_):
            rows.append({"context": ctx, "auc": auc, "n_train": len(ytr), "n_test": len(yte), "feature": f, "importance": imp})
    df = pd.DataFrame(rows)
    return df.sort_values(["context", "importance"], ascending=[True, False]) if len(df) else df


def clustering_test(metric: pd.DataFrame) -> dict:
    """Welch t-test of the clustering metric between the first two contexts."""
    ctxs = sorted(metric["context"].unique()) if len(metric) else []
    if len(ctxs) < 2:
        return {}
    a = metric.loc[metric["context"] == ctxs[0], "metric"]
    b = metric.loc[metric["context"] == ctxs[1], "metric"]
    if len(a) < 2 or len(b) < 2:
        return {}
    r = stats.ttest_ind(a, b, equal_var=False)
    return {"contexts": ctxs[:2], "statistic": float(r.statistic), "pvalue": float(r.pvalue),
            f"mean_{ctxs[0]}": float(a.mean()), f"mean_{ctxs[1]}": float(b.mean())}
