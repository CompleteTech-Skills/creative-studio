# Canonical immutable document publication

This adapter depends on canonical AssetLibrary storage from PR404 (`f79f0310`),
including `register(..., expected_identity_refs=...)`, `resolve`, `read_file`, and
`list_references`. It must not be applied alone to the older SQLite-only library
in frozen PR376/PR406 trees. The candidate includes approved shared directory
capability authority `9f70ed85` and preserves both schema/test registrations.

```python
from asset_library_documents import DocumentPublisher

publisher = DocumentPublisher(library, artifact_root=common_artifact_root)
reference = publisher.publish_document(
    payload=payload_bytes,
    artifact_id='project:logical',
    kind='director-project',
    schema_version=1,
    semantic_version='1.0.0',
    dependencies=[],
    fixture=False,
)
assert publisher.read_ref(reference) == payload_bytes
```

`AssetLibrary.publish_document(...)` is the same convenience API with the common
artifact root equal to `library.root`. The configured common root may also be an
ancestor. Its portable relative path to the library is included in ArtifactRefs.
A relocated tree must preserve that relative layout; references are not silently
rewritten. `schema_version` is a JSON integer in 1..999 and `fixture` is boolean.
The exact closed ArtifactRef schema remains owned by `director_schema`.

The only backing authority is AssetLibrary. Each publication registers an explicit
`document` AssetIdentity, a mandatory `document.payload`, mandatory
`document-metadata.json` containing the complete ArtifactRef, exact source
provenance, and canonical dependencies. The additive document kind preserves all
existing kinds and schema/type registrations. Director's broader artifact ID
alphabet is encoded deterministically into a bounded portable canonical identity;
the original ID, kind, schema, fixture flag, and dependencies remain exact in the
mandatory metadata source. The fixed creation timestamp is an adapter identity
constant, not a claim about source authorship or acquisition time. Metadata records
license evaluation as unknown; publication supplies no license/native approval.

Returned paths refer directly to the already authoritative immutable
`records/blobs/<sha256>` payload. No derived copy, index, separate publisher,
logical revision journal, or activation selection is introduced. READY canonical
storage is required. Legacy libraries need their owner's explicit
`upgrade_storage()` operation first. Registry and actual no-follow source path
readback must both agree byte for byte. No payload is omitted or truncated:
canonical limits remain 64 MiB per mandatory file, 256 MiB per version, and 1 MiB
per manifest/reference metadata admission. Empty source payloads are rejected by
the existing mandatory file contract.

Identical identity/version **and complete publication metadata** is idempotent.
Different bytes, kind, schema, fixture flag, dependency order/content, or retained
root-relative path conflict. Writers use the existing canonical identity-history
CAS; a racing identical publication can return its existing exact reference. A
racing different version invalidates stale identity history and requires reload.
Every returned ref is verified against its actual canonical bytes. Because Director
requires one exact ArtifactRef per path, different references to identical source
bytes are rejected. The adapter scans the existing canonical publications before
registration and again on readback, covering races across different canonical
identities. A conflict discovered after registration may leave unselected sources;
it never returns a successful conflicting ref. Retrying identical bytes with a
different ID/version/metadata cannot silently alias the original publication.
Malformed canonical document envelopes and identity/source/dependency bindings
fail with typed metadata errors, including registrations made through the lower
level canonical API.

By default dependencies must already be documents published by this adapter in
the same configured library/root. A native owner can be injected explicitly as
`DocumentPublisher(..., native_dependency_resolver=owning_resolver)`. This is a
trusted application capability, never a user-selected callback or path registry.
Its contract is `owning_resolver(ArtifactRef) -> AssetRef`: it must validate the
entire exact native ArtifactRef metadata (ID/version/kind/schema/fixture,
provenance/dependencies/path), reject an incomplete native closure, and return an
already registered canonical version. The adapter independently resolves that
canonical version/closure, requires matching inventoried payload hash/size/media,
and reads its actual common-root path safely. Missing bridges fail with typed
`LIBRARY_DOCUMENT_*` errors; paths alone never authorize a dependency. Native
schema validation, traversal, and snapshot capture stay with their existing owner.

This generic document path does **not** create complete native project snapshots.
Native project JSON with local relative files needs its native snapshot owner's
complete verified closure and stable namespace. Until that separate bridge exists,
real Director36 native integration remains blocked. Unit callback tests establish
only this narrow capability boundary.

Director36 owns logical project/version policy and native format validation.
EditorRepository owns selection, activation, and history gates; Director37 is a
review journal. Publication never calls their activation APIs. Failed later gates
may leave unselected immutable sources, while no selection advances here.

The adapter imports no optional 3D/image packages. `test_asset_library_documents.py`
is automatically enrolled by the existing compatibility gate's `test_*.py`
discovery (without removing any tests). Tests use real canonical registration,
restart/readback, exact dependencies/native capability, concurrent/idempotent/
stale conflicts, malformed/oversized inputs, Windows spellings, symlink/root
replacement rejection, and subprocess optional-import exclusion.
