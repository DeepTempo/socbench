/** patchloop: a verification-loop remediation harness for security findings. */

export {
  type HarnessOptions,
  type Patcher,
  RemediationHarness,
} from "./harness.js"
export {
  type Attempt,
  CommandTimeoutError,
  type CostHint,
  decodeEditPatch,
  decodeFinding,
  Edit,
  EditPatch,
  Finding,
  FindingKind,
  GenerationError,
  isEmptyPatch,
  PatchApplicationError,
  type RemediationResult,
  UnroutedFindingError,
  Verdict,
  WorkspaceError,
} from "./models.js"
export {
  DEFAULT_BASE_URL,
  DirectEditPatcher,
  type DirectEditOptions,
} from "./patcher.js"
export { type HarnessFactory, Router } from "./router.js"
export {
  type CommandResult,
  MAX_FEEDBACK_CHARS,
  renderCommand,
  runCommand,
  truncateFeedback,
  type Verifier,
} from "./verify/base.js"
export { OrderedVerifier } from "./verify/composite.js"
export {
  RegressionTestVerifier,
  type RegressionOptions,
} from "./verify/regression.js"
export {
  ReproductionVerifier,
  type ReproductionOptions,
} from "./verify/reproduce.js"
export { RescanVerifier, type RescanOptions } from "./verify/rescan.js"
export { GitWorkspace } from "./workspace.js"
