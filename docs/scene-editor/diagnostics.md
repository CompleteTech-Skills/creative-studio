# Current native preview limitations

A saved scene can contain a valid native object whose interactive preview is
unavailable. Such a limitation belongs to the current saved snapshot, rather than
a failed edit. A successful unrelated save must not hide it.

The existing diagnostic region now keeps two kinds of feedback together: the
current operation rejection and the current native preview warning set. Clearing
a transient rejection restores any current warnings. Loading a saved revision
recomputes warnings from that revision's native asset/light availability; deletion
or resolution removes a warning, and undo can restore it. Warnings carry a stable
code, severity, affected native name/ID and the current snapshot revision/hash.
The loader's existing epoch rejects warnings from an older asynchronous load.

Native warning producers are explicit installer unavailability branches. Arbitrary
transient errors are not promoted into persistent warnings. No geometry, material,
light substitute, prefab expansion or persistence controller is introduced.
Area lights remain unavailable in this preview where the existing adapter cannot
represent their dimensions. Saved native configurations remain unchanged.

Current warning storage is capped at 64 deduplicated code/object records, with a
visible truncation message. Warning names are displayed at up to 128 characters
and messages at up to 2048; the canonical saved document is preserved. The existing
historical `errors` array retains its prior append-only behavior. This increment
does not certify that legacy history as a bounded long-running log.

Output uses text nodes, identifies warnings in text rather than color alone, and
announces warnings politely while retaining assertive operation errors. The
region is keyboard focusable and scrollable. Error/warning display never steals
focus from a pending edit.

The actual browser regressions in `test_rig_editor3d_accessibility.py` cover a
native area-light fixture followed by numeric save, reload, genuine GPU context
loss/restoration, fresh authenticated document reopen, deletion and undo/redo.
They also exercise client rejection followed by an accepted native edit,
source-revision/hash binding, literal markup names, deduplication and a 70-light
boundary that displays 64 records plus a truncation notice. The tests use real
native documents and the existing revision-checked service; they do not claim the
fixture was created through an unfinished Add Light interface.

This is bounded #106 / #17 §30 and §33 acceptance. Full project recovery, responsive
workspaces, native materials/rigs, all browser environments, manual assistive
technology testing, profiling, archives and production rendering remain with the
existing owners and integrated acceptance program. Later material-omission warning
producers must reuse this same warning authority during integration; this branch
does not duplicate that owner's adapter.
