/**
 * Verifier interface + shared command plumbing.
 *
 * This is the seam the whole project exists for. In benchmark form the
 * verifier was PatchEval's evaluator; in a SOC it is whatever can
 * independently confirm the finding is gone: a rescan of the original
 * detector, the project's test suite, or a reproduction of the exploit.
 * Implement `verify` and return a Verdict whose `feedback` carries verbatim
 * failure output — that feedback is what the repair round runs on.
 *
 * Infrastructure failures are typed errors, not verdicts; the harness maps
 * any verifier error to a FAILED verdict (a broken scanner must never pass
 * a patch).
 */
import { spawn } from "node:child_process"
import { Duration, Effect } from "effect"

import {
  CommandTimeoutError,
  type Finding,
  type Verdict,
} from "../models.js"
import type { GitWorkspace } from "../workspace.js"

export const MAX_FEEDBACK_CHARS = 20_000

export interface Verifier {
  readonly name: string
  readonly verify: (
    finding: Finding,
    workspace: GitWorkspace,
  ) => Effect.Effect<Verdict, unknown>
}

/** Keep head and tail; failures usually live at the edges of output. */
export const truncateFeedback = (
  output: string,
  limit: number = MAX_FEEDBACK_CHARS,
): string => {
  if (output.length <= limit) return output
  const half = Math.floor(limit / 2)
  return (
    output.slice(0, half) +
    `\n... [${output.length - limit} chars truncated] ...\n` +
    output.slice(-half)
  )
}

export interface CommandResult {
  readonly exitCode: number
  /** stdout followed by stderr, the way a human reads a failed run */
  readonly output: string
}

/**
 * Run a shell command in the workspace with a hard time budget.
 *
 * Interruption-safe by construction: the timeout interrupts the fiber and
 * the cleanup effect kills the whole process group, so a hung scanner or
 * PoC cannot outlive its verdict.
 */
export const runCommand = (
  command: string,
  workspace: GitWorkspace,
  timeoutSeconds: number,
): Effect.Effect<CommandResult, CommandTimeoutError> =>
  Effect.async<CommandResult>((resume) => {
    const child = spawn("sh", ["-c", command], {
      cwd: workspace.root,
      detached: true, // own process group, so cleanup kills grandchildren too
    })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk: Buffer) => (stdout += chunk.toString()))
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()))
    child.on("error", (error) =>
      resume(Effect.succeed({ exitCode: 127, output: String(error) })),
    )
    child.on("close", (code) =>
      resume(
        Effect.succeed({
          exitCode: code ?? 1,
          output: stdout + (stderr ? "\n" + stderr : ""),
        }),
      ),
    )
    return Effect.sync(() => {
      if (child.pid !== undefined && child.exitCode === null) {
        try {
          process.kill(-child.pid, "SIGKILL")
        } catch {
          child.kill("SIGKILL")
        }
      }
    })
  }).pipe(
    Effect.timeoutFail({
      duration: Duration.seconds(timeoutSeconds),
      onTimeout: () => new CommandTimeoutError({ command, timeoutSeconds }),
    }),
  )

const shellQuote = (value: string): string =>
  "'" + value.replaceAll("'", `'\\''`) + "'"

/** Substitute `{workspace}` and `{finding_id}` placeholders, shell-quoted. */
export const renderCommand = (
  template: string,
  finding: Finding,
  workspace: GitWorkspace,
): string =>
  template
    .replaceAll("{workspace}", shellQuote(workspace.root))
    .replaceAll("{finding_id}", shellQuote(finding.id))
