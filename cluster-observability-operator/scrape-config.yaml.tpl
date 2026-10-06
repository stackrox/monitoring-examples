apiVersion: monitoring.rhobs/v1alpha1
kind: ScrapeConfig
metadata:
  name: sample-stackrox-scrape-config
  labels:
    app: central
spec:
  jobName: sample-stackrox-metrics
  scheme: HTTPS
  staticConfigs:
    - targets:
        - "${SCRAPE_SERVICE}.${NAMESPACE}.svc:443"
  tlsConfig:
    # The setup script picks the service and its CA together: central-ocp with
    # the cluster's service CA where RHACS publishes it, and central with the
    # StackRox CA otherwise. Both reach the same Central endpoint.
    ca: ${SCRAPE_CA}
    cert:
      secret:
        key: tls.crt
        name: sample-stackrox-prometheus-tls
    keySecret:
      key: tls.key
      name: sample-stackrox-prometheus-tls
