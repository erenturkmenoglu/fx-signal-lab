# Deployment: an always-on server and the dashboard on your phone

Checked on 15 November 2026. Prices and console labels change; the links at the end are
the places to re-check them.

The author's build environment has no Docker and no cloud account, so the steps were
written without running them; the setup script was tested against stand-ins for Docker
and the application (key handling, swap file, re-runs, failure paths). Steps 1 to 4
have since been run on a Lightsail server (15 November 2026), and step 4 printed what
is described there. The domain of step 5.4 was tried with the real Caddy in front of the
real application, but only on a trial name; what that leaves untested is said there.
Report what differs for you.

## What to run it on

A small cloud server, not a home PC.

| | Cloud server | Home PC |
|---|---|---|
| Always on | yes | only while it never sleeps, updates or loses power or internet |
| Distance to Massive | same coast: a few milliseconds | from Europe: on the order of 100 to 150 ms round trip |
| Reachable from your phone or any browser | yes, see step 5 | the same way, but only while the PC is up |
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
simulator, not from Massive, at three rates. Massive's history shows 1.4 to 1.8 quotes
a second for EUR/USD in the last half hour before a weekly close; busy hours are not
known yet. The disk figures come from synthetic quotes shaped like EUR/USD; real quotes
may compress better or worse.

| | Measured |
|---|---|
| Application | 65 to 95 MB; the same at 5, 20 and 50 quotes a second and with three pairs |
| Database | 530 MB once its cache has filled (it takes a quarter of the memory by design), 50 to 70 MB right after a start |
| Database, while it compresses a day of ticks | 180 to 280 MB more, for a few seconds |
| Application, while it recovers a gap | about 40 MB more per page of 50,000 quotes |
| Docker itself | 170 to 185 MB |
| Caddy, only with a domain of your own (5.4) | 45 MB after its start, 51 MB after 80 requests and a live stream, on the development machine. CI limits it to 100 MB |
| Image build next to the running stack | about 375 MB at its peak (up to about 670 MB for the very first build, which also downloads the images) |
| A backtest (a year of one-minute bars) | about 165 MB for 2 to 3 seconds, in a process of its own |
| A signal board (81 methods on the same year) | about 210 MB for 8 seconds on the development machine, in a process of its own; a month: 115 MB for 2 seconds. Not yet timed on the server |
| A setup (two methods on the same year, with the momentum reading of all 40 momentum methods) | about 200 MB for 5 seconds on the development machine, in a process of its own; a month: 110 MB for 2 seconds. The chart window of the board: 100 to 125 MB for 2 seconds |
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

Under **Networking** you can delete the HTTP (port 80) rule. Only SSH needs to stay,
unless the dashboard gets a domain of its own (5.4), which needs ports 80 and 443.

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
start on Sunday at 17:00 New York time (22:00 UTC in winter, 21:00 UTC in summer).

Once quotes have been flowing for about fifteen minutes:

```bash
sudo make reconcile
```

It compares the last ten settled minutes of live ticks with Massive's quote history and
prints what is equal and what differs. It only reads, so the feed is not interrupted.
(`make verify` can be repeated too, but it stops the application for about half a
minute; the gap is recovered afterwards.)

## 5. The dashboard on your phone and in any browser

Two steps: a password, then an address.

### 5.1 A password

```bash
sudo make password
```

It asks for the password twice (hidden, at least 12 characters), stores only a hash of
it in `.env` and restarts the application. From then on the dashboard, its API and the
live stream need a login; a browser stays signed in for 30 days. Running it again
changes the password and signs every browser out. `sudo make password ARGS=--remove`
turns the login off again.

**Without a password the dashboard must not get a public address.** It shows the data
and can start downloads.

### 5.2 A public address without a domain

Tailscale Funnel gives the server an address like
`https://<server-name>.<your-network>.ts.net`. It opens in any browser, with a valid
certificate, without opening a port on the server and without installing anything on
the phone or PC. Tailscale's personal plan is free. Funnel is in beta and its bandwidth
is limited, which does not matter for a dashboard.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8000
```

* `tailscale up` prints a link. Open it and sign in with a Google, Apple, Microsoft or
  GitHub account.
* The first `tailscale funnel` prints a link that turns on HTTPS and Funnel for your
  network. Open it, confirm, and run the command again. It then prints the address.
  `--bg` keeps it published across restarts.
* In the Tailscale admin console, under Machines, open the server's menu and choose
  **Disable key expiry**. Otherwise the server drops out after about six months.
* `sudo tailscale funnel status` shows what is published. `sudo tailscale funnel reset`
  takes it off the internet again.

On the phone, "Add to Home Screen" makes the page behave like an app.

The author has no Tailscale account in the build environment: these commands are from
Tailscale's documentation and were not run. The login itself was tested in a real
browser against the running application.

### 5.3 Private instead of public

`sudo tailscale serve --bg 8000` instead of `funnel` publishes the same address only to
devices that run the Tailscale app and are signed in to your account. That is the more
private choice if every device you use can run the app. One port is either private or
public: the last of the two commands wins.

### 5.4 A domain of your own

An address like `https://signale.example` instead of the Tailscale one. Funnel cannot
give it: it serves names under `ts.net` only. With a domain the server answers on the
internet itself. Caddy, a small web server, sits in front of the application, gets the
HTTPS certificate from Let's Encrypt, renews it and sends plain `http` on to `https`.
The password of 5.1 stays the only way in, and the command below refuses to run
without one.

