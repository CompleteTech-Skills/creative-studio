# Optional image analysis and resumable proposals

`decompose` remains the offline source import and manual annotation workflow. The
optional analysis controller adds source-bound jobs and a working local detector.
It never grants semantic approval, invents hidden pixels, changes the original
image, or treats a detector rectangle as a source-faithful segmentation.

## Available backends

- `opencv-haar`: fixed OpenCV frontal human face and eye cascades, executed in a
  bounded subprocess without network calls. This is useful for photographic
  human faces. It produces coarse `face/region` and `face/eye-region` proposals;
  it does not find full heads, limbs, clothing, animals, robots or general cartoon
  anatomy. Even supported inputs can yield false positives or no detections.
  The backend records the installed OpenCV version and both cascade SHA-256s.
- `external`: an explicit request and strict response ingestion interface. No
  endpoint, remote model or API key is invented or called. A separate authorized
  analyzer can supply inline polygon/run proposals, its actual identity and
  evidence. Such evidence is an attributed external assertion, not a signature
  or independent proof of a remote provider call. Paid execution is not enabled.

Install optional local inference separately from a normal 2D installation:

```bash
python -m pip install -r requirements-analysis.txt
python scripts/decomposition_analysis.py capabilities
```

OpenCV's [cascade tutorial](https://docs.opencv.org/4.13.0/db/d28/tutorial_cascade_classifier.html)
describes the same trained face/eye detector and its rectangle output. The
cascade XML files retain their Intel Open Source Computer Vision license headers (redistribution permitted with notices) in the installed OpenCV
package. No model or optional dependency is downloaded at runtime. Model
identity changes require a new request. Local detection has no calibrated
probabilities: its conservative `0.5` annotation confidence is explicitly marked
unknown in each part's notes and must not be interpreted as measured accuracy.

## Import, analyze, inspect

Use a new run directory. Original bytes and source coordinates remain unchanged:

```bash
python scripts/hatch.py decompose --source photo.png --run-dir output/person \
  --origin 'Source attribution and permission' --background keep
python scripts/hatch.py decompose-analyze --run-dir output/person \
  --analysis-backend opencv-haar --enable-analysis
```

The second command returns a job SHA-256. Without `--analysis-backend`, the
legacy `decompose-analyze` command still returns the passive manual handoff.
`--enable-analysis` is mandatory when creating an automated job. Local detection
only accepts the two declared categories; unsupported taxonomies and prompts
fail. This controller does not solve complex-background isolation.

```bash
python scripts/hatch.py decompose-analysis-status --run-dir output/person --job JOB_SHA
python scripts/hatch.py decompose-analysis-apply --run-dir output/person --job JOB_SHA
python scripts/hatch.py decompose-review-ui --run-dir output/person \
  --destination output/person-review
python scripts/hatch.py decompose-validate --run-dir output/person
```

Analysis itself only writes `analysis-jobs/JOB_SHA/`. Applying proposals uses the
existing transactional annotation correction path, retaining exact original
pixels, full unassigned foreground and independent pending review. Overlaps are
resolved by the existing ownership rules. The annotation extension binds the
exact response hash, job, model and execution evidence. The manifest distinguishes
locally executed model-assisted proposals from manual or external assertions.
Neither label is a semantic verdict. A source or reviewed manifest changed after
request creation cannot be overwritten by a stale proposal.

## External results

Create an explicit request with `--analysis-backend external --enable-analysis`
and optional repeated `--analysis-taxonomy CATEGORY`. The returned request and
its digest are stored at `analysis-jobs/JOB_SHA/request.json`.
[`decomposition-analysis-request.schema.json`](../profiles/decomposition-analysis-request.schema.json)
and [`decomposition-analysis-response.schema.json`](../profiles/decomposition-analysis-response.schema.json)
are generated from the same strict runtime definitions.

A response includes exact request/source hashes, status, actual provider/model
identity and version, available model-file hashes, annotation confidence and
source-space primitives, provider call ID or a nonempty reason it is unavailable,
actual elapsed/cost fields when known, and errors. Use null for unknown elapsed
or cost, never invented measurements. The request records the prompt and bounded
parameters; fixed local cascades reject text prompts. Inline polygons and runs
avoid external mask paths and resource URIs. Model output cannot replace the
foreground owner or run arbitrary code. Unknown fields, nonfinite numbers,
malformed hierarchy, out-of-canvas coordinates, unsupported categories and unsafe
paths fail before any annotation is committed.

```bash
python scripts/hatch.py decompose-analysis-ingest --run-dir output/person \
  --job JOB_SHA --receipt response.json
```

External ingestion preserves its attributed status. It never marks a provider
call as locally verified and does not authenticate model-supplied metadata.

## Recovery, cancellation and resource limits

The identical request returns the existing job. After a successful call,
`decompose-analysis-run` reuses the validated immutable response. A crash after
worker completion ingests that output; a running job without output becomes
`interrupted` and is never automatically redispatched. This prevents an unknown
remote/local outcome from silently causing duplicated work. Applying a response
is also idempotent after a crash between the annotation transaction and its job
acknowledgement.

To cancel even while the Hatch controller holds its run lock, use the dedicated
marker command from another process:

```bash
python scripts/decomposition_analysis.py cancel --root output/person --job JOB_SHA
```

Cancellation kills a running fixed worker, waits for its exit and prevents
proposal publication. Failed, cancelled or interrupted work requires explicit
`decompose-analysis-retry`; it archives the earlier state, response and log.
Retry means authorizing a new attempt when the earlier outcome is known or
uncertain. It is never implicit in resume. A completed but unacknowledged worker
response must be recovered before retrying. The fixed limits are 64 requests per
run, three attempts per request, four million source pixels, eight MiB per
request/response metadata file, 30 seconds per local attempt by default (Python
API range 1–120), one OpenCV thread and a 64 KiB worker log. Annotation component,
mask-run and working-set limits remain owned by the existing decomposition
validator. The whole machine's memory/CPU isolation is not claimed.

Python callers can use `prepare`, `run`, `ingest`, `apply`, `status` and `retry`
from `decomposition_analysis` while holding `hatch.ownership(root)`, matching the
public CLI. `cancel` only writes an exclusive cancellation marker and is safe to
call without that lock. Existing directory/ZIP export includes exact job records
and restores them through the existing verified package path.

## Executed evidence and limits

[The example](../examples/decomposition/analysis/README.md) uses the public-domain
NASA photograph distributed by scikit-image. It was imported without annotations
and actual OpenCV inference proposed one face region and two eye regions. The
source was new to this repository workflow; no claim is made that it was excluded
from upstream model training. Coarse masks retain most pixels as unassigned;
this is not a complete character dissection or a measured semantic-quality study.
The broader automatic decomposition work (#225, #226, #231–#234) remains open.

Core tests use explicitly labelled mechanical fixtures for schema admission,
invalid output, quotas, external failure retry, at-most-once recovery, stale
review rejection, real subprocess timeout/cancellation, and source fidelity.
Optional backend tests execute the real cascades and check a blank image fails
with an explicit unsupported result. Run them separately from an ordinary 2D
installation:

```bash
python -m unittest discover -s tests -p 'test_decomposition_analysis.py' -v
HATCH_ANALYSIS_BACKEND_TEST=1 python -m unittest discover -s tests \
  -p 'test_decomposition_analysis_backend.py' -v
```
