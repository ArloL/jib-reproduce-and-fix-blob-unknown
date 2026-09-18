#!/usr/bin/env python3
"""Per job of a jib experiment run: big-blob commit timeouts, 404 error codes, and whether the blob
exists on ghcr now. Usage: ghcr-failure-table.py RUN_ID BLOB_DIGEST"""
import base64, json, re, subprocess, sys, urllib.error, urllib.request

run, digest = sys.argv[1], sys.argv[2]
token = base64.b64encode(subprocess.check_output(["gh", "auth", "token"], stdin=subprocess.DEVNULL).strip()).decode()
jobs = json.loads(subprocess.check_output(["gh", "run", "view", run, "--json", "jobs"], stdin=subprocess.DEVNULL))["jobs"]
for job in sorted(jobs, key=lambda j: (j["name"].split()[0], int(j["name"].split("#")[1]))):
    log = subprocess.run(["gh", "run", "view", "--job", str(job["databaseId"]), "--log"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    log = re.sub(r"\x1b\[[0-9;]*m", "", log)
    repo = re.search(r"ghcr\.io/v2/(\S+?)/blobs/", log)
    if not repo:
        print(f"{job['name']:<13} {job['conclusion']}: no registry traffic in log")
        continue
    timeouts = len(re.findall(r"PUT \S+digest=" + digest + r" failed and will be retried", log))
    codes = re.findall(r'CONFIG: \{"errors":\[\{"code":"(\w+)"', log)
    req = urllib.request.Request(f"https://ghcr.io/v2/{repo.group(1)}/blobs/{digest}", method="HEAD",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        status = urllib.request.urlopen(req, timeout=30).status
    except urllib.error.HTTPError as e:
        status = e.code
    print(f"{job['name']:<13} {job['conclusion']:<8} timeouts={timeouts} codes={','.join(codes) or '-'} HEAD now={status}",
          flush=True)
