/**
 * Loop-semantics tests with fake patcher/verifier — no LLM, no scanners.
 *
 * These pin the behaviors that made the loop work on PatchEval:
 * empty-patch rejection, repair-on-top-of-patched-state, reset only on
 * apply failure, verifier crash != pass, bounded attempts, fail-fast
 * chains.
 */
import { execFileSync } from "node:child_process"
import * as fs from "node:fs/promises"
import * as os from "node:os"
import * as path from "node:path"
import { Effect, Logger } from "effect"
import { afterEach, describe, expect, it } from "vitest"

import {
  type EditPatch,
  Finding,
  GitWorkspace,
  OrderedVerifier,
  type Patcher,
  RemediationHarness,
  Verdict,
  type Verifier,
} from "../src/index.js"

const FINDING = new Finding({
  id: "CVE-2099-0001",
  kind: "cve",
  title: "hardcoded credential",
  description: "app.py contains a hardcoded password",
  locations: ["app.py"],
})

const VULNERABLE = 'PASSWORD = "hunter2"\n\ndef connect():\n    return PASSWORD\n'

const tmpDirs: Array<string> = []
afterEach(async () => {
  while (tmpDirs.length > 0) {
    await fs.rm(tmpDirs.pop()!, { recursive: true, force: true })
  }
})

const makeRepo = async (): Promise<GitWorkspace> => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "patchloop-"))
  tmpDirs.push(root)
  await fs.writeFile(path.join(root, "app.py"), VULNERABLE)
  const g = (...args: Array<string>) =>
    execFileSync("git", ["-C", root, ...args], { stdio: "pipe" })
  g("init", "-q")
  g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
  g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
  return run(GitWorkspace.make(root))
}

// Every effect in tests runs with logging silenced.
const run = <A, E>(effect: Effect.Effect<A, E>): Promise<A> =>
  Effect.runPromise(effect.pipe(Effect.provide(Logger.remove(Logger.defaultLogger))))

/** Returns queued patches; records the feedback it was called with. */
class ScriptedPatcher implements Patcher {
  readonly feedbackSeen: Array<string | null> = []
  constructor(private readonly patches: Array<EditPatch>) {}

  propose = (
    _finding: Finding,
    _workspace: GitWorkspace,
    feedback: string | null,
  ) =>
    Effect.sync(() => {
      this.feedbackSeen.push(feedback)
      return this.patches.shift()!
    })
}

/** Passes when the hardcoded password is gone from app.py. */
const MarkerVerifier = (
  onVerify?: (workspace: GitWorkspace) => Promise<void>,
): Verifier => ({
  name: "marker",
  verify: (_finding, workspace) =>
    Effect.gen(function* () {
      if (onVerify) yield* Effect.promise(() => onVerify(workspace))
      const content = yield* workspace.read("app.py")
      return content.includes("hunter2")
        ? Verdict({
            passed: false,
            verifier: "marker",
            summary: "still vulnerable",
            feedback: "hunter2 still present",
          })
        : Verdict({ passed: true, verifier: "marker", summary: "clean" })
    }),
})

const patch = (edits: EditPatch["edits"]): EditPatch => ({
  edits,
  rationale: "",
})

const GOOD_PATCH = patch([
  {
    path: "app.py",
    search: 'PASSWORD = "hunter2"',
    replace: 'import os\nPASSWORD = os.environ["APP_PASSWORD"]',
  },
])
const NOOP_PATCH = patch([])
const BAD_APPLY_PATCH = patch([
  { path: "app.py", search: "text that does not exist", replace: "x" },
])
const INEFFECTIVE_PATCH = patch([
  { path: "app.py", search: "def connect():", replace: "def connect():  # reviewed" },
])

