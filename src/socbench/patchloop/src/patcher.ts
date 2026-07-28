/**
 * Reference LLM patcher: direct exact-edit generation, no agentic tool loop.
 *
 * Deliberately NOT a read/write/bash agent. For a scoped task like "patch
 * this finding", one structured request per attempt is cheaper and more
 * predictable than an open-ended tool-calling loop: on PatchEval full-230
 * this shape ran at roughly $0.15-0.17/CVE. The intelligence budget goes to
 * the verification loop instead (see harness.ts).
 *
 * Two-phase when locations are unknown:
 * 1. localize: model picks candidate files from `git ls-files` output
 * 2. edit: model sees the finding + numbered file contents, returns JSON
 *    search/replace edits — decoded with Effect Schema, so malformed model
 *    output is a typed GenerationError, not a mystery crash downstream.
 *
 * Any OpenAI-compatible chat-completions endpoint works (OpenRouter
 * default); swap `request` for your provider of choice.
 */
import { Duration, Effect, Schema } from "effect"

import type { Patcher } from "./harness.js"
import {
  decodeEditPatch,
  type EditPatch,
  type Finding,
  GenerationError,
} from "./models.js"
import type { GitWorkspace } from "./workspace.js"

export const DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

const LocalizationResponse = Schema.Struct({
  paths: Schema.Array(Schema.String),
})

export const extractJsonObject = (text: string): string => {
  const start = text.indexOf("{")
  const end = text.lastIndexOf("}")
  if (start === -1 || end <= start) {
    throw new Error("model response contained no JSON object")
  }
  return text.slice(start, end + 1)
}

const numbered = (content: string): string =>
  content
    .split("\n")
    .map((line, i) => `${i + 1}\t${line}`)
    .join("\n")

export interface DirectEditOptions {
  readonly model: string
  readonly apiKeyEnv?: string
  readonly baseUrl?: string
  readonly requestTimeoutSeconds?: number
  readonly maxLocalizedFiles?: number
  readonly extraHeaders?: Readonly<Record<string, string>>
}

