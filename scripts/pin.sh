#!/usr/bin/env bash
# Writes pins into the charts' values.yaml and bumps the version of every chart
# it changed. Driven by pins.yaml; run from the repo root.
#
#   scripts/pin.sh [DIGESTS_DIR]
#
# DIGESTS_DIR holds one file per freshly built image, named after the image in
# pins.yaml and containing its digest (sha256:...). Models whose revision is not
# a commit SHA are resolved with scripts/hf_resolve.py (HF_RESOLVE overrides the
# command, for tests). Prints the changed charts, one per line, on stdout.
set -euo pipefail

digests=${1:-}
resolve=${HF_RESOLVE:-python3 scripts/hf_resolve.py}
changed=()

mark() { [[ " ${changed[*]-} " == *" $1 "* ]] || changed+=("$1"); }
log() { echo "$*" >&2; }

# replace_line FILE PATH VALUE: rewrite only the scalar at PATH, keeping
# comments and blank lines (yq -i would reformat the whole file).
replace_line() {
  python3 scripts/yaml_set.py "$1" "$2" "$3"
  [[ "$(yq "$2" "$1")" == "$3" ]] || { log "$1: failed to set $2"; exit 1; }
}

# set_value CHART PATH VALUE: write VALUE at PATH in the chart's values.yaml if it differs.
set_value() {
  local f="charts/$1/values.yaml" cur
  cur=$(yq "$2" "$f")
  if [[ "$cur" != "$3" ]]; then
    replace_line "$f" "$2" "$3"
    log "$1: $2 ${cur:-(empty)} -> $3"
    mark "$1"
  fi
}

# Image digests.
if [[ -n "$digests" && -d "$digests" ]]; then
  for img in $(yq '.images | keys | .[]' pins.yaml); do
    [[ -f "$digests/$img" ]] || continue
    d=$(tr -d '[:space:]' < "$digests/$img")
    [[ "$d" =~ ^sha256:[0-9a-f]{64}$ ]] || { log "bad digest for $img: $d"; exit 1; }
    n=$(yq ".images.\"$img\".targets | length" pins.yaml)
    for ((i = 0; i < n; i++)); do
      set_value "$(yq ".images.\"$img\".targets[$i].chart" pins.yaml)" \
                "$(yq ".images.\"$img\".targets[$i].path" pins.yaml)" "$d"
    done
  done
fi

# Model revisions (and file checksums).
n=$(yq '.models | length' pins.yaml)
for ((i = 0; i < n; i++)); do
  chart=$(yq ".models[$i].chart" pins.yaml)
  p=$(yq ".models[$i].path" pins.yaml)
  f="charts/$chart/values.yaml"
  rev=$(yq "$p.revision" "$f")
  [[ "$rev" =~ ^[0-9a-f]{40}$ ]] && continue
  repo=$(yq "$p.repo" "$f")
  file=$(yq "$p.file // \"\"" "$f")
  read -r sha sum < <($resolve "$repo" "$rev" ${file:+"$file"})
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { log "$repo@$rev did not resolve to a commit: $sha"; exit 1; }
  set_value "$chart" "$p.revision" "$sha"
  if [[ -n "$file" && "$(yq "$p | has(\"sha256\")" "$f")" == "true" ]]; then
    [[ "${sum:-}" =~ ^[0-9a-f]{64}$ ]] || { log "$repo@$sha/$file: no sha256"; exit 1; }
    set_value "$chart" "$p.sha256" "$sum"
  fi
done

# One patch bump per changed chart.
for chart in "${changed[@]}"; do
  f="charts/$chart/Chart.yaml"
  old=$(yq '.version' "$f")
  [[ "$old" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { log "$f: version $old is not x.y.z"; exit 1; }
  new="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))"
  replace_line "$f" .version "$new"
  log "$chart: version $old -> $new"
  echo "$chart"
done
