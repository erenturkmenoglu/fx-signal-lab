# Deployment: an always-on server and the dashboard on your phone

Checked on 15 November 2026. Prices and console labels change; the links at the end are
the places to re-check them.

Nothing in this document has been executed by the author: the build environment had no
Docker and no cloud account. The setup script was tested against stand-ins for Docker
and the application (key handling, re-runs, failure paths). Treat the first real run as
a test and report what differs.

## What to run it on

A small cloud server, not a home PC.

| | Cloud server | Home PC |
|---|---|---|
| Always on | yes | only while it never sleeps, updates or loses power or internet |
| Distance to Massive | same coast: a few milliseconds | from Europe: on the order of 100 to 150 ms round trip |
| Reachable from your phone | yes, see step 5 | the same way, but only while the PC is up |
| Cost | about $24 a month | electricity |
| Effort | about 15 minutes, once | Docker Desktop, power settings, router quirks |

The latency figures are estimates. The dashboard shows the measured p50, p95 and p99
once quotes flow.

Massive's servers are in Secaucus, New Jersey (Equinix NY2 and NY5), and Massive itself
recommends hosting close to them. So choose a region on the US east coast.

**Size:** 2 vCPU, 4 GB memory, 80 GB disk, Ubuntu 24.04 LTS. On Amazon Lightsail, which
is AWS without the complexity of EC2, that is the $24 a month Linux plan in the Virginia
region. Any provider with a US east coast region does the same job.

4 GB is for the image build and for TimescaleDB with room to spare. 2 GB probably works
and has not been tried.

## 1. Create the server (Lightsail)

1. Sign in at <https://lightsail.aws.amazon.com> and choose **Create instance**.
2. Region: **Virginia (us-east-1)**.
3. Platform: **Linux/Unix**. Blueprint: **OS Only**, then **Ubuntu 24.04 LTS**.
4. Plan: **$24** (4 GB memory, 2 vCPUs, 80 GB SSD).
5. Give it a name and create it. When it is running, open **Connect using SSH**: a
   terminal in the browser, no key files needed.

Under **Networking** you can delete the HTTP (port 80) rule. Only SSH needs to stay.

## 2. Get the code

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/YOUR-GITHUB-USER/fx-signal-lab.git
cd fx-signal-lab
```

That is all for a public repository. If the repository is private, the server has to
sign in to GitHub once, before the clone:

```bash
sudo apt-get install -y gh
gh auth login
```

Answer: GitHub.com, HTTPS, Yes, **Login with a web browser**. It prints a code; open
<https://github.com/login/device> on your phone and type it in. Then clone with
`gh repo clone YOUR-GITHUB-USER/fx-signal-lab`.

## 3. Start it

```bash
./scripts/server-setup.sh
```

The script installs Docker, asks for the Massive API key (what you type or paste stays
hidden), generates a database password, writes both to `.env` (readable only by you),
builds the image, starts everything and waits until the application answers. The first
build takes a few minutes. Running the script again changes nothing that exists.

The key lives only in `.env` on the server. It is never part of the repository.

## 4. Check the provider

```bash
sudo make verify
```

(`make verify` without `sudo` works after you have logged out and in once.)

It stops the application for a moment, because Massive allows one WebSocket connection,
runs seven checks with your key and prints a report. Keep the report.

On a weekend the six REST checks should say `OK` and the WebSocket check `NODATA`, with
the note that it authenticated and subscribed and that the FX market is closed. Quotes
start on Sunday at 17:00 New York time (22:00 UTC in winter, 21:00 UTC in summer). Run
it again then: every check should say `OK`.

## 5. The dashboard on your phone

The dashboard has no login of its own, so it must not be put on the public internet.
Tailscale gives your devices a private network instead: only devices signed in to your
account can open the dashboard, and no port is opened to anyone else. The personal plan
is free.

On the server:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8000
```

* `tailscale up` prints a link. Open it and sign in with a Google, Apple, Microsoft or
  GitHub account.
* The first `tailscale serve` prints a link that turns on HTTPS for your network. Open
  it, confirm, and run the command again. It then prints the address of the dashboard:
  `https://<server-name>.<your-network>.ts.net/`. `--bg` keeps it running across
  restarts.
* In the Tailscale admin console, under Machines, open the server's menu and choose
  **Disable key expiry**. Otherwise the server drops out of the network after about six
  months.

On the phone: install the Tailscale app, sign in with the same account, leave it
connected, and open the address in the browser. "Add to Home Screen" makes it behave
like an app.

Use your own phone on mobile data. An office network may block Tailscale.

## 6. First look

1. Header: **Live market data**. "Test endpoint" or "Mock data" would mean a wrong
   `.env`; "No data provider" means the key is missing.
2. System health: feed **Streaming**, latency figures with no warning under them.
3. Market monitor: EUR/USD with a quote age of a second or two during market hours.
4. Data gaps: empty, or entries marked "Recovered".

What each message means and what to do about it is in [RUNBOOK.md](RUNBOOK.md).

## Day to day

```bash
git pull && docker compose up -d --build    # update to the latest version
docker compose logs -f app                  # follow the log
make backup                                 # database dump into backups/
```

A restart or a reboot is safe. The downtime is recorded and recovered from Massive's
history, up to 72 hours automatically.

For backups, also turn on automatic snapshots of the instance in the Lightsail console.
`make backup` writes to the same disk as the database.

## Monthly cost

| Item | Cost |
|---|---|
| Massive Currencies Starter | $49 |
| Server (Lightsail, 4 GB) | $24 |
| Tailscale, personal plan | free |
| **Total** | **about $73** |

## A home PC instead

It works: install Docker Desktop, then steps 2 to 5 are the same (run the script in
WSL on Windows). Turn off sleep and automatic restarts. Every hour the PC is off is an
hour of missing live capture; after 72 hours the gap is no longer recovered
automatically.

## Sources

* [Where are Massive's servers located?](https://massive.com/knowledge-base/article/where-are-massives-servers-located)
* [Amazon Lightsail pricing](https://aws.amazon.com/lightsail/pricing)
* [Tailscale: host a private web server with Docker](https://tailscale.com/docs/solutions/host-private-server-docker)
* [Tailscale pricing](https://tailscale.com/pricing/)
