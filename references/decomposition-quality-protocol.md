# Automatic decomposition evaluation protocol v1

This protocol defines **future evaluation gates**, before any held-out evaluation
under this version. It does not approve an analyzer. The current
[candidate inventory](../examples/decomposition/quality/candidates.json) contains
zero accepted held-out cases. Previously inspected or evaluated pilot sources
remain development examples, including existing NASA, Penn-Fudan, Milo and Noto
runs. Model-training overlap is unknown and must remain disclosed.

## Freeze the evaluation before execution

Register an immutable evaluation record containing this protocol's digest, exact
source and license/provenance descriptors, model revision/weights digest, runtime,
prompts, thresholds, resource/call budgets and reviewer identities. Sources must
be rights-cleared for their actual use. Preserve original bytes through existing
asset/decomposition storage; do not recompress them or replace source manifests.
An explicit crop is a new attributed input with parent source hash and crop
coordinates, never an unchanged original. Reusing prompts or thresholds tuned on
a source removes that source from the held-out partition.

At least 20 previously unobserved images, with at least two independently reviewed
cases per required category, must cover flat/cartoon, textured illustration,
pixel art, humanoid, quadruped, fantasy/wings/tails, robot/abstract, unusual
proportions, clothing/props, disconnected markings, complex background,
multiple characters, partial crops and difficult occlusion. Categories may
overlap. Record the source's consent/license evidence and missing categories.
An incomplete distribution cannot pass as a complete evaluation.

Two reviewers independently annotate visible component instances, exact semantic
labels, hierarchy, mask boundaries, orientation ambiguity, genuinely occluded
areas and uncertain pixels. Resolve disagreements before scoring, retaining both
original records. Unknown regions are excluded from scored reference pixels and
reported separately; excluding an entire failed category is prohibited. Label
synonyms are frozen before inference. A mask for one of two eyes does not count
as detecting both eyes. No hidden anatomy is ground truth from flattened pixels.

## Metrics and gates

These are initial engineering gates for this version, not claims about model
performance. Change them only by a new protocol version before another held-out
run. Report per-category and per-image values alongside aggregate values.

| Dimension | Gate |
| --- | --- |
| Supported visible part identification | Precision at least 0.95 and recall at least 0.85 in each required category; instance counts matter |
| Nonexistent anatomy | Zero confidently asserted nonexistent parts; unsupported/unknown suggestions stay pending and are counted separately |
| Visible mask quality | Median source-space binary IoU at least 0.80 and minimum per scored part 0.60; no source resizing to improve scores |
| Original/source fidelity | Original encoded bytes unchanged; observed selected pixels recompose with maximum channel error 0, exact alpha and full selected coverage |
| Uncertainty | All ambiguous orientation/occlusion/pivot claims remain pending; 0 false automatic approval or rig-ready assertions |
| Probability calibration | Report ten equal-width confidence-bin counts, empirical precision, mean confidence and absolute gap; require ECE at most 0.10 only for providers declaring calibrated probabilities, otherwise mark uncalibrated and do not treat scores as probabilities |
| Operational success | Every source has a recorded success, explicit unsupported result or typed failure; success rate and denominator reported without omitting failures |
| Correction burden | Report edited parts, mask pixels, hierarchy/pivot changes and elapsed correction time separately; corrected results receive no automatic quality credit |

`decomposition_quality.binary_mask_metrics` reports IoU, missing and extra pixels
without resampling. Empty/empty masks are unscored (`null`), never perfect.
`semantic_label_metrics` preserves repeated instances and reports exact labels;
it cannot decide whether a label is actually supported by an image. Neither API
grants semantic approval. Foreground instance IoU, semantic labels, reconstruction
and optional hidden-area reconstruction must remain separate result dimensions.

## Security and integrated execution

Run fixture-only deterministic regressions independently from actual model runs.
Use existing manual decomposition, source validation, strict annotation/mask,
review, package and forward-generation APIs. Do not introduce a second analyzer
controller or relax their schema/atlas/timing requirements.

Before a release gate, execute source-hash changes; corrupt/truncated images;
excess dimensions/encoded bytes; malicious paths, symlinks and ZIP traversal;
malformed/missing/stale provider output; budget overruns; cancellation/crash and
resume; output tampering; and offline/unavailable-backend fallback. Record exact
commands, reviewed SHA, versions, exit status and retained evidence. Originals,
prior review records and source-bound uncertainty must survive every failure.

For every accepted held-out source execute the actual automatic route without
hand-authored proposal JSON: source -> automatic named proposals -> original-space
masks -> transparent layers -> deterministic validation -> explicit local review
and corrections -> package export -> clean reimport -> forward-run preparation.
Record each stage's immutable references. Preparation is not a generated pose or
external model call. Parent #222 and issue #234 remain open until this integrated
gate and every required functional child are independently reviewed and merged.

## Current evidence limitations

The existing CLIPSeg pilot observed 75 queries on five already-known inputs and
reported missed visible parts and false positive nonexistent anatomy. It failed
quality requirements. The caption-first Florence experiment is also development
work, not held-out acceptance. Do not relabel either experiment under this new
protocol or retroactively call these thresholds predeclared for those runs.

The Noto candidate's pinned publisher README describes most image resources as
Apache-2.0, while its root LICENSE at that same revision is OFL-1.1. This inventory
preserves that unresolved discrepancy and does not redistribute new Noto image
files or present them as a rights-cleared held-out corpus. Penn-Fudan research
terms and NASA source attribution also need their exact records retained before
new corpus promotion. Existing project artwork is already observed development
material, with no new blanket redistribution license asserted here.
