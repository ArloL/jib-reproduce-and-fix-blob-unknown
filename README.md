# jib#4301 reproduction

This repository reproduces [GoogleContainerTools/jib#4301](https://github.com/GoogleContainerTools/jib/issues/4301)
(`BLOB_UNKNOWN` / `BLOB_UPLOAD_UNKNOWN` when pushing large layers to ghcr.io) on GitHub Actions, and
holds the proposed fix, [`jib-verify-committed-blob.patch`](jib-verify-committed-blob.patch).

The patch applies to jib `fb949e26`, which the `jib` submodule pins:
`git -C jib am ../jib-verify-committed-blob.patch` recreates the fix commit.

Every experiment job builds jib from the submodule, then pushes `demo/` to its own ghcr.io
repository. The demo's dependencies form one ~700 MB layer, and a fresh repository per job forces
that layer to be uploaded rather than skipped as already present. A lowered `jib.httpTimeout` makes
committing the layer time out almost every time; at the default 20 s it fails only intermittently.

The workflows, with results as of 2026-09-18:

| Workflow | jib | Timeout | Pushes succeeded |
|---|---|---|---|
| [`jib-fix.yaml`](.github/workflows/jib-fix.yaml) | stock vs patched | 5 s | stock 0/10, patched 9/10 ([run](https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown/actions/runs/35364024602)) |
| [`jib-commit-latency.yaml`](.github/workflows/jib-commit-latency.yaml) | stock | 600 s | 10/10; commit took 5.1–10.7 s ([run](https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown/actions/runs/35363222125)) |
| [`jib-monolithic.yaml`](.github/workflows/jib-monolithic.yaml) | stock vs single-request upload (rejected alternative) | 5 s | stock 0/10, single-request 10/10 ([run](https://github.com/ArloL/jib-reproduce-and-fix-blob-unknown/actions/runs/29899201282)) |
| [`jib.yaml`](.github/workflows/jib.yaml) | released 3.5.2 | 600 s | this repository's regular build |

To rerun an experiment, fork the repository and `gh workflow run jib-fix.yaml`.

The error code on the retried commit tells whether ghcr.io kept the layer: `BLOB_UPLOAD_UNKNOWN`
means committed, `BLOB_UNKNOWN` means lost. `scripts/ghcr-failure-table.py RUN_ID DIGEST` prints, per
job, the codes from the log and whether the layer exists now; `scripts/ghcr-commit-latency.py RUN_ID`
prints how long each commit took. Both read logs with `gh`; the packages are public.

[`FINDINGS.md`](FINDINGS.md) is the full investigation log, including the jib code paths involved and
the comparison with docker's push.
