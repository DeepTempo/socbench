# patchloop

A verification-loop remediation harness for security findings: propose a patch, **verify it with the same machinery that raised the alert**, feed the verbatim failure back to the model, and retry — up to a small bounded number of attempts.

```
Finding (CVE / SAST / pentest)
        │
        ▼
   ┌─────────┐     kind: cve ──► CVE patching harness
   │ Router  │────►kind: pentest ──► PoC-backed harness
   └─────────┘     kind: ...  ──► your harness
        │
        ▼
┌──────────────────────── RemediationHarness ────────────────────────┐
│                                                                    │
│   propose patch ──► apply ──► verify ──► pass? ──► verified diff   │
│        ▲                        │                                  │
│        │   verbatim failure     │ fail                             │
│        └────────────────────────┘  (repair ON TOP of current       │
│             (≤ N attempts)          patched state, not a reset)    │
│                                                                    │
│   verify = OrderedVerifier([ rescan,          ← cheap, default     │
│                              regression tests,← don't break it     │
│                              reproduction ])  ← expensive, PoC     │
└────────────────────────────────────────────────────────────────────┘
```



## Why this exists

Many benchmarks effectively evaluate zero-shot model performance, which makes as a proxy for intelligence and preventing brute-force gaming. But that hides the system-design result that matters in production: **a single round of execution feedback is worth more than every prompting and harness improvement combined.**

