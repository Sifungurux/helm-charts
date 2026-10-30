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