export const DirectEditPatcher = (options: DirectEditOptions): Patcher => {
  const {
    model,
    apiKeyEnv = "OPENROUTER_API_KEY",
    baseUrl = DEFAULT_BASE_URL,
    requestTimeoutSeconds = 1200,
    maxLocalizedFiles = 8,
    extraHeaders = {},
  } = options

  const fail = (message: string, cause?: unknown) =>
    new GenerationError(cause === undefined ? { message } : { message, cause })

  // -- transport -------------------------------------------------------------

  const request = (
    system: string,
    user: string,
  ): Effect.Effect<string, GenerationError> =>
    Effect.gen(function* () {
      const apiKey = process.env[apiKeyEnv] ?? ""
      if (apiKey === "") {
        return yield* fail(`set ${apiKeyEnv} in the environment`)
      }
      const response = yield* Effect.tryPromise({
        try: (signal) =>
          fetch(baseUrl, {
            method: "POST",
            signal,
            headers: {
              Authorization: `Bearer ${apiKey}`,
              "Content-Type": "application/json",
              ...extraHeaders,
            },
            body: JSON.stringify({
              model,
              messages: [
                { role: "system", content: system },
                { role: "user", content: user },
              ],
            }),
          }),
        catch: (cause) => fail(`provider request failed: ${cause}`, cause),
      }).pipe(
        Effect.timeoutFail({
          duration: Duration.seconds(requestTimeoutSeconds),
          onTimeout: () =>
            fail(`provider request timed out after ${requestTimeoutSeconds}s`),
        }),
      )
      if (!response.ok) {
        return yield* fail(`provider returned HTTP ${response.status}`)
      }
      const body = yield* Effect.tryPromise({
        try: () => response.json() as Promise<any>,
        catch: (cause) => fail("provider response was not JSON", cause),
      })
      const content: unknown = body?.choices?.[0]?.message?.content
      if (typeof content !== "string" || content === "") {
        return yield* fail("provider returned an empty completion")
      }
      return content
    })

  const parseWith = <A>(
    decode: (input: unknown) => Effect.Effect<A, unknown>,
    text: string,
  ): Effect.Effect<A, GenerationError> =>
    Effect.try({
      try: () => JSON.parse(extractJsonObject(text)) as unknown,
      catch: (cause) => fail(`unparseable model response: ${cause}`, cause),
    }).pipe(
      Effect.flatMap((json) =>
        decode(json).pipe(
          Effect.mapError((cause) =>
            fail(`model response failed schema validation: ${cause}`, cause),
          ),
        ),
      ),
    )

  // -- phases ------------------------------------------------------------------

  const localize = (
    finding: Finding,
    workspace: GitWorkspace,
  ): Effect.Effect<ReadonlyArray<string>, GenerationError> =>
    Effect.gen(function* () {
      const repoFiles = yield* workspace
        .listFiles()
        .pipe(
          Effect.mapError((e) => fail(`cannot list repository files: ${e.message}`)),
        )
      const prompt = [
        "Locate the files that must be inspected to fix this security finding.",
        'Return one JSON object and nothing else: {"paths": ["relative/path", "..."]}',
        `Choose at most ${maxLocalizedFiles} paths from the list below.`,
        "",
        `## Finding\n${finding.title}\n\n${finding.description}`,
        "",
        "## Repository files\n" + repoFiles.join("\n"),
      ].join("\n")
      const response = yield* request(
        "You localize security vulnerabilities to source files.",
        prompt,
      )
      const parsed = yield* parseWith(
        Schema.decodeUnknown(LocalizationResponse),
        response,
      )
      const known = new Set(repoFiles)
      const valid = parsed.paths.filter((p) => known.has(p))
      if (valid.length === 0) {
        return yield* fail("localization returned no valid repository paths")
      }
      return valid.slice(0, maxLocalizedFiles)
    })

  const editPrompt = (
    finding: Finding,
    files: ReadonlyArray<readonly [string, string]>,
    feedback: string | null,
  ): string => {
    const sections = [
      "Fix the following security finding with the smallest correct change.",
      "Return one JSON object and nothing else:",
      '{"rationale": "one paragraph", "edits": [{"path": "...", ' +
        '"search": "exact current text", "replace": "new text"}]}',
      "Rules:",
      "- search text must match the CURRENT file content exactly and uniquely",
      "- do not include line-number prefixes in search or replace text",
      '- to create a new file, use an empty "search"',
      `\n## Finding ${finding.id}\n${finding.title}\n\n${finding.description}`,
    ]
    for (const [relPath, content] of files) {
      sections.push(
        `\n### Current file: ${relPath} (1-based line numbers shown; ` +
          "do not include them in edits)\n```text\n" +
          numbered(content) +
          "\n```",
      )
    }
    if (feedback !== null) {
      sections.push(
        "\n## Previous attempt failed verification\n" +
          "Repair your patch so verification passes. The file contents " +
          "above already include your previous edits.\n\n" +
          feedback,
      )
    }
    return sections.join("\n")
  }

  // -- Patcher interface ---------------------------------------------------------

  const propose = (
    finding: Finding,
    workspace: GitWorkspace,
    feedback: string | null,
  ): Effect.Effect<EditPatch, GenerationError> =>
    Effect.gen(function* () {
      const paths =
        finding.locations.length > 0
          ? finding.locations
          : yield* localize(finding, workspace)
      const files: Array<readonly [string, string]> = []
      for (const p of paths) {
        if (yield* workspace.exists(p)) {
          const content = yield* workspace
            .read(p)
            .pipe(Effect.mapError((e) => fail(e.message, e)))
          files.push([p, content] as const)
        }
      }
      const response = yield* request(
        "You write minimal, correct security patches as exact " +
          "search/replace edits. You never weaken tests or disable " +
          "the vulnerable feature to make checks pass.",
        editPrompt(finding, files, feedback),
      )
      return yield* parseWith(decodeEditPatch, response)
    })

  return { propose }
}
