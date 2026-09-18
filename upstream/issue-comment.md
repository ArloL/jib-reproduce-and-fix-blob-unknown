I reproduced this reliably against ghcr.io and traced the cause. The `404` is spurious: ghcr has
already committed the blob when jib reports the error.

**What happens**

1. jib uploads the layer in a single `PATCH`, then sends a bodyless `PUT <upload>?digest=…` to
   commit it.
2. ghcr takes longer than `jib.httpTimeout` (default 20 s) to commit a large blob, so the read
   times out.
3. `FailoverHttpClient` retries the same `PUT`. The first `PUT` consumed the upload session, so ghcr
   answers `404` with `BLOB_UPLOAD_UNKNOWN` or `BLOB_UNKNOWN`. Both codes come from this one bug.
4. jib fails the build. A `HEAD` on the digest returns `200`: the first `PUT` committed the blob.

**Reproducing**

A ~700 MB layer pushed with `-Djib.httpTimeout=5000` fails every time; the default 20 s fails
intermittently. Repro repo, workflows and logs:
https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown

**Workaround**

`-Djib.httpTimeout=120000` lowers the odds, but ghcr can outlast any client timeout.

**Fix**

When the commit fails, `HEAD` the digest and treat the push as successful if the blob exists. I have
a patch with tests and will open a PR.
<!-- CI_RESULTS -->

For comparison, `docker push` (containerd) sends the blob in one `PUT …?digest=` with no separate
bodyless commit, and waits up to 30 s for response headers.
