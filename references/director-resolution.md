# Director exact asset resolution

`director_resolver.AssetResolver(library, project_root).resolve(request)` accepts
the existing strict `AssetResolver.resolve` Director request and returns its
schema-validated result. It delegates byte storage and closure verification to
`AssetLibrary`; geometry and rig inspection remain with `rig_import`. No new
asset store, latest-version selector, acquisition claim or generation call is
created by resolution.

The request's registry snapshot is an exact, hash-bound JSON file:

```json
{"schema_version":1,"kind":"director-registry-snapshot","candidates":[]}
```

Each candidate is an existing Director ArtifactRef naming an actual sealed
asset-library manifest. The snapshot admits at most 64 unique candidates.
The request's `specs` list similarly names exact Director AssetSpec documents.
All these files live beneath the supplied project root and use the existing
no-follow descriptor-relative reader. A candidate manifest's actual identity,
version and schema must match its reference; its exact library version must
also resolve with every registered byte intact.

`reuse_policy` is either `require-reviewed` or `engineering-fixtures`.
`license_policy` is a comma-separated list of exact license identifiers. The
specification's `licensing` field must match an admitted identifier. Policies
are not inferred from filenames or provider claims. The latter reuse policy
explicitly permits technical reuse of labeled fixtures. It confers no artistic
approval. There is currently no native asset-library visual-review adapter, so
production requests remain `unreviewed` even when structural checks succeed.

Candidates rank by disposition, then exact identity/type/license and inspected
capabilities, then the number of required adaptations and a stable reference hash. Identity substitutions are rejected.
Unit-scale or output-format differences are `adaptable` only after real source
geometry is decoded; no conversion is silently executed. A missing registry
candidate is `missing`; wrong identity, unsupported capabilities or incompatible
licensing is `incompatible`; absent/corrupt bytes or undecodable geometry is
`unavailable`. Reasons distinguish these outcomes. This adapter currently
inspects self-contained GLB geometry and a separately registered native rig
GLB. Other formats remain explicit unavailable/adaptation requirements.

Declared capabilities never prove execution. The adapter reads actual mesh
buffers and, when a rig is required, actual skeleton and skin records. The
specification's triangle and memory budgets are passed to the native importer.
Named material requirements must exist in the actual decoded material slots;
texture-identity matching has no implemented adapter and remains incompatible.
Source identity, fixture classification, exact hashes and versions are retained.
Rig exports must bind the registered source hash and identity and contain matching
decoded geometry. Requested visual characteristics remain unreviewed without
authoritative visual evidence, including when scale or format adaptation is needed.

Each result contains one requirement disposition per input specification,
ranked exact candidate references, required conversions and conservative budget
fields. CPU time is measured locally; provider calls and GPU execution are zero.
Unmeasured usage remains null with an explanation. Resolution itself performs
no persistent writes and does not certify a scene, render, production milestone
or human review.

Run `PYTHONPATH=scripts:tests python -m unittest test_rig_director_resolver -v`
with the optional rig test dependencies. Tests reuse actual Aster/Moss/laboratory
and access-key outputs, inspect no-generation/no-mutation behavior, then inject
missing/changed manifests, corrupt library bytes, wrong identities, disallowed
licenses, unsupported capabilities and duplicate snapshots. The separate
[acquisition adapter](director-acquisition.md) preserves existing native claims;
3D provider execution remains unavailable until its owner supplies that backend.
