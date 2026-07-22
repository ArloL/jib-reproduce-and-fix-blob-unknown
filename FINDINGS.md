# jib #4301 — `BLOB_UNKNOWN` / `BLOB_UPLOAD_UNKNOWN` pushing large blobs to ghcr.io

**Investigation record — 2026-07-22.** Written to be picked up cold in a later session. This is a
findings dump, not a polished issue comment yet.

---

## TL;DR (the answer)

Pushing a large image to ghcr.io intermittently fails with `blob unknown to registry`
(`BLOB_UNKNOWN`) or `blob upload unknown to registry` (`BLOB_UPLOAD_UNKNOWN`), HTTP 404, and the
build aborts.

- **Root cause:** jib finishes the blob upload and sends a **separate, bodyless finalize `PUT
  …?digest=`** to commit it. ghcr is slow to commit large blobs and doesn't answer within jib's read
  timeout (`jib.httpTimeout`, default **20 000 ms**). The timeout fires; `FailoverHttpClient` then
  **retries the same non-idempotent `PUT`**; by now the single-use upload session is consumed, so
  ghcr returns **404** and jib fails the build.
- **The failure is SPURIOUS.** When the retry 404s, **ghcr has already committed the blob.** Verified
  by `HEAD`ing the digests of failed jobs directly on ghcr → `200` (see “Decisive evidence” below).
  jib trusts the 404 on the stale upload session over the actual state of the registry.
- **Why raising the timeout only half-helps:** ghcr's commit can outlast any client timeout (the
  reporter has seen it fail even at a 2-minute timeout). `-Djib.httpTimeout=120000` reduces the odds
  but is not a real fix.
