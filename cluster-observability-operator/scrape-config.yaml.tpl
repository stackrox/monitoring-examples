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
        - "central.${NAMESPACE}.svc:443"
  tlsConfig:
    ca:
      secret:
        key: ca.pem
        name: service-ca
    cert:
      secret:
        key: tls.crt
        name: sample-stackrox-prometheus-tls
    keySecret:
      key: tls.key
      name: sample-stackrox-prometheus-tls
