/**
 * End-to-end example: route a CVE finding through the remediation harness.
 *
 * Usage:
 *   export OPENROUTER_API_KEY=...
 *   npx tsx examples/remediate-cve.ts --repo /path/to/checkout \
 *       --finding examples/finding-cve.json \
 *       --model anthropic/claude-sonnet-4.5
 *
 * The verifier chain here is the intended production shape: cheap rescan
 * first, regression tests second, and (for PoC-backed findings) exploit
 * reproduction last. Swap the command templates for your detector.
 */
import * as fs from "node:fs/promises"
import { parseArgs } from "node:util"
import { Effect } from "effect"

import {
  decodeFinding,
  DirectEditPatcher,
  type Finding,
  GitWorkspace,
  OrderedVerifier,
  RegressionTestVerifier,
  RemediationHarness,
  ReproductionVerifier,
  RescanVerifier,
  Router,
} from "../src/index.js"

const buildRouter = (model: string): Router => {
  const router = Router()

  const str = (finding: Finding, key: string, fallback: string): string => {
    const value = finding.metadata[key]
    return typeof value === "string" ? value : fallback
  }

  router.register("cve", (finding) =>
    RemediationHarness({
      patcher: DirectEditPatcher({ model }),
      verifier: OrderedVerifier([
        RescanVerifier({
          commandTemplate: str(
            finding,
            "rescan_command",
            "trivy fs --scanners vuln --quiet {workspace}",
          ),
        }),
        RegressionTestVerifier({
          commandTemplate: str(finding, "test_command", "make test"),
        }),
      ]),
      maxAttempts: 3,
    }),
  )

  router.register("pentest", (finding) =>
    RemediationHarness({
      patcher: DirectEditPatcher({ model }),
      verifier: OrderedVerifier([
        RegressionTestVerifier({
          commandTemplate: str(finding, "test_command", "make test"),
        }),
        ReproductionVerifier({
          commandTemplate: str(finding, "poc_command", ""),
        }),
      ]),
      maxAttempts: 3,
    }),
  )

  return router
}

const main = Effect.gen(function* () {
  const { values } = parseArgs({
    options: {
      repo: { type: "string" },
      finding: { type: "string" },
      model: { type: "string", default: "anthropic/claude-sonnet-4.5" },
      out: { type: "string", default: "remediation_result.json" },
    },
  })
  if (!values.repo || !values.finding) {
    return yield* Effect.dieMessage(
      "usage: remediate-cve.ts --repo <git checkout> --finding <finding.json>",
    )
  }

  const raw = yield* Effect.promise(() => fs.readFile(values.finding!, "utf8"))
  const finding = yield* decodeFinding(JSON.parse(raw))
  const workspace = yield* GitWorkspace.make(values.repo)

  const result = yield* buildRouter(values.model).remediate(finding, workspace)

  yield* Effect.promise(() =>
    fs.writeFile(
      values.out,
      JSON.stringify(
        {
          findingId: result.findingId,
          success: result.success,
          attempts: result.attempts.length,
          finalDiff: result.finalDiff,
          verdicts: result.attempts.map((a) => ({
            attempt: a.index,
            error: a.error,
            verdict: a.verdict?.summary ?? null,
          })),
        },
        null,
        2,
      ),
    ),
  )

  const status = result.success ? "VERIFIED" : "NOT REMEDIATED"
  yield* Effect.log(
    `${finding.id}: ${status} after ${result.attempts.length} attempt(s); ` +
      `details in ${values.out}`,
  )
  if (result.success) {
    yield* Effect.log("Accepted diff:\n\n" + result.finalDiff)
  }
})

Effect.runPromise(main).catch((error) => {
  console.error(error)
  process.exitCode = 1
})
