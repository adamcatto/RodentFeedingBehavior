# Behaviour embedding (optional)

`feeding embed` is an unsupervised analysis of posture and movement. It learns a compact representation of short
windows of tracking, then clusters them into behaviour types and counts transitions between types. It needs PyTorch:

```bash
uv sync --extra embed
```

## How it works

1. **Windows.** Every tracked stretch of every video is cut into windows of `embedding.window` frames (default 90),
   taking every `embedding.stride`-th window (default 6).
2. **Features.** With `bowl_centred` featurization (the default), each window holds the x and y of every tracked
   keypoint relative to the bowl centre, plus its confidence, z-scored over all videos. This needs bowl annotations.
3. **Model.** An LSTM variational autoencoder encodes each window into a 30-dimensional latent vector.
   `feeding embed --train` trains a new model on the project's videos. The trained model is saved as `model.pt` in
   the output folder.
4. **Clusters.** The latent means are clustered with k-means for each k in `embedding.k_range`. The k with the best
   silhouette score is kept.
5. **Transitions.** For every video, the matrix of transition probabilities between clusters of consecutive windows
   is computed.

GPU (CUDA) or Apple-silicon (MPS) acceleration is used when available.

```bash
feeding embed --train --epochs 50      # train a model and cluster
feeding embed                          # reuse the model in embedding.checkpoint
```

To reuse a trained model, e.g. to embed a new cohort with the same model, set `embedding.checkpoint` to its
`model.pt`. The `legacy` featurization (absolute coordinates, min-max scaled per video) exists for models trained
with an earlier version of this pipeline; use it only with such a checkpoint.

## Outputs

`data/results/embedding_<date-time>_<trained|pretrained>/`:

| File | Contents |
|---|---|
| `embeddings.parquet` | video, frame, cluster and the latent vector of every window |
| `cluster_labels.csv` | video, frame, cluster |
| `silhouette.csv` | silhouette score per k |
| `transition_matrices/<video>.npy` | cluster-to-cluster transition probabilities |
| `training_history.csv`, `model.pt` | with `--train`: losses per epoch and the trained model |
| `manifest.json` | settings, model, features, best k, code and package versions |

## Interpreting clusters

Clusters are found without supervision. Before giving one a name such as "grooming" or "rearing", watch windows from
it: the frames are listed in `cluster_labels.csv`, and the app's address bar takes
`#<video>/track/<frame>`. Clusters can differ between models and data sets, so compare cluster use between groups
only within one embedding run.
