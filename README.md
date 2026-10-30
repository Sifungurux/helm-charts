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

## First-time setup

1. **Create the repo** `Sifungurux/helm-charts` and push this tree to `main`.
2. **Pin colibri**: put a full upstream commit SHA in `images/colibri/COLIBRI_REF`
   and push. The `image` workflow builds, pushes and signs
   `ghcr.io/sifungurux/colibri:<short-sha>` and prints its digest in the run summary.
3. **Publish the chart**: the `release` workflow runs on the same push
   (or trigger it by hand) and publishes `colibri` 0.1.0.
4. **Make both GHCR packages public**, or create a pull secret and uncomment
   `secretRef` in `deploy/flux/colibri/ocirepository.yaml`. New GHCR packages start private.
5. **Wire up Flux**: copy `deploy/flux/colibri/` into `Sifungurux/homelab`
   (e.g. `apps/colibri/`), fill in the image digest and the Hugging Face commit
   SHA in `helmrelease.yaml`, add the SOPS-encrypted API key, and reference it
   from the cluster (`deploy/flux/flux-kustomization.yaml` if nothing already
   reconciles `apps/`).
6. **Prepare the node**:
   ```bash
   grep -c avx2 /proc/cpuinfo        # on the worker; 0 = Dockerfile.slim's x86-64-v3 build won't run
   kubectl label node <worker> homelab/llm=true
   kubectl label ns <open-webui-ns> homelab/llm-client=true
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
- **Chart change**: bump `version`; Flux picks it up through the OCIRepository
  semver range (`>=0.1.0 <1.0.0`).

## Chart values

See [`charts/colibri/values.yaml`](charts/colibri/values.yaml); every key is commented.
