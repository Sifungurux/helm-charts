#!/usr/bin/env bash
# Exits non-zero, naming what is missing, if any pin pins.yaml lists for CHART is
# empty or unresolved in its values.yaml. Charts not in pins.yaml always pass.
#
#   scripts/check-pins.sh CHART
set -euo pipefail

chart=$1
f="charts/$chart/values.yaml"
missing=()

for img in $(yq '.images | keys | .[]' pins.yaml); do
  n=$(yq ".images.\"$img\".targets | length" pins.yaml)
  for ((i = 0; i < n; i++)); do
    [[ "$(yq ".images.\"$img\".targets[$i].chart" pins.yaml)" == "$chart" ]] || continue
    p=$(yq ".images.\"$img\".targets[$i].path" pins.yaml)
    [[ "$(yq "$p" "$f")" =~ ^sha256:[0-9a-f]{64}$ ]] || missing+=("$p (image $img)")
  done
done

n=$(yq '.models | length' pins.yaml)
for ((i = 0; i < n; i++)); do
  [[ "$(yq ".models[$i].chart" pins.yaml)" == "$chart" ]] || continue
  p=$(yq ".models[$i].path" pins.yaml)
  [[ "$(yq "$p.revision" "$f")" =~ ^[0-9a-f]{40}$ ]] || missing+=("$p.revision")
  if [[ -n "$(yq "$p.file // \"\"" "$f")" && "$(yq "$p | has(\"sha256\")" "$f")" == "true" ]]; then
    [[ "$(yq "$p.sha256" "$f")" =~ ^[0-9a-f]{64}$ ]] || missing+=("$p.sha256")
  fi
done

if ((${#missing[@]})); then
  echo "$chart: unpinned: ${missing[*]}" >&2
  exit 1
fi
