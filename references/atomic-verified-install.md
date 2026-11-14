# Verified byte installation

`atomic_install` is a standard-library-only consumer of canonical filesystem
publication authorities. It copies certified bytes into a fresh private payload,
checks a closed inventory, then commits exclusively. It does not parse archives,
validate schemas, approve artwork, register assets or certify semantics. Consumers
retain those responsibilities and must finish all writers before entry.

Supply immutable `Limits(max_files, max_file_bytes, max_total_bytes, max_depth)`.
File count is positive; byte limits are nonnegative (zero-byte files are supported);
depth is 1–64. Booleans are rejected as integer limits or sizes. Inventory is
captured before filesystem I/O and entries require exactly
`{'path': relative_name, 'sha256': lowercase_hex_digest, 'bytes': integer_size}`.
Paths follow `portable_paths.existing_relative`. Duplicate names, case aliases,
file/directory collisions and paths beyond the depth limit fail before staging.
Captured file entries are immutable tuples of plain strings and integers.

```
install_directory(source_authority, destination_parent_authority,
                  destination_name, inventory, *, limits, directories=())
install_file(source_authority, source_name, destination_parent_authority,
             destination_name, *, sha256, bytes, limits)
```

Both functions return `None` after publication and propagate errors on failure.
The directory installer includes parents of listed files/directories implicitly;
list other empty directories explicitly. At most `max_files` explicit directories
are accepted in addition to `max_files` files. Source and copied payload must
contain exactly the expected regular files and physical directories. An extra
empty directory is an error. Links, reparse points, special files, missing entries,
changed source fingerprints, wrong sizes and wrong hashes fail closed. Every scan
consumes entries incrementally, bounds count/depth/bytes and closes scanner
resources on failure. Reads use at most 128 KiB per chunk.

Directory installation rejects a destination parent equal to or below the closed
source tree before staging. POSIX ancestry admission uses descriptor identities
and at most 1024 parent steps; it never resolves a mutable namespace alias. File
installation allows sibling publication in the same directory authority, while
preserving original source bytes/identity and unrelated originals. The new target
and managed empty wrapper are intentional additions.

The file installer publishes a real regular file. A ZIP remains a file named
`result.zip`; this helper neither interprets its entries nor creates a directory
masquerading as ZIP. Source/destination file names are single portable components.
Bytes are checked before commit, with no posthoc reopening of the destination
substituted for prepublication verification.

On POSIX, authorities must be live directory descriptors (`int`, excluding bool).
Acquire ordinary parents through `atomic_publish.pinned_directory_fd` and retain
descriptor authority throughout the call. The installer owns duplicates and uses
no-follow/nonblocking files, fresh `open('.')` directory streams and canonical
`private_stage_fd`/`rename_no_replace_at`. It never accepts a string or Path as an
FD authority. Contents are fsynced and directory entries synchronized before
commit. This is not a power-loss durability guarantee for the entire transaction.

On Windows, authorities must be active registered canonical `atomic_windows`
directory pins, admitted by `require_directory_pin`. Pin paths with the canonical
`pinned_directory`, retaining ancestor handles. The installer uses shared
`entries`, `pin_child` and `pin_file` readers and canonical private stages. It
closes descendant pins before publication. Directory publication moves the exact
held payload; file publication delegates final verification and exclusive movement
of the exact file handle to `PrivateStage.publish_file`. Ordinary path writes
occur inside the pinned private payload and finish before publication.

Existing destinations are never replaced. Failure cleans owned private contents;
canonical helpers retain empty outer wrappers to protect foreign basename
replacements. Source bytes are never written. This is not a sandbox for hostile
same-user mutation: callers retain lifecycle ownership and stop all workers.
No user callback runs between final validation and publication.

The dedicated `atomic-install.yml` workflow runs actual Linux/macOS descriptor
installation and Windows handle installation, with portable admission separately.
Local Linux tests do not establish Windows execution. Changes require a different
independent reviewer than the installer author.
