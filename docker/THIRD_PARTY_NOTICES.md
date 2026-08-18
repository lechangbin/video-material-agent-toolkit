# Docker third-party notices

## Playwright seccomp profile

`seccomp_profile.json` is copied from Microsoft Playwright `v1.62.0`:

- upstream repository: <https://github.com/microsoft/playwright>
- upstream path: `utils/docker/seccomp_profile.json`
- source tag: `v1.62.0`
- license: Apache License 2.0

The profile is the Docker default seccomp policy with the user-namespace permissions recommended
by Playwright for sandboxed Chromium. It is included unchanged.
