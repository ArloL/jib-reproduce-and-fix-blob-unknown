Following up on my comment above: I dug further, and I'd like to propose a fix.

**What happens**

jib uploads a layer with one `PATCH`, then commits it with a bodyless `PUT …?digest=`. ghcr.io needs
5–11 s to commit a ~700 MB blob (measured over 10 pushes with no client timeout, all of which
succeeded). Whenever the commit outlasts `jib.httpTimeout`, jib's I/O retry re-sends the `PUT` to the
now-consumed upload session, and ghcr.io answers 404 with one of two codes. Across 51 such failures:

| code on the retried `PUT` | blob afterwards (`HEAD`) | seen |
|---|---|---|
| `BLOB_UPLOAD_UNKNOWN` | present: the first `PUT` committed it | 40 |
| `BLOB_UNKNOWN` | missing, even months later: the upload was lost | 11 |

So most of these failures are spurious, and the rest need the blob uploaded again. This is also why
raising `jib.httpTimeout` helps but doesn't settle it: it only lowers the odds that the commit outlasts
the timeout.

**Proposed fix**

In `RegistryClient.pushBlob`, when the commit fails with `BLOB_UPLOAD_UNKNOWN` or `BLOB_UNKNOWN`:

1. `HEAD` the digest. If the blob exists, the push succeeded.
2. Otherwise, upload the blob once more from a new session (only if the blob can be re-read).

Other errors propagate as before. I have this ready with unit tests. Against ghcr.io with
`-Djib.httpTimeout=5000` (so committing the large layer almost always times out), stock jib failed 10 of 10 pushes and the
patched jib 1 of 10 (the upload was lost on both attempts).

Reproduction, workflows and logs: https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown

@mpeddada1, would you label this issue "Accepting Contributions" so I can open the PR?
