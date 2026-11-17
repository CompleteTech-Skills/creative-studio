# Native verification of registered normalized GLB

`asset_library_validation.validate_asset(library, reference, limits=None)`
takes an exact `AssetRef` and returns a versioned technical report or raises a
typed `RigError`. The supported input is an imported self-contained version
with a single `payload` record named `normalized.glb` in GLB format. The
verifier supports mesh-bearing props, characters, rigs, animations, materials,
and textures. The registered category must match the capabilities actually
observed during verification.

The AssetLibrary checks the immutable manifest and payload inventory, including
the canonical records where durable storage is enabled. The verifier stages
only the exact normalized GLB in a private temporary directory. It rejects any
buffer or image URI, including data URIs, before invoking readers that could
resolve resources. It checks the same digest through native decode and a fresh
run of the pinned offline Khronos glTF Validator. Results come from those reads
and the native validators. Stored importer reports, diagnostics, and original
source paths are unnecessary for inspection. Their blobs still belong to the
AssetLibrary's integrity check.

The report identifies the normalized payload by logical path, byte length and
SHA-256. It includes selected-scene parentage and world transforms, measured
base-mesh geometry, authored material count, texture dimensions and digests,
and validated skeleton, skin and clip summaries where present. Animation
summaries include key counts, exact rational endpoints and a SHA-256 of each
complete decoded key-time sequence in the native canonical JSON encoding.
Skeleton hierarchy and rest measurements are represented as digests. The
report omits embedded names and arbitrary validator messages. Errors retain
typed codes, retryability and applicable integer counts.

Callers can tighten native resource limits. Byte and memory admission precedes
payload retrieval, and native preflight budgets geometry, hierarchy and embedded
texture work before validation. Memory admission is a conservative working-set
estimate. Node, clip and key limits bound the report as well.

Verification leaves the library read-only and reports visual review as
`unreviewed` and production approval as false. Prefabs, environments, linked
assets, original/alternate payload formats, Blender, skeleton-free animation,
and renderer execution remain outside this slice. The full #140 acceptance
and its #139 prerequisite remain open.
