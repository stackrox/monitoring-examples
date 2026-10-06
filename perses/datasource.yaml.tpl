apiVersion: perses.dev/v1alpha2
kind: PersesDatasource
metadata:
  name: sample-stackrox-datasource
spec:
  client:
    tls:
      enable: false
  config:
    default: true
    display:
      name: RHACS Prometheus Datasource
    plugin:
      kind: PrometheusDatasource
      spec:
        proxy:
          kind: HTTPProxy
          spec:
            url: 'http://${PROMETHEUS_SERVICE}.${NAMESPACE}.svc:9090'
