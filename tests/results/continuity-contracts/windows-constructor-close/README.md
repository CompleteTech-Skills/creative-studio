# Windows constructor rejection leaked a database handle

PR #209 at 2619444 failed real Windows public-delivery CI run 40063635594,
job 120782192724. The original unmodified report and test log are retained here:
837 tests, zero failures, one error and two declared skips. Their actual tested
merge revision is de8a7d0, and both affected file hashes match published 2619444.
The error was WinError 32 when the unchanged teardown tried removing identity.sqlite.

The legitimate fixture connection and initial registry were explicitly closed.
The second IdentityRegistry constructor opened its own native connection, passed
the application-ID guard, then rejected the unsupported meta version outside
that guard. Its transaction rolled back but its connection stayed open. Linux
allows unlinking an open file, so previous local runs did not expose this owner.
SQLite's own transaction context also does not close a connection.

Four new actual-native-connection tests retain the failed handles and exception,
so garbage collection cannot hide the problem. Against the published runtime,
three rejection cases fail the closed-handle assertion and the success control
passes. The cases include the real unknown-version rejection and real SQLite
authorizer denials during PRAGMA/schema initialization. The retained baseline
log has three expected failures, zero errors and zero skips.

The corrected constructor closes every connection it still owns on any
initialization error; successful ownership transfers to the returned registry.
All 35 focused tests pass in 4.850 seconds on Linux/Python 3.12.15. New assertions
verify closed native handles before cleanup, unchanged database/source bytes,
rolled-back schema writes and the legitimate existing connection still usable.
The original Windows teardown assertion remains unchanged; no cleanup error is
suppressed and no test becomes optional. Schema parity passes.

`verification.json` binds native failure bytes, baseline/fixed logs and exact
source hashes. This is local deterministic resource-owner evidence, not a claim
that the corrected Windows CI has run. A fresh exact-head independent Astra
review, explicit publication lease and new native CI remain merge gates.
