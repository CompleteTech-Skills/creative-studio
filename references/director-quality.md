# Director QA and local dashboard

`director_qa.QAService` executes checks against exact `ArtifactRef` bytes in one
explicit artifact root. Its profile declares each check, evidence, resource
limits, and independent milestone scopes. The generated
`profiles/director-qa-profile.schema.json` describes structure; runtime checks
reference identity, IDs, resource admission, operation scope and frame ordering.

Numerical checks and decoded images do not grant artistic approval. Reports
retain failures, unknown measurements and cancellation separately. Native
reopen and archive restore require installed owner adapters; absent adapters
return blocked capability. A structural contract or artifact hash does not
complete a render, restore, or visual milestone.

`director_dashboard.DashboardService` extends the existing authenticated
`EditorServer`. Its trusted host supplies the actual `EditorRepository`, exact
logical DirectorProject and profile references, and configured canonical service
drivers. HTTP clients cannot install callbacks, change artifact roots, or choose
Python modules. Native-scene adapters explicitly declare that context domain;
logical Director contexts use the exact selected Director document ID and SHA.
Native inventory revision and document SHA remain distinct.

The extension uses `runGuardedAction` for requests, waits for existing editor
saves, and refuses actions if the guard is unavailable. Preview opens actual
verified bytes but does not record a review. An identified reviewer must
explicitly attest inspection and select the precise scope and verdict. The
existing production workflow journal retains review and validation history;
appending review metadata does not change native content signatures.

The CLI provides status, graph, assets, storyboard, capabilities, QA, preview,
review, owner action and serve commands. Run `python scripts/director_dashboard.py
--help` for the required explicit repository/reference inputs. Missing owner
services are reported as unavailable. The current adapter does not claim that a
native-only revision updates the logical DirectorProject selection.
