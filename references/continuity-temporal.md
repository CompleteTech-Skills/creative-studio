# Exact temporal replay (issue 116)

The temporal engine extends `IdentityRegistry` in the existing SQLite sidecar.
It stores immutable timeline revisions and authored event history. Native
Scene3D projects retain their own scene, node, asset, version and dependency
identities; existing production v1 documents are unchanged. Existing canonical
sidecar instance bindings, when present, must agree with temporal node/entity
mappings. This is a structural, synthetic-tested temporal implementation, not
visual approval or a complete continuity P1 acceptance claim.

## Source and timing contract

`continuity-temporal-timeline` binds a native `scene3d-project` reference, scenes,
explicit canonical scene-to-world transforms, persistent entity/node mappings,
and native shots. References include exact version, path and SHA-256. Each
selected shot must bind the exact selected scene reference, including version,
rooted path and hash; matching a reused scene ID alone is insufficient. The native
closure loader checks all transitive files; the native static hierarchy evaluator
computes actual parent/world transforms in right-handed Y-up meters. It does not
infer geometry from identity metadata or reinterpret v1 pixels.

The first chronological shot anchors initial state at story tick zero. Events
then modify that evaluated state. Every scene must explicitly map the same
persistent entity set with unchanged logical identities. Each additional scene's
incoming native transform/visibility must agree with replay at its earliest
mapped story start; contradictory native state fails with `CONTINUITY_SOURCE_STATE`
instead of being silently discarded. Frames are integer;
`scene3d_contracts.frame_time` supplies exact rational periods. The chosen integer
ticks-per-second must exactly represent each frame period and story-speed mapping
(no rounding). Scene and global presentation intervals are half-open and cannot
overlap. Story offsets independently express flashbacks, flashforwards, parallel
presentation and time skips; nonnegative rational speed also permits freeze
frames. Negative playback is rejected; use an explicit chronological offset.

For N displayed frames, frame N is available only with `boundary=terminal`.
Displayed frames are 0 through N−1. `before` excludes all events at a frame's
story tick; `after` includes them. Supplying `event_id` narrows before/after to
that event's exact `(tick, priority, event_id)` ordering, including earlier events
at the same tick. The event must map exactly to the selected frame.
`temporal_event_snapshot` also queries events between frames or inside time skips:
it returns the exact story tick and null frame/presentation coordinates, since
chronological time may have no unique presentation occurrence.

## Typed events and branches

`continuity-temporal-event` carries immutable identity, integer story tick,
priority, prerequisite event IDs, author, revision, hash-pinned source references
and one or more typed operations. The minimal operations for this phase are
absolute canonical `relocate` transforms, `visibility` booleans, and scalar
`set-fact` transitions. Every operation has a before-value precondition. Null
facts represent the initially unknown value. There are no unrestricted mutation
paths. Costume, ownership, spawning and richer fact scopes remain later tasks.

Append requires the expected head SHA-256. Duplicate IDs, repeated targets in one
event, nonincreasing order, missing/cyclic prerequisites, impossible preconditions
and events outside mapped story time reject the entire transaction. A scene's
static source is never rewritten by an event.

Fork copies the current immutable history into an explicit new timeline/branch.
Merge checks both expected heads, identical world/mapping contracts, common
immutable ancestry and identical bytes for shared event IDs. Disjoint changes
are replayed in canonical order. Competing writes to a state field fail with
`CONTINUITY_CONFLICT`, even when a last-writer choice could be guessed. Resolve
such changes by explicitly authoring a compatible branch; no automatic winner
is selected and both original histories remain available.

## Snapshots, persistence and recovery

`temporal_snapshot` always revalidates native dependencies and replays from the
verified source; query order cannot influence results. Snapshots retain project
revision, world, timeline, branch, scene, shot, frame, exact coordinates, effective
native dependency digest, immutable head hash, evaluator code/runtime-version hash, evaluated
entities, minimal facts and applied event IDs. Unknown animation, physics, pose,
costume, ownership and visual-approval fields are explicitly listed as unavailable.

