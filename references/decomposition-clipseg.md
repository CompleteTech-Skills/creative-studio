# Experimental text-conditioned part proposals

This optional backend uses the actual CLIPSeg learned model with a requested
category vocabulary. It produces source-space ownership suggestions for review.
A high pixel-membership score does not prove that a named part exists or that its
mask is anatomically correct. The model can respond to absent-part prompts;
unknown sides, hierarchy, attachment points and hidden anatomy remain unknown.
This implementation does not establish full issue #226 acceptance.

The model is CIDAS/clipseg-rd64-refined at immutable revision
`999e0328d9e10b484360c477313983f9afdd7050`. Its
[official documentation](https://huggingface.co/docs/transformers/model_doc/clipseg)
describes zero-shot text/image-prompted segmentation. Only the pinned
safetensors checkpoint, tokenizer and configuration bytes are accepted; no custom
remote Python code or automatic download is supported. Model licensing is
Apache-2.0 according to its pinned model card. Dataset/source permissions remain
separate.

Provision the optional dependencies and model snapshot explicitly in a separate
environment, then set `HATCH_CLIPSEG_MODEL_DIR` to the local snapshot directory.
The accepted file digests live in `scripts/decomposition_clipseg.py`. The full
checkpoint digest is
`d00ca85d6b859f9d07b7cfb8ef26fe9771cb275b34c9368f2ecf603139307f55`.
Discovery and execution use the local files only; a missing dependency or changed
snapshot leaves the manual annotation route available. Requests also bind the
exact adapter/controller source hashes, so changed preprocessing or execution
code receives a different job identity instead of silently reusing old calls.

```bash
python -m pip install -r requirements-clipseg.txt
export HATCH_CLIPSEG_MODEL_DIR=/absolute/path/to/verified/snapshot
python scripts/decomposition_analysis.py capabilities
python scripts/hatch.py decompose-analyze --run-dir output/character \
  --analysis-backend clipseg --enable-analysis
```

Use the existing source import and foreground selection first. This backend
cannot replace the foreground owner. Requested categories can be supplied through
repeated `--analysis-taxonomy` values; the final slash-separated category token
becomes the text query. Categories are flexible and bounded to 32 unique queries,
128 characters each, within the model's 77-token limit. No body plan or fixed
anatomical slot count is asserted. Additional free-form prompts are rejected.

The default membership threshold is 0.5; the default maximum pixel-score gate is
0.7. These are explicit filtering parameters, not calibrated semantic confidence
or a guarantee against false positives. Every category's maximum membership
score, proposed area and filter outcome is retained in the response diagnostics.
No filtered result is proof of absence. Model responses remain subject to the
strict source-bound job, cancellation, retry, immutable response and stale-apply
checks of the analysis controller. Its default controller time limit is 120
seconds, with one inference thread; an explicit Python `prepare` call can
request a shorter limit.

The internal model map is 352 pixels square and is projected back into the
original image dimensions before binary ownership and source-alpha intersection.
Original RGB and alpha values are never resampled. Fine details can be missed;
broad part masks may overlap. Small proposed regions receive first source-pixel
ownership when applying annotations, without asserting depth or anatomy. The
canonical decomposition rejects fully hidden proposals; edit uncertain proposals
before applying them rather than treating rejected ownership as a semantic pass.
All successful components and source-faithful recomposition still require
independent semantic review. This is not a rigging or animation-readiness gate.

Current fixture tests establish only strict options, model snapshot safety and
job binding. Real-provider execution, diverse part-quality evaluation, clothing,
props, fantasy/nonhuman cases and absent-part false-positive measurements must be
retained separately before this backend can be called accepted.
