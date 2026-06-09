# benchmark-v0 dataset provenance

Combined, canonical NetFlow parquet built by `scripts/build_from_gcs.py` from
the benign + Stratosphere malware captures under
`gs://tempo-datasets-001/benchmark-v0/`.

| Field | Value |
|---|---|
| Canonical artifact | `gs://tempo-datasets-001/benchmark-v0/combined/benchmark-v0-canonical.parquet` |
| Local staging path | `data/benchmark-v0.parquet` |
| SHA-256 | `6ce1b3f0d8aacdd08b083eeddd618db13554bad50c5d30cea356a4ac6dc33b0f` |
| Bytes | 12,188,994 |
| Flows | 757,641 (282,595 malicious / 475,046 benign — 37.3% malicious) |

## Sources

| `source` | Origin | Flows | Malicious |
|---|---|--:|--:|
| `normal-https-website` | `benign/normal-HTTPS-website-traffic.parquet` | 51 | 0 |
| `normal-at-home-linux` | `benign/normal-at-home-user-traffic-linux.parquet` | 9,797 | 0 |
| `normal-university-linux` | `benign/normal-university-user-traffic-linux.parquet` | 7,769 | 0 |
| `normal-xdsl-linux` | `benign/normal-xDSL-user-linux.parquet` | 9,907 | 0 |
| `malware_1_1` | `stratosphere/malware_1_1.parquet` | 632,787 | 191,299 |
| `malware_34_1` | `stratosphere/malware_34_1.parquet` | 7,209 | 5,318 |
| `malware_3_1` | `stratosphere/malware_3_1.parquet` | 85,884 | 83,922 |
| `malware_8_1` | `stratosphere/malware_8_1.parquet` | 4,237 | 2,056 |

## Normalization rules

- Labels are **per-flow** from each capture's `Label` (numeric) and `attack_type`
  columns — preserved as `Label` and `Attack`. The malware captures contain
  individually-labeled benign background flows.
- `timestamp` (Datetime[ns]) → `ts_start` epoch seconds (float).
- bytes: `fwd_bytes` → `bytes_out`, `bwd_bytes` → `bytes_in`.
- packets: `fwd_pkts`/`bwd_pkts` → `pkts_out`/`pkts_in`; **null** for benign
  captures (which carry no packet columns).
- `protocol` rendered as string; `"unknown"` where absent (benign captures).
- `flow_dur` → `flow_duration_ms` (unit carried as-is).
- `sampling_rate` defaulted to 1.
- IPs are kept **raw** (no per-capture namespacing): identical private IPs
  appearing in different captures share host/pair identity downstream.
- A `source` column records the originating capture (note: the current index
  pipeline selects only canonical + label columns, so `source` lives in the
  combined parquet but is not yet propagated into `flows.parquet`).

## Regenerate

```bash
python scripts/build_from_gcs.py \
    --output gs://tempo-datasets-001/benchmark-v0/combined/benchmark-v0-canonical.parquet \
    --local-staging data/benchmark-v0.parquet \
    --project prod-loglm
```
