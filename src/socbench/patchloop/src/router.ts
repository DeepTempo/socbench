/**
 * Router: the SOC-side entry point.
 *
 * An upstream triage agent (or a human) decides "this finding should be
 * auto-remediated" and hands a normalized Finding to the router. The router
 * picks the specialized harness for that finding kind — it does NOT hand
 * the finding to a general-purpose coding agent. Specialized harness +
 * packaged verification is the whole point.
 *
 * This scaffold ships one harness family (code patching). The registry
 * shape is where new ones land over time: dependency bumps, IaC
 * misconfigurations, detection-rule tuning — each with its own patcher and
 * its own verifiers.
 */
import { Effect } from "effect"

import type { RemediationHarness } from "./harness.js"
import {
  type Finding,
  type FindingKind,
  type RemediationResult,
  UnroutedFindingError,
  type WorkspaceError,
} from "./models.js"
import type { GitWorkspace } from "./workspace.js"

export type HarnessFactory = (finding: Finding) => RemediationHarness

export interface Router {
  readonly register: (kind: FindingKind, factory: HarnessFactory) => void
  readonly remediate: (
    finding: Finding,
    workspace: GitWorkspace,
  ) => Effect.Effect<RemediationResult, UnroutedFindingError | WorkspaceError>
}

export const Router = (): Router => {
  const registry = new Map<FindingKind, HarnessFactory>()
  return {
    register: (kind, factory) => {
      registry.set(kind, factory)
    },
    remediate: (finding, workspace) => {
      const factory = registry.get(finding.kind)
      if (factory === undefined) {
        return new UnroutedFindingError({
          kind: finding.kind,
          registered: [...registry.keys()],
        })
      }
      return factory(finding).run(finding, workspace)
    },
  }
}
