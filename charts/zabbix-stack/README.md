# zabbix-stack

Umbrella chart for Zabbix monitoring on Kubernetes: nodes, the cluster itself,
network gear, rack equipment, databases and services, configured entirely
through Helm values.

The chart contains no site data and no secrets. Defaults are neutral and the
device, database and service lists are empty. Credentials come from a Secret
you create (`existingSecret`), and addresses or URLs you don't want in plain
text can be passed in an encrypted values Secret (Flux `valuesFrom`).

## What it monitors

| Area | What | How |
| --- | --- | --- |
| Nodes | CPU, memory, disks, NICs, load, CPU temperature | zabbix-agent2 DaemonSet on every node (control plane included), active checks, autoregistration with a generated template |
| Kubernetes | Node conditions, pods, workloads, PVs, API server, kubelets, scheduler, controller manager | kube-state-metrics + `Kubernetes cluster state by HTTP` and `Kubernetes nodes by HTTP` |
| Network | Interfaces, errors, utilisation, reachability | SNMP v2c hosts from `configure.network.devices` |
| Rack | UPS/PDU and similar | SNMP or ICMP hosts from `configure.rack.devices` |
| Databases | PostgreSQL connections, replication, locks, slow queries, sizes | probe agent with the agent 2 PostgreSQL plugin |
| Services | HTTP availability, TLS expiry | web scenarios; `Website certificate by Zabbix agent 2` |

## Components

| Component | Source |
| --- | --- |
| Zabbix server, frontend, PostgreSQL 16 | [zabbix-community/zabbix](https://github.com/zabbix-community/helm-zabbix) 7.1.0 (Zabbix 7.0 LTS) |
| Node agents (DaemonSet, host network) | same subchart, `runAsDaemonSet` |
| kube-state-metrics | prometheus-community 8.7.0 |
| Probe agent (Deployment + NetworkPolicy) | this chart |
| Configure Job | this chart, `files/configure.py` |
| Triage CronJob (optional) | this chart, `files/triage.py` |
| ServiceAccount token for the Kubernetes templates | this chart |

## Configure Job

Makes Zabbix match `configure.*` through the API. It is idempotent and reruns
whenever its config or script changes (the Job name is a hash of both). It:

- rotates the Admin password from `zabbix` to secret key `admin-password`;
- creates the host groups, the node template (`configure.nodes.templateName`:
  Linux active + container path filters + CPU temperature triggers) and the
  autoregistration action;
- unlinks the Linux agent template from the built-in "Zabbix server" host;
- creates or updates the hosts in `configure.*`, tagged `managed-by=zabbix-stack`.
  Hosts removed from values are disabled, not deleted.

The Job's ConfigMap holds the desired state (addresses, URLs) but never
secrets: those are read from the mounted `existingSecret` at run time.

## Triage CronJob

Off by default (`triage.enabled`). Every `triage.schedule` (5 minutes) it takes
the open problems at or above `triage.minSeverity` that have no `[triage]`
message yet, up to `triage.maxBatch`, and sends them in one request to an
OpenAI-compatible chat endpoint (`triage.llm.url`, `triage.llm.model`). Each
verdict becomes a problem message, visible in the problem's history:

```
[triage] PAGE: PostgreSQL service down
[triage] TICKET (duplicate of 4711): same failed CronJob pod
```

It only adds messages: it never acknowledges, closes or changes severity, so
notification actions are unaffected. A problem without a usable verdict is
retried next run. Runs never overlap (`concurrencyPolicy: Forbid`).

It logs in as Admin (`admin-password`) and reads the LLM key from
`existingSecret` key `triage.llm.apiKeyKey` (`llm-api-key`). The LLM's own
NetworkPolicy must admit this namespace.

## Secret contract

`existingSecret` must contain `db-name`, `db-user`, `db-password`,
`admin-password`; `snmp-community` when SNMP devices are configured; and every
key named in `configure.databases.instances[].passwordKey`; `llm-api-key` when
`triage.enabled`.

## Install with Flux

The Flux wiring goes in `Sifungurux/homelab`: `clusters/rack/sources/zabbix-stack.yaml`
(OCIRepository, cosign-verified), `clusters/rack/releases/zabbix-stack.yaml` (HelmRelease
reading site values from a Secret created with kubectl), listed in
`clusters/rack/kustomization.yaml`. See colibri there.

## Things to know

- **Release `zabbix`, namespace `monitoring`** are assumed by the agents'
  server address in `values.yaml` (subchart values cannot be templated). If you
  use other names, override `zabbix.zabbixAgent.ZBX_SERVER_HOST`.
- **Agent metadata** in `zabbix.zabbixAgent.extraEnv` (`ZBX_METADATA`) must
  equal `configure.nodes.metadata`.
- **Privileged namespace** is required for the agent DaemonSet (hostNetwork,
  read-only host mounts).
- **Passive agent port** on nodes accepts only 127.0.0.1 by default; add the
  node subnet to `zabbix.zabbixAgent.ZBX_PASSIVESERVERS` so the kubelet's TCP
  probe isn't rejected.
- **kubeadm controller manager and scheduler** listen on 127.0.0.1 by default,
  so their discovered hosts are unreachable unless `bind-address` is changed.
- **TimescaleDB** must be chosen before the first install (see `values.yaml`).
- **Persistence** uses the default StorageClass unless one is set.

## CI

Run `helm dependency update charts/zabbix-stack` before `helm lint` and
`helm package`. `ci/full-values.yaml` exercises every configure section with
example data.
