# Security Policy

SOCBench is an open benchmark and a set of harnesses for AI in cybersecurity
operations. We take security reports seriously and we would rather hear about a
problem early and informally than not at all.

Machine-readable contact information is published at
[socbench.org/.well-known/security.txt](https://socbench.org/.well-known/security.txt)
per [RFC 9116](https://www.rfc-editor.org/rfc/rfc9116).

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security problem.**

Two private channels, in order of preference:

1. **GitHub private vulnerability reporting (preferred).**
   [Open a draft advisory](https://github.com/DeepTempo/socbench/security/advisories/new).
   This keeps the report private, keeps the whole discussion attached to the
   repository, and is the path that lets us request a CVE if one is warranted.
2. **Email.** [security@deeptempo.ai](mailto:security@deeptempo.ai). If you want
   to encrypt, say so in a first message with no sensitive detail and we will
   arrange a key.

A useful report usually includes:

- The affected component (the `socbench` Python package, PatchLoop, the CI
  workflows, or socbench.org) and the commit SHA you tested.
- What an attacker gains, stated plainly. "Reads any file on the host" is more
  useful than a severity label.
- Reproduction steps, ideally against the mock provider so we can reproduce
  without spending API budget.
- Any proof of concept, and whether you have shared it anywhere else.

## What to expect

| Stage | Target |
| --- | --- |
| Acknowledgment that a human has read it | 3 business days |
| Initial triage, severity, and whether we agree it is in scope | 10 business days |
| Fix or documented mitigation for accepted reports | 90 days from triage |

If a report is accepted, we will keep you updated as the fix progresses and
will coordinate timing with you before any public disclosure. If we disagree
that something is a vulnerability, we will say so and explain why, rather than
letting the thread go quiet.

We do not run a paid bug bounty. We do credit reporters (see
[Acknowledgments](#acknowledgments)).

## Supported versions

SOCBench is pre-1.0 and has no released versions or maintained release
branches. **Only the latest commit on `main` is supported.** Fixes land on
`main`; there are no backports. If you are running a pinned older commit,
expect to move forward to pick up a fix.

## Scope

### In scope

- **Code execution or file access outside the intended boundary.** PatchLoop
  applies model-generated patches and runs verifier commands by design (see
  [Known design constraints](#known-design-constraints)). A way to reach code
  execution, path traversal, or host file access *without* the operator opting
  into it, or a way to escape the workspace that the documented setup is
  supposed to contain, is in scope.
- **Credential exposure.** Provider API keys (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) leaking into logs, run artifacts,
  scored output, error messages, or anything the repo commits or publishes.
- **Prompt injection with a concrete security consequence.** Content in a
  finding, a dataset record, or a tool result that steers an agent into an
  action outside its persona's read-only tool scope, exfiltrates credentials,
  or escapes its dollar or turn budget. The consequence matters here; see
  the out-of-scope note on model behavior below.
- **Input handling at trust boundaries.** Deserialization, schema decoding, or
  path handling that mishandles untrusted findings, JSON, or corpus files.
- **Vulnerable dependencies** that are actually reachable from shipped code
  paths.
- **Supply chain and CI.** Workflow injection, privilege escalation in
  `.github/workflows`, or anything that could tamper with the GitHub Pages
  deployment or the published artifacts.
- **socbench.org** itself, to the extent a static site can be affected.

### Out of scope

- **Model behavior.** Jailbreaks, refusals, hallucinations, or unsafe
  generations from Anthropic, OpenAI, Google, or any self-hosted model. Report
  those to the model provider. We are interested only in cases where our
  harness turns model output into a concrete security consequence on the host.
- **PatchLoop executing patches and verifier commands.** This is the documented
  purpose of the tool, not a vulnerability. See below.
- **Benchmark results.** Disagreements about scores, methodology, or fairness
  are welcome, but they belong in a public GitHub issue, not a security report.
- **Missing HTTP security headers, CSP, or cookie flags on socbench.org.** It
  is a static site on GitHub Pages with no authentication, no cookies, and no
  user data.
- **Volumetric denial of service**, rate-limit exhaustion, and load testing
  against socbench.org or against provider APIs using our code.
- **Self-XSS**, issues that need a fully compromised host or a
  physically-present attacker, and social engineering of maintainers.
- **Automated scanner output** with no demonstrated impact.

## Known design constraints

These are deliberate properties of the software. Reporting them as
vulnerabilities will get a polite pointer back to this section, but a
demonstrated *bypass* of any of them is very much in scope.

- **PatchLoop runs untrusted code on purpose.** It asks a model for a patch,
  applies it to a git workspace, and executes operator-supplied verifier
  commands (scanners, test suites, exploit reproductions) against the result.
  Run it in a container or a disposable VM, against repositories you trust,
  with credentials scoped to that job. Command timeouts kill the whole process
  group, which bounds runaway verifiers; they are not a security sandbox.
- **The benchmark corpus is data, not a trust boundary.** Network flow records
  and findings are attacker-influenced by nature. The harness constrains what
  an agent can *do* with them through read-only, persona-scoped tools and
  bounded budgets. Treat any gap in that constraint as in scope.
- **Provider API keys are read from the shell environment.** The repo never
  stores them. Anything that causes them to be written to disk or emitted in
  output is a bug we want to hear about.

## Acknowledgments

We credit everyone who reports a valid security issue, unless they ask to stay
anonymous. Reporters are listed here once the corresponding fix has shipped.

No reports yet. This section will be updated as they come in.
