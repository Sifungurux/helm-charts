# llm-bench

Measures the inference engines on the rack one at a time on the same worker,
with prompts shaped like the jobs they are meant for: Zabbix alert triage,
node-log summaries and dependency-diff reviews.

`bench.py` needs Python 3.9+ and `kubectl`, nothing else. For each target in
`targets.json` it:

1. scales every other target to 0, since they share w1's 32 GB;
2. scales the target to 1, waits for the rollout and `/health`;
3. port-forwards its Service, reads its API key from the Secret;
4. sends one warm-up request (colibri's expert cache fills on the first), then
   each workload × prompt size × repeat, every prompt prefixed with a nonce so
   no engine can answer from its prompt cache;
5. reads the container's cgroup `memory.peak`;
6. restores every Deployment's original replica count, also on Ctrl-C.

Flux leaves the replica counts alone in between: helm-controller only upgrades
a release when its chart or values change.

## Run

```bash
cp targets.example.json targets.json      # adjust names, ports, secrets
./bench.py targets.json --quick           # smoke test: one workload, 512 + 2048 tokens
./bench.py targets.json                   # full: 3 workloads × 512/2048/8192 × 3 repeats
./bench.py targets.json --only ik-llama-cpp --workloads log --sizes 8192
./bench.py targets.json --prompts-dir ~/real-prompts   # alert-*.txt, log-*.txt, diff-*.txt
```

The full run is 27 measured requests per engine; at CPU prefill speeds the
8K-token prompts dominate, so expect an hour or more per engine. Run it with
the node otherwise idle.

Output goes to `results-<timestamp>/`: `results.jsonl` (one line per request,
including the first 160 characters of each answer for a quality check) and
`summary.md` (medians).

## Reading the numbers

- **TTFT** is what the async jobs wait for: on 4 cores, prompt processing
  dominates every workload here.
- **Prefill tok/s** = prompt tokens / TTFT. **Decode tok/s** = tokens after the
  first / time after the first. Both include HTTP and port-forward overhead.
- **Engine prefill / decode** are llama-server's own `timings` (llama.cpp and
  ik_llama.cpp); colibri does not report them.
- colibri runs its own int4 container of Qwen3-Coder-30B-A3B, the llama.cpp
  engines the Q4_K_M GGUF: close, not identical, quantisations. Compare the
  answers in `results.jsonl`, not only the speed.
