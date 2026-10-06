"""LSTM variational autoencoder + k-means behaviour clustering (port of ``behavior_code``).

Optional: needs the ``embed`` extra (``uv sync --extra embed``).

Two modes:

- **Training a new model** (``feeding embed --train``) on bowl-centred
  features (``bowl_centred``, see :func:`bowl_segments`). The model is saved
  in the run folder (``model.pt``).
- **Inference with a trained model** (``feeding embed``): loads the checkpoint
  in ``embedding.checkpoint`` (a ``model.pt`` from training, or a compatible
  PyTorch-Lightning checkpoint) and encodes windows built with the featurization
  it was trained on (``embedding.featurization``). ``legacy`` reproduces the
  absolute-coordinate featurization of earlier checkpoints (:func:`legacy_segments`).

Both then embed every ``stride``-th ``window``-frame window of every tracked
segment, cluster the latent means with k-means for each k in ``k_range``,
keep the k with the best silhouette, and write per-video cluster transition
matrices. All randomness is seeded from ``analysis.seed``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

from .config import Config
from .pipeline import video_status
from .provenance import git_state, now_iso, package_versions, sha256, write_json
from .sleap_runner import prediction_paths
from .tracks import load_tracks, missing_inputs, quality_mask, segment_ids

Log = Callable[[str], None]
Segments = tuple[list[np.ndarray], list[tuple[str, int]], list[str]]

def legacy_segments(cfg: Config, log: Log) -> Segments:
    """Features as earlier (legacy) checkpoints were trained on them.

    For each video's SLEAP analysis file:

    1. ``tracks`` (2, n_nodes, T) is reshaped *without transposing* to (T, 12) and
       labelled with the skeleton's x/y column names. This is what the legacy
       training code did; it means each "x"/"y" column actually holds one node
       coordinate from 12 consecutive frames. We reproduce it because the
       trained weights expect it -- it is not a pose representation.
    2. Each column is min-max scaled over the whole video; only tracked nodes
       (``quality.drop_nodes`` removed) are kept, columns sorted by node name.
    3. Per-node scores (NaN -> 0) are the ``*_accuracy`` columns.
    4. Frames pass the same confidence filter as the main pipeline and are
       split into contiguous segments; NaNs within windows become 0.

    Only predictions are needed (no bowl annotation).
    """
    from sklearn.preprocessing import MinMaxScaler

    q = cfg.quality
    legacy_nodes = sorted(cfg.tracked_nodes)
    legacy_columns = [f"{n}_{c}" for n in legacy_nodes for c in ("accuracy", "x", "y")]
    segs, where = [], []
    for row in video_status(cfg).itertuples(index=False):
        h5 = prediction_paths(cfg, row.video)["h5"]
        if not h5.exists():
            continue
        with h5py.File(h5, "r") as f:
            nodes = [n.decode() if isinstance(n, bytes) else str(n) for n in f["node_names"][:]]
            tracks = np.squeeze(np.asarray(f["tracks"], dtype=float), axis=0)  # (2, n_nodes, T)
            scores = np.squeeze(np.asarray(f["point_scores"], dtype=float), axis=0).T  # (T, n_nodes)
        T = tracks.shape[-1]
        xy_cols = [f"{n}_{c}" for n in nodes for c in ("x", "y")]
        xy = pd.DataFrame(MinMaxScaler().fit_transform(tracks.reshape((T, len(xy_cols)))), columns=xy_cols)
        acc = pd.DataFrame(np.nan_to_num(scores, nan=0.0), columns=[f"{n}_accuracy" for n in nodes])
        df = pd.concat([xy, acc], axis=1)[legacy_columns]
        valid = quality_mask(acc.rename(columns=lambda c: c.replace("_accuracy", "_score")),
                             legacy_nodes, q.score_window, q.min_score)
        seg = segment_ids(valid, q.min_segment_frames)
        values = df.fillna(0.0).to_numpy(np.float32)
        for k in np.unique(seg[seg >= 0]):
            idx = np.flatnonzero(seg == k)
            segs.append(values[idx])
            where.append((row.video, int(idx[0])))
    log(f"legacy features: {len(segs)} segments from {len({w[0] for w in where})} videos")
    return segs, where, legacy_columns


def bowl_segments(cfg: Config, log: Log) -> Segments:
    """Correct features for training a new model: bowl-centred x, y and score of
    each tracked node, z-scored over all videos. Needs predictions and a bowl."""
    nodes = cfg.tracked_nodes
    cols = [f"{n}_{c}" for n in nodes for c in ("x", "y", "score")]
    segs, where = [], []
    for row in video_status(cfg).itertuples(index=False):
        if missing_inputs(cfg, row.video):
            continue
        tracks, info = load_tracks(cfg, row.video, Path(row.path))
        cx, cy = info["center"]
        df = tracks[cols].copy()
        for n in nodes:
            df[f"{n}_x"] -= cx
            df[f"{n}_y"] -= cy
        values = df.fillna(0.0).to_numpy(np.float32)
        seg = tracks["segment"].to_numpy()
        for k in np.unique(seg[seg >= 0]):
            idx = np.flatnonzero(seg == k)
            segs.append(values[idx])
            where.append((row.video, int(idx[0])))
    if segs:
        allf = np.concatenate(segs)
        mu, sd = allf.mean(0), allf.std(0) + 1e-6
        segs = [(s - mu) / sd for s in segs]
    log(f"bowl_centred features: {len(segs)} segments from {len({w[0] for w in where})} videos")
    return segs, where, cols


def _model_class():
    import torch
    from torch import nn

    class Encoder(nn.Module):
        def __init__(self, n_feat, hidden, depth):
            super().__init__()
            self.model = nn.LSTM(n_feat, hidden, depth, batch_first=True)

        def forward(self, x):
            _, (h, _) = self.model(x)
            return h[-1]

    class Lambda(nn.Module):
        def __init__(self, hidden, latent):
            super().__init__()
            self.hidden_to_mean = nn.Linear(hidden, latent)
            self.hidden_to_logvar = nn.Linear(hidden, latent)

    class Decoder(nn.Module):
        def __init__(self, n_feat, hidden, depth, latent):
            super().__init__()
            self.depth = depth
            self.model = nn.LSTM(1, hidden, depth, batch_first=True)
            self.latent_to_hidden = nn.Linear(latent, hidden)
            self.hidden_to_output = nn.Linear(hidden, n_feat)

        def forward(self, z, seq_len):
            h0 = self.latent_to_hidden(z).unsqueeze(0).repeat(self.depth, 1, 1)
            inp = torch.zeros(z.shape[0], seq_len, 1, device=z.device)
            y, _ = self.model(inp, (h0, torch.zeros_like(h0)))
            return self.hidden_to_output(y)

    class VRAE(nn.Module):
        """Parameter names match the original Lightning module so its checkpoints load directly."""

        def __init__(self, n_feat, hidden=20, depth=4, latent=30):
            super().__init__()
            self.encoder = Encoder(n_feat, hidden, depth)
            self.lmbd = Lambda(hidden, latent)
            self.decoder = Decoder(n_feat, hidden, depth, latent)

        def encode(self, x):
            h = self.encoder(x)
            return self.lmbd.hidden_to_mean(h), self.lmbd.hidden_to_logvar(h)

        def forward(self, x):
            mu, logvar = self.encode(x)
            z = mu + torch.randn_like(mu) * torch.exp(0.5 * logvar) if self.training else mu
            return self.decoder(z, x.shape[1]), mu, logvar

    return VRAE


def load_checkpoint(path: Path, n_feat: int):
    """Load a checkpoint (architecture inferred from tensor shapes)."""
    import torch

    ck = torch.load(path, map_location="cpu", weights_only=False)
    sd = {k: v for k, v in ck["state_dict"].items() if k not in ("decoder.c_0", "decoder.decoder_inputs")}
    hidden = sd["encoder.model.weight_hh_l0"].shape[1]
    depth = sum(1 for k in sd if k.startswith("encoder.model.weight_hh_l"))
    latent = sd["lmbd.hidden_to_mean.weight"].shape[0]
    feat = sd["encoder.model.weight_ih_l0"].shape[1]
    if feat != n_feat:
        raise ValueError(f"checkpoint expects {feat} features, featurization gives {n_feat}")
    model = _model_class()(n_feat, hidden, depth, latent)
    model.load_state_dict(sd, strict=True)
    return model, {"hidden_size": hidden, "depth": depth, "latent": latent, "epoch": ck.get("epoch"),
                   "global_step": ck.get("global_step"), "lightning_version": ck.get("pytorch-lightning_version")}


def train_model(segs, n_feat, window, epochs, windows_per_epoch, batch_size, seed, device, log, hidden=20, depth=4, latent=30):
    import torch
    from torch import nn

    rng = np.random.default_rng(seed)
    torch.manual_seed(seed)
    order = rng.permutation(len(segs))
    n_val = max(1, len(segs) // 5)
    val_ids, train_ids = order[:n_val], order[n_val:]

    def sample(ids, n):
        lens = np.array([len(segs[i]) - window + 1 for i in ids])
        pick = rng.choice(len(ids), size=n, p=lens / lens.sum())
        starts = rng.integers(0, lens[pick])
        return torch.from_numpy(np.stack([segs[ids[p]][s:s + window] for p, s in zip(pick, starts)]))

    model = _model_class()(n_feat, hidden, depth, latent).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=5e-4)
    sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, factor=0.5, patience=3, min_lr=1e-5)

    def loss_fn(x):
        y, mu, logvar = model(x)
        kl = -0.5 * torch.mean(1 + logvar - mu.pow(2) - logvar.exp())
        return nn.functional.mse_loss(y, x, reduction="sum") / x.shape[0] + kl

    val_x = sample(val_ids, min(10_000, max(batch_size, windows_per_epoch // 5))).to(device)
    history = []
    for ep in range(epochs):
        model.train()
        xs = sample(train_ids, windows_per_epoch)
        tot = 0.0
        for i in range(0, len(xs), batch_size):
            x = xs[i:i + batch_size].to(device)
            opt.zero_grad()
            loss = loss_fn(x)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            tot += loss.item() * len(x)
        model.eval()
        with torch.no_grad():
            vl = float(np.mean([loss_fn(val_x[i:i + batch_size]).item() for i in range(0, len(val_x), batch_size)]))
        sched.step(vl)
        history.append({"epoch": ep, "train_loss": tot / len(xs), "val_loss": vl})
        log(f"epoch {ep + 1}/{epochs}  train {tot / len(xs):.2f}  val {vl:.2f}")
    return model, history


def run_embedding(cfg: Config, train: bool = False, epochs: int = 50, windows_per_epoch: int = 100_000,
                  batch_size: int = 256, log: Log = print) -> Path:
    import torch
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    e = cfg.embedding
    seed = cfg.analysis.seed
    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    t0 = time.time()

    featurization = "bowl_centred" if train else e.featurization
    segs, where, cols = (legacy_segments if featurization == "legacy" else bowl_segments)(cfg, log)
    keep = [i for i, s in enumerate(segs) if len(s) >= e.window]
    segs, where = [segs[i] for i in keep], [where[i] for i in keep]
    if not segs:
        raise RuntimeError("no tracked segments long enough (run SLEAP / annotate bowls first)")

    history, model_info = [], {}
    if train:
        model, history = train_model(segs, len(cols), e.window, epochs, windows_per_epoch, batch_size, seed, device, log)
        model_info = {"source": "trained", "epochs": epochs, "windows_per_epoch": windows_per_epoch}
    else:
        if e.checkpoint is None:
            raise FileNotFoundError("no embedding.checkpoint set: train a model with `feeding embed --train`, "
                                    "then set embedding.checkpoint to its model.pt to reuse it")
        if not e.checkpoint.exists():
            raise FileNotFoundError(f"embedding.checkpoint not found: {e.checkpoint}")
        model, model_info = load_checkpoint(e.checkpoint, len(cols))
        model_info = {"source": "pretrained", "checkpoint": str(e.checkpoint),
                      "checkpoint_sha256": sha256(e.checkpoint), **model_info}
        log(f"loaded pretrained model {e.checkpoint.name} (epoch {model_info['epoch']})")
    model = model.to(device).eval()

    lat, meta = [], []
    with torch.no_grad():
        for s, (video, f0) in zip(segs, where):
            starts = np.arange(0, len(s) - e.window + 1, e.stride)
            for i in range(0, len(starts), 1024):
                b = torch.from_numpy(np.stack([s[j:j + e.window] for j in starts[i:i + 1024]])).to(device)
                lat.append(model.encode(b)[0].cpu().numpy())
                meta += [(video, f0 + int(j)) for j in starts[i:i + 1024]]
    Z = np.concatenate(lat)
    log(f"embedded {len(Z)} windows")

    rng = np.random.default_rng(seed)
    sil_idx = rng.choice(len(Z), size=min(len(Z), 20_000), replace=False)
    scores, labels = {}, {}
    for k in range(*e.k_range):
        labels[k] = KMeans(n_clusters=k, random_state=seed, n_init=10).fit_predict(Z)
        scores[k] = float(silhouette_score(Z[sil_idx], labels[k][sil_idx], random_state=seed))
        log(f"k={k} silhouette {scores[k]:.3f}")
    best = max(scores, key=scores.get)

    out = cfg.paths.results / f"embedding_{datetime.now():%Y%m%d-%H%M%S}_{model_info['source']}"
    (out / "transition_matrices").mkdir(parents=True)
    emb = pd.DataFrame(meta, columns=["video", "frame"])
    emb["cluster"] = labels[best]
    emb = pd.concat([emb, pd.DataFrame(Z, columns=[f"z{i}" for i in range(Z.shape[1])])], axis=1)
    emb.to_parquet(out / "embeddings.parquet")
    emb[["video", "frame", "cluster"]].to_csv(out / "cluster_labels.csv", index=False)
    pd.DataFrame({"k": list(scores), "silhouette": list(scores.values())}).to_csv(out / "silhouette.csv", index=False)
    if train:
        pd.DataFrame(history).to_csv(out / "training_history.csv", index=False)
        torch.save({"state_dict": model.state_dict(), "columns": cols}, out / "model.pt")

    for video, g in emb.groupby("video"):
        g = g.sort_values("frame")
        lab, fr = g["cluster"].to_numpy(), g["frame"].to_numpy()
        M = np.zeros((best, best))
        consecutive = np.diff(fr) == e.stride
        for a, b in zip(lab[:-1][consecutive], lab[1:][consecutive]):
            if a != b:
                M[a, b] += 1
        with np.errstate(invalid="ignore"):
            M = M / M.sum(axis=1, keepdims=True)
        np.save(out / "transition_matrices" / f"{video}.npy", np.nan_to_num(M))

    write_json(out / "manifest.json", {
        "created_at": now_iso(), "elapsed_s": round(time.time() - t0, 1), "device": device,
        "model": model_info, "featurization": featurization, "features": cols,
        "params": {**e.model_dump(mode="json"), "seed": seed},
        "best_k": best, "silhouette": scores, "n_windows": int(len(Z)),
        "git": git_state(), "packages": package_versions(), "videos": sorted({w[0] for w in where}),
    })
    log(f"done: best k={best} -> {out}")
    return out
