apiVersion: monitoring.rhobs/v1alpha1
kind: ScrapeConfig
metadata:
  name: sample-stackrox-scrape-config
  namespace: ${NAMESPACE}
  labels:
    app: central
spec:
  jobName: sample-stackrox-metrics
  scheme: HTTPS
  scrapeClass: rhacs-m2m
  staticConfigs:
    - targets:
        - "${SCRAPE_SERVICE}.${NAMESPACE}.svc:443"
      labels:
        rhacs_scrape: sample-stackrox-scrape-config
  tlsConfig:
    # Central's service certificate is verified with the OpenShift service CA.
    # The M2M client identity comes from the projected token, not this CA.
    ca: ${SCRAPE_CA}
