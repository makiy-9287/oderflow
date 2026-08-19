# orderflow-signals

Asynchronous Binance USDT-M futures **signal engine** built on Smart Money Concepts,
volume profile, footprint delta and order-book depth. Signals are dispatched to Telegram.
It does **not** place orders.

Strategy specification: [`STRATEGY.md`](STRATEGY.md) · Parameters: [`config.yaml`](config.yaml)

---

## Quickstart (Ubuntu 22.04 / 24.04 VPS)

```bash
git clone <your-repo> orderflow-signals && cd orderflow-signals
chmod +x setup.sh && ./setup.sh

sudo nano /opt/ofsignals/.env            # add Binance (read-only) + Telegram credentials

cd /opt/ofsignals/app
sudo -u ofsignals PYTHONPATH=/opt/ofsignals/app/src \
     /opt/ofsignals/.venv/bin/python -m ofsignals.tools.preflight

sudo systemctl start ofsignals
journalctl -u ofsignals -f -o cat
```

### What `setup.sh` does

| Step | Why it matters |
|---|---|
| apt base + build toolchain | numpy/scipy wheels occasionally need to compile |
| Python ≥3.11 (deadsnakes fallback) | `asyncio.TaskGroup`, `tomllib`, faster exceptions |
| **chrony + UTC timezone** | Binance rejects signed requests outside `recvWindow`; drift is the #1 silent killer |
| `ufw` — SSH in only | The workload is egress-only; nothing should reach in |
| System user `ofsignals`, nologin shell | The daemon never runs as you or as root |
| venv at `/opt/ofsignals/.venv` | Isolated from system Python and apt upgrades |
| `.env` at 0600, outside the app dir | Secrets survive `rsync --delete` redeploys and stay unreadable |
| systemd unit + logrotate | Restart-on-failure, resource caps, filesystem hardening |

---

## Layout

```
orderflow-signals/
├── setup.sh                  # Phase 1 VPS bootstrap
├── requirements.txt
├── config.yaml               # every strategy threshold — code reads, never hardcodes
├── .env.example              # secrets template (never committed)
├── STRATEGY.md               # Agent 1 blueprint
├── deploy/
│   ├── ofsignals.service     # hardened systemd unit
│   └── logrotate.ofsignals
└── src/ofsignals/
    ├── main.py               # supervised async entrypoint  [Phase 1 ✅]
    ├── config.py             # YAML + env loader, secret masking  [✅]
    ├── logging_setup.py      # structlog + regex secret redaction  [✅]
    ├── exchange/
    │   ├── universe.py       # >10M USDT filter, depth probe, ranking  [✅]
    │   ├── rest_client.py    # OHLCV batch fetch + gap repair  [Phase 2]
    │   └── ws_streams.py     # depth/aggTrade/kline multiplexer  [Phase 2]
    ├── analytics/            # structure, liquidity, zones, volume profile,
    │                         # footprint, orderbook  [Phase 2]
    ├── strategy/             # base, mtf cascade, scalp, intraday, swing  [Phase 3]
    ├── risk/sizing.py        # structural SL, TP ladder, liq-aware leverage  [Phase 3]
    ├── notify/
    │   ├── telegram_bot.py   # token-bucket dispatcher, RetryAfter aware  [✅]
    │   └── formatter.py      # signal → HTML card  [Phase 4]
    ├── store/db.py           # aiosqlite journal + dedupe + outcomes  [Phase 4]
    └── tools/preflight.py    # clock/API/universe/Telegram smoke test  [✅]
```

---

## Operations

```bash
systemctl status ofsignals
journalctl -u ofsignals -f -o cat            # live
journalctl -u ofsignals --since "1 hour ago" -p err
sudo systemctl restart ofsignals

# redeploy after code changes
sudo rsync -a --delete --exclude .git --exclude .env ./ /opt/ofsignals/app/
sudo chown -R ofsignals:ofsignals /opt/ofsignals/app
sudo systemctl restart ofsignals
```

**Credential hygiene:** the Binance key needs *read* scope only — disable trading and
withdrawals, and whitelist the VPS IP. The engine emits signals; it never touches your
balance. Public market data works with no key at all (leave the fields blank); keys only
raise your REST weight ceiling.

---

## Roadmap

| Phase | Contents | State |
|---|---|---|
| 1 | VPS bootstrap, config/logging/secrets, universe filter, Telegram transport, systemd | **delivered** |
| 2 | WebSocket ingestion, OHLCV cache, six analytics modules | **delivered** |
| 3 | MTF cascade, three mode engines, confluence scorer, risk & leverage math | **delivered** |
| 4 | Formatter, journal, dedupe, outcome tracking, portfolio filters, live wiring | **delivered** |
| 5 | Tick-level backtest harness, score-band calibration | **not built — see below** |

## What has NOT been validated

The engine is complete and tested; the *strategy* is not validated. Specifically:

- **No backtest exists.** The confluence gates (72/75/78), the 1.8× sweep volume
  multiple, the 0.15 ATR penetration threshold and the 8-second wall persistence
  window are all reasoned guesses. None has been fitted to data.
- **Fees and slippage are not modelled** in the R:R gate. At taker 0.045% a
  0.26% stop means round-trip fees eat roughly a third of 1R. A "3.1R" signal is
  not a 3.1R outcome.
- **Sweep entries are the hardest fills to backtest.** Bar-level replay will
  overstate them badly; honest validation needs aggTrade-level data.

Run it read-only, let the journal accumulate 200+ resolved signals, then query
`store.performance()` for outcomes bucketed by score band. If 72–79 does not
outperform, raise the gate. That loop is the only thing that turns this from a
hypothesis into an edge.

---

## Disclaimer

Research and educational tooling. Generated signals are hypotheses produced by heuristics
that have not been validated on your data — backtest and paper-trade before risking capital.
Leveraged futures can lose more than the margin posted. Nothing here is financial advice.
