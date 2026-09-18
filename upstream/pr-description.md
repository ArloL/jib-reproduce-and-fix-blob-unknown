# fix: verify BLOB exists when commit fails after a timed-out retry

Fixes #4301.

## Problem

jib commits a pushed BLOB with a bodyless `PUT <upload>?digest=…`. ghcr.io can take longer than
`jib.httpTimeout` (default 20 s) to commit a large BLOB. The read times out, `FailoverHttpClient`
retries the `PUT`, and ghcr rejects the retry with 404 `BLOB_UPLOAD_UNKNOWN` or `BLOB_UNKNOWN`
because the first `PUT` consumed the upload session. jib fails the build, yet the first `PUT`
committed the BLOB: a `HEAD` on the digest returns 200.

Raising `jib.httpTimeout` only lowers the odds; ghcr's commit time has no upper bound.

## Fix

If the commit `PUT` fails with a registry error, `RegistryClient.pushBlob` sends `HEAD` for the
digest. If the BLOB exists, the push succeeded; otherwise jib rethrows the original error. BLOBs are
content-addressed, so an existing digest is the pushed content.

## Testing

- `RegistryClientTest` gains two tests backed by an in-process HTTP server that stalls the first
  commit past the read timeout and answers the retry with 404 `BLOB_UPLOAD_UNKNOWN`:
  - BLOB present on `HEAD` → push succeeds (fails without the fix, with the error from #4301).
  - BLOB absent on `HEAD` → the original error propagates.
- End to end against ghcr.io with a ~700 MB layer and `-Djib.httpTimeout=5000`:
  <!-- CI_RESULTS -->
