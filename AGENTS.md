# Repository Agent Instructions

These instructions apply to every Agent working from this repository.

## Fresh-machine setup

When the user asks to install or configure this toolkit from the repository URL:

1. Work on Windows x64 in PowerShell. Clone the default branch, then run commands from the
   repository root.
2. Do not reconstruct dependency, wheel, or Skill installation commands yourself. Run:

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1
   ```

3. The bootstrap script detects and, when missing, installs CPython 3.14, uv, Node.js, Chrome,
   FFmpeg, both released CLI tools, and all repository Skills for supported Agent hosts.
4. For an Agent host that is not supported by `npx skills`, pass each of its Skills roots with
   `-AdditionalSkillsDirectory`.
5. Treat the script's final JSON object as the authoritative result. If it reports a missing
   package manager, required restart, or failed command, report that exact blocker; do not invent
   process-control, PATH, package-install, or file-copy workarounds.
6. After installation, ask the user for the intended video workspace. Initialize and diagnose it
   only when the user has supplied that path:

   ```powershell
   semvideo init <workspace> --json
   semvideo doctor --workspace <workspace> --json
   ```

## Human-only boundaries

- Never put API keys, cookies, browser profiles, or tokens in this repository, a Skill, a command
  argument, a generated workspace file, or logs. Accept API keys only through the user's secret
  manager or process environment.
- Platform login is an interactive desktop action. Keep the authorized collector executor alive
  while the user completes any visible browser login. Do not create an alternative login command.
- Do not claim the toolkit is fully operational until `semvideo doctor` passes for the user's
  workspace. Installation success and workspace readiness are separate states.

## Safe inspection

To inspect prerequisites without changing the machine, run:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -CheckOnly
```

## Remote Windows verification

Before running remote compatibility, packaged-CLI, PowerShell, or browser-channel tests, read
`SSH-HANDOFF.md` and follow its verified connection, host-fingerprint, recovery, and safety
instructions.

- Use the document's Windows entry for Windows PowerShell 5.1/7, Edge/Chrome, packaged executable,
  and host integration tests. Invoke the required PowerShell edition explicitly inside the remote
  session.
- Use the Debian entry only for Linux, WSL, or Docker work; a passing Debian test is not evidence of
  native Windows compatibility.
- SSH is suitable for non-interactive execution. Visible browser login tests additionally require an
  active interactive desktop on the target Windows machine and human confirmation; SSH success alone
  does not satisfy that acceptance criterion.
- Record the remote host, shell version, browser channel, command result, and structured error output
  needed to reproduce each compatibility conclusion, without copying credentials or private-key
  material into the repository or logs.

## Docker deployment

- Treat `compose.yaml` as the supported container entry point. Build and start it with
  `docker compose up -d --build` from the repository root.
- Never pass API keys through Docker build arguments or bake them into an image. Docker mode may
  inherit `SEMVIDEO_API_KEY` from the launching process environment only.
- Preserve `/data` across container recreation. It owns browser authentication profiles, material
  workspaces, Semvideo state, and model caches; it must never be copied into an image or committed.
- The noVNC port must remain bound to host loopback by default. Interactive platform login is still
  a human action performed through the local noVNC page.
- Validate container changes with `docker compose config --quiet`, an image build, and smoke checks
  for both `material-collector --help` and `semvideo --version --json`.

## Repository changes

- Preserve the MIT `LICENSE` and third-party notices in distributions.
- Keep release installation checksum-verified.
- Validate PowerShell syntax, all four Skills, both test suites, Ruff, and strict mypy before a
  release-affecting change.

## Agent skills

### Issue tracker

Issues and implementation specs are tracked in GitHub Issues. Follow
`docs/agents/issue-tracker.md` when creating, reading, relating, updating, or closing
work items.

### Triage labels

Use the repository's standard triage labels and transitions described in
`docs/agents/triage-labels.md`.

### Domain docs

This repository contains multiple bounded contexts. Start with
`CONTEXT-MAP.md`, then read the relevant package `CONTEXT.md` and ADRs.
Follow `docs/agents/domain.md` when changing domain terminology, boundaries,
or architectural decisions.
