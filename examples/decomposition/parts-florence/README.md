# Recorded Florence experiment (not accepted semantic detection)

Six actual local CPU model calls completed in 44.799 seconds, with peak RSS
1,745,364 KiB. The pinned model, runtime, source hashes and raw outputs are
retained in `experiment.json` and the two source-named directories. No production
Florence adapter or analysis-controller execution is claimed.

The builtin parser returned empty polygon arrays for all four segmentation
responses because its regex expects contiguous location tokens and the recorded
outputs contain whitespace between them. The retained scratch diagnostic script
extracts only those recorded coordinate pairs, using the builtin 1,000-bin center
conversion. It performs no inference. Polygon closure is a visualization aid;
it does not validate the model's label or completeness. Original model responses
remain unchanged.

The contact sheet shows each unchanged source on the left and the diagnostic
mask overlay on the right. Author inspection found one-eye-only masks for both
plural eyes queries, a tongue rather than a complete dog mouth, and a scarf
response at the generation token limit. A fresh independent semantic review is
still pending. These results do not satisfy issue #226. No semantic approval,
rig readiness, production capability or complete anatomy claim is granted.

The diagnostic script retains exact local scratch input paths; it is a record
of this run, not a portable model execution command. Sources retain the hashes
recorded in each `source.json`; original source bytes were not edited.
