# Apply with --server-side after MonitoringStack/sample-rhacs has created this
# Prometheus resource. COO does not own these fields in the tested release.
# Keep this scrape class non-default: only the RHACS scrape should receive this
# audience-bound service account token. Apply after the MonitoringStack exists.
apiVersion: monitoring.rhobs/v1
kind: Prometheus
metadata:
  name: sample-rhacs
  namespace: ${NAMESPACE}
spec:
  volumes:
    - name: rhacs-identity
      projected:
        sources:
          - serviceAccountToken:
              audience: central.stackrox.io
              expirationSeconds: 3600
              path: token
  volumeMounts:
    - name: rhacs-identity
      mountPath: /var/run/secrets/rhacs
      readOnly: true
  scrapeClasses:
    - name: rhacs-m2m
      authorization:
        type: Bearer
        credentialsFile: /var/run/secrets/rhacs/token
