# Grafana Cloud wiring for Alloy (compose variables), as 1Password secret
# references (vault AkkiaBot, item staging-grafana, shared by dev and staging).
# Appended to `.env` on the staging host, see docs/operations.md §8:
#
#   sed -i '/^GRAFANA_CLOUD_/d' .env && op inject -i grafana.env.tpl >> .env
#
# Keep the reference prefix out of comments (op inject resolves it everywhere).
GRAFANA_CLOUD_PROMETHEUS_URL=op://AkkiaBot/staging-grafana/prometheus_url
GRAFANA_CLOUD_PROMETHEUS_USER=op://AkkiaBot/staging-grafana/prometheus_user
GRAFANA_CLOUD_LOKI_URL=op://AkkiaBot/staging-grafana/loki_url
GRAFANA_CLOUD_LOKI_USER=op://AkkiaBot/staging-grafana/loki_user
GRAFANA_CLOUD_TOKEN=op://AkkiaBot/staging-grafana/cloud_token
