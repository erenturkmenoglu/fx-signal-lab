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
| Cost | about $12 a month | electricity |
| Effort | about 15 minutes, once | Docker Desktop, power settings, router quirks |

The latency figures are estimates. The dashboard shows the measured p50, p95 and p99
once quotes flow.

Massive's servers are in Secaucus, New Jersey (Equinix NY2 and NY5), and Massive itself
recommends hosting close to them. So choose a region on the US east coast.

**Size:** 2 vCPU, 2 GB memory, 60 GB disk, Ubuntu 24.04 LTS. On Amazon Lightsail, which
is AWS without the complexity of EC2, that is the $12 a month Linux plan. Any provider
with a US east coast region does the same job.

### Server size: what was measured

Measured on 15 November 2026 with quotes from the local test provider and from the
simulator, not from Massive. How many quotes a second Massive sends for EUR/USD is not
known yet, so three rates were used. The disk figures come from synthetic quotes shaped
like EUR/USD; real quotes may compress better or worse.

| | Measured |
|---|---|
| Application | 65 to 95 MB; the same at 5, 20 and 50 quotes a second and with three pairs |
| Database | 530 MB once its cache has filled (it takes a quarter of the memory by design), 50 to 70 MB right after a start |
| Database, while it compresses a day of ticks | 180 to 280 MB more, for a few seconds |
| Application, while it recovers a gap | about 40 MB more per page of 50,000 quotes |
| Docker itself | 170 to 185 MB |
| Image build next to the running stack | about 375 MB at its peak (500 to 620 MB for the very first build, which also downloads the images) |
| CPU, application and database together | 1.3% of one core at 5 quotes a second, 2.0% at 20, 3.0% at 50 |
| Disk per pair and trading day | ticks: 108 bytes a quote (45 MB at 5 a second, 178 MB at 20), about a sixth of that once compressed after 7 days. 1-second grid: 13 MB, then 2.9 MB. Bars: 4.8 MB, then 1.1 MB |

What follows for the plan:

* **2 GB ($12) is enough.** Database, application and Docker need about 800 MB. Ubuntu
  and Tailscale were not measured; with a typical 250 MB for the two, the server uses a
  little over 1 GB of the 1.9 GB it really has. A rebuild fits next to that, even one
  that coincides with a compression run (about 1.7 GB in total). The setup script
  still adds a 2 GB swap file on servers below 4 GB, so that a build can never exhaust
  the memory. CI runs the stack on every push with the database limited to 1,000 MB and
  the application to 350 MB.
* **1 GB ($7) is not.** The database sizes itself smaller there (about 290 MB), but
  with everything else some 800 MB of 950 MB are taken before anything is built. Every
  update would push the running application into swap.
* **4 GB ($24) buys memory and disk, nothing else.** The $12 and $24 plans have the same
  two vCPUs with the same baseline of 20% each; this workload uses 1 to 3% of one.
* **Disk is not the constraint.** One pair at 20 quotes a second settles at about
  1.3 GB of ticks (30 days are kept) and adds about 1 GB a year for the 1-second grid
  and the bars. Eight pairs at that rate come to about 20 GB in the first year.

**When to move up:** when backtests over months of 1-second data are to run on the
server (Phase 3), or when `free -m` shows swap in use during normal operation. Lightsail
does not resize a server in place: you take a snapshot and create a larger instance
from it. That works upwards only, which is the reason to start small.

### If your AWS account is new

Accounts created with AWS's simplified sign-up work in one Region, which AWS picks (for
example Stockholm), and the console has no Region selector. Another Region needs
"advanced AWS features": a change that cannot be reversed, turns the account into an
AWS Organization and removes its spend limits for good. That is not worth it here.

Stay in the Region you are offered. A server in Europe receives the feed roughly 50 ms
later than one in Virginia, and it is about as much closer to a phone in Europe, so what
you see arrives at nearly the same time. For manual trading over seconds to minutes
that difference is small next to your own reaction time. If you want the US east coast
without that step, use a provider that lets you pick the region when you create the
server.

On AWS's free plan the account closes when the sign-up credit is used up or after six
months, and the server stops with it. Upgrade to the paid plan before that (AWS
Settings, Billing, Upgrade); unused credit still counts.

## 1. Create the server (Lightsail)

1. Sign in at <https://lightsail.aws.amazon.com> and choose **Create instance**.
2. Region: **Virginia (us-east-1)** if your account lets you choose, otherwise the
   Region you are offered (see "If your AWS account is new").
3. Platform: **Linux operating system** (older consoles: **Linux/Unix**, then
   **OS Only**). Blueprint: **Ubuntu 24.04 LTS**. Not one of the app blueprints such as
   WordPress or "Plesk Hosting Stack on Ubuntu".
4. Plan type **General purpose**, network type **Dual-stack**, size **$12** (2 GB
   memory, 2 vCPUs, 60 GB SSD). Why this size: "Server size" above.
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

The script installs Docker, adds a 2 GB swap file if the server has less than 4 GB of
memory, asks for the Massive API key (what you type or paste stays hidden), generates a
database password, writes both to `.env` (readable only by you), builds the image,
starts everything and waits until the application answers. The first build takes a few
minutes. Running the script again changes nothing that exists.

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
| Server (Lightsail, 2 GB) | $12 |
| Tailscale, personal plan | free |
| **Total** | **about $61** |

## A home PC instead

It works: install Docker Desktop, then steps 2 to 5 are the same (run the script in
WSL on Windows). Turn off sleep and automatic restarts. Every hour the PC is off is an
hour of missing live capture; after 72 hours the gap is no longer recovered
automatically.

## Sources

* [Where are Massive's servers located?](https://massive.com/knowledge-base/article/where-are-massives-servers-located)
* [Amazon Lightsail pricing](https://aws.amazon.com/lightsail/pricing)
* [Lightsail: baseline CPU performance of each plan](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)
* [Lightsail: a larger instance from a snapshot](https://docs.aws.amazon.com/lightsail/latest/userguide/how-to-create-larger-instance-from-snapshot-using-console.html)
* [AWS: activate advanced AWS features](https://docs.aws.amazon.com/accounts/latest/reference/activate-advanced-features.html)
* [AWS: choosing a Free Tier plan](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html)
* [Tailscale: host a private web server with Docker](https://tailscale.com/docs/solutions/host-private-server-docker)
* [Tailscale pricing](https://tailscale.com/pricing/)
