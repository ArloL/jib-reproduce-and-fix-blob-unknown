I've been hitting this pushing a large-ish image to ghcr.io and got it to reproduce reliably with HTTP logging on (`com.google.api.client.http` at CONFIG, `jib.serialize` off). `blob unknown to registry` is misleading — jib is retrying a `PUT` that shouldn't be retried and that is what the registry rejects it.

This was the sequence for the layer that failed (~140 MB):

1. The chunked upload finishes — `PATCH` is fully received, registry acks `range: 0-147372292`.
2. Jib sends the finalizing `PUT .../blobs/upload/11.<uuid>?digest=sha256:861cab54...`
3. About 20 seconds later it dies client-side with `java.net.SocketTimeoutException: Read timed out`:
   ```
   [WARNING] PUT .../blobs/upload/11.<uuid>?digest=sha256:861cab54... failed and will be retried
   java.net.SocketTimeoutException: Read timed out
   …
   ```
4. Jib retries the exact same `PUT` to the exact same upload URL, and that comes back as:
   ```
   HTTP/1.1 404 Not Found
   {"errors":[{"code":"BLOB_UNKNOWN","message":"blob unknown to registry"}]}
   ```

And yes, the 20 seconds are suspicious. It's the default `jib.httpTimeout`. GHCR is slow to commit the blob, and it hasn't answered yet when Jib gives up. Then the retry re-sends the same `PUT`, but the upload session is single-use and already consumed by the first attempt, so ghcr has nothing to point it at → 404.

The workaround that fixed it for me is bumping the timeout so the `PUT` actually waits for the response:

```
-Djib.httpTimeout=120000
```

I tested it in both configs: at the default it reproduced within 3 runs, and after bumping the timeout I could build 10 times with no failures.

Not sure if jib should even attempt to retry that PUT but I'm not well versed enough in container registry APIs.

---

### Where this happens in the jib code

The blob push is a three-stage sequence — POST to open an upload, one monolithic PATCH with the whole blob, then a PUT to commit it — driven by `RegistryClient.pushBlob`:

- `jib-core/.../registry/RegistryClient.java:523-569` — `pushBlob()`: `initializer()` (POST) → `writer()` (PATCH) → `committer()` (PUT `?digest=`).
- `jib-core/.../registry/BlobPusher.java:119-168` — `Writer`: sends the entire blob in a **single PATCH** (`BlobHttpContent`), no `Content-Range` chunking.
- `jib-core/.../registry/BlobPusher.java:171-216` — `Committer`: the finalizing `PUT <Location>?digest=<sha256>` against the upload URL returned by the PATCH.

The retry that turns a timeout into `BLOB_UNKNOWN`:

- `jib-core/.../http/FailoverHttpClient.java:359-373` — `createBackOffRetryHandler()` installs a `HttpBackOffIOExceptionHandler`. It retries **any** `IOException` (including `SocketTimeoutException`) on **any** HTTP method — there is no idempotency check, so the finalizing `PUT` is retried like a GET.
- `jib-core/.../http/FailoverHttpClient.java:340-342` — the handler is attached to every request whenever `enableRetry && retryOnIoException`.

The 20s timeout:

- `jib-core/.../global/JibSystemProperties.java:45-50` — `getHttpTimeout()` defaults to `20000` ms.
- `jib-core/.../registry/RegistryEndpointCaller.java:132` — that value is applied as both connect and read timeout on every registry request, including the commit PUT.

### Why this differs from a plain `docker push`

The registry v2 / OCI distribution spec allows two ways to push a blob:

- **Monolithic** — `POST` to open the session, then a **single `PUT ...?digest=<sha256>` that carries the whole blob in its body**. This is what `docker push` does: one data-bearing request per blob.
- **Two-step** — `POST` → `PATCH` (upload the bytes) → `PUT ...?digest=` (finalize). This is the path jib takes.

Jib's finalize `PUT` carries **no body** — `Committer.getContent()` returns `null` (`BlobPusher.java:175-179`); every byte of the layer is streamed in the `PATCH`, and the empty `PUT` only tells ghcr "now commit this upload under this digest." For a ~140 MB layer, that server-side commit is exactly the step that **takes forever** on ghcr, and it's a separate request with its own 20s read timeout. `docker push` has no equivalent standalone finalize to stall on — the commit is part of the one big `PUT` that's already streaming data.

The monolithic path is what `docker push` uses via containerd's pusher (which also backs buildkit / the containerd image store):

