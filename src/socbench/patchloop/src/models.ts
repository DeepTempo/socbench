/**
 * Core data types shared across the harness.
 *
 * A `Finding` is the normalized input a SOC router hands to a remediation
 * harness: enough to locate the code, and enough to re-run whatever raised
 * the alert. It is an Effect Schema class so untrusted JSON (from a queue,
 * a webhook, a case-management export) decodes with real validation instead
 * of a cast. Everything else (attempts, verdicts, results) is the loop's
 * audit trail — plain data, safe to serialize into your case system.
 */
import { Data, Schema } from "effect"

/** Coarse routing key. Extend freely; the router matches on the value. */
export const FindingKind = Schema.Literal(
  "cve",     // known-vulnerability finding (SCA / image scan)
  "sast",    // static-analysis finding
  "pentest", // bug-bounty / pentest style, usually PoC-backed
  "custom",
)
export type FindingKind = typeof FindingKind.Type

/**
 * Normalized security finding.
 *
 * `locations` is optional on purpose: with locations you are in the
 * "oracle" setting, without them the patcher must localize itself
 * (measurably harder — budget more attempts).
 */
export class Finding extends Schema.Class<Finding>("Finding")({
  id: Schema.String, // e.g. "CVE-2023-1234" or detector finding id
  kind: FindingKind,
  title: Schema.String,
  description: Schema.String, // the alert body / advisory text
  detector: Schema.optionalWith(Schema.String, { default: () => "" }),
  locations: Schema.optionalWith(Schema.Array(Schema.String), {
    default: () => [], // repo-relative paths
  }),
  metadata: Schema.optionalWith(
    Schema.Record({ key: Schema.String, value: Schema.Unknown }),
    { default: () => ({}) },
  ),
}) {}

export const decodeFinding = Schema.decodeUnknown(Finding)

/**
 * One exact search/replace edit. Exact-match edits (not free-form diffs)
 * are deliberate: they fail loudly when the model hallucinates context,
 * instead of fuzzy-applying garbage. An empty `search` with a non-empty
 * `replace` creates a new file.
 */
export const Edit = Schema.Struct({
  path: Schema.String,
  search: Schema.optionalWith(Schema.String, { default: () => "" }),
  replace: Schema.optionalWith(Schema.String, { default: () => "" }),
})
export type Edit = typeof Edit.Type

/** A concrete change set proposed by a patcher. */
export const EditPatch = Schema.Struct({
  edits: Schema.optionalWith(Schema.Array(Edit), { default: () => [] }),
  rationale: Schema.optionalWith(Schema.String, { default: () => "" }),
})
export type EditPatch = typeof EditPatch.Type

export const decodeEditPatch = Schema.decodeUnknown(EditPatch)

export const isEmptyPatch = (patch: EditPatch): boolean =>
  patch.edits.length === 0 || patch.edits.every((e) => e.search === e.replace)

export type CostHint = "cheap" | "moderate" | "expensive"

/**
 * Outcome of one verification pass.
 *
 * `feedback` is the payload that makes the loop work: verbatim failing
 * output (rescan report, PoC output, test failures) that gets fed back to
 * the patcher. Empty feedback on failure wastes the retry.
 */
export interface Verdict {
  readonly passed: boolean
  readonly verifier: string // name of the verifier that produced this
  readonly summary: string
  readonly feedback: string // verbatim failure output, truncated by producer
  readonly costHint: CostHint
}

export const Verdict = (
  input: Pick<Verdict, "passed" | "verifier"> & Partial<Verdict>,
): Verdict => ({
  summary: "",
  feedback: "",
  costHint: "cheap",
  ...input,
})

export interface Attempt {
  readonly index: number
  readonly patch: EditPatch | null
  /** cumulative diff vs baseline after this attempt */
  readonly diff: string
  readonly verdict: Verdict | null
  /** patch-application or generation error, if any */
  readonly error: string
}

export interface RemediationResult {
  readonly findingId: string
  readonly success: boolean
  readonly attempts: ReadonlyArray<Attempt>
  /** cumulative diff of the accepted (or last) state */
  readonly finalDiff: string
}

// ---------------------------------------------------------------------------
// Typed errors. Tagged so callers can catch exactly what they mean to catch
// (Effect.catchTag) instead of string-matching exception messages.
// ---------------------------------------------------------------------------

/** Edits could not be applied (bad search text, path escape, …). */
export class PatchApplicationError extends Data.TaggedError(
  "PatchApplicationError",
)<{ readonly message: string }> {}

/** Underlying git/filesystem operation failed. */
export class WorkspaceError extends Data.TaggedError("WorkspaceError")<{
  readonly message: string
  readonly cause?: unknown
}> {}

/** Patch generation failed (provider error, unparseable model output, …). */
export class GenerationError extends Data.TaggedError("GenerationError")<{
  readonly message: string
  readonly cause?: unknown
}> {}

/** A verifier command exceeded its time budget. */
export class CommandTimeoutError extends Data.TaggedError(
  "CommandTimeoutError",
)<{ readonly command: string; readonly timeoutSeconds: number }> {}

/** No harness is registered for a finding kind. */
export class UnroutedFindingError extends Data.TaggedError(
  "UnroutedFindingError",
)<{ readonly kind: FindingKind; readonly registered: ReadonlyArray<string> }> {}
