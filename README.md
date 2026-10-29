# helm-charts

Helm charts for the homelab rack. Flux builds them straight from this Git
repository; there is no chart registry yet (planned: Git + registry on the NAS).

| Chart | What it runs |
| --- | --- |
| [`colibri`](charts/colibri) | [colibri](https://github.com/JustVugg/colibri) LLM inference server (Qwen3-Coder-30B by default), OpenAI- and Anthropic-compatible API |

## How it flows

```
PR ──► lint.yaml   helm lint + render + kubeconform, version-bump check
homelab repo:      GitRepository (this repo, main) ─► HelmRelease ─► cluster
```

Flux upgrades a release when `version` in `Chart.yaml` changes on `main`, so every
chart change needs a version bump (CI enforces it).

## First-time setup (colibri)

1. **Build the image**: colibri publishes none. Build `docker/Dockerfile.slim` from
   upstream at the commit in `images/colibri/COLIBRI_REF`, push it to your registry,
   and note the digest. Blocked until the NAS registry exists.
2. **Wire up Flux**: copy `deploy/flux/colibri/` into `Sifungurux/homelab`, fill in
   the image repository and digest and the Hugging Face commit SHA in
   `helmrelease.yaml`, add the SOPS-encrypted API key, and reference it from the
   cluster (`deploy/flux/flux-kustomization.yaml` if nothing already reconciles `apps/`).
3. **Prepare the node**:
   ```bash
   kubectl label node <worker> homelab/llm=true
   kubectl label ns <open-webui-ns> homelab/llm-client=true
   ```

## Verify

```bash
flux get sources git colibri
flux get helmreleases colibri
kubectl -n ai get pods               # dl job completes, then the server starts
kubectl -n ai exec deploy/colibri -- coli doctor --model /model/qwen3-coder-30b
```

## Upgrading

- **New colibri build**: change `images/colibri/COLIBRI_REF`, build and push, then
  update `image.digest` in the HelmRelease.
- **New model revision or model**: change `model.*` in the HelmRelease. The
  download Job's name is a hash of its spec, so a new revision gets a new Job,
  and the server's init container waits for it before starting.
- **Chart change**: bump `version` and merge to `main`.

## Open points to confirm against `coli serve --help`

- The serve port (5000, per the upstream slim-image docs).
- Whether `--allowed-host` can repeat. The chart passes one flag per entry in
  `server.allowedHosts`; if repeating works, swap `"*"` for the Service DNS names.

## Chart values

See [`charts/colibri/values.yaml`](charts/colibri/values.yaml); every key is commented.
