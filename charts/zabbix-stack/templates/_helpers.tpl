{{- define "zs.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Prefix for this chart's own resources: the release name. */}}
{{- define "zs.prefix" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" }}
{{- end }}

{{/* Mirrors zabbix-community's "zabbix.fullname" so we can address its services. */}}
{{- define "zs.subFullname" -}}
{{- if contains "zabbix" .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-zabbix" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "zs.commonLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: {{ include "zs.name" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "zs.labels" -}}
{{ include "zs.commonLabels" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "zs.probe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "zs.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: probe-agent
{{- end }}

{{- define "zs.serviceDomain" -}}
{{- printf "%s.svc.cluster.local" .Release.Namespace }}
{{- end }}

{{- define "zs.webUrl" -}}
{{- printf "http://%s-zabbix-web.%s:%v" (include "zs.subFullname" .) (include "zs.serviceDomain" .) (dig "zabbixWeb" "service" "port" 80 .Values.zabbix) }}
{{- end }}

{{- define "zs.probeHost" -}}
{{- printf "%s-probe-agent.%s" (include "zs.prefix" .) (include "zs.serviceDomain" .) }}
{{- end }}

{{- define "zs.tokenSecretName" -}}
{{- printf "%s-k8s-api-token" (include "zs.prefix" .) }}
{{- end }}

{{- define "zs.agentImage" -}}
{{- $tag := default .Values.zabbix.zabbixImageTag .Values.probeAgent.image.tag }}
{{- printf "%s:%s" .Values.probeAgent.image.repository $tag }}
{{- end }}

{{- define "zs.configureImage" -}}
{{- with .Values.configure.image }}
{{- if .digest }}
{{- printf "%s:%s@%s" .repository .tag .digest }}
{{- else }}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Desired state handed to configure.py as JSON. Everything the script needs that
is not a secret lives here; secrets are read from the mounted Secret.
*/}}
{{- define "zs.configureState" -}}
{{- $c := .Values.configure -}}
{{- $state := dict
  "apiUrl" (printf "%s/api_jsonrpc.php" (include "zs.webUrl" .))
  "rotateAdminPassword" $c.rotateAdminPassword
  "hostGroups" $c.hostGroups
  "nodes" $c.nodes
  "kubernetes" (merge (dict
      "stateEndpoint" (printf "%s-kube-state-metrics" .Release.Name)
      "nodesEndpoint" (printf "%s-zabbix-agent" (include "zs.subFullname" .))
      "enabled" (and $c.kubernetes.enabled (index .Values "kube-state-metrics" "enabled"))
    ) (deepCopy $c.kubernetes))
  "network" $c.network
  "rack" $c.rack
  "databases" (merge (dict
      "probeHost" (include "zs.probeHost" .)
      "probeEnabled" .Values.probeAgent.enabled
      "selfConnString" (printf "tcp://%s-postgresql.%s:%v" (include "zs.subFullname" .) (include "zs.serviceDomain" .) (dig "postgresql" "service" "port" 5432 .Values.zabbix))
      "selfMonitor" (and $c.databases.selfMonitor (dig "postgresql" "enabled" true .Values.zabbix))
    ) (deepCopy $c.databases))
  "services" (merge (dict "probeHost" (include "zs.probeHost" .) "probeEnabled" .Values.probeAgent.enabled) (deepCopy $c.services))
-}}
{{- toJson $state }}
{{- end }}
