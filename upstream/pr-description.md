<!-- Title: fix: recover when a registry rejects a BLOB commit it completed or lost -->
Thank you for your interest in contributing! For general guidelines, please refer to
the [contributing guide](https://github.com/GoogleContainerTools/jib/blob/master/CONTRIBUTING.md).

Please follow the guidelines below before opening an issue or a PR:
- [x] Ensure the issue was not already reported.
- [x] Create a new issue at https://github.com/GoogleContainerTools/jib/issues/new/choose if you are unable to find an existing issue addressing your problem. Make sure to include a title and clear description, as much relevant information as possible, and a code sample or an executable test case demonstrating the expected behavior that is not occurring.
- [x] Discuss the priority and potential solutions with the maintainers in the issue. The maintainers would review the issue and add a label "Accepting Contributions" once the issue is ready for accepting contributions.
- [x] Open a PR only if the issue is labeled with "Accepting Contributions", ensure the PR description clearly describes the problem and solution. Note that an open PR without an issues labeled with "Accepting Contributions" will not be accepted.
- [x] Verify that integration tests and unit tests are passing after the change.
- [x] Address all checkstyle issues. Refer to the [style guide](https://github.com/GoogleContainerTools/jib/blob/master/STYLE_GUIDE.md).

Fixes #4301 🛠️

## Problem

jib commits an uploaded BLOB with a bodyless `PUT …?digest=`. ghcr.io can take longer than
`jib.httpTimeout` to commit a large BLOB; the read times out, and `FailoverHttpClient` re-sends the
`PUT` to the same upload session. ghcr.io then answers 404 with:

- `BLOB_UPLOAD_UNKNOWN` when the first `PUT` committed the BLOB (40 of 51 observed failures), or
- `BLOB_UNKNOWN` when the upload was lost (11 of 51).

jib fails the build in both cases.

## Fix

When the commit fails with either code, `RegistryClient.pushBlob` checks the BLOB with `HEAD`. If it
exists, the push succeeded. If not, and the BLOB is retryable, jib uploads it once more from a new
session. Other commit errors, and failures of the check itself, surface the original error as before.

Why not stop `FailoverHttpClient` from retrying the `PUT`? That retry still helps with real
connection failures, and without it jib would fail anyway: the timed-out commit needs the same
recovery, and an upload the registry lost can only be recovered by uploading again. Checking the
registry works regardless of how a registry reports the failure.

## Testing

- `RegistryClientTest`: six tests against an in-process registry cover both codes, BLOB present or
  missing, a failed re-upload, a non-retryable BLOB, an unrelated commit error, and a failing check.
- `./gradlew :jib-core:build` passes on JDK 8 and 11, and the jib-core registry integration tests
  (`BlobPusherIntegrationTest` etc.) pass against a local `registry:2`.
- Against ghcr.io with a ~700 MB layer and `-Djib.httpTimeout=5000`, ten pushes each:
  stock jib failed 10, this change failed 1 (the upload was lost on both attempts; the 5 s
  timeout makes committing that layer almost always time out).
  ([workflow](https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown/blob/main/.github/workflows/jib-fix.yaml))
