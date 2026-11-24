# Manual end-to-end test with one real Tapo P110

The automated tests use simulated plugs and a simulated website. Do this test once after installing, and after
updating python-kasa or the plug firmware. It takes about 30 minutes. Write the results in the table at the end.

**You need:** one P110 set up in the Tapo app and plugged into a lamp or a device with a steady load (a 40–100 W
lamp or a soldering station is ideal), the workshop computer with the collector installed (README sections 4–8),
an administrator account on the website, and the ingestion token.

## 1. Read the plug directly

```sh
sudo tapo-collector test-device --host <plug-ip> --count 5 --interval 2
```

Expected: `Connected: model P110 …` and 5 lines with power in W, today and month in kWh. With newer firmware,
voltage (~230 V) and current are also shown.

* [ ] Power is plausible for the load. For example, a 60 W lamp shows about 55–62 W. The Tapo app shows the same value at the same moment.
* [ ] "today" in kWh matches the Tapo app's "Today's consumption" (the app shows Wh or kWh).
* [ ] **The plug stayed on.** The collector never switches plugs.

## 2. Website receives readings

1. On the website, go to **Energy → Register plug** and register the plug's ID exactly as in `config.yaml`.
2. Run `sudo tapo-collector test-upload`. Expected: `OK: … accepted the token`.
3. Run `sudo systemctl restart tapo-collector`, wait 1 minute, then run `sudo tapo-collector status`.
   * [ ] Queue `0 pending` (or a small number) and `Last upload OK` shows a recent time.
4. Open **Energy** on the website.
   * [ ] The plug shows **Online** and the same power as the Tapo app.
   * [ ] The power graph shows a line for the last minutes.

## 3. Plug offline

1. Pull the P110 out of the wall socket for 3 minutes.
   * [ ] `sudo tapo-collector status` shows the plug as `OFFLINE`, and the other plugs keep updating.
   * [ ] The website shows **Offline** for it within a minute. After 5 minutes the log shows `ALERT device …` (`journalctl -u tapo-collector`).
2. Plug it back in.
   * [ ] Within about 1–5 minutes it is **Online** again and the log says `reconnected at <ip>`.

## 3b. Plug comes back at a different IP address (automatic rediscovery)

This is the acceptance test for automatic recovery. Do it once on the workshop network after installing 1.1.

1. Run `sudo tapo-collector status`. Note the plug's address and check that it shows `identity AA:BB:… (config)` or
   `(learned)`. If it says `identity unknown`, let the collector read the plug once first.
2. Force a new address: pull the plug, then either remove its DHCP reservation / lease in the router, or move it to a
   different access point or VLAN segment that still reaches the workshop computer. Plug it back in. Check its new IP
   in the Tapo app (Device info).
3. Do **not** touch `config.yaml` and do **not** restart the service. Watch `journalctl -u tapo-collector -f`.
   * [ ] Within about 2–5 minutes the log shows `rediscovery started`, `identity verified (MAC … at <new ip>)`,
     `IP address changed from <old> to <new>` and `reconnected`.
   * [ ] `sudo tapo-collector status` shows the new address and `address changed <old> -> <new>`.
   * [ ] The website shows the plug **Online** again; the graph has offline markers but no invented values.
   * [ ] `sudo systemctl restart tapo-collector`: after the restart it connects straight to the new address
     (no `rediscovery started` in the log).
4. If the log instead says `broadcast discovery blocked` or `no response`, broadcast is filtered on this network.
   Ask the network administrator whether a unicast scan of the plugs' subnet is allowed and set
   `rediscovery.scan_networks`, or list the plugs' possible addresses/hostnames in `rediscovery.candidate_hosts`
   (README section 12), restart once, and repeat this test.

## 4. Internet outage (offline buffering)

1. Disconnect the workshop computer from the internet for 10 minutes, but keep the local network to the plug. For example, block the website in the router, or unplug the router's uplink.
   * [ ] `sudo tapo-collector status`: `pending` keeps increasing and uploads are failing. The website shows **No recent data**.
2. Reconnect.
   * [ ] Within a few minutes `pending` returns to 0. The website graph has **no gap** for those 10 minutes, and the plug-counter kWh for the period includes them.

## 5. Complete restart of the collector and computer

1. Run `sudo systemctl stop tapo-collector`. The log ends with `Collector stopped cleanly; N reading(s) queued`.
2. Run `sudo reboot`. Do not log in after the reboot.
   * [ ] Within 2 minutes after boot, `systemctl status tapo-collector` shows `active (running)`, and the website shows **Online** again.
   * [ ] The graph shows a gap only for the minutes the computer was off. Readings are never invented for that time.
   * [ ] The kWh total (plug counter) for today still includes the energy used while the computer was off. The plug keeps counting.

## 6. Crash recovery

Run `sudo systemctl kill -s KILL tapo-collector`.
* [ ] After about 15 s `systemctl status` shows it running again, and `status` shows no lost readings.

## 7. Energy check

Leave a constant load (e.g. a 60 W lamp) on for exactly 1 hour, then look at **Energy** for today:
* [ ] Plug counter ≈ 0.060 kWh for that hour (± 0.002 kWh rounding), and the estimate from sampled power is about the same.

## Results

| Date | Tester | Plug firmware | python-kasa | Steps passed | Notes |
|---|---|---|---|---|---|
| | | | 0.12.0 | | |
