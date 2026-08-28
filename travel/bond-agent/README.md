# zippie (travel agent)

Python control plane that:

1. Joins configured Wi-Fi SSIDs (Starlink / phone hotspots)
2. Brings up **one WireGuard tunnel per path** (own key + tunnel IP)
3. Probes latency/loss
4. Installs weighted multipath (or failover) default routes
5. Serves a small status dashboard

## Dev

```bash
cd travel/bond-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ZIPPIE_DRY_RUN=1 zippie once -c ../../configs/examples/zippie.toml
```

## Runtime layout

| Path | Purpose |
|---|---|
| `/etc/zippie/zippie.toml` | SSIDs, policy, home endpoint |
| `/etc/zippie/keys.json` | per-path WireGuard private keys |
| `/etc/zippie/wifi-secrets.json` | SSID → PSK |
| `/run/zippie/*.conf` | live wg-quick configs |
| `/run/zippie/status.json` | last status snapshot |
