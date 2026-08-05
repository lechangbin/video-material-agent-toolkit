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

## Repository changes

- Preserve the MIT `LICENSE` and third-party notices in distributions.
- Keep release installation checksum-verified.
- Validate PowerShell syntax, all four Skills, both test suites, Ruff, and strict mypy before a
  release-affecting change.
