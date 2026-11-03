# helm-charts

Helm charts for the homelab rack, published as signed OCI artifacts to
`oci://ghcr.io/sifungurux/charts` and consumed by Flux.

| Chart | What it runs |
| --- | --- |
| [`colibri`](charts/colibri) | [colibri](https://github.com/JustVugg/colibri) LLM inference server (Qwen3-Coder-30B by default), OpenAI- and Anthropic-compatible API |
| [`llama-server`](charts/llama-server) | llama-server from [llama.cpp](https://github.com/ggml-org/llama.cpp) or [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) for one GGUF model, OpenAI-compatible API. `ci/` holds a profile per engine |
| [`zabbix-stack`](charts/zabbix-stack) | Zabbix 7.0 LTS umbrella chart: server, frontend and PostgreSQL (zabbix-community), node agents, kube-state-metrics, a probe agent and a configure Job that sets up hosts and templates through the API |

## How it flows

```
PR ──► lint.yaml        helm lint + render + kubeconform, version-bump check
main ─► release.yaml    package changed charts ─► push to GHCR (OCI) ─► cosign sign (keyless)
main ─► image.yaml      build each changed images/<name> at its pinned upstream commit ─► push ─► sign + SBOM + provenance
                                      │
homelab repo:  OCIRepository (verify: cosign, this repo's release workflow) ─► HelmRelease ─► cluster
```

Charts are only published when `version` in `Chart.yaml` changes, so every
chart change needs a version bump (CI enforces it). Charts with dependencies
(`zabbix-stack`) get `helm dependency update` in CI; run it locally before
`helm lint`, and commit the resulting `Chart.lock`.

## Deploying

Flux wiring lives in `Sifungurux/homelab`, not here. Each chart gets
`clusters/rack/sources/<chart>.yaml` (OCIRepository pinned by tag and digest,
cosign-verified against this repo's release workflow) and
`clusters/rack/releases/<chart>.yaml` (HelmRelease), listed in
`clusters/rack/kustomization.yaml`. Secrets are created with kubectl, never
committed. colibri's files there are the reference.

colibri also needs a labelled node, and its clients a labelled namespace:

```bash
kubectl label node <worker> homelab/llm=true
kubectl label ns <client-ns> homelab/llm-client=true
```

## Verify

```bash
flux get sources oci colibri         # READY with a verified signature
flux get helmreleases colibri
kubectl -n ai get pods               # dl job completes, then the server starts
kubectl -n ai exec deploy/colibri -- coli doctor --model /model/qwen3-coder-30b
```

Manual signature check:

```bash
cosign verify ghcr.io/sifungurux/charts/colibri:0.1.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/Sifungurux/helm-charts/\.github/workflows/release\.yaml@refs/heads/main$'
```

## Testing colibri

The Service is ClusterIP-only and its NetworkPolicy admits only labelled
namespaces, so test from your machine through a port-forward (it reaches the
pod directly). Leave this running in one terminal:

```bash
kubectl -n ai port-forward svc/colibri 5000:5000
```

In a second terminal, read the API key from its Secret:

```bash
KEY=$(kubectl -n ai get secret colibri-api-key -o jsonpath='{.data.api-key}' | base64 -d)

curl -s localhost:5000/health                                     # no key needed: {"status":"ok"}
curl -s localhost:5000/v1/models -H "Authorization: Bearer $KEY"  # 401 without the key
```

A chat completion (OpenAI format; `x-api-key: $KEY` works too):

```bash
curl -s localhost:5000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-coder-30b","max_tokens":200,
       "messages":[{"role":"user","content":"Write a bash one-liner that counts lines in all .py files"}]}'
```

Add `"stream":true` to watch tokens arrive instead of waiting for the whole answer.

### Measuring speed

Time a request and divide the generated tokens by the seconds:

```bash
time curl -s localhost:5000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-coder-30b","max_tokens":200,
       "messages":[{"role":"user","content":"Explain a Kubernetes readiness probe in 150 words."}]}' \
  | jq .usage        # completion_tokens / real seconds = tokens per second
```

The engine logs the same per request:

```bash
kubectl -n ai logs deploy/colibri -c colibri | grep -v /health | tail
```

- `cache auto-sized: N slots/layer of 128 experts` (at startup): the expert
  cache. 128 means every expert fits in RAM. `cache=8/layer` in the banner
  means `--cap` was not applied and every token re-reads its experts.
- `[qwen36 spec gate] turn N: T(1) 4293.1 ms`: time per forward pass, roughly
  per token. Tokens per second is about 1000 / that number.
- The first request after a restart is slower while the cache fills; measure
  the second.

### Reference results

The readiness-probe prompt above, `max_tokens` 256, on w1 (i7-7700, 32 GB),
chart 0.1.5, measured 2026-11-01:

| Request | Tokens | Time | Speed | `T(1)` |
| --- | --- | --- | --- | --- |
| First after restart | 160 | 69 s | 2.3 tok/s | 118 ms |
| Second | 170 | 25 s | 6.8 tok/s | 107 ms |
| Before 0.1.5 (`cache=8/layer`) | 173 | 835 s | 0.2 tok/s | 4336 ms |

Pure decode is about 9 tok/s (1000 / `T(1)`); the rest is prompt processing
and request overhead. RSS is about 18 GB once every expert is loaded.

## Comparing engines

`tools/llm-bench/bench.py` runs colibri, llama.cpp and ik_llama.cpp one at a
time on the same worker and reports TTFT, prefill and decode speed and peak
memory per workload. The llama-server releases are installed at
`replicaCount: 0`, so they never compete with colibri for RAM; the script
scales each up while it is measured. See [`tools/llm-bench`](tools/llm-bench).

The llama-server releases need their own API key Secret:

```bash
kubectl -n ai create secret generic llama-server-api-key --from-literal=api-key="$(openssl rand -hex 24)"
```

## Upgrading

- **New colibri build**: change `images/colibri/COLIBRI_REF`, merge, then
  set `image.digest` in `charts/colibri/values.yaml` and bump the chart.
- **New llama.cpp or ik_llama.cpp build**: change `images/llama-cpp/REF` or
  `images/ik-llama-cpp/REF`, merge, then set `image.digest` in the HelmRelease.
  Both images come from `images/llama-cpp/Dockerfile`, so editing it rebuilds both.
- **New model revision or model**: change `model.*` in `charts/colibri/values.yaml`
  and bump the chart (or override it in the HelmRelease). The
  download Job's name is a hash of its spec, so a new revision gets a new Job,
  and the server's init container waits for it before starting.
- **Chart change**: bump `version`. Renovate then bumps the pinned tag and
  digest in the homelab repo.

## Chart values

See [`charts/colibri/values.yaml`](charts/colibri/values.yaml); every key is commented.