1. **A name.** Any registrar will do, and the name does not matter to the software.
   Inside Lightsail: **Domains & DNS**, then **Register domain**; Lightsail creates the
   DNS zone for it by itself. A `.com` costs about $15 a year there (Route 53's price
   list, July 2026); other endings cost less or more. Leave the privacy protection for
   your contact details on.
2. **An address that stays.** A Lightsail server gets a new public address when it is
   stopped and started, unless a static IP is attached: instance page, **Networking**,
   **Create static IP** (older consoles: **Attach static IP**). It costs nothing while
   it is attached.
3. **Two open ports.** Same page, **IPv4 firewall**, **Add rule**: HTTPS (TCP 443), and
   HTTP (TCP 80) again if you deleted it in step 1. Port 80 answers with a redirect to
   HTTPS and lets Let's Encrypt check that the name is yours.
4. **The name points at the server.** With the name in Lightsail: instance page,
   **Domains**, **Assign domain**. At another registrar: an `A` record from the name to
   the static address. A subdomain such as `fx.signale.example` works the same way.
5. **Turn it on.**

   ```bash
   cd ~/fx-signal-lab && git pull
   sudo make domain ARGS="--check signale.example"    # optional: the checks alone
   sudo make domain ARGS=signale.example
   ```

   It checks that a password is set and that the name points at this server, and
   changes nothing if not. Then it restarts the application (a few seconds of the feed,
   recovered like after any restart), starts Caddy and waits until `https://` answers
   with a valid certificate, usually within a minute. If that has not happened after
   four minutes it prints what Caddy reported and puts everything back as it was. Run it
   again when the reason is fixed.
6. **One door.** Funnel keeps publishing the old address until it is told to stop:
   `sudo tailscale funnel reset`. Do that once the new address works.

Good to know:

* The name is public from the moment the certificate exists (a Funnel address is too).
  Let's Encrypt publishes every certificate it issues, and programs that scan the
  internet read that list. They reach the login page and the health check, nothing
  else; the password is what stands between them and the dashboard
  ([RISKS.md](RISKS.md), R6).
* `sudo make domain` shows what is set. `sudo make domain ARGS=--remove` stops Caddy,
  which closes the two ports on the server. For another name, run the command with it.
* While a domain is set, `make password ARGS=--remove` refuses. An application that
  finds a domain in `.env` and no password serves nothing and says why.
* The certificate lives in the Docker volume `caddy_data`. Renewal needs nothing from
  you while the two ports stay open and the name points at the server.
* The registration of the name ends after a year unless it renews itself.
* A trial without a name: `sudo make domain ARGS=fxlab.localhost` serves the dashboard
  to the server itself only, with a certificate Caddy signs. CI does this on every push.

Tested: the real Caddy in front of the real application on such a trial name, by hand
with a browser and in CI (the redirect, the certificate, the login and its cookie, the
live stream, compression, the refusals, turning it off). Not tested: a certificate from
Let's Encrypt for a real name, the Lightsail console steps (they are from its
documentation), and Funnel and Caddy running side by side.

### 5.5 A work computer

Company networks often block addresses they do not know, and a `ts.net` address may be
one of them. A domain of your own (5.4) is the next thing to try. A name registered
days ago can be blocked as well: several corporate web filters have a category for
domains younger than about a month. Check your employer's rules for personal trading
tools and for what a work device may be used for.

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
sudo make domain                            # the domain that is set, if any (5.4)
```

With a domain, Caddy is part of the stack and is updated by the first command. Only a
changed `Caddyfile` needs one more: `docker compose restart caddy`.

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
| A domain of your own (optional, 5.4) | about $1.25 ($15 a year for a .com) |
| **Total** | **about $61, $62 with a domain** |

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
* [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel) and the [`tailscale funnel` command](https://tailscale.com/docs/reference/tailscale-cli/funnel)
* [Tailscale pricing](https://tailscale.com/pricing/)
* [Caddy: automatic HTTPS](https://caddyserver.com/docs/automatic-https) and the [Caddy image](https://hub.docker.com/_/caddy)
* [Lightsail: IP addresses and static IPs](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-public-ip-and-private-ip-addresses-in-amazon-lightsail.html) and the [firewall rules reference](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-firewall-rules-reference.html)
* [Lightsail: register a domain](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-register-new-domain.html) and [route a domain to an instance](https://docs.aws.amazon.com/en_us/lightsail/latest/userguide/amazon-lightsail-routing-to-instance.html)
* [Route 53's domain prices, as tracked by tldes.com](https://tldes.com/registrars/amazon)
* [Let's Encrypt: certificate transparency logs](https://letsencrypt.org/docs/ct-logs/)
* [Palo Alto Networks: the newly-registered-domain category](https://docs.paloaltonetworks.com/pan-os/10-0/pan-os-admin/url-filtering/url-categories/url-risk-categories)
