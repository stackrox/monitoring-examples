#!/usr/bin/env bash
# Opt-in smoke checks for an already installed example; no provisioning.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash tests/e2e.sh coo|prometheus-operator [namespace]

Check an already installed example in the current kubectl context.
Namespace defaults to the context's namespace, then "default".
SMOKE_TIMEOUT_SECONDS: timeout per readiness/check stage (default: 300).
Requires bash, kubectl, curl, jq, and permission to port-forward Prometheus.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
case "$1" in
    coo) prometheus=sample-rhacs; group=monitoring.rhobs ;;
    prometheus-operator) prometheus=sample-stackrox-prometheus-server; group=monitoring.coreos.com ;;
    *) usage >&2; exit 2 ;;
esac
path=$1

for command in kubectl curl jq mktemp sed sleep; do
    command -v "$command" >/dev/null || die "Required command not found: $command"
done
timeout=${SMOKE_TIMEOUT_SECONDS:-300}
if ! [[ $timeout =~ ^[1-9][0-9]{0,4}$ ]] || (( timeout > 86400 )); then
    die "SMOKE_TIMEOUT_SECONDS must be an integer from 1 to 86400"
fi
context=$(kubectl config current-context)
[[ -n $context ]] || die "Select a kubectl context first"
namespace=${2:-$(kubectl config view --minify -o 'jsonpath={..namespace}')}
namespace=${namespace:-default}
[[ ${#namespace} -le 63 && $namespace =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
    die "Invalid namespace: $namespace"
kube=(kubectl --context "$context" --namespace "$namespace")
echo "Smoke check: path=$path context=$context namespace=$namespace"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/stackrox-smoke.XXXXXX")
forward_pid=
cleanup() {
    if [[ -n $forward_pid ]]; then
        kill "$forward_pid" 2>/dev/null || true
        wait "$forward_pid" 2>/dev/null || true
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Check the named CR and its exact StatefulSet, rather than any ready Prometheus.
"${kube[@]}" get "prometheuses.$group" "$prometheus" -o json > "$tmp/prometheus.json"
prometheus_uid=$(jq -er '.metadata.uid' "$tmp/prometheus.json")
statefulset="prometheus-$prometheus"
"${kube[@]}" get statefulset "$statefulset" -o json > "$tmp/statefulset.json"
jq -e --arg uid "$prometheus_uid" '
    (.spec.replicas > 0) and
    any(.metadata.ownerReferences[]?; .uid == $uid and .kind == "Prometheus")
' "$tmp/statefulset.json" >/dev/null || die "Expected a nonzero StatefulSet owned by $prometheus"
"${kube[@]}" rollout status "statefulset/$statefulset" --timeout="${timeout}s"
statefulset_uid=$(jq -er '.metadata.uid' "$tmp/statefulset.json")
"${kube[@]}" get pods -o json > "$tmp/pods.json"
pod=$(jq -er --arg uid "$statefulset_uid" '
    [.items[] | select(.metadata.deletionTimestamp == null) |
     select(any(.metadata.ownerReferences[]?; .uid == $uid)) |
     select(any(.status.conditions[]?; .type == "Ready" and .status == "True")) |
     .metadata.name] | sort | .[0] // empty
' "$tmp/pods.json") || die "No Ready pod owned by $statefulset"

# Let kubectl allocate a free loopback port; do not use the shared operated Service.
"${kube[@]}" port-forward --address=127.0.0.1 "pod/$pod" :9090 > "$tmp/port-forward.log" 2>&1 &
forward_pid=$!
deadline=$((SECONDS + timeout))
port=
while true; do
    if ! kill -0 "$forward_pid" 2>/dev/null; then
        cat "$tmp/port-forward.log" >&2
        die "Prometheus port-forward exited"
    fi
    port=$(sed -nE 's/^Forwarding from 127\.0\.0\.1:([0-9]+) -> 9090$/\1/p' "$tmp/port-forward.log")
    [[ -z $port ]] || break
    (( SECONDS < deadline )) || die "Timed out starting Prometheus port-forward"
    sleep 1
done
base="http://127.0.0.1:$port"

retry() {
    local description=$1 deadline=$((SECONDS + timeout))
    shift
    while true; do
        kill -0 "$forward_pid" 2>/dev/null || { cat "$tmp/port-forward.log" >&2; die "Port-forward exited"; }
        if "$@"; then
            echo "PASS: $description"
            return 0
        fi
        (( SECONDS < deadline )) || { echo "Timed out: $description" >&2; return 1; }
        sleep 2
    done
}

http() {
    curl --fail --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 15 "$@"
}
retry "Prometheus /-/ready" http "$base/-/ready" --output /dev/null

check_targets() {
    http "$base/api/v1/targets?state=active" --output "$tmp/targets.json" || return 1
    # COO can expose an operator-generated job label. Match Central's endpoint,
    # then use the returned job/instance labels in queries instead of guessing.
    # The setup scrapes central-ocp where RHACS publishes it, central otherwise.
    jq -e --arg ns "$namespace" --arg path "$path" '
        select(.status == "success") |
        [.data.activeTargets[] |
         select(.scrapeUrl | test("^https://central(-ocp)?(\\." + $ns + "(\\.svc(\\.[a-zA-Z0-9.-]+)?)?)?(:443)?/metrics$")) |
         select($path == "coo" or .labels.job == "sample-stackrox-metrics")] |
        select(length > 0)
    ' "$tmp/targets.json" > "$tmp/central-targets.json" || return 1
    jq -e 'all(.[];
        .health == "up" and (.lastError // "") == "" and
        (.labels.job | type == "string" and length > 0) and
        (.labels.instance | type == "string" and length > 0))
    ' "$tmp/central-targets.json" >/dev/null
}
if ! retry "sample-stackrox-metrics Central target is healthy" check_targets; then
    jq '.data.activeTargets[]? | {scrapeUrl, scrapePool, labels, health, lastError}' "$tmp/targets.json" >&2 || true
    die "Central scrape failed; check the installed authentication, authorization, and TLS configuration"
fi
jq '.[] | {scrapeUrl, scrapePool, labels, health}' "$tmp/central-targets.json"

check_query() {
    http --get "$base/api/v1/query" --data-urlencode "query=$1" --output "$tmp/query.json" || return 1
    jq -e '
        .status == "success" and .data.resultType == "vector" and
        (.data.result | length > 0) and
        all(.data.result[]; .value[1] | test("^-?[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?$"))
    ' "$tmp/query.json" >/dev/null
}
# JSON string escaping is also valid for these PromQL string label values.
selectors=$(jq -r '.[] | "{job=\(.labels.job | tojson),instance=\(.labels.instance | tojson)}"' "$tmp/central-targets.json")
while IFS= read -r selector; do
    for metric in rox_central_health_cluster_info rox_central_cfg_total_policies rox_central_cert_exp_hours; do
        if ! retry "$metric$selector has samples" check_query "$metric$selector"; then
            cat "$tmp/query.json" >&2 || true
            die "Missing fixed metric $metric from Central; check RHACS permissions and metric gathering"
        fi
    done
done <<< "$selectors"

if [[ $path == coo ]]; then
    # Perses v1alpha2 uses Available/Degraded, not Ready. Some operator versions
    # omit observedGeneration; enforce freshness when it is actually reported.
    check_perses() {
        "${kube[@]}" get "$1" "$2" -o json > "$tmp/perses.json" || return 1
        jq -e '
            .metadata.generation as $generation |
            any(.status.conditions[]?;
                .type == "Available" and .status == "True" and
                ((.observedGeneration // 0) == 0 or .observedGeneration >= $generation)) and
            any(.status.conditions[]?; .type == "Degraded" and .status == "False")
        ' "$tmp/perses.json" >/dev/null
    }
    for resource in persesdatasources.perses.dev persesdashboards.perses.dev; do
        case "$resource" in
            persesdatasources*) name=sample-stackrox-datasource ;;
            persesdashboards*) name=sample-stackrox-dashboard ;;
        esac
        if ! retry "$resource/$name Available=True, Degraded=False" check_perses "$resource" "$name"; then
            jq '{apiVersion, kind, metadata: {name: .metadata.name, generation: .metadata.generation}, status}' "$tmp/perses.json" >&2 || true
            die "Perses reconciliation not confirmed; inspect this resource's status and operator version"
        fi
        jq '{kind, name: .metadata.name, status}' "$tmp/perses.json"
    done
fi

echo "PASS: $path installed-setup smoke checks (no installation or UI render validation)"
