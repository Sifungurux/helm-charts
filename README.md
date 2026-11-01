# helm-charts

Helm charts for the homelab rack, published as signed OCI artifacts to
`oci://ghcr.io/sifungurux/charts` and consumed by Flux.

| Chart | What it runs |
| --- | --- |
| [`colibri`](charts/colibri) | [colibri](https://github.com/JustVugg/colibri) LLM inference server (Qwen3-Coder-30B by default), OpenAI- and Anthropic-compatible API |

## How it flows

```
PR ──► lint.yaml        helm lint + render + kubeconform, version-bump check
main ─► release.yaml    package changed charts ─► push to GHCR (OCI) ─► cosign sign (keyless)
main ─► image.yaml      build colibri at images/colibri/COLIBRI_REF ─► push ─► sign + SBOM + provenance
                                      │
homelab repo:  OCIRepository (verify: cosign, this repo's release workflow) ─► HelmRelease ─► cluster
```

Charts are only published when `version` in `Chart.yaml` changes, so every
chart change needs a version bump (CI enforces it).

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

## Upgrading

- **New colibri build**: change `images/colibri/COLIBRI_REF`, merge, then
  update `image.digest` in the HelmRelease.
- **New model revision or model**: change `model.*` in the HelmRelease. The
  download Job's name is a hash of its spec, so a new revision gets a new Job,
  and the server's init container waits for it before starting.
- **Chart change**: bump `version`. Renovate then bumps the pinned tag and
  digest in the homelab repo.

## Chart values

See [`charts/colibri/values.yaml`](charts/colibri/values.yaml); every key is commented.
