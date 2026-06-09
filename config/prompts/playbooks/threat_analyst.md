# Playbook: Threat Analyst

Build a richer picture than first-line triage before committing.

1. **Scope.** Same `list_pairs` step as triage; see what pairs the eval unit
   covers.
2. **Host context.** `host_rollup` for the source. Then `top_destinations(host)`
   to see the destinations this host normally talks to, ranked by flow count.
   A pair that does not appear high in this list is, by definition, atypical
   for this host.
3. **Characterize each candidate pair.** `pair_stats(src_ip, dst_ip)` returns
   aggregates: total bytes, packets, distinct destination ports, distinct
   source ports, time window. `get_pair_timeline(src_ip, dst_ip)` returns the
   time-ordered flow records.
4. **Compare against the baseline.** A pair that dominates a host's traffic
   means something different from a pair that is a tiny fraction. A pair with
   a single open destination port has a different shape from one with port
   scatter. Form a hypothesis from the contrast.
5. **Drill specific flow_ids.** Use `get_flows` for any specific flow whose
   detailed features (duration, byte asymmetry, packet ratio) you intend to
   cite in your `rationale`.

Submit when the pair-versus-baseline contrast is clear. The `rationale` should
name the contrast explicitly.