Cached snapshots are derived rows in the same registry. A cached row is accepted
only when its canonical bytes and hash match a fresh evaluation. Corruption or
changed evaluator dependencies reject stale rows. `discard_temporal_cache`
removes only cache rows, after which replay rebuilds them. Immutable revisions
remain addressable by SHA-256, including after process restart and branch merge;
all reachable parent contents are hash-checked on history reads. SQLite FULL
synchronous transactions, existing size limits and expected-head checks protect
writes. Backup/restore must preserve the existing database and the exact native
project closure together; missing or changed native bytes fail closed.

## Python and CLI

```python
from continuity_registry import IdentityRegistry
with IdentityRegistry("continuity.sqlite", asset_root="project") as registry:
    head = registry.create_timeline(plan)
    head = registry.append_events("main", events, expected_sha256=head)
    terminal = registry.temporal_snapshot("main", "shot-1", 24, boundary="terminal")
    start = registry.temporal_scene_boundary("main", "scene-1", "start")
    event_state = registry.temporal_event_snapshot("main", "event-1", boundary="before")
    history = registry.temporal_history("main", revision=head)
```

All commands use the same API and print JSON; errors return exit status 1.

```sh
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project create plan.json
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project append main events.json --expected HEAD_SHA256
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project snapshot main shot-1 24 --boundary terminal
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project scene-boundary main scene-1 end
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project event-snapshot main event-1 --boundary before
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project fork main alternate alternative --expected HEAD_SHA256
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project merge main alternate --expected TARGET_SHA256 --source-head SOURCE_SHA256
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project history main
python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project discard-cache
```

Generate/check schemas using the existing continuity generator:
`python scripts/continuity_contracts.py --generate-schemas profiles [--check]`.

## Verification boundary

`tests/test_rig_continuity_temporal.py` constructs a real hash-bound native project
with two locations, two character stand-ins, a distinct prop and three shots.
The dedicated continuity CI gate executes all 15 temporal tests with optional
3D dependencies on Linux, Windows and macOS. Its `test_rig_` name keeps native
evaluation outside ordinary 2D discovery, which does not install NumPy.
Tests exercise actual replay, native hierarchy evaluation, out-of-order requests,
terminal and event boundaries, rational rates, intentional chronology changes,
atomic rejection, branches, cache corruption, changed native source bytes,
recovery and CLI execution. The existing contract/identity suite remains required.

Native animation, constraint and physics evaluation is not implemented by this
module. Such declarations fail through the owning static evaluator or explicit
`CONTINUITY_ADAPTER_REQUIRED`; no synthetic pose, dynamic-scene acceptance,
original-production-media recovery, render or aesthetic approval is claimed.
The static evaluator and integer event transitions are the supported executable
capability. Rich costume/ownership/rules, live rendering and capability-admitted
dynamic evaluation remain outside issue 116's implemented structural slice.

When an incoming native scene already contains an intentional relocation, pass
its authored initial events atomically with `registry.create_timeline(plan, events=events)`
or `python scripts/continuity_temporal.py --registry continuity.sqlite --asset-root project create plan.json --events events.json`.
The initial event list receives the same ordering, precondition, reference and replay checks
as appended events. Contradicting native state or invalid initial events reject before
any timeline head is persisted. Omitting events retains empty-history creation.

A retained trace can be reproduced with `python tests/continuity_temporal_probe.py NEW_OUTPUT_DIRECTORY`. It writes the native fixture, event history, exact snapshots and source/runtime hashes. The SQLite file is a disposable probe sidecar, never an approved asset.

Issue 117 adds explicitly selected `1.1.0` rich world-state plans, events and
snapshots through this same temporal owner. See [persistent world state](continuity-worldstate.md)
for costume/accessory, physical presence, location, environment and scoped
knowledge contracts. Original `1.0.0` records retain their closed shapes.
