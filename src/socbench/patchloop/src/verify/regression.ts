/**
 * Regression verifier: the patch must not break the project.
 *
 * Closing the finding is necessary, not sufficient — a patch that deletes
 * the vulnerable feature "passes" a rescan. Run the project's build/tests
 * as a regression gate alongside the security check.
 */
import { Effect } from "effect"

import { type Finding, Verdict } from "../models.js"
import type { GitWorkspace } from "../workspace.js"
import {
  renderCommand,
  runCommand,
  truncateFeedback,
  type Verifier,
} from "./base.js"

export interface RegressionOptions {
  /** e.g. "make test" or "npm test" */
  readonly commandTemplate: string
  readonly timeoutSeconds?: number
  readonly name?: string
}

export const RegressionTestVerifier = (options: RegressionOptions): Verifier => {
  const {
    commandTemplate,
    timeoutSeconds = 1800,
    name = "regression-tests",
  } = options
  return {
    name,
    verify: (finding: Finding, workspace: GitWorkspace) =>
      runCommand(
        renderCommand(commandTemplate, finding, workspace),
        workspace,
        timeoutSeconds,
      ).pipe(
        Effect.map((result) =>
          result.exitCode === 0
            ? Verdict({
                passed: true,
                verifier: name,
                costHint: "moderate",
                summary: "regression tests pass",
              })
            : Verdict({
                passed: false,
                verifier: name,
                costHint: "moderate",
                summary: `regression tests fail (exit ${result.exitCode})`,
                feedback: truncateFeedback(result.output),
              }),
        ),
        Effect.catchTag("CommandTimeoutError", (e) =>
          Effect.succeed(
            Verdict({
              passed: false,
              verifier: name,
              costHint: "moderate",
              summary: `test suite timed out after ${e.timeoutSeconds}s`,
              feedback: `Regression command timed out: ${e.command}`,
            }),
          ),
        ),
      ),
  }
}
