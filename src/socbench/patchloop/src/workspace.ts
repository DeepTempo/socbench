/**
 * Workspace: a git checkout the harness patches and verifies in place.
 *
 * The loop repairs on top of the current patched state rather than resetting
 * between attempts, so the workspace's job is small: apply exact edits,
 * report the cumulative diff vs the baseline commit, and hard-reset only
 * when asked (patch application failure, or caller wants a clean slate).
 */
import { execFile } from "node:child_process"
import * as fs from "node:fs/promises"
import * as path from "node:path"
import { Effect } from "effect"

import {
  type EditPatch,
  PatchApplicationError,
  WorkspaceError,
} from "./models.js"

const git = (
  root: string,
  ...args: ReadonlyArray<string>
): Effect.Effect<string, WorkspaceError> =>
  Effect.async<string, WorkspaceError>((resume) => {
    const child = execFile(
      "git",
      ["-C", root, ...args],
      { maxBuffer: 64 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          resume(
            Effect.fail(
              new WorkspaceError({
                message: `git ${args[0]} failed: ${stderr.trim() || error.message}`,
                cause: error,
              }),
            ),
          )
        } else {
          resume(Effect.succeed(stdout))
        }
      },
    )
    return Effect.sync(() => child.kill("SIGKILL"))
  })

const tryFs = <A>(message: string, run: () => Promise<A>) =>
  Effect.tryPromise({
    try: run,
    catch: (cause) => new WorkspaceError({ message, cause }),
  })

export class GitWorkspace {
  private readonly createdPaths = new Set<string>()

  private constructor(
    readonly root: string,
    /** resolved commit hash patches diff against */
    readonly baselineRef: string,
  ) {}

  /** `baselineRef` is the commit patches diff against. */
  static make = (
    root: string,
    baselineRef = "HEAD",
  ): Effect.Effect<GitWorkspace, WorkspaceError> =>
    Effect.gen(function* () {
      const resolved = path.resolve(root)
      const gitDir = yield* tryFs(`cannot stat ${resolved}/.git`, () =>
        fs.stat(path.join(resolved, ".git")).catch(() => null),
      )
      if (gitDir === null) {
        return yield* new WorkspaceError({
          message: `${resolved} is not a git checkout`,
        })
      }
      const baseline = yield* git(resolved, "rev-parse", baselineRef)
      return new GitWorkspace(resolved, baseline.trim())
    })

  // -- file access ---------------------------------------------------------

  read = (relPath: string): Effect.Effect<string, WorkspaceError> =>
    this.safePath(relPath).pipe(
      Effect.catchTag(
        "PatchApplicationError",
        (e) => new WorkspaceError({ message: e.message }),
      ),
      Effect.flatMap((abs) =>
        tryFs(`cannot read ${relPath}`, () => fs.readFile(abs, "utf8")),
      ),
    )

  exists = (relPath: string): Effect.Effect<boolean, never> =>
    Effect.promise(() =>
      fs
        .stat(path.resolve(this.root, relPath))
        .then(() => true)
        .catch(() => false),
    )

  listFiles = (): Effect.Effect<ReadonlyArray<string>, WorkspaceError> =>
    git(this.root, "ls-files", "--cached").pipe(
      Effect.map((out) => out.split("\n").filter((line) => line.trim() !== "")),
    )

  // -- mutation --------------------------------------------------------------

  /** Apply exact search/replace edits. All-or-nothing per patch. */
  apply = (
    patch: EditPatch,
  ): Effect.Effect<void, PatchApplicationError | WorkspaceError> => {
    const self = this
    return Effect.gen(function* () {
      const staged: Array<{ abs: string; content: string }> = []
      const created: Array<string> = []
      for (const edit of patch.edits) {
        const abs = yield* self.safePath(edit.path)
        if (edit.search === "") {
          if (yield* self.exists(edit.path)) {
            return yield* new PatchApplicationError({
              message: `empty search targets existing file ${edit.path}`,
            })
          }
          staged.push({ abs, content: edit.replace })
          created.push(edit.path)
          continue
        }
        const content: string = yield* tryFs(`cannot read ${edit.path}`, () =>
          fs.readFile(abs, "utf8"),
        ).pipe(
          Effect.catchTag(
            "WorkspaceError",
            (e) => new PatchApplicationError({ message: e.message }),
          ),
        )
        const occurrences = content.split(edit.search).length - 1
        if (occurrences !== 1) {
          return yield* new PatchApplicationError({
            message: `search text matched ${occurrences} times in ${edit.path} (need exactly 1)`,
          })
        }
        staged.push({ abs, content: content.replace(edit.search, edit.replace) })
      }
      for (const { abs, content } of staged) {
        yield* tryFs(`cannot write ${abs}`, async () => {
          await fs.mkdir(path.dirname(abs), { recursive: true })
          await fs.writeFile(abs, content, "utf8")
        })
      }
      for (const rel of created) self.createdPaths.add(rel)
    })
  }

  /** Discard all changes back to the baseline ref. */
  reset = (): Effect.Effect<void, WorkspaceError> =>
    Effect.gen(this, function* () {
      yield* git(this.root, "checkout", this.baselineRef, "--", ".")
      yield* git(this.root, "clean", "-fd")
      this.createdPaths.clear()
    })

  /**
   * Cumulative diff vs baseline.
   *
   * Untracked files are included only if a patch created them — verifier
   * build artifacts (bytecode, scan caches) stay out.
   */
  diff = (): Effect.Effect<string, WorkspaceError> =>
    Effect.gen(this, function* () {
      const created: Array<string> = []
      for (const rel of [...this.createdPaths].sort()) {
        if (yield* this.exists(rel)) created.push(rel)
      }
      if (created.length === 0) {
        return yield* git(this.root, "diff", this.baselineRef)
      }
      yield* git(this.root, "add", "-N", "--", ...created)
      return yield* git(this.root, "diff", this.baselineRef).pipe(
        Effect.ensuring(Effect.ignore(git(this.root, "reset", "-q"))),
      )
    })

  // -- internals ---------------------------------------------------------------

  private safePath = (
    relPath: string,
  ): Effect.Effect<string, PatchApplicationError> => {
    const abs = path.resolve(this.root, relPath)
    if (abs !== this.root && !abs.startsWith(this.root + path.sep)) {
      return new PatchApplicationError({
        message: `path escapes workspace: ${relPath}`,
      })
    }
    return Effect.succeed(abs)
  }
}
