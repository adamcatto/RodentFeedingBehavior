# Statistical methods

This chapter describes exactly how comparisons are tested. The settings are under `analysis:` in `feeding.yaml`.

## Unit of analysis

Bouts from one animal are not independent: an animal that feeds slowly does so in every bout. Treating each bout as
a separate sample inflates n and makes p-values too small. The default therefore treats the **animal** as the unit.

| `analysis.unit` | Sample | Use |
|---|---|---|
| `animal` (default) | for each group, every animal contributes the mean of its bouts in that group | inference about animals; paired tests possible |
| `bout` | every bout is a sample | descriptive use, or to reproduce bout-level analyses; p-values are optimistic |

**Session and locomotion measures are always per animal.** If an animal has several sessions in a group (e.g. both
arenas pooled), they are averaged. Sessions recorded in several parts are first combined into one session.

## Tests

| `analysis.test` | Independent groups | Paired groups | 3+ groups (omnibus) | 3+ groups, same animals |
|---|---|---|---|---|
| `welch` (default) | Welch's t-test (unequal variances) | paired t-test | Alexander-Govern test | Friedman test |
| `student` | Student's t-test | paired t-test | one-way ANOVA | Friedman test |
| `mannwhitney` | Mann-Whitney U (two-sided) | Wilcoxon signed-rank | Kruskal-Wallis | Friedman test |

When groups are paired is decided per pair (see [Paired and independent tests](10-comparisons.md#paired-and-independent-tests)).
A paired test uses only the animals present in both groups.

The **omnibus** test runs only for comparisons with three or more groups and *All pairs*. It uses the Friedman test
when every group contains exactly the same animals (repeated measures, unit = animal).

A test is left empty (`–`) when a group has fewer than two values, or when paired differences are all zero.

## Effect sizes

Effect sizes are signed: **positive means the second group of the pair is higher**.

- **Independent groups: Hedges' g.** The mean difference (second − first) divided by the pooled standard deviation,
  with the small-sample correction *J* = 1 − 3 / (4(n₁ + n₂) − 9).
- **Paired groups: d_z.** The mean of the paired differences divided by their standard deviation.

As a rough guide, |g| ≈ 0.2 is small, 0.5 medium and 0.8 large. With few animals, effect sizes are imprecise; read
them together with n.

## Multiple comparisons

Every feature is tested in every pair, so many tests are run. Two adjusted p-values are reported:

| Column | Corrected across | Used for |
|---|---|---|
| `p_adj` | all features of one pair (bout features and session measures separately) | the stars in the app and figures |
| `p_adj_view` | every pair × feature of the comparison | a stricter view over the whole comparison |

`analysis.correction` is `fdr_bh` (Benjamini-Hochberg false discovery rate, the default) or `bonferroni`. The
omnibus tests are corrected across features. The unadjusted `pvalue` is always reported too.

Stars: `*` < 0.05, `**` < 0.01, `***` < 0.001, `****` < 0.0001, from `p_adj`; `ns` otherwise.

## The statistics tables

Each comparison's `stats_bouts.csv` and `stats_sessions.csv` (and the Excel workbook) have one row per pair × feature:

| Column | Meaning |
|---|---|
| `view`, `pair`, `a`, `b` | comparison, pair label, first and second group |
| `feature`, `unit` | measure tested; animal or bout |
| `n_a`, `n_b` | values tested in each group |
| `mean_a`, `mean_b`, `sd_a`, `sd_b`, `median_a`, `median_b` | group summaries of the tested values |
| `diff` | `mean_b − mean_a` |
| `effect_size`, `effect_measure` | Hedges g or d_z |
| `paired`, `test`, `statistic`, `pvalue` | the test and its result |
| `p_adj`, `p_adj_view`, `correction`, `stars` | adjusted p-values (see above) |

`omnibus_bouts.csv` / `omnibus_sessions.csv` have one row per feature with the omnibus test, its statistic, p and
adjusted p. `groups.csv` lists what each group matched: animals (with IDs), sessions, videos and bouts.

## Exploratory analyses

The exploratory analyses (PCA/UMAP, local clustering, random forest; see [Results](09-results.md#exploratory)) are
descriptive and work on bouts. Their p-values are bout-level and are not corrected; use them to generate hypotheses,
not to confirm them.

## Reproducibility

Everything random is seeded from `analysis.seed`: the random forest, oversampling, subsampling for density plots,
UMAP and the embedding. Running the same analysis on the same inputs gives the same numbers. Each run records its
settings, and comparisons record their definition, in the run folder.

## Good practice

- **Decide the comparisons before looking.** Define them (and their pairs) up front, and keep *Choose pairs* small:
  every extra pair or feature makes the correction stricter.
- **Look at the per-animal box plots** (Exploratory tab). A significant effect should be visible in most animals, not
  driven by one.
- **Check tracking quality per group.** If one group's videos are tracked worse (`valid_fraction` in the Videos tab),
  the amount of observed behaviour differs too.
- **Report n as animals.** With `unit: animal`, the n in the tables is the number of animals.
