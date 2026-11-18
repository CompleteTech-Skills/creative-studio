# Timing implementation evidence

Local stack base: #16860acf5cc23567ea0cb3e29d48092d62d153a00d1; #167 publicPR208 pending. No merge/publication claim.

Initial native lifecycle run:46tests PASS36.191seconds, zero skips. Initial timing numerical run:4tests PASS14.044seconds, including six genuine PyBullet simulations at integer24,NTSC30000/1001 and fractional47/2fps with480Hz effective solver steps. `initial-numerical.json` contains actual backend identities, cache/input/output hashes, resource measurements and numerical errors. Source was subsequently extended with explicit per-sample bracket receipts and a Blender numerical oracle; these initial records are preliminary, not final exact-commit acceptance.

`contention.log` preserves a failed static suite: the empty-interpreter discovery test collided with #168's exclusive global slot and returned degraded instead of unavailable. No failure was suppressed; the entire46-test native suite passed after explicit ownership transfer.

Pinned host: /home/complete/.cache/hatch-fleet/rig-py312/bin/python -B
Blender: /home/complete/.cache/hatch-fleet/blender-4.5.3-linux-x64/blender
PyBullet interpreter: /home/complete/.cache/hatch-fleet/pybullet311/bin/python

Tests use `PYTHONPATH=scripts HATCH_PHYSICS_BACKEND_TEST=1 HATCH_BLENDER=... HATCH_PYBULLET_PYTHON=...` and unittest discovery for `test_rig_physics_backends.py` / `test_physics_timing.py`. Set `HATCH_TIMING_EVIDENCE=/absolute/new/directory/numerical.json` to retain actual sampled caches alongside numerical evidence; the artifact directory must not already exist.

No human visual or artistic approval is implied by these numerical checks.

Final runtime and tested source: c3e2dac. Final local dependency is actual independent Astra PASS1682e1190c, normally merged with Scene59 integer-parity and mainB. `validation.json` binds exact source-file SHA256 values and these checks:

- 48 actual Blender/PyBullet lifecycle tests: PASS45.149seconds, zero skips.
- Nine timing tests: PASS41.103seconds, zero skips. Native tests execute two actual Blender bakes/native reopens, six paired PyBullet simulations, two paired contact-slop response controls and one expected low-velocity-limit failure. Static tests cover exact clocks, legacy compatibility, solver/output disagreement, unknown-worker downgrade and public Blender solver-injection rejection.
- 22 strict physics contract tests: PASS1.543seconds (unchanged contract/schema).
- 67 Scene3D contract tests: PASS12.009seconds (unchanged Scene3D source).
- Generated physics schema consistency: PASS.

`executed/blender-artifacts/{24,30000}` retain actual native `.blend` caches, diagnostic PNGs, complete hash manifests and numerical states; `executed/blender-numerical.json` records numerical errors/tolerances/settings/backend/resource identities. `executed/artifacts` retains all six PyBullet sampled caches, every actual solver-step state, exact timing brackets and actual-step contacts. `executed/numerical.json` records paired reproduction classification, numerical errors/tolerances, backend/settings/seed/hardware/resource records and hashes. `executed/contact-*` and `executed/stability.json` retain the physical contact controls and located numerical-instability failure. All remain engineering fixtures and unreviewed visually. Source files and retained #168 caches were preserved.

Independent Astra review caught solver/output cross-record disagreement, arbitrary-worker legacy downgrade and cross-backend solver-cache injection. These are now rejected with regressions. The first new failure-location test exposed native body-index shadowing of the solver index; `diagnostic-index-failure.log` preserves that failed check. c3e2dac fixes the shadowing and all nine tests pass without skips or suppression. Legacy handling admits only explicit known #168 worker digests with their actual integral timing profile. Exact seek results include native/output source, bracket indices/times and alpha; native interval counts and contact timestamps record actual solver execution.
