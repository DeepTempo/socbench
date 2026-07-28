/**
 * Rescan verifier: re-run the detector that raised the finding.
 *
 * This is the cheap, default verification in a SOC — the well-oiled path.
 * If Trivy/Grype/Semgrep/your-detector raised the alert, the strongest
 * cheap signal that the patch worked is that the same scan no longer
 * reports it.
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

export interface RescanOptions {
  /**
   * Shell command with `{workspace}` and `{finding_id}` placeholders, e.g.
   *   "trivy fs --scanners vuln --format json {workspace}"
   *   "semgrep scan --config auto --json {workspace}"
   */
  readonly commandTemplate: string
  /**
   * - "finding-absent" (default): finding.id does not appear in scan output.
   *   Correct for scanners that list finding ids (CVE ids, rule ids).
   * - "exit-zero": the command exits 0. Correct for scanners run with a
   *   fail-on-findings flag scoped to this finding.
   */
  readonly passWhen?: "finding-absent" | "exit-zero"
  readonly timeoutSeconds?: number
  readonly name?: string
}

export const RescanVerifier = (options: RescanOptions): Verifier => {
  const {
    commandTemplate,
    passWhen = "finding-absent",
    timeoutSeconds = 600,
    name = "rescan",
  } = options
  return {
    name,
    verify: (finding: Finding, workspace: GitWorkspace) =>
      runCommand(
        renderCommand(commandTemplate, finding, workspace),
        workspace,
        timeoutSeconds,
      ).pipe(
        Effect.map((result) => {
          const passed =
            passWhen === "exit-zero"
              ? result.exitCode === 0
              : !result.output.includes(finding.id)
          return passed
            ? Verdict({
                passed: true,
                verifier: name,
                summary: `detector no longer reports ${finding.id}`,
              })
            : Verdict({
                passed: false,
                verifier: name,
                summary: `detector still reports ${finding.id}`,
                feedback: truncateFeedback(result.output),
              })
        }),
        Effect.catchTag("CommandTimeoutError", (e) =>
          Effect.succeed(
            Verdict({
              passed: false,
              verifier: name,
              summary: `rescan timed out after ${e.timeoutSeconds}s`,
              feedback: `Rescan command timed out: ${e.command}`,
            }),
          ),
        ),
      ),
  }
}
