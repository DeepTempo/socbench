/**
 * Ordered verifier chain: cheapest first, fail fast, feedback from the
 * first failure.
 *
 * The intended production shape:
 *
 *     OrderedVerifier([rescan, regressionTests, reproduction])
 *
 * A patch that still trips the detector never pays for a test run; a patch
 * that breaks the build never pays for an exploit reproduction. The
 * verdict's feedback comes from the check that failed, so the repair round
 * always sees the earliest, cheapest actionable failure.
 */
import { Effect } from "effect"

import { type Finding, Verdict } from "../models.js"
import type { GitWorkspace } from "../workspace.js"
import type { Verifier } from "./base.js"

export const OrderedVerifier = (
  verifiers: ReadonlyArray<Verifier>,
  name = "ordered",
): Verifier => {
  if (verifiers.length === 0) {
    throw new Error("OrderedVerifier requires at least one verifier")
  }
  return {
    name,
    verify: (finding: Finding, workspace: GitWorkspace) =>
      Effect.gen(function* () {
        const summaries: Array<string> = []
        for (const verifier of verifiers) {
          const verdict = yield* verifier.verify(finding, workspace)
          summaries.push(`${verdict.verifier}: ${verdict.summary}`)
          if (!verdict.passed) {
            return Verdict({
              passed: false,
              verifier: name,
              summary: summaries.join("; "),
              feedback: `[failed check: ${verdict.verifier}]\n${verdict.feedback}`,
              costHint: verdict.costHint,
            })
          }
        }
        return Verdict({
          passed: true,
          verifier: name,
          summary: summaries.join("; "),
        })
      }),
  }
}