describe("RemediationHarness loop", () => {
  it("succeeds on the first attempt", async () => {
    const ws = await makeRepo()
    const harness = RemediationHarness({
      patcher: new ScriptedPatcher([GOOD_PATCH]),
      verifier: MarkerVerifier(),
    })
    const result = await run(harness.run(FINDING, ws))
    expect(result.success).toBe(true)
    expect(result.attempts).toHaveLength(1)
    expect(result.finalDiff).toContain("APP_PASSWORD")
  })

  it("never counts an empty patch as success and triggers feedback", async () => {
    const ws = await makeRepo()
    const patcher = new ScriptedPatcher([NOOP_PATCH, GOOD_PATCH])
    const result = await run(
      RemediationHarness({ patcher, verifier: MarkerVerifier() }).run(FINDING, ws),
    )
    expect(result.success).toBe(true)
    expect(result.attempts).toHaveLength(2)
    expect(result.attempts[0]!.error).toBe("empty patch")
    expect(patcher.feedbackSeen[1]).toContain("no effective edits")
  })

  it("repairs on top of the patched state, not from a reset", async () => {
    const ws = await makeRepo()
    const patcher = new ScriptedPatcher([INEFFECTIVE_PATCH, GOOD_PATCH])
    const result = await run(
      RemediationHarness({ patcher, verifier: MarkerVerifier() }).run(FINDING, ws),
    )
    expect(result.success).toBe(true)
    // both edits present: the loop did not reset between attempts
    const content = await run(ws.read("app.py"))
    expect(content).toContain("# reviewed")
    expect(content).toContain("APP_PASSWORD")
    // repair prompt carried the verbatim failure and the current diff
    expect(patcher.feedbackSeen[1]).toContain("hunter2 still present")
    expect(patcher.feedbackSeen[1]).toContain("# reviewed")
  })

  it("resets the workspace when a patch fails to apply", async () => {
    const ws = await makeRepo()
    const patcher = new ScriptedPatcher([BAD_APPLY_PATCH, GOOD_PATCH])
    const result = await run(
      RemediationHarness({ patcher, verifier: MarkerVerifier() }).run(FINDING, ws),
    )
    expect(result.success).toBe(true)
    expect(result.attempts[0]!.error).toContain("apply error")
    expect(patcher.feedbackSeen[1]).toContain("reset to the original code")
  })

  it("includes patch-created files in the diff but not verifier artifacts", async () => {
    const newFilePatch = patch([
      GOOD_PATCH.edits[0]!,
      { path: "security_notes.md", search: "", replace: "rotated creds\n" },
    ])
    // simulates a scanner dropping a cache file in the workspace
    const artifactVerifier = MarkerVerifier(async (workspace) => {
      await fs.writeFile(
        path.join(workspace.root, "scan-cache.bin"),
        Buffer.from([0, 106, 117, 110, 107]),
      )
    })
    const ws = await makeRepo()
    const result = await run(
      RemediationHarness({
        patcher: new ScriptedPatcher([newFilePatch]),
        verifier: artifactVerifier,
      }).run(FINDING, ws),
    )
    expect(result.success).toBe(true)
    expect(result.finalDiff).toContain("security_notes.md")
    expect(result.finalDiff).not.toContain("scan-cache.bin")
  })

  it("stops after maxAttempts", async () => {
    const ws = await makeRepo()
    const patcher = new ScriptedPatcher([NOOP_PATCH, NOOP_PATCH, NOOP_PATCH])
    const result = await run(
      RemediationHarness({
        patcher,
        verifier: MarkerVerifier(),
        maxAttempts: 3,
      }).run(FINDING, ws),
    )
    expect(result.success).toBe(false)
    expect(result.attempts).toHaveLength(3)
  })

  it("treats a crashing verifier as a failure, never a pass", async () => {
    const crashing: Verifier = {
      name: "crash",
      verify: () => Effect.die(new Error("scanner unavailable")),
    }
    const ws = await makeRepo()
    const result = await run(
      RemediationHarness({
        patcher: new ScriptedPatcher([GOOD_PATCH]),
        verifier: crashing,
        maxAttempts: 1,
      }).run(FINDING, ws),
    )
    expect(result.success).toBe(false)
    expect(result.attempts[0]!.verdict!.summary).toContain("verifier crashed")
    expect(result.attempts[0]!.verdict!.summary).toContain("scanner unavailable")
  })
})

describe("OrderedVerifier", () => {
  const check = (name: string, passed: boolean, calls: Array<string>): Verifier => ({
    name,
    verify: () =>
      Effect.sync(() => {
        calls.push(name)
        return Verdict({
          passed,
          verifier: name,
          summary: name,
          feedback: `${name} output`,
        })
      }),
  })

  it("fails fast and skips expensive checks", async () => {
    const calls: Array<string> = []
    const chain = OrderedVerifier([
      check("rescan", false, calls),
      check("reproduce", true, calls),
    ])
    const ws = await makeRepo()
    const verdict = await run(chain.verify(FINDING, ws))
    expect(verdict.passed).toBe(false)
    expect(calls).toEqual(["rescan"])
    expect(verdict.feedback).toContain("failed check: rescan")
  })

  it("passes when every check passes", async () => {
    const calls: Array<string> = []
    const chain = OrderedVerifier([
      check("a", true, calls),
      check("b", true, calls),
    ])
    const ws = await makeRepo()
    const verdict = await run(chain.verify(FINDING, ws))
    expect(verdict.passed).toBe(true)
    expect(calls).toEqual(["a", "b"])
  })
})