Evidence, from [PatchEval](https://github.com/bytedance/PatchEval) runs with a direct-edit harness:


| Setup                     | Strict score    | Note                                        |
| ------------------------- | --------------- | ------------------------------------------- |
| No feedback               | 77/230 = 33.5%  | GPT-5 w/ custom direct-edit harness         |
| One verify-feedback round | 132/230 = 57.4% | **+25.2pp from the loop alone**, ~$0.17/CVE |


(The feedback-round number is not leaderboard-comparable, but that's the point: the benchmark constraint excludes the thing that makes a production system work. In a SOC you generally have the detector that raised the alert; using it is free alpha.)

The second observation: for a scoped task like "patch this finding", a specialized harness beats the standard general-purpose agent loop (read/write/edit/bash round-trips) on cost and predictability. One structured direct-edit request per attempt, with the intelligence budget spent on the verification loop instead.

`patchloop` packages both observations as reusable scaffolding, with the benchmark's built-in evaluator replaced by the thing you actually have in a security operation: **your own detectors.**

## The Verifier seam

`Verifier` is the interface the project exists for. Ship your own; three production-shaped ones are included:


| Verifier                 | Cost      | Pass condition                                                                      |
| ------------------------ | --------- | ----------------------------------------------------------------------------------- |
| `RescanVerifier`         | cheap     | re-run the detector that raised the finding; the finding id is no longer reported   |
| `RegressionTestVerifier` | moderate  | project tests/build still pass (a patch that deletes the feature "passes" a rescan) |
| `ReproductionVerifier`   | expensive | run the PoC / exploit; it **no longer works**                                       |


`OrderedVerifier` chains them cheapest-first and fails fast: a patch that still trips the scanner never pays for a test run; a patch that breaks the build never pays for an exploit reproduction. Whatever check fails supplies the verbatim output for the repair round.

A `Verifier` is one method returning an Effect. Because verification is a data-fetching effect, timeouts, interruption, and typed failures come from the runtime: `runCommand` kills the whole process group when its budget expires, so a hung scanner or PoC cannot outlive its verdict.

```typescript
import { Effect } from "effect"
import {
  DirectEditPatcher, Finding, GitWorkspace, OrderedVerifier,
  RegressionTestVerifier, RemediationHarness, RescanVerifier,
} from "patchloop"

const harness = RemediationHarness({
  patcher: DirectEditPatcher({ model: "anthropic/claude-sonnet-4.5" }),
  verifier: OrderedVerifier([
    RescanVerifier({ commandTemplate: "trivy fs --scanners vuln --quiet {workspace}" }),
    RegressionTestVerifier({ commandTemplate: "make test" }),
  ]),
  maxAttempts: 3,
})

const finding = new Finding({
  id: "CVE-2099-0001", kind: "cve",
  title: "path traversal in download endpoint",
  description: "...advisory text...",
  locations: [], // empty → the patcher localizes from `git ls-files`
})

const program = Effect.gen(function* () {
  const workspace = yield* GitWorkspace.make("/path/to/checkout")
  const result = yield* harness.run(finding, workspace)
  if (result.success) yield* openPullRequest(result.finalDiff) // your side of the fence
})
```



## The Router seam

In an AI SOC, an upstream triage agent decides a finding is auto-remediable and hands a normalized `Finding` to the router, which selects the specialized harness for that finding kind — instead of handing it to a general-purpose coding agent:

```typescript
const router = Router()
router.register("cve", cveHarnessFactory)
router.register("pentest", pentestHarnessFactory) // PoC-backed
const result = yield* router.remediate(finding, workspace)
```

`Finding` is an Effect `Schema.Class`, so a finding arriving as untrusted JSON from a queue or webhook decodes with `decodeFinding` and real validation — a malformed payload is a typed parse error, not a landmine three stages downstream. An unroutable finding kind is a typed `UnroutedFindingError`.

See `[examples/remediate-cve.ts](examples/remediate-cve.ts)` for a complete routed run. The registry is where new harness families land over time — dependency bumps, IaC misconfigurations, detection-rule tuning — each with its own patcher and its own verifier chain.

## Why Effect

The loop is a small state machine over fallible, interruptible I/O, which is exactly what Effect models well — and using it is a deliberate, opinionated choice, not decoration:

- **Typed errors, not string-matched exceptions.** `PatchApplicationError`, `GenerationError`, `WorkspaceError`, and `CommandTimeoutError` are distinct tagged types. The loop reacts to each precisely: a generation error is retried, a bad-apply error resets the workspace, a genuine workspace/git failure aborts. None of that leans on parsing a `message`.
- **A crashing verifier can never pass a patch.** The harness runs verification under `catchAllCause`, so *any* failure — typed error or unexpected defect — becomes a failed verdict carrying the pretty-printed cause. This is a safety property, and it is enforced by the type system rather than by remembering to wrap a `try`.
- **Interruption-safe timeouts for free.** Verifier commands run detached and are killed by process group when their `Duration` budget expires.
- **Schema-validated boundaries.** Findings and model output are decoded, not cast, so untrusted JSON fails loudly at the edge.



## Loop rules (learned on the benchmark, encoded in `harness.ts`)

1. **An empty patch is never a success.** The largest silent-failure bucket in early runs was "model returned nothing, harness recorded a submit."
2. **Repair on top of the patched state.** The repair prompt carries the current (already patched) files, the cumulative diff, and the verbatim failure output. No reset between attempts.
3. **Reset only when a patch fails to apply** — then the workspace no longer matches what the model believes, so start clean and say so in feedback.
4. **A crashing verifier is a failure, never a pass.**
5. **Bounded attempts** (default 3). The first feedback round buys the most.

These are pinned by the test suite (`test/harness.test.ts`), which runs with no LLM and no scanners — scripted patchers and marker verifiers exercise the whole loop against a throwaway git repo:

```bash
npm install
npm test
```



## Install / run

```bash
npm install
export OPENROUTER_API_KEY=...   # reference patcher; any OpenAI-compatible endpoint works
npx tsx examples/remediate-cve.ts --repo /path/to/checkout \
    --finding examples/finding-cve.json
```

The reference `DirectEditPatcher` talks to OpenRouter by default (`baseUrl` is configurable). The `Patcher` interface is one method — swapping in your own provider, or a full agent, is a few lines.

Requires Node 20+. Written in TypeScript on [Effect](https://effect.website); `npm run build` emits JS + `.d.ts` to `dist/`.

Python scaffold (archived on the `python-scaffold` branch)

```bash
pip install -e .
export OPENROUTER_API_KEY=...   # reference patcher; any OpenAI-compatible endpoint works
python examples/remediate_cve.py --repo /path/to/checkout \
    --finding examples/finding_cve.json
```

The reference `DirectEditPatcher` talks to OpenRouter by default (`base_url` is configurable). The `Patcher` protocol is one method — swapping in your own provider, or a full agent, is a few lines.

## License

MIT