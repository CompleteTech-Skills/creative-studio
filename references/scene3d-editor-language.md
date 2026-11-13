# Language plans in the native editor

The Scene64 owner supplies `scene3d_editor_language` and its deterministic grammar.
`scene3d_editor_language_routes.language_routes()` adapts that owner to the existing
`EditorServer(repository, routes=...)` transport. Merge this fixed route map into
the trusted application's route map. The server retains its existing Host,
Origin, bearer-token and bounded JSON checks.

`POST language/plan` accepts exactly `text`, boolean `environment`, integer
`expected_revision` and `expected_sha256`. It returns a reviewable plan for that
exact saved repository revision. An unresolved description remains a plan with
diagnostics and cannot be applied. `POST language/apply` accepts exactly `plan`
and `transaction_id`; the canonical native transaction performs integrity,
revision/hash and idempotency checks before publication. Neither endpoint accepts
a callable, module, shell command or source filesystem path.

The optional `editor-language.js` extension mounts with
`mountSceneLanguage(context)`. Register it through the existing editor extension
factory, with a dedicated container. It requires the shared editor owner's
`runGuardedAction(callback)` context method. That method must finish or reject
pending local edits and serialize other editor mutations until the callback and
its reload finish. Without this capability the panel disables both actions and
explains the missing integration. It does not implement a second pending-edit
controller.

Users review the exact typed commands before explicitly applying them. Changing
the input, environment mode or saved revision invalidates the reviewed plan.
Errors and diagnostics are plain text. Plan success does not grant visual approval
or imply support for arbitrary prose, provider execution, unavailable assets or
rendering.

Integration status: the adapter and panel require the canonical Scene64 modules,
the shared editor action guard, fixed-route registration and static-asset boot
registration together. A standalone adapter test does not establish complete
browser authoring acceptance. Full browser validation must use the integrated
application and verify a reopened native revision, stale-plan refusal, pending
edit handling and authentication.
