# Docker

[Back to README](../README.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

The image includes Python, stem4b and FFmpeg. It does **not** include model weights,
a model server, books or credentials. You still need compatible LLM and TTS endpoints.

## Build

From the repository root, with Docker Engine or Docker Desktop running:

```sh
docker build -t stem4b:local .
docker run --rm stem4b:local --help
```

The build downloads dependencies. `.dockerignore` allows only application files
into the build context; local books, `.env`, personal configurations, caches and
Git history are excluded. The runtime uses an unprivileged user. Review
[dependency licensing](../THIRD_PARTY_NOTICES.md) before redistributing an image.

## Configure and convert (macOS / Linux)

Create a working directory, copy your input book into it, and run the following
commands **from that directory**. It need not be the source checkout.

```sh
docker run --rm --init --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/data" stem4b:local init
```

Edit `stem4b.toml` and `.env` on your host, just as in the native quick start.
Use container-visible paths in the configuration. Files in this directory appear
under `/data`; host paths outside the mount are not accessible.

```sh
docker run --rm --init --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/data" stem4b:local doctor

docker run --rm --init --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/data" stem4b:local \
  convert textbook.pdf --until narrate

# Review/edit textbook.work/narration.txt on the host, then:
docker run --rm --init --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/data" stem4b:local \
  synthesize textbook.work/narration.txt -o textbook.m4b
```

The M4B and `.work` directory persist on your host. `--rm` removes the container,
not those files. `--user` gives new files your host UID/GID; the mount must be
writable by that user. The app loads `/data/.env` itself: Docker's `--env-file` is
not necessary. Alternatively, pass credentials as runtime environment variables;
never put them in a Dockerfile or build arguments.

Omit `--until narrate` for a one-command conversion. Add `-it` for guided TOC repair:

```sh
docker run --rm --init -it --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/data" stem4b:local \
  repair-toc textbook.work
```

Resume using the same command and mount location. Receipts and some metadata
contain absolute paths; avoid switching a partly completed run between native
host paths and different container paths. Use a fresh workspace when changing
execution environments if cached assets no longer resolve.

## Windows PowerShell

Use Docker Desktop with Linux containers. From your book working directory:

```powershell
docker run --rm --init --mount "type=bind,src=$($PWD.Path),dst=/data" stem4b:local init
# Edit stem4b.toml and .env, then:
docker run --rm --init --mount "type=bind,src=$($PWD.Path),dst=/data" stem4b:local doctor
docker run --rm --init --mount "type=bind,src=$($PWD.Path),dst=/data" stem4b:local convert textbook.pdf --until narrate
docker run --rm --init --mount "type=bind,src=$($PWD.Path),dst=/data" stem4b:local synthesize textbook.work/narration.txt -o textbook.m4b
```

The image's non-root user is used here. If access is denied, check Docker Desktop
file sharing and host directory permissions. Do not solve it by baking credentials
or books into the image.

## Connecting to a model server on the host

Inside a container, `localhost` means the container, not your computer. With
[Docker Desktop](https://docs.docker.com/desktop/features/networking/networking-how-tos/),
replace a host endpoint such as `http://localhost:4000/v1` with
`http://host.docker.internal:4000/v1` in the mounted configuration.

On Linux Docker Engine, add
`--add-host=host.docker.internal:host-gateway` to each relevant `docker run` command.
The host server must accept connections from the Docker network; a server bound
only to host loopback may not. Configure its bind address and firewall deliberately,
without exposing an unauthenticated model service to public networks. See
[Docker's host-gateway documentation](https://docs.docker.com/reference/cli/docker/container/run/#add-entries-to-container-hosts-file---add-host).

For another container on a shared Docker network, use that service's network name
and pass `--network NETWORK`. Remote hosted-provider URLs normally need no changes.
There are no inbound application ports to publish: stem4b is a CLI, not a web server.

`doctor` remains offline, so it cannot detect network connectivity failures. Try
one small conversion after checking endpoint URLs.
