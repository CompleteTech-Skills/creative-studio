# Optional rig-test Pillow update

This is the bounded dependency update requested in [issue #58](https://github.com/CompleteTech-Skills/creative-studio/issues/58#issuecomment-6387072005).
`requirements-rig-test.txt` now selects Pillow 13.0.0, matching the existing
delivery environment. No runtime code, format guard, workflow, historical
receipt, public dependency minimum or active feature branch is changed.
The broader rig lifecycle and demonstration requirements in #58 remain open.

## Security evidence

The [upstream MIC advisory](https://github.com/python-pillow/Pillow/security/advisories/GHSA-22hg-r758-rrw9)
lists Pillow 7.2.0 through 12.3.0 as affected and 13.0.0 as patched.
An independent reviewer constructed ordinary TIFF and MIC fixtures in a
separate environment with the optional `olefile` plugin. The three-IFD MIC
case timed out after three seconds with 12.3.0 and returned one MIC frame in
0.070 seconds with 13.0.0. Plain three-IFD TIFF and one-IFD MIC controls passed
with both versions. The target rig-test environment does not contain `olefile`.

Current PyPI version metadata returned 37 advisory matches for 12.3.0, each
listing 13.0.0 as fixed. All 19 distributions in the resolved Linux rig-test
environment returned successful PyPI responses with no matching advisories.
An `npm audit --package-lock-only --ignore-scripts --json` of the unchanged
optional rig lock returned zero findings. These are dated database results,
not proof of universal vulnerability absence. Independent OSV queries were
denied by the network proxy (HTTP 403) and were not retried or routed around.

The native import, Model3D, asset and production image boundaries inspected by
the reviewer reject MIC before accessing `n_frames`. That limits this specific
route; a format check after generic `Image.open` does not establish safety
against every parser-level advisory. This update does not claim broader image
boundary hardening or change the separate codec work in PR #211.

## Reproduction and acceptance

Validation used Linux x86_64, Python 3.12.14 and the exact resolved environment
in [the machine-readable receipt](../tests/results/pillow13-validation-20261025.json).
The existing workflow uses Python 3.12, compatible with Pillow 13.0.0's
Python >=3.11 requirement. Transitive versions are recorded observations,
not a new cross-platform lock.

Install `requirements-rig-test.txt` in an isolated environment. Run the existing
schema/template checkers and named release gates:

```sh
python scripts/generate_schemas.py --check
python scripts/generate_rig_templates.py --check
python scripts/test_rig_gate.py schema-math
python -m unittest discover -s tests -p 'test_raster*.py' -v
python scripts/test_rig_gate.py skinning
python scripts/test_rig_gate.py animation-ik
python scripts/test_rig_gate.py operations-archive
HATCH_RIG_INTEROP_TEST=1 HATCH_RIG_BROWSER_TEST=1 python scripts/test_rig_gate.py interoperability
HATCH_DECOMPOSITION_BROWSER_TEST=1 python scripts/test_rig_gate.py compatibility
```

Khronos verification requires the unchanged `tools/rig` npm lock installed
with `npm ci --ignore-scripts --prefix tools/rig`; browser verification requires
the Chromium build selected by the pinned Playwright release.

The exact commands, counts, skips, log hashes, independent review evidence and
source-preservation result are retained in the receipt and coordinator archive.
No existing test or skip condition was changed for this update.

| Local check | Observed result |
|---|---|
| Schema and template generators | Both checks passed |
| Schema/math and dependency isolation | 31 passed |
| Raster decoder/extraction tests | 34 passed |
| Skinning | 7 passed |
| Animation/IK | 27 passed |
| Operations/archive | 26 passed |
| Khronos and actual Chromium interoperability | 15 passed, 3 native Blender tests skipped |
| Complete existing 2D suite in the optional rig-test environment | 831 run: 825 passed, 4 failed, 1 error, 1 skipped |
| Complete existing 2D suite in the configured delivery environment | 831 run: 830 passed, 1 skipped, zero failures/errors |

The full-suite failures are preserved: the decomposition review selected the
wrong component; the review UI download timed out; three production-preview
assertions observed empty status, play-button or origin text. These browser
failures require separate diagnosis; the full suite is not reported as passing.
All five cases reproduced with identical resolved packages except Pillow
12.3.0, then reproduced again with 13.0.0. Both control runs used Playwright
1.55.0 / Chromium 140.0.7339.16 and failed with four assertions and one timeout.
These failures therefore also exist without this pin update. The configured
CI compatibility job installs the separate `requirements-delivery.txt`
environment, including Playwright 1.64.0; the environments are not interchangeable.
All five cases pass with that delivery environment's Chromium 156.0.8078.4,
and its complete 831-case run passed in 272.054 seconds with one explicit skip.
The failed older-browser run and both controls remain in the evidence record.

The local attempt to obtain CI's checksum-pinned Blender 4.5.3 returned HTTP
403. The installed Blender 4.3.2 was then exercised and correctly rejected by
the existing 4.5-only worker; three native tests errored for that reason. This
failed run is retained separately from the successful browser/Khronos run,
which skips the three unavailable native tests. No native acceptance is inferred
from either run. Exact published-head hosted checks are a separate gate.

## Scope and rollback

This evidence supports the optional test pin and the executed regressions.
It does not close #58, establish production visual approval, audit OS/browser
binaries, or prove all supported platforms. Public `requirements-test.txt`
continues to permit older Pillow releases; raising that runtime support floor
requires its own compatibility decision. A normal revert restores the prior
pin; no source asset or historical evidence needs rewriting.
