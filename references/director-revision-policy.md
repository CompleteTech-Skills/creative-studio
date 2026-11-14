# Logical Director revision policy

`director_revision_policy.LogicalRevisionPolicy` prepares and publishes an
**unselected immutable candidate** from an exact DirectorProject v1 and exact
native revision patch. It never writes an EditorRepository, changes ONE HEAD,
activates a pair, executes regeneration, approves protected native edits, or
removes immutable bytes. `rollback` abandons an unselected candidate and retains
its artifact; it does not undo a native transaction.

The application must install two trusted owners. `binding_resolver(project_ref,
patch_ref)` supplies this exact mapping, resolved from existing registrations:

```python
{
    'logical_project': project_ref,
    'patch': patch_ref,
    'replacements': [{'field': 'scenes', 'before': old_ref, 'after': new_ref}],
    'invalidated_reviews': [affected_review_ref],
    'invalidated_exports': [affected_export_ref],
}
```

Only `scenes`, `timelines`, and `render_configuration` can select replacement
references. The old reference must actually occur once in the specified field;
new bytes retain native artifact identity and advance its semantic version.
Duplicate, missing, stale or conflicting bindings fail. There is no native to
logical name inference. Review and export invalidation must name actual exact
references; the trusted owner is responsible for their scope. Remaining reviews
refer only to their original inputs. They do not approve the changed inputs.

The original request, brief, story, shot plan, character identities and asset
registry remain exact. Brief project identity and locked requirements are
checked. Camera, camera pose and world changes invalidate scene, rendering,
video, native editability, visual QA and delivery milestone evidence. Material
changes also invalidate assets. The helper never promotes a milestone or grants
review authority. Unaffected milestones and references remain unchanged.
Duration, ending and story edits fail with a dependency diagnostic until the
logical story/shot owner can perform exact semantic updates; no metadata-only
duration success is reported.

Every referenced file is read using the canonical hash-verifying artifact
reader, including resource dependency refs and historical Director documents.
Director documents use the canonical validator. Changed native payload format
and resource completeness still belong to the registered native snapshot owner;
a hash-verified manifest alone does not prove those properties. This helper
does not register a native snapshot or supply a new asset store.

`prepare(project_ref, patch_ref, expected_project_sha256=..., next_version=...)`
returns a candidate without writing bytes. It retains the predecessor logical
project and patch as dependencies, so historical reviews, resources and prior
versions remain reachable without a hash cycle. Changed refs are added to
inventory; old inventory is retained as history. `publish(prepared)` re-prepares
and compares the complete candidate before calling the installed canonical
publisher. The publisher signature matches the native revision adapter:

```python
publish_artifact(payload, artifact_id=..., kind=..., schema_version=...,
                 semantic_version=..., dependencies=..., fixture=...)
```

Its result must be a canonical ArtifactRef with exactly matching readback bytes,
identity, fixture status and predecessor/patch dependencies. Publication failure
cannot mutate native state because this module has no native mutation owner.
Publication can leave an unselected artifact; cleanup and activation remain
existing-owner operations. The expected hash pins the supplied logical input,
and cannot establish that it is currently selected without Root's selection
reader and preactivation concurrency check.

Production integration remains blocked on canonical full native snapshot
registration, the Root preactivation/ONE HEAD hook, and the final trusted
DocumentPublisher wiring. The storage owner's candidate is not implicitly
integrated or approved here. DirectorProject v2 requires its owner's explicit
integration. Unit tests write actual fixture bytes and exercise publication
readback, preservation and rejection behavior; they establish neither production
native publication nor full issue 36 acceptance.
