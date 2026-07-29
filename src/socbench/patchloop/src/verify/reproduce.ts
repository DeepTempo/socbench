/**
 * Reproduction verifier: run the exploit / PoC and expect it to stop working.
 *
 * The expensive, high-confidence option — the natural fit for pentest and
 * bug-bounty style findings that ship with reproduction steps. Semantics
 * are inverted relative to a test suite: the patch passes when the exploit
 * FAILS.
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

export interface ReproductionOptions {
  /** e.g. "python3 poc/exploit.py --target {workspace}" */
  readonly commandTemplate: string
  /**
   * - "exploit-nonzero" (default): PoC exits nonzero after the patch.
   * - "marker-absent": `successMarker` no longer appears in PoC output
   *   (for PoCs that exit 0 either way and print e.g. "VULNERABLE").
   */
  readonly passWhen?: "exploit-nonzero" | "marker-absent"
  readonly successMarker?: string
  readonly timeoutSeconds?: number
  readonly name?: string
}

export const ReproductionVerifier = (options: ReproductionOptions): Verifier => {
  const {
    commandTemplate,
    passWhen = "exploit-nonzero",
    successMarker = "",
    timeoutSeconds = 900,
    name = "reproduce",
  } = options
  if (passWhen === "marker-absent" && successMarker === "") {
    throw new Error("marker-absent requires successMarker")
  }
  return {
    name,
    verify: (finding: Finding, workspace: GitWorkspace) =>
      runCommand(
        renderCommand(commandTemplate, finding, workspace),
        workspace,
        timeoutSeconds,
      ).pipe(
        Effect.map((result) => {
          const exploited =
            passWhen === "marker-absent"
              ? result.output.includes(successMarker)
              : result.exitCode === 0
          return exploited
            ? Verdict({
                passed: false,
                verifier: name,
                costHint: "expensive",
                summary: "exploit still reproduces against patched code",
                feedback: truncateFeedback(result.output),
              })
            : Verdict({
                passed: true,
                verifier: name,
                costHint: "expensive",
                summary: "exploit no longer reproduces",
              })
        }),
        // A hanging PoC is ambiguous — never count it as a pass.
        Effect.catchTag("CommandTimeoutError", (e) =>
          Effect.succeed(
            Verdict({
              passed: false,
              verifier: name,
              costHint: "expensive",
              summary: `PoC timed out after ${e.timeoutSeconds}s (ambiguous, treated as fail)`,
              feedback: `Reproduction command timed out: ${e.command}`,
            }),
          ),
        ),
      ),
  }
}
