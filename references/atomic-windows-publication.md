# Windows handle-pinned publication

The additive `atomic_publish.windows_private_stage_supported()` probe and
`windows_private_stage(parent_path, prefix='.publish-', cleanup=True,
cleanup_on_error=True)` context provide an ordinary path for existing writers,
SQLite and verifiers. Inside the context, write through `stage.path`, finish all
workers and verification, then call `stage.publish_to(destination)`. Publication
is exclusive and moves the exact held payload; it never reopens a mutable source
basename. Calls after context exit or successful publication are rejected.

Every physical ancestor is held with delete sharing denied before caller I/O.
Reparse ancestors, traversal, device namespaces, drive-relative paths, alternate
streams and UNC paths fail closed. New wrapper ACLs are protected and inherited
by children, allowing the owner and LocalSystem. The payload handle retains
DELETE access while denying delete sharing to other opens. Publication uses
`SetFileInformationByHandle(FileRenameInfo)` with `ReplaceIfExists=False` and a
pinned destination directory handle. Cleanup pins descendants, does not follow
junctions, deletes exact held objects, and leaves the empty outer wrapper.
`cleanup=False` retains contents; `cleanup_on_error=False` retains diagnostics.
Writers must finish before publication and cleanup. Same-user hostile process
isolation is outside this ownership boundary.

This API is implemented in the standard-library-only `atomic_windows` platform
module. Existing POSIX staging defaults are unchanged. Unsupported hosts report
`ENOTSUP` before inspecting or creating caller paths. The dedicated Windows CI
gate exercises actual handles, attempted namespace substitution, immutable
destinations, SQLite, ACLs, junctions, cleanup, Unicode names and malformed paths.
Linux admission tests establish no Windows execution. Consumer integration still
requires actual Windows evidence and fresh independent whole-candidate review.

The native contract follows Microsoft's documentation for
[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info),
[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle),
[FILE_DISPOSITION_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_disposition_info),
[FILE_ATTRIBUTE_TAG_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_attribute_tag_info),
[security descriptor conversion](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertstringsecuritydescriptortosecuritydescriptorw)
and [SID strings](https://learn.microsoft.com/en-us/windows/win32/secauthz/sid-strings).
