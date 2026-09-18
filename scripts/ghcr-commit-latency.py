#!/usr/bin/env python3
"""Per job of a jib experiment run: time from each commit PUT to its response (>= 1 s only).
Needs jib HTTP logging at CONFIG. Usage: ghcr-commit-latency.py RUN_ID"""
import json, re, subprocess, sys
from datetime import datetime

def ts(line):
    m = re.search(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6})", line)
    return datetime.fromisoformat(m.group(1)) if m else None

run = sys.argv[1]
jobs = json.loads(subprocess.check_output(["gh", "run", "view", run, "--json", "jobs"], stdin=subprocess.DEVNULL))["jobs"]
for job in sorted(jobs, key=lambda j: (j["name"].split()[0], int(j["name"].split("#")[1]))):
    log = subprocess.run(["gh", "run", "view", "--job", str(job["databaseId"]), "--log"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    lines = re.sub(r"\x1b\[[0-9;]*m", "", log).splitlines()
    commits = []
    for i, line in enumerate(lines):
        m = re.search(r"Z PUT \S+/blobs/upload/\S+\?digest=(sha256:[0-9a-f]+)$", line)
        if not m:
            continue
        for later in lines[i + 1:]:
            ok = "docker-content-digest: " + m.group(1) in later
            if ok or ('CONFIG: {"errors"' in later):
                commits.append(((ts(later) - ts(line)).total_seconds(), m.group(1)[7:15], "ok" if ok else "error"))
                break
    slow = ", ".join(f"{d:.1f}s {g} {r}" for d, g, r in sorted(commits, reverse=True) if d >= 1) or "-"
    print(f"{job['name']:<13} {job['conclusion']:<8} {slow}", flush=True)
