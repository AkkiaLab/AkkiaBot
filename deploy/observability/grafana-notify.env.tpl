# `op run --env-file` template for `scripts/grafana_sync.py notifications`
# (alert dispatch contact point, docs/grafana-alerting.md). Grafana access as
# in grafana-sync.env.tpl; the relay endpoint and its HMAC secret come from the
# 1Password item grafana-webhook (vault AkkiaBot).
GRAFANA_URL=op://AkkiaBot/staging-grafana/grafana_url
GRAFANA_SA_TOKEN=op://AkkiaBot/staging-grafana/grafana_sa_token
GRAFANA_DISPATCH_WEBHOOK_URL=op://AkkiaBot/grafana-webhook/relay_url
GRAFANA_DISPATCH_HMAC_SECRET=op://AkkiaBot/grafana-webhook/hmac_secret
