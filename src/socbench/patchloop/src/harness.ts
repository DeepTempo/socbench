/**
 * The remediation loop: propose -> apply -> verify -> feed failure back -> repair.
 *
 * Loop rules, learned the expensive way on PatchEval full-230 runs:
 *
 * 1. An empty patch is never a success. Count it as a failed attempt and
 *    retry with explicit feedback saying the previous response changed
 *    nothing.
 * 2. Repairs happen ON TOP of the current patched state, not from a reset.
 *    The repair prompt gets the current files, the cumulative diff, and the
 *    verbatim failure output. One round of this feedback was worth +25.2pp
 *    strict on PatchEval — more than every prompting improvement combined.
 * 3. Reset only when patch application itself fails (bad search text),
 *    because then the workspace state no longer matches what the model
 *    believes.
 * 4. A crashing verifier is a failure, never a pass — every verifier error
 *    (typed failure or defect) maps to a failed verdict via catchAllCause.
 * 5. Bounded attempts. The first feedback round buys the most; returns fall
 *    off fast after 2-3.
 */
import { Cause, type Either, Effect } from "effect"

import {
  type Attempt,
  type EditPatch,
  type Finding,
  type GenerationError,
  isEmptyPatch,
  type PatchApplicationError,
  type RemediationResult,
  Verdict,
  type WorkspaceError,
} from "./models.js"
import type { Verifier } from "./verify/base.js"
import type { GitWorkspace } from "./workspace.js"

export interface Patcher {
  /**
   * Propose exact edits for a finding. `feedback` is null on the first
   * attempt; afterwards it carries the failure context assembled by the
   * loop (verdict feedback + current diff).
   */
  readonly propose: (
    finding: Finding,
    workspace: GitWorkspace,
    feedback: string | null,
  ) => Effect.Effect<EditPatch, GenerationError>
}

export interface HarnessOptions {
  readonly patcher: Patcher
  readonly verifier: Verifier
  readonly maxAttempts?: number
}

export interface RemediationHarness {
  readonly run: (
    finding: Finding,
    workspace: GitWorkspace,
  ) => Effect.Effect<RemediationResult, WorkspaceError>
}

const feedbackFor = (
  failureOutput: string,
  workspace: GitWorkspace,
): Effect.Effect<string, WorkspaceError> =>
  Effect.gen(function* () {
    const diff = yield* workspace.diff()
    const parts = ["## Verification failure output", failureOutput]
    if (diff.trim() !== "") {
      parts.push(
        "## Your patch so far (currently applied to the workspace)",
        diff,
        "Repair on top of this state; edits apply to the current " +
          "(already patched) file contents.",
      )
    }
    return parts.join("\n\n")
  })

export const RemediationHarness = (
  options: HarnessOptions,
): RemediationHarness => {
  const { patcher, verifier, maxAttempts = 3 } = options

  const run = (
    finding: Finding,
    workspace: GitWorkspace,
  ): Effect.Effect<RemediationResult, WorkspaceError> =>
    Effect.gen(function* () {
      const attempts: Array<Attempt> = []
      let feedback: string | null = null

      for (let i = 0; i < maxAttempts; i++) {
        const proposed: Either.Either<EditPatch, GenerationError> =
          yield* Effect.either(patcher.propose(finding, workspace, feedback))

        if (proposed._tag === "Left") {
          // provider/network errors are retryable
          yield* Effect.logWarning(
            `${finding.id} attempt ${i}: generation failed: ${proposed.left.message}`,
          )
          attempts.push({
            index: i,
            patch: null,
            diff: yield* workspace.diff(),
            verdict: null,
            error: `generation error: ${proposed.left.message}`,
          })
          continue
        }
        const patch: EditPatch = proposed.right

        if (isEmptyPatch(patch)) {
          yield* Effect.logInfo(
            `${finding.id} attempt ${i}: empty patch, retrying`,
          )
          attempts.push({
            index: i,
            patch,
            diff: yield* workspace.diff(),
            verdict: null,
            error: "empty patch",
          })
          feedback = yield* feedbackFor(
            "Your previous response contained no effective edits. " +
              "You must change the code to remediate the finding.",
            workspace,
          )
          continue
        }

        const applied: Either.Either<
          void,
          PatchApplicationError | WorkspaceError
        > = yield* Effect.either(workspace.apply(patch))
        if (applied._tag === "Left") {
          if (applied.left._tag === "WorkspaceError") return yield* applied.left
          yield* Effect.logInfo(
            `${finding.id} attempt ${i}: patch failed to apply: ${applied.left.message}`,
          )
          yield* workspace.reset()
          attempts.push({
            index: i,
            patch,
            diff: "",
            verdict: null,
            error: `apply error: ${applied.left.message}`,
          })
          feedback =
            `Your previous edits failed to apply: ${applied.left.message}\n` +
            "The workspace has been reset to the original code. " +
            "Search text must match the current file content exactly and uniquely."
          continue
        }

        // A broken verifier must never pass a patch: any typed error or
        // defect becomes a failed verdict carrying the pretty-printed cause.
        const verdict = yield* verifier.verify(finding, workspace).pipe(
          Effect.catchAllCause((cause) =>
            Effect.succeed(
              Verdict({
                passed: false,
                verifier: verifier.name,
                summary: `verifier crashed: ${Cause.pretty(cause)}`,
                feedback: `Verification infrastructure error: ${Cause.pretty(cause)}`,
              }),
            ),
          ),
        )
        const diff = yield* workspace.diff()
        attempts.push({ index: i, patch, diff, verdict, error: "" })

        if (verdict.passed) {
          yield* Effect.logInfo(
            `${finding.id} attempt ${i}: verified (${verdict.summary})`,
          )
          return {
            findingId: finding.id,
            success: true,
            attempts,
            finalDiff: diff,
          } satisfies RemediationResult
        }

        yield* Effect.logInfo(
          `${finding.id} attempt ${i}: verification failed (${verdict.summary})`,
        )
        feedback = yield* feedbackFor(
          verdict.feedback !== "" ? verdict.feedback : verdict.summary,
          workspace,
        )
      }

      return {
        findingId: finding.id,
        success: false,
        attempts,
        finalDiff: yield* workspace.diff(),
      } satisfies RemediationResult
    })

  return { run }
}