- [`pusher.go#L189`](https://github.com/containerd/containerd/blob/51bf0959daaa333a3597a4085829e4c9ebbe5667/core/remotes/docker/pusher.go#L189) — `POST .../blobs/uploads/` opens the upload session.
- [`pusher.go#L291-L296`](https://github.com/containerd/containerd/blob/51bf0959daaa333a3597a4085829e4c9ebbe5667/core/remotes/docker/pusher.go#L291-L296) — builds a **single** `PUT ...?digest=<digest>` with `Content-Type: application/octet-stream`.
- [`pusher.go#L310`](https://github.com/containerd/containerd/blob/51bf0959daaa333a3597a4085829e4c9ebbe5667/core/remotes/docker/pusher.go#L310) — `// TODO: Support chunked upload`.
- [`pusher.go#L312-L322`](https://github.com/containerd/containerd/blob/51bf0959daaa333a3597a4085829e4c9ebbe5667/core/remotes/docker/pusher.go#L312-L322) — the whole blob body streams into that one PUT; a single request commits it.

There is no `PATCH` in that path — one `POST`, then one data-bearing `PUT`.

The upload session is also **stateful and single-use**: the `Location`/upload-UUID URL is consumed once the `PUT` completes. So when jib's finalize `PUT` times out at 20s (ghcr still committing), `HttpBackOffIOExceptionHandler` blindly re-sends that non-idempotent `PUT` to the now-consumed upload URL, and the retry is what returns `404 BLOB_UNKNOWN`. The error is a symptom of retrying jib's separate finalize step, not of a genuinely missing blob — and it's a step `docker push` doesn't have.

### Reproduction experiment: does a monolithic (docker-style) push avoid it?

To test the hypothesis I patched jib (submodule pinned at `fb949e26`) with an experimental
single-request push behind `-Djib.experimentalMonolithicBlobPush` — after the initializing
`POST`, it sends the whole blob + digest in **one** `PUT`, skipping the `PATCH`/bodyless-finalize
(a new `MonolithicCommitter` in `BlobPusher`; the branch is in `RegistryClient.pushBlob`). A
GitHub Actions workflow (`.github/workflows/jib-monolithic.yaml`) builds the patched jib and pushes
the demo image to ghcr, at the **default 20s `httpTimeout`** (workaround removed), with a matrix of
`baseline` (stock POST+PATCH+PUT) vs `monolithic`, several attempts each, each to a unique ghcr repo.

**Local sanity check (registry:2):** with the flag on, the wire trace is `POST` → single
`PUT ...?digest=` (`Content-Type: application/octet-stream`, body present), **zero `PATCH`** — i.e.
identical shape to containerd's pusher. Build succeeds. Confirms the mechanism works.

**Run 1 — ~350 MB deps (microsoft-graph + ffmpeg-platform), `jre-alpine` base:**

- `baseline #2`: the bodyless finalize `PUT ...?digest=` **timed out at 20s** (`SocketTimeoutException:
  Read timed out`) on **both** big blobs (the 62 MB base JRE layer and the 327 MB deps layer) → jib
  retried the same `PUT` → ghcr returned **`201 Created`** on the retry (recovered).
- `baseline #4`: same on the 327 MB deps layer → retry → `201`.
- All 5 `monolithic` jobs: **zero timeouts**, including on the 327 MB blob.
- Global `BLOB_UNKNOWN` count: **0** — the timeout+retry *precursor* reproduced, but the 404 subcase
  did not occur this run (the re-sent finalize was honored).

**What this establishes so far:**

- The finalize-`PUT` timeout is real and reproduces at ~300 MB: baseline's *separate, bodyless*
  finalize (pure wait while ghcr assembles the blob) blew past 20s three times across two jobs.
- The monolithic path never entered that stall — supporting the hunch, though not yet decisive.
- The `BLOB_UNKNOWN` itself is the *racy subcase* of that retry: whether the re-sent finalize gets
  `201` (session still there) or `404 BLOB_UNKNOWN` (session consumed) is ghcr-timing-dependent. We
  have reproduced the timeout+retry but not yet a fresh 404.

**Layer note (correcting an earlier misattribution):** the deps are still a **single** layer. In the
manifest the two large blobs are the deps layer (e.g. `767db9f7…`, 327 MB) *and* a base-image layer
(`9f5487be…`, 62 MB, the Temurin JRE — jib re-uploads base layers to each fresh unique repo). The
`9f5487be…` blob is **base-image**, not app dependencies.

**Run 2 — ~700 MB deps (added opencv-platform) + full `eclipse-temurin:25-jdk` base** (bigger deps
layer *and* bigger base layer, both re-uploaded to each fresh unique repo). This captured the actual
failure:

- `baseline #2`: **`BLOB_UNKNOWN`, build failed** — the finalize `PUT ...?digest=sha256:d14cbaca…`
  timed out (`Read timed out`), was retried, and the retry got the 404:

  ```
  [WARNING] PUT .../blobs/upload/14.95e38bd2…?digest=sha256:d14cbaca… failed and will be retried
            java.net.SocketTimeoutException: Read timed out
  HTTP/1.1 404 Not Found
  {"errors":[{"code":"BLOB_UNKNOWN","message":"blob unknown to registry"}]}
  [ERROR] Failed to execute goal …:build … Tried to push BLOB … digest sha256:d14cbaca…
          but failed because: blob unknown to registry (something went wrong): 404 Not Found
  [INFO] BUILD FAILURE
  ```
- `baseline #3/#4/#5`: success; `baseline #1`: cancelled.
- All 5 `monolithic` jobs: **success** — zero timeouts, each a full ~700 MB fresh upload to its own
  unique repo (not a HEAD-200 short-circuit).

**Bottom line:** at the default 20s timeout, the stock POST+PATCH+PUT path fails with `BLOB_UNKNOWN`,
while the docker-style monolithic POST+PUT path pushes the same ~700 MB cleanly across all attempts.
The `BLOB_UNKNOWN` is confirmed to be the timeout-then-retry of jib's separate bodyless finalize.

**Open question / residual risk:** the monolithic `PUT` also ends in a server-side commit that is, in
principle, exposed to the read timeout; it simply never stalled across these runs. So monolithic
reduces the exposure (no separate zero-traffic finalize to stall on) but may not be a complete fix on
its own. The deeper fix is arguably to **not retry a non-idempotent finalize on `IOException`**
(`FailoverHttpClient.createBackOffRetryHandler` retries any method) — or to restart the whole upload
(new POST) rather than re-`PUT` a consumed upload URL.

PS Analysis was done with assistance from Claude Code ;)
