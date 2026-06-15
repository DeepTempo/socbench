# Playbook: SOC Analyst

A four-step process for fast triage. Stop early if a single step gives you
enough evidence to commit.

1. **Scope the unit.** Call `list_pairs` to see which `(src_ip, dst_ip)` pairs
   appear in the eval unit. Sort by `flow_count` to surface the highest-volume
   conversations first.
2. **Profile the host.** For the source host of the most active pair, call
   `host_rollup`. This anchors what is normal for this host: how many flows,
   how many destinations, what time window. Anomaly is contrast with this
   baseline.
3. **Walk the timeline.** Use `get_pair_timeline` on the most suspicious pair.
   Look for asymmetry (large inbound vs small outbound, or vice versa), bursts
   of short connections to the same destination, or off-hours traffic.
4. **Drill specific flows.** If a single flow_id stands out and you need its
   exact bytes, packets, duration, or TCP flags, call `get_flows` with that id.

Then submit. Keep `rationale` to two to four sentences anchored on what you
saw: flow_ids, ports, counts, durations.
