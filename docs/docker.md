# Docker deployment

Docker mode packages all three CLI tools, Google Chrome, FFmpeg, an X virtual display, and a local
noVNC page in one Linux/amd64 container. It is intended for Docker Desktop on Windows or an
amd64 Linux Docker host.

The service runs as an unprivileged user with Chromium sandboxing enabled. Compose applies the
Playwright-recommended seccomp profile and host IPC so Chrome can create its sandbox namespaces
without using `--no-sandbox`, an unconfined seccomp policy, or `SYS_ADMIN` capability.

## Deploy in one command

Run from the repository root:

```powershell
docker compose up -d --build
```

The first build downloads the Python base image, Google Chrome, FFmpeg, and Python dependencies,
so it can take several minutes. Check readiness with:

```powershell
docker compose ps
docker compose logs -f toolkit
```

The service is ready when its health is `healthy`. The default host data directory is
`./docker-data`; it is ignored by Git and mounted at `/data` in the container. To keep data
elsewhere, set an explicit host path before the first deployment:

```powershell
$env:VIDEO_TOOLKIT_DATA = 'D:\video-toolkit-data'
docker compose up -d --build
```

Do not change `VIDEO_TOOLKIT_DATA` while a collection or Semvideo worker is running. The mounted
directory retains material workspaces, Semvideo jobs, browser login profiles, and model caches
across container recreation.

## Initialize a video workspace

The deployment does not silently choose or initialize a workspace. After choosing a persistent
container path under `/data`, initialize and diagnose it explicitly:

```powershell
docker compose exec toolkit semvideo init /data/workspace --json
docker compose exec toolkit semvideo doctor --workspace /data/workspace --json
```

Inject the Semvideo credential through the launching PowerShell environment, never through a
Dockerfile, build argument, committed `.env` file, or command argument:

```powershell
$env:SEMVIDEO_API_KEY = '<your key>'
docker compose up -d
docker compose exec toolkit semvideo doctor --workspace /data/workspace --json
```

Changing a Compose environment variable requires service recreation. `docker compose up -d`
performs that recreation without deleting `/data`.

## Interactive platform login

Open the local noVNC page:

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
```

When `material-collector` requests authentication, its visible Chrome window appears there. Keep
the collector command running while you complete login. The noVNC listener has no password because
Compose binds it to host loopback only; do not change the binding to `0.0.0.0` or publish it through
a public reverse proxy without adding transport security and authentication.

To use another local port:

```powershell
$env:VIDEO_TOOLKIT_NOVNC_PORT = '6081'
docker compose up -d
```

## Run the CLI tools

Place collection JSON files below the configured host data directory, for example
`docker-data/input/`. They appear in the container below `/data/input/`.

```powershell
docker compose exec toolkit material-collector contracts normalize `
  --input /data/input/collection-input.json `
  --query-plans /data/input/query-plans.json

docker compose exec toolkit material-collector run `
  --workspace /data/workspace `
  --input /data/input/collection-input.json `
  --query-plans /data/input/query-plans.json `
  --progress-format jsonl
```

The collector's stable exit-code contract is unchanged. In particular, exit code `20` means it
reached an `integration_required`, authentication, or decision checkpoint; inspect the final JSON
instead of treating every nonzero result as an unrecoverable failure.

Run other commands in the same long-lived service so detached Semvideo workers and collector
authentication state remain available:

```powershell
docker compose exec toolkit material-collector sessions list --workspace /data/workspace
docker compose exec toolkit semvideo status <job-id> --workspace /data/workspace --json
```

Do not use `docker compose run --rm` for Semvideo processing jobs: removing that one-off container
would also terminate its worker processes.

## Agent Skills

The container includes a read-only copy of all four Skills at `/opt/toolkit/skills`, but it does not
modify the host Agent's configuration. Install the Skills on the Agent host using the documented
`npx skills add` command. Docker deployment and host Skill installation are intentionally separate
security boundaries.

The existing collection Skill invokes a host-side PowerShell runner and therefore targets the
native Windows CLI installation. An Agent that should execute the containerized CLIs must call the
documented `docker compose exec toolkit ...` boundary or use a separately reviewed Docker-aware
Agent adapter; it must not pretend that the native runner controls a container executor.

## Stop and remove

```powershell
docker compose down
```

This removes the container and network but retains the configured host data directory. Do not add
`--volumes` as a cleanup shortcut if the Compose file has later been changed to use named volumes.
