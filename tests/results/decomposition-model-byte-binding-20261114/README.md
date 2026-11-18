# Captured OpenCV model bytes

The baseline on merged main `d6d06eb9` records a genuine model-identity defect:
the fixed face-model request/response claimed SHA-256 `0f7d4527…`, while a real
OpenCV filename constructor loaded owned replacement bytes `7b0c967d…` and a
20×20 classifier. Original installed models and the NASA source were unchanged.

The fixed execution at `cb66cb6d` captures each bounded XML payload once, hashes
those bytes and passes the same bytes to native `FileStorage` memory parsing and
`CascadeClassifier.read`. The real constructor was again instrumented to replace
and restore the owned model-copy filename. Both actual memory-parser input hashes
match the request/response model hashes; the loaded face classifier is 24×24.
The constructor receives no filename. Real detection produced unreviewed
proposals; no detector output or parser result was mocked.

`validation.json` binds source/test/artifact hashes and the 37 core plus six actual
OpenCV test results. Historical merged receipts remain unchanged. Baseline and
fixed runs request different proposal subsets, so proposal counts establish no
semantic quality improvement.

Reproduce the native replacement, model-hash/load, changed-model and malformed-XML
regressions with optional analysis dependencies installed:

```bash
HATCH_ANALYSIS_BACKEND_TEST=1 python -m unittest discover -s tests/optional \
  -p test_decomposition_analysis_backend.py -v
```

The backend remains limited to coarse photographic human face/eye regions.
General anatomy, pixel segmentation and semantic approval are outside this fix.