- **The fix I'd recommend:** on a finalize timeout, or on a `BLOB_UNKNOWN`/`BLOB_UPLOAD_UNKNOWN` from
  the commit step, **`HEAD` the blob digest; if present, treat the push as successful.** This is
  timeout- and registry-latency-independent. Secondary: stop blindly re-`PUT`ing a consumed upload
  session (restart with a fresh `POST` if the blob genuinely isn't there).
- **Monolithic push (docker-style POST + single PUT, no separate finalize) is a mitigation, not a
  fix** — it pushed 20/20 cleanly in testing, but its own commit wait is exposed to the same ghcr
  slowness. Implemented behind `-Djib.experimentalMonolithicBlobPush` for the experiment.

---

## Orientation — what I'd want to know at the start

**Working dirs on this machine:**

- `/Users/arlookeeffe/Developer/reproduce-jib-issue` — the repro repo (GitHub:
  `ArloL/jib-reproduce-and-fix-blob-unknown`, default branch `main`). Contains `demo/` (the app that
  builds the fat image), `.github/workflows/`, the patch, and the cleanup script. `jib/` here is a
  **git submodule** pointing at upstream `GoogleContainerTools/jib` pinned at `fb949e26` (clean).
- `/Users/arlookeeffe/Developer/jib` — a **separate** clone of jib on `master` where I made the
  monolithic edits (uncommitted). Also has a `.tool-versions` pinning `java temurin-11`. The patch
  file was generated from here.
- `/Users/arlookeeffe/Developer/containerd` — clone of containerd (HEAD `51bf0959`) used to cite the
  docker/containerd push path.

**Build gotcha (cost me time):** jib's Gradle wrapper is **6.9.2**, which only runs on **Java ≤ 16**.
This machine has JDK 21 and 26 only. Fix: `mise install java@temurin-11`, put `java temurin-11` in a
`.tool-versions`, and run gradle via `mise exec -- ./gradlew …`. The demo itself needs JDK 25
(`demo/.tool-versions` = `temurin-25`).

**ghcr package auth:** `gh` token already has `read:packages` + `delete:packages`. For the registry
v2 API, `Authorization: Bearer $(printf '%s' "$(gh auth token)" | base64)` works for pulls/HEADs.

**Ephemeral vs durable artifacts:** the raw job logs I saved live under the session scratchpad
(`…/scratchpad/*.log`) and will **not** survive into a new session. Durable: the GitHub Actions run
logs (retained ~90 days) and the ghcr packages (until deleted). Run IDs are listed below.

---

## Root cause in detail

jib's blob push is three stages (`RegistryClient.pushBlob`):

1. `POST /v2/<name>/blobs/uploads/` → opens an upload session, returns a `Location` (upload UUID URL).
2. `PATCH <Location>` → uploads the **entire** blob in one request (jib does *not* chunk with
   `Content-Range`).
3. `PUT <Location>?digest=<sha256>` → **bodyless** finalize that tells ghcr “commit this upload.”

The finalize `PUT` carries no data, so during ghcr's (slow) server-side commit there is zero traffic
on the socket — it's a pure wait, maximally exposed to the read timeout. When it fires:
`HttpBackOffIOExceptionHandler` retries the same `PUT`; the upload session is single-use and already
consumed, so ghcr 404s. jib surfaces that as `BLOB_UNKNOWN` / `BLOB_UPLOAD_UNKNOWN`.

**Code references (jib-core):**

- `registry/RegistryClient.java:523-569` — `pushBlob()`: `initializer()` (POST) → `writer()` (PATCH)
  → `committer()` (PUT). (I added a monolithic branch here — see patch.)
- `registry/BlobPusher.java` — `Writer` (single PATCH, no `Content-Range`); `Committer` (finalize PUT,
  `getContent()` returns `null` → bodyless).
- `http/FailoverHttpClient.java:359-373` — `createBackOffRetryHandler()` retries **any** `IOException`
  on **any** method (no idempotency check); attached at `:340-342`.
- `global/JibSystemProperties.java:45-50` — `getHttpTimeout()` default `20000`.
- `registry/RegistryEndpointCaller.java:132` — that value applied as **connect and read** timeout on
  every request, including the finalize PUT.

The two 404 error codes (`BLOB_UNKNOWN` vs `BLOB_UPLOAD_UNKNOWN`) are the **same bug** — both are
ghcr rejecting the re-`PUT` of a consumed upload session. The original report saw both
(`job-logs.txt` = `BLOB_UNKNOWN`, `job-logs2.txt` = `BLOB_UPLOAD_UNKNOWN`).

---

## Decisive evidence — the blob is already committed

After failed `baseline` jobs (run `29899201282`), I `HEAD`ed the blob digests directly on ghcr:

```
baseline #1   HEAD sha256:d14cbaca…  -> 200
baseline #1   HEAD sha256:d660575…   -> 200
baseline #2/#5/#8/#10  HEAD (big deps blob d14cbaca…) -> 200
tags/list -> 404   (manifest never written; jib aborted, so the committed blob is orphaned)
```

So the finalize `PUT` **did** commit the blob server-side; ghcr just answered too slowly, the session
expired, and the retry 404'd on the stale session while the blob was present. The build failure is
spurious. This is why no finite `jib.httpTimeout` reliably fixes it, and why the correct fix is to
`HEAD`-verify rather than trust the 404.

---

## docker / containerd comparison

`docker push` (via containerd's pusher, which also backs buildkit / the containerd image store) uses
the **monolithic** upload: `POST` then a **single `PUT …?digest=` carrying the whole blob body** — no
`PATCH`, no separate bodyless finalize.

- `containerd core/remotes/docker/pusher.go:189` — `POST .../blobs/uploads/`.
- `pusher.go:291-296` — single `PUT …?digest=`, `Content-Type: application/octet-stream`.
- `pusher.go:310` — `// TODO: Support chunked upload`.
- `pusher.go:312-322` — whole body streams into that one PUT.

**Timeouts** (`containerd core/remotes/docker/registry.go:254-268`, `DefaultHTTPTransport`): no
overall request deadline; dial 30 s, TLS 10 s, ExpectContinue 5 s, **`ResponseHeaderTimeout: 30 s`**
(time to first response header after the body is sent), IdleConn 30 s. So docker gives the commit up
to ~30 s to *begin* responding, with no total deadline — vs jib's 20 s socket read timeout on a step
docker doesn't even have.

---

## Is monolithic the fix? No — downsides

- **Doesn't fix the root bug:** a monolithic PUT also ends in a commit wait exposed to the read
  timeout; a timeout there would be retried the same non-idempotent way and could 404 too. It only
  narrows the window (no separate zero-traffic finalize). In testing it never stalled, but that's
  ghcr behavior, not a guarantee.
- **Expensive retries:** digest is in the same request as the body, so any retry re-uploads the whole
  blob (vs a cheap empty finalize retry today).
- **Registry/proxy limits:** a single ~700 MB PUT can hit request-body-size or single-request-duration
  caps that a chunked/two-step upload slips under; no resumability (restart from byte 0 on failure).
- Mitigating note: jib already sends the whole blob in one PATCH (no chunking), so it isn't using
  resumability today anyway — switching loses little jib actually exercises. But it's a step away from
  ever doing proper resumable chunked uploads.

**Better fix (repeat):** `HEAD`-verify after finalize timeout / commit 404; don't re-`PUT` a consumed
session. Offer monolithic as opt-in only.

---

## Reproduction setup

**Repro repo layout:** `demo/` (Spring Boot app with deliberately huge deps), `jib/` (submodule,
upstream, pinned `fb949e26`), `.github/workflows/jib-monolithic.yaml` (the experiment),
`.github/workflows/jib.yaml` (original repro workflow), `jib-monolithic-blob-push.patch`,
`scripts/delete-repro-packages.py`.

**Versions at the pinned jib commit:** `jib-core` `0.28.3-SNAPSHOT`, `jib-maven-plugin`
`3.5.3-SNAPSHOT`.

**The patch (`jib-monolithic-blob-push.patch`)** adds an experimental single-request push behind
`-Djib.experimentalMonolithicBlobPush`:

- `JibSystemProperties` — `MONOLITHIC_BLOB_PUSH` flag + `useMonolithicBlobPush()`.
- `BlobPusher` — new `MonolithicCommitter` (PUT with body + `?digest=`) and `monolithicCommitter()`.
- `RegistryClient.pushBlob` — when the flag is set, does `POST` → monolithic `PUT`, skipping the PATCH.

Applies cleanly onto the submodule commit: `git -C jib apply jib-monolithic-blob-push.patch`.

**Build patched jib locally (JDK 11 via mise):**

```
cd /Users/arlookeeffe/Developer/jib        # (or the submodule, after applying the patch)
mise install java@temurin-11               # once
mise exec -- ./gradlew :jib-core:publishToMavenLocal :jib-maven-plugin:publishToMavenLocal \
  -x test -x javadoc
# publishes 3.5.3-SNAPSHOT / 0.28.3-SNAPSHOT to ~/.m2
```

**Local end-to-end test against a throwaway registry (proves wire shape, not the ghcr latency):**

```
docker run -d -p 5001:5000 --name jib-repro-registry registry:2
cd demo
mise exec -- ./mvnw -B -DskipTests -Djava.util.logging.config.file=logging.properties \
  -Djib.experimentalMonolithicBlobPush=true -Djib.allowInsecureRegistries=true \
  compile com.google.cloud.tools:jib-maven-plugin:3.5.3-SNAPSHOT:build \
  -Djib.from.image=eclipse-temurin:25-jre-alpine \
  -Djib.to.image=localhost:5001/jib-repro-monolithic:test
```

With the flag on the wire trace is `POST` → single `PUT …?digest=` (body present) → **zero PATCH**,
i.e. identical to containerd. HTTP logging: `logging.properties` sets
`com.google.api.client.http.level=CONFIG` (hides the Authorization header; do not use `ALL`).

**CI workflow (`.github/workflows/jib-monolithic.yaml`), `workflow_dispatch`:** matrix
`variant ∈ {monolithic, baseline}` × `attempt`. Each job: checkout with submodules → `git -C jib
apply` the patch → build+publish patched jib under JDK 11 → push the demo to a **unique** ghcr repo
(`…-repro/<run>-<attempt>-<variant>-<n>`, so blobs always upload fresh) under JDK 25. `baseline` =
stock POST+PATCH+PUT (flag false); `monolithic` = flag true. `timeout-minutes: 15` guards hangs.
`-Djib.httpTimeout` is the key knob (see runs). Trigger: `gh workflow run jib-monolithic.yaml --ref
main` (must live on the default branch to be dispatchable).

**Demo blob (`demo/pom.xml`):** `microsoft-graph` + `microsoft-graph-beta` (~150 MB) + bytedeco
`ffmpeg-platform` (~173 MB) + `opencv-platform` (~260 MB) → one ~700 MB **single** `dependencies`
layer (all release jars land in one layer). Base image `eclipse-temurin:25-jdk` (~180 MB base layer,
also re-uploaded to each fresh repo). Layer note: in the manifest, `9f5487be…` (62 MB) is the base
**JRE** layer, NOT app deps — `jib/plan.md` misattributes it.

---

## Experiment runs & results

All at default timeout unless noted; unique repo per job; matrix baseline vs monolithic.

| Run ID | Config | baseline | monolithic |
|---|---|---|---|
| `29897924881` | jre-alpine + ffmpeg (~350 MB deps), 20 s timeout, 5 attempts | `#2`,`#4` **timed out** on finalize `PUT` of big blobs → retried → ghcr `201` (recovered); others clean. **0** `BLOB_UNKNOWN`. | 5/5 success, **0 timeouts** |
| `29898470121` | JDK base + opencv (~700 MB deps), 20 s, 5 attempts | `#2` **FAILED `BLOB_UNKNOWN`** (finalize timeout→retry→404, digest `d14cbaca…`); `#3/#4/#5` pass; `#1` cancelled (hung) | 5/5 success |
| `29899201282` | same image, **`-Djib.httpTimeout=5000`**, 10 attempts | **10/10 FAILED** — 404 after finalize timeout+retry (**9× `BLOB_UPLOAD_UNKNOWN`, 1× `BLOB_UNKNOWN`**) | **10/10 success** |

Key reading: shrinking the timeout to 5 s makes ghcr's commit lose the race deterministically →
reliable reproduction without needing an ever-bigger blob. It is the *same* read timeout the bug rides
on. `will NOT be retried` = 0 in run 3 → the build dies on the 404, not on exhausted IO retries. And
the failed blobs are present on ghcr anyway (Decisive evidence).

Aggregate: monolithic pushed the same ~700 MB **20/20** cleanly across runs 2–3; baseline fails
deterministically at 5 s and intermittently at 20 s.

---

## Facts / gotchas learned (so I don't re-derive them)

- The 404 is spurious — **`HEAD` the digest to confirm the blob is really there** before believing a
  push failed.
- `BLOB_UNKNOWN` and `BLOB_UPLOAD_UNKNOWN` are the same bug (stale upload session on retry).
- jib does **one big PATCH**, not chunked; the finalize PUT is **bodyless** — that bodyless wait is
  the fragile step.
- Raising `jib.httpTimeout` is unreliable because ghcr commit time is unbounded from the client side.
- Reproduce **reliably** by lowering the timeout (5 s), not by enlarging the blob.
- Build jib with **JDK 11** (Gradle 6.9.2); use mise. Demo uses JDK 25.
- Unique ghcr repos are required, else jib sees the blob (HEAD 200) and skips the upload.
- Bigger base image = another big blob re-uploaded per fresh repo = another timeout chance.
- Pushing the workflow to a non-default branch won't let `workflow_dispatch` register — it must be on
  `main`.

---

## Artifacts & pointers

- GitHub Actions runs: `29897924881`, `29898470121`, `29899201282` (repo
  `ArloL/jib-reproduce-and-fix-blob-unknown`, workflow “Jib monolithic experiment”). Logs retained
  ~90 days: `gh run view <id> --log` / `--log-failed`.
- ghcr packages: `…-repro/*` (many, from all runs). Cleanup:
  `python3 scripts/delete-repro-packages.py [--yes] [--pattern <run_id>]` (dry-run by default; token
  needs `delete:packages`). **Not yet deleted** — the failed-baseline blobs are the evidence for the
  “committed anyway” claim; keep until the finding is posted/captured.
- Local (ephemeral, session scratchpad — will vanish next session): full run logs `run-full.log`,
  `run3-failed.log`, local monolithic `monolithic-run.log`.
- Original evidence in-repo: `job-logs.txt` (BLOB_UNKNOWN), `job-logs2.txt` (BLOB_UPLOAD_UNKNOWN).
- Reporter's original first-person account is preserved at the bottom of this file.

---

## Open questions / next steps

- Confirm the `HEAD`-verify fix against a real jib change: on finalize timeout / commit 404, HEAD the
  digest and accept if present; only fail if genuinely absent. Then re-run `29899201282`-style at 5 s
  and expect baseline to pass too.
- Decide framing for upstream: primary fix = HEAD-verify (+ don't re-PUT consumed session); secondary
  = monolithic opt-in. Draft PR against jib.
- Tidy the reporter's narrative below (line “chunked upload” is really jib's single PATCH) before
  posting as an issue comment.
- Clean up ghcr packages once done.

---

## Reporter's original account (primary evidence, verbatim)

I've been hitting this pushing a large-ish image to ghcr.io and got it to reproduce reliably with
HTTP logging on (`com.google.api.client.http` at CONFIG, `jib.serialize` off). `blob unknown to
registry` is misleading — jib is retrying a `PUT` that shouldn't be retried and that is what the
registry rejects.

Sequence for the layer that failed (~140 MB): the upload finishes (`PATCH` fully received, registry
acks `range: 0-147372292`); jib sends the finalizing `PUT …/blobs/upload/11.<uuid>?digest=sha256:
861cab54…`; ~20 s later it dies client-side with `java.net.SocketTimeoutException: Read timed out`
(`PUT … failed and will be retried`); jib retries the exact same `PUT` and gets `HTTP/1.1 404 Not
Found` `{"errors":[{"code":"BLOB_UNKNOWN","message":"blob unknown to registry"}]}`. The 20 s is the
default `jib.httpTimeout`; ghcr is slow to commit and hasn't answered when jib gives up; the retry
re-sends the same `PUT` but the single-use upload session is already consumed → 404. Workaround:
`-Djib.httpTimeout=120000` (reproduced within 3 runs at default; 10 clean builds after bumping) — but
see above, this is not reliable when ghcr is slow enough.
