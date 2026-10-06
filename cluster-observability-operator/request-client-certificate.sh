#!/bin/bash
set -euo pipefail
umask 077
#
# Requests a client certificate for the monitoring stack's Prometheus service
# account and stores it in a TLS secret.
#
# The cluster's kubernetes.io/kube-apiserver-client signer issues it, so the
# issuer belongs to the client CA that the RHACS "OpenShift Platform Client
# Certificates" auth provider trusts. The certificate's common name carries the
# service account identity, which a role mapping in that provider authorizes.
#
# Approving the request needs permission to approve certificate signing
# requests for that signer. The signer sets the validity period, typically
# about a month, so rerun this script to renew the certificate in place.
#
# To test the installed credentials against RHACS:
#
#   kubectl get "secret/$CLIENT_SECRET" -o jsonpath='{.data.tls\.crt}' | base64 -d > tls.crt
#   kubectl get "secret/$CLIENT_SECRET" -o jsonpath='{.data.tls\.key}' | base64 -d > tls.key
#   curl --cert tls.crt --key tls.key "$ROX_API_ENDPOINT/v1/auth/status"

: "${NAMESPACE:?Set NAMESPACE to the namespace of the monitoring stack}"
: "${SERVICE_ACCOUNT:?Set SERVICE_ACCOUNT to the Prometheus service account name}"
: "${CLIENT_SECRET:?Set CLIENT_SECRET to the secret that holds the client certificate}"
TIMEOUT=${TIMEOUT:-300}
RENEW_BEFORE_DAYS=${RENEW_BEFORE_DAYS:-7}

subject="system:serviceaccount:$NAMESPACE:$SERVICE_ACCOUNT"
# X.509 limits a common name to 64 characters, and the whole identity has to fit.
if (( ${#subject} > 64 )); then
  echo "The identity $subject is ${#subject} characters; a common name allows 64." >&2
  echo "Shorten the MonitoringStack name or install it in a shorter namespace." >&2
  exit 1
fi

kube=(kubectl -n "$NAMESPACE")
# Certificate signing requests are cluster scoped, so the name carries the namespace.
request_name="$NAMESPACE-$SERVICE_ACCOUNT"

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT

# The installed Secret is authoritative while its certificate matches and lasts.
secret=$("${kube[@]}" get "secret/$CLIENT_SECRET" --ignore-not-found -o json)
if [[ -n "$secret" ]] &&
  jq -er '.data["tls.crt"]' <<< "$secret" | base64 --decode > "$temporary_dir/installed.crt" 2>/dev/null &&
  openssl x509 -in "$temporary_dir/installed.crt" -noout \
    -checkend "$((RENEW_BEFORE_DAYS * 86400))" >/dev/null 2>&1 &&
  [[ "$(openssl x509 -in "$temporary_dir/installed.crt" -noout -subject -nameopt RFC2253)" \
    == "subject=CN=$subject" ]]; then
  echo "Reusing the installed client certificate for $subject."
  exit 0
fi

openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$subject" \
  -keyout "$temporary_dir/tls.key" -out "$temporary_dir/request.pem" 2>/dev/null

# A leftover request from an earlier run holds the same name but another key.
kubectl delete certificatesigningrequest "$request_name" --ignore-not-found
kubectl create -f - <<EOF
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: $request_name
spec:
  signerName: kubernetes.io/kube-apiserver-client
  request: $(base64 < "$temporary_dir/request.pem" | tr -d '\n')
  usages:
    - client auth
EOF
# Only the Secret has to survive; the issued request is disposable.
trap 'rm -rf "$temporary_dir"; kubectl delete certificatesigningrequest "$request_name" --ignore-not-found >/dev/null' EXIT

kubectl certificate approve "$request_name"

# A condition wait cannot express issuance, which populates status.certificate.
deadline=$((SECONDS + TIMEOUT))
while true; do
  certificate=$(kubectl get "certificatesigningrequest/$request_name" -o jsonpath='{.status.certificate}')
  [[ -z "$certificate" ]] || break
  if (( SECONDS >= deadline )); then
    echo "Timed out waiting for $request_name to be issued" >&2
    exit 1
  fi
  sleep 2
done

base64 --decode <<< "$certificate" > "$temporary_dir/tls.crt"
[[ "$(openssl x509 -in "$temporary_dir/tls.crt" -noout -subject -nameopt RFC2253)" == "subject=CN=$subject" ]] || {
  echo "The issued certificate does not identify $subject" >&2
  exit 1
}

# Renewals replace the certificate in place; Prometheus reloads the mounted files.
"${kube[@]}" create secret tls "$CLIENT_SECRET" \
  --cert="$temporary_dir/tls.crt" --key="$temporary_dir/tls.key" \
  --dry-run=client -o yaml | "${kube[@]}" apply -f -

echo "Installed a client certificate for $subject, valid until" \
  "$(openssl x509 -in "$temporary_dir/tls.crt" -noout -enddate | cut -d= -f2)."
