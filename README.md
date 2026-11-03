# tapo-energy-collector

Measures how much electricity the UGent Racing workshop machines use (3D printers, soldering stations, …) with
**TP-Link Tapo P110** smart plugs, and shows it on the **CCBOM website** under **Energy**.

A small Python program on a workshop computer reads every plug every few seconds and saves each reading in a local
database first. It then uploads the readings to the website over HTTPS. If the internet or the website is down, the
readings wait on the workshop computer and are uploaded automatically later. The program starts by itself when the
computer boots and restarts itself after a crash.

> **It only reads the plugs.** It never switches a plug on or off, and the website has no buttons to do so.

---

## Contents

1. [What the system does](#1-what-the-system-does)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Install Python and the collector](#4-install-python-and-the-collector)
5. [Configure the Tapo credentials](#5-configure-the-tapo-credentials)
6. [Discover the plugs](#6-discover-the-plugs)
7. [Register a new plug](#7-register-a-new-plug)
8. [Configure the website endpoint](#8-configure-the-website-endpoint)
9. [Run a local test](#9-run-a-local-test)
10. [Automatic start (service)](#10-automatic-start-service)
11. [Verify that data arrives on the website](#11-verify-that-data-arrives-on-the-website)
12. [Troubleshooting](#12-troubleshooting)
13. [Rotate passwords and tokens](#13-rotate-passwords-and-tokens)
14. [After the computer has been offline](#14-after-the-computer-has-been-offline)
15. [Maintenance: add/remove plugs, update, dependencies](#15-maintenance)
16. [How it works (for developers)](#16-how-it-works-for-developers)

---

## 1. What the system does

* Reads each configured P110 every `polling_interval_seconds` (default 10 s):

  | Value | Unit | Notes |
  |---|---|---|
  | power | W | instantaneous |
  | energy today | kWh | the plug's own counter, **resets at local midnight** |
  | energy this month | kWh | the plug's own counter, **resets on the 1st** |
  | voltage, current | V, A | only plugs with recent firmware; otherwise empty |
  | online | yes/no | whether the plug answered |

* Stamps every reading with the UTC time at which it was taken, and checks that values are plausible (e.g. 0–4500 W).
  Values the plug does not provide stay empty. Nothing is ever estimated or invented.
* Stores every reading in SQLite **before** uploading it, and uploads in batches. A reading only counts as delivered
  after the website has confirmed it.
* The website shows the plugs and their latest power, when each was last read, whether it is online, offline or has
  no recent data, a power graph and the kWh used per day or over a date range.

## 2. Architecture

```text
 Tapo P110 plugs ──(local Wi-Fi / LAN, python-kasa, read-only)──┐
                                                                ▼
                                    Workshop computer (Linux, systemd service "tapo-collector")
                                      • one polling task per plug (an offline plug never blocks the others)
                                      • SQLite buffer /var/lib/tapo-collector/collector.sqlite
                                      • uploader: batches, retries with backoff, idempotent IDs
                                                                │
                                   outbound HTTPS POST only     │   Authorization: Bearer <ingestion token>
                                   (no port forwarding, no VPN) ▼
                                    Azure VM "ugr-server": Caddy (HTTPS) → CCBOM Next.js app
                                      • POST /api/energy/measurements   (token, write-only)
                                      • SQLite tables energy_devices / energy_measurements
                                                                │
                                                                ▼
                                    CCBOM website → "Energy" page (member login, read-only)
```

The website never connects to the workshop: all traffic starts at the workshop computer. No TP-Link cloud API or paid
service is needed. python-kasa talks to the plugs directly on the local network. The TP-Link account is only used
to log in to the plugs, because newer firmware requires it.

## 3. Prerequisites

* **A computer that is always on in the workshop**, with **Linux** (Debian 12/13, Ubuntu 24.04, or Raspberry Pi OS
  bookworm). A Raspberry Pi 4/5 or an old laptop with Linux is enough.
  Windows 10/11 also works (see [Windows](#windows-1011-alternative)). **Windows Vista/7 cannot run it**: current
  Python and python-kasa do not support them, and they no longer receive security updates.
* **Python 3.11 or newer** (the default on the systems above).
* The computer and the plugs on the **same local network**, or at least able to reach each other over TCP port 80.
  UDP discovery is optional.
* The **Tapo app account** (email and password) that the plugs were set up with.
* An **ingestion token** from a website administrator ([section 8](#8-configure-the-website-endpoint)).
* Ask the network administrator for a **fixed IP address (DHCP reservation) for each plug**. Then the address in the
  configuration never changes.

## 4. Install Python and the collector

On the workshop computer, in a terminal:

```sh
sudo apt update
sudo apt install -y git python3 python3-venv
python3 --version          # must say 3.11 or higher
git clone https://github.com/UgentRacing/tapo-energy-collector.git
cd tapo-energy-collector
sudo sh deployment/linux/install.sh
```

The installer:

* creates a system user `tapo-collector` (no login, no home directory);
* installs the program with the exact tested library versions (`requirements.lock`) in `/opt/tapo-collector/venv`;
* creates `/etc/tapo-collector/config.yaml` (settings) and `/etc/tapo-collector/collector.env` (secrets,
  readable by root only);
* installs and enables the systemd service `tapo-collector` and the command `sudo tapo-collector …`.

Running it again is safe: it is also the update command. Your configuration, secrets and queued readings are kept.

## 5. Configure the Tapo credentials

```sh
sudo nano /etc/tapo-collector/collector.env
```

```dotenv
TAPO_USERNAME=the-tapo-app-email@example.org
TAPO_PASSWORD=the-tapo-app-password
ENERGY_API_TOKEN=          # filled in at step 8
```

* Use the account that owns the plugs in the Tapo app. If the password contains spaces, `#` or quotes, put it between
  single quotes: `TAPO_PASSWORD='my pass#word'`.
* The file must stay `-rw------- root root` (the installer sets this). Never copy it into git, a chat or a screenshot.

## 6. Discover the plugs

Set up each plug in the Tapo app first (Wi-Fi, name). Then find its IP address:

**Option A: automatic discovery** (only works when broadcasts are allowed on the network, often not on university networks):

```sh
sudo tapo-collector discover
# or for a specific subnet: sudo tapo-collector discover --target 192.168.1.255
```

It lists every plug with its IP address, model and name, and prints a ready-to-paste `devices:` block.

**Option B: manually.** In the Tapo app open the plug, then ⚙ → *Device info*, and note the **IP address** and the
**MAC address**. You can also find them in the router's list of DHCP clients.

Then **reserve that IP address for the plug's MAC address** in the router (DHCP reservation), or ask the network
administrator to do it. Without a reservation the address can change and the plug will appear offline.

Test one plug. This only reads it:

```sh
sudo tapo-collector test-device --host 192.168.1.50
```

## 7. Register a new plug

A plug has to be added in **two places**, with the **same ID**:

1. **Collector**: add it to `/etc/tapo-collector/config.yaml`:

   ```yaml
   devices:
     - id: printer_01              # lowercase letters, digits, _ or -; never reuse an ID for another machine
       name: "3D Printer 1"
       host: "192.168.1.50"        # its reserved IP address (or hostname)
       machine: "Prusa MK4"        # optional
       category: "3D printer"      # optional
       # polling_interval_seconds: 30   # optional, default from the top of the file
   ```

2. **Website** (administrator): **Energy → Register plug**, using the same *Device ID*. Name, machine and category
   can be changed there later. *Machine* suggests the tools from **Processes & tools**.

Then:

```sh
sudo tapo-collector check-config
sudo tapo-collector test-device --device printer_01
sudo systemctl restart tapo-collector
```

If you forget step 2, nothing is lost. The website answers `unknown_device`, the collector keeps those readings and
logs `Register them on the website's Energy page`, and it retries every 10 minutes. Run `sudo tapo-collector retry-now`
to send them immediately after registering.

## 8. Configure the website endpoint

1. **Create an ingestion token** (a website administrator, on the Azure VM, in the CCBOM folder):

   ```sh
   docker compose exec app node scripts/admin.mjs energy-token create workshop-pc
   ```

   It prints a token starting with `tec_`. It is shown **only once**; the website stores only a hash of it.
   Copy it into `ENERGY_API_TOKEN=` in `/etc/tapo-collector/collector.env`. Do not reuse anyone's website password.

2. **Set the endpoint** in `/etc/tapo-collector/config.yaml`:

   ```yaml
   upload:
     endpoint_url: "https://ccbom.ugentracing.be/api/energy/measurements"
   ```

   (This is the production CCBOM address from the CCBOM repository; use the address your website actually runs on.
   It must start with `https://`.)

3. Check it:

   ```sh
   sudo tapo-collector test-upload      # OK: ... accepted the token
   ```

## 9. Run a local test

Before (or instead of) the service, you can run everything by hand. This is also how you develop on your own laptop:

```sh
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.lock && pip install -e ".[dev]"
cp config.example.yaml config.yaml                  # edit: endpoint_url and devices
cp .env.example .env                                # edit: secrets   (chmod 600 .env)
tapo-collector check-config
tapo-collector test-device --device printer_01
python scripts/test_connection.py                   # config + website token + every plug, one summary
tapo-collector run                                  # Ctrl+C stops it cleanly
```

Automated tests (no plugs or website needed): `pytest`. Lint and type checks: `ruff check src tests scripts && mypy`.

The full manual test with one real P110 (plug offline, internet outage, reboot, crash, energy check) is in
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## 10. Automatic start (service)

The installer already enabled the service. It starts at every boot, after the network is up and the clock is
synchronised, and it restarts 15 s after any crash.

| What | Command |
|---|---|
| start / stop / restart | `sudo systemctl start tapo-collector` / `stop` / `restart` |
| is it running? | `systemctl status tapo-collector` |
| collector health (queue, uploads, plugs) | `sudo tapo-collector status` (exit code 0 = healthy, 1 = warning) |
| live log | `journalctl -u tapo-collector -f` |
| log files (rotated at 5 MB, 5 kept) | `/var/log/tapo-collector/collector.log` |
| local buffer (SQLite) | `/var/lib/tapo-collector/collector.sqlite` |
| uninstall (keeps config and data) | `sudo sh deployment/linux/uninstall.sh` (add `--purge` to delete everything) |

`stop` is graceful: the current upload finishes, a final upload is attempted, and anything left waits for the next
start. The service runs as the unprivileged user `tapo-collector`, with a read-only system and no access to home
folders. Only its own data and log folders are writable.

### Windows 10/11 (alternative)

Install Python 3.11+ from python.org, then in an **administrator** PowerShell in the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File deployment\windows\install.ps1
```

This creates the scheduled task `TapoEnergyCollector` (runs at boot as SYSTEM and restarts after crashes), and puts
the configuration, secrets (`collector.env`, readable by SYSTEM and Administrators only), buffer and logs in
`C:\ProgramData\TapoCollector`. Control it with `Start-ScheduledTask` / `Stop-ScheduledTask TapoEnergyCollector`.
`Stop-ScheduledTask` stops the process immediately: queued readings are safe, but the last upload is repeated at the
next start. Remove it with `deployment\windows\uninstall.ps1`.

## 11. Verify that data arrives on the website

1. `sudo tapo-collector status` shows `0 pending` (or a small number that goes down) and a recent `Last upload OK`.
2. Website → **Energy** (any member): each plug shows **Online**, its current power, and "Last reading … s ago".
   The top right shows "Last upload from workshop-pc: … ago".
3. The power graph and **Energy** table fill in for today.
4. On the VM, `docker compose logs app | grep "Energy upload"` shows only uploads with rejected readings. No output is good.

Status meanings on the website:

* **Online**: recent reading, the plug answered.
* **Offline**: the collector runs, but the plug does not answer (unplugged, Wi-Fi, changed IP).
* **No recent data**: nothing arrived for 3 polling intervals (at least 3 minutes). The workshop computer,
  collector or internet is down; readings are being buffered there.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `test-device`: `FAILED (auth) TP-Link login rejected` | Wrong `TAPO_USERNAME`/`TAPO_PASSWORD`, or the plug belongs to another Tapo account. Log in to the Tapo app with the same account and check the plug is listed there. |
| `FAILED (unreachable)` | Wrong IP (check in the Tapo app), plug unplugged, or the computer is on a different network or VLAN (guest Wi-Fi often isolates devices). Try `ping <ip>`. |
| `discover` finds nothing | Broadcasts are blocked (normal on managed networks). Use the IP from the Tapo app ([section 6](#6-discover-the-plugs)). |
| `FAILED (unsupported) … has no energy monitoring` | That is not a P110 (e.g. a P100). Only energy-monitoring plugs work. |
| Plug was fine, now always offline | Its IP changed: make a DHCP reservation and update `host:`. After a firmware update the cached connection is renewed automatically. |
| `test-upload`: `HTTP 401` | The token is wrong or revoked. Create a new one ([section 13](#13-rotate-passwords-and-tokens)). |
| `HTTP 404` / `endpoint_url` error | Wrong URL, or the website version without energy monitoring is still deployed. |
| `network error` / `timed out` | No internet, DNS problem, or a proxy/firewall blocks outbound HTTPS. Readings are kept; check `curl -I https://<website>/api/health`. |
| Log: `does not accept device(s) …` | Register the plug on the website with exactly that ID, then `sudo tapo-collector retry-now`. |
| Log: `permanently rejected` | See `sudo tapo-collector status` (shows the reason). After fixing the cause: `sudo tapo-collector requeue-rejected`. |
| Log: `System clock is not set` | The clock is not synchronised (Raspberry Pi without internet at boot). Check `timedatectl`. Readings resume once NTP works. |
| Log: `queue limit … dropped` | Uploads have failed for a very long time and the buffer is full (`storage.max_pending_measurements`). Fix the upload problem first. |
| Service does not start | `journalctl -u tapo-collector -n 50`, then `sudo tapo-collector check-config` shows configuration errors in plain language. |

For more detail, set `logging.level: DEBUG` in the configuration and restart.

## 13. Rotate passwords and tokens

**Ingestion token** (e.g. yearly, when a team member with access leaves, or if it may have leaked):

```sh
# on the Azure VM
docker compose exec app node scripts/admin.mjs energy-token rotate workshop-pc   # prints the new token; the old one stops working
# on the workshop computer
sudo nano /etc/tapo-collector/collector.env      # paste the new ENERGY_API_TOKEN
sudo systemctl restart tapo-collector
sudo tapo-collector test-upload
```

Readings taken between the rotation and the restart are kept and uploaded afterwards. `energy-token list` shows all
collectors and their last upload. `energy-token revoke <name>` blocks a collector immediately.

**Tapo account password**: change it in the Tapo app, then update `TAPO_PASSWORD` in `collector.env` and run
`sudo systemctl restart tapo-collector`. Then check with `sudo tapo-collector status`.

## 14. After the computer has been offline

Nothing needs to be done:

* **Internet or website down, computer on**: readings are collected and stored locally. When the connection
  returns, the backlog is uploaded in batches of 200, oldest first. The upload status in `sudo tapo-collector status`
  goes back to normal.
* **Computer off or collector stopped**: no readings exist for that time, and none are invented. The power graph
  shows a gap. The **kWh total still includes that energy** as long as the plugs had power: the plugs keep counting
  themselves, and the website uses the difference of the plug counters. For an outage that crosses midnight, the
  website shows the part it cannot assign to a day separately.
* The buffer holds about 2 million readings (weeks for a few plugs). Delivered readings are deleted locally after 7 days.

## 15. Maintenance

**Remove a plug**: delete it from `devices:` in `config.yaml` and restart the service. On the website (administrator),
**Energy → Edit → untick "Accept readings"**. Its history stays visible. Do not reuse its ID for another machine.

**Move a plug to another machine**: on the website, **Edit → Machine**. If the meaning changes completely, give it a
new ID instead (new config entry and website registration), so old and new data are not mixed.

**Update the application**:

```sh
cd ~/tapo-energy-collector
sudo sh deployment/linux/update.sh     # git pull + reinstall + graceful restart; queued readings are kept
```

**Updating dependencies** (developers): change the ranges in `pyproject.toml`, then regenerate the lock file in a clean
environment (`python -m venv /tmp/lock && /tmp/lock/bin/pip install . && /tmp/lock/bin/pip freeze --exclude tapo-energy-collector`).
Keep the `; platform_system == "Windows"` markers on `colorama` and `tzdata`. Run `pytest`, open a pull request (CI runs
tests and lint on Python 3.11–3.13), and repeat [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md) with a real plug after a
python-kasa upgrade.

## 16. How it works (for developers)

```text
src/tapo_collector/
  main.py            CLI: run, discover, test-device, test-upload, check-config, status, retry-now, requeue-rejected
  config.py          YAML + environment loading and validation (secrets only from the environment)
  devices.py         python-kasa connection: cached params → discover_single (UDP) → try_connect_all (TCP); read-only
  measurements.py    Measurement model, units, plausibility checks, UTC timestamps, stable UUIDv5 IDs
  storage.py         SQLite queue (WAL, synchronous=FULL): pending → delivered | rejected, per-record deferral
  uploader.py        HTTP client + status-code handling, exponential backoff with jitter, batch splitting
  service.py         one polling task per plug, upload loop, alerts, status.json, graceful shutdown
  discovery.py       broadcast discovery and single-plug test
  logging_config.py  console + rotating file logs, secret redaction
deployment/linux     systemd unit, installer, update, uninstall, CLI wrapper
deployment/windows   Task Scheduler installer (alternative)
docs/API.md          ingestion API contract (website side lives in UgentRacing/ccbom)
```

Key decisions:

* **Readings are saved before upload.** IDs are deterministic (UUIDv5 of collector + device + timestamp), so a retry
  after a lost response is acknowledged as a duplicate and never stored twice.
* **Energy totals come from the plug's counters**, not from adding up power samples. The month counter is preferred
  because it does not reset at midnight. A decrease is a reset. The website computes this in
  `scripts/energy-math.mjs` (CCBOM repository); the estimate from sampled power is shown next to it as a cross-check.
* **Timestamps are UTC** everywhere. "Days" on the website are Belgian local days (Europe/Brussels, including summer time).
* python-kasa 0.12 (verified against its source): `Module.Energy.current_consumption` in W,
  `consumption_today`/`consumption_this_month` in kWh (from Wh), and `voltage`/`current` only when the firmware
  answers `get_emeter_data`. The P110 has no lifetime counter (`consumption_total` is None) and python-kasa does not
  read its historical statistics, so missed periods cannot be reconstructed apart from what the counters cover.
