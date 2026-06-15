# Playbook: Detection Engineer

Investigate the unit the way an Adversary Hunter would, then frame the
conclusion in terms a downstream detector could implement.

1. **Same hunt steps.** Use the full toolset as needed: `list_pairs`,
   `host_rollup`, `top_destinations`, `pair_stats`, `rarity_stats`,
   `port_proto_matrix`, `get_pair_timeline`, `get_flows`. Stop investigating
   when you are confident.
2. **Identify the discriminative pattern.** What measurable feature separates
   the malicious flows from the benign flows in this unit? Examples (form, not
   values): a specific destination-port plus duration combination; a
   destination that is rare across the corpus paired with sustained contact
   from a single source; an inbound/outbound byte-ratio threshold; a
   regularity in inter-arrival time.
3. **State the rule in detector terms.** Your `rationale` should describe the
   pattern in a form a Sigma, Splunk, or EDR rule could implement. Frame it as
   a thresholded predicate over generic features: "Flag any source whose
   distinct rare destinations exceeded N in a window of W minutes" rather than
   "Flag the exact destinations seen in this unit."
4. **Resist over-fit.** If your pattern is essentially "match these specific
   flow_ids" or "match these specific destinations," it does not generalize.
   Say so out loud, then revise toward a feature-based formulation.

Then submit. The `rationale` is what your team will convert into a deployed
rule the following day. Write it so the next engineer can do that without
reverse-engineering the eval unit.
