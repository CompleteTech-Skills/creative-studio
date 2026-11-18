# Windows handle-pinned publication

The additive `atomic_publish.windows_private_stage_supported()` probe and
`windows_private_stage(parent_path, prefix='.publish-', cleanup=True,
cleanup_on_error=True)` context provide an ordinary path for existing writers,
SQLite and verifiers. Inside the context, write through `stage.path`, finish all
workers and verification, then call `stage.publish_to(destination)`. Publication
is exclusive and moves the exact held payload; it never reopens a mutable source
basename. Calls after context exit or successful publication are rejected.

Use `with stage.pin_child('receipts', create=True) as child:` for a new physical
direct child, and `child.path` for its writers. Child pins can nest. Existing
`pinned_directory(stage.path / 'existing-child')` contexts reuse the active
held-parent capability instead of reopening the DELETE-held payload and causing
a Windows sharing violation. Every descendant is checked for reparse points.
Close all child pin contexts before calling `publish_to`; premature publication
is rejected. Expired child contexts cannot create more children.

Every physical ancestor is held with delete sharing denied before caller I/O.
Reparse ancestors, traversal, device namespaces, drive-relative paths, alternate
streams and UNC paths fail closed. New wrapper ACLs are protected and inherited
by children, allowing the explicit creating process user and LocalSystem.
After opening the wrapper, its owner SID and exact protected access rules are
validated through that held handle before creating a payload or yielding to
writers. An ordinary directory substituted between creation and acquisition
is rejected if its ownership or permissions differ; its contents are preserved.
The payload handle retains
DELETE access while denying delete sharing to other opens. Publication uses
`SetFileInformationByHandle(FileRenameInfo)` with `ReplaceIfExists=False` and an
absolute destination whose directory ancestors remain pinned. The Win32
NULL-`RootDirectory` form avoids the relative-root parameter rejection observed
on the actual Windows runner; source publication still uses its held handle.
Cleanup pins descendants, does not follow
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
Handle-bound ownership and ACL validation use
[GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo),
[GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)
and [GetAce](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getace).
