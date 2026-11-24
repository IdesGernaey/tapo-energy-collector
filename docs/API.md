# Ingestion API contract

The collector sends readings to the CCBOM website (repository `UgentRacing/ccbom`, Next.js on the Azure VM).
The receiving side is implemented there in `app/api/energy/measurements/route.ts` and `scripts/energy.mjs`.

## Request

```http
POST /api/energy/measurements
Authorization: Bearer tec_<43 characters>
Content-Type: application/json
```

```json
{
  "collector_id": "workshop-pc",
  "measurements": [
    {
      "measurement_id": "1b4e28ba-2fa1-51d2-883f-0016d3cca427",
      "device_id": "printer_01",
      "timestamp": "2026-11-03T12:00:00.123Z",
      "online": true,
      "power_w": 145.7,
      "energy_today_kwh": 0.82,
      "energy_month_kwh": 12.345,
      "voltage_v": 231.2,
      "current_a": 0.66
    }
  ]
}
```

| Field | Type | Rules |
|---|---|---|
| `collector_id` | string, optional | Informational. The server identifies the collector by its token. |
| `measurements` | array | 0–500 items. An empty array only checks the token (`tapo-collector test-upload`). |
| `measurement_id` | string | 8–100 characters `[A-Za-z0-9_-]`. The collector generates a random UUID (v4) when the reading is taken and stores it with the reading, so it stays the same on every retry. Readings queued by collector 1.0 carry UUIDv5 IDs; both are accepted. |
| `device_id` | string | `[a-z0-9][a-z0-9_-]{0,63}`. Must be registered **and enabled** on the website. |
| `timestamp` | string | ISO 8601 with a time zone (`Z` or `±hh:mm`). Not more than 5 minutes in the future and not older than 400 days. |
| `online` | boolean, optional (default `true`) | `false` = the plug did not answer that poll; then all metrics must be `null`. |
| `power_w` | number or null | Watts, 0–4500. |
| `energy_today_kwh` | number or null | kWh since the plug's local midnight, 0–110. **Resets daily.** |
| `energy_month_kwh` | number or null | kWh since the start of the month, 0–3400. **Resets monthly.** |
| `voltage_v` | number or null | Volts, 0–300 (only newer P110 firmware). |
| `current_a` | number or null | Amperes, 0–20 (only newer P110 firmware). |

Unknown fields are rejected. Missing measurements are never invented: unsupported or unknown values are sent as `null`.

## Responses

**200**: per-record acknowledgement. Every `measurement_id` of the request appears in exactly one list:

```json
{
  "accepted":   ["…"],
  "duplicates": ["…"],
  "rejected":   [{"measurement_id": "…", "reason": "unknown_device"}]
}
```

* `accepted`: stored now.
* `duplicates`: already stored by an earlier request. Retries are harmless: the database has a primary key on `measurementId` and inserts use `INSERT OR IGNORE`.
* `rejected` reasons: `unknown_device`, `device_disabled` (the collector keeps these readings and retries every 10 minutes), and `invalid_measurement`, `unknown_field`, `invalid_measurement_id`, `invalid_device_id`, `invalid_timestamp`, `timestamp_in_future`, `timestamp_too_old`, `invalid_online`, `invalid_<field>`, `offline_with_values` (the collector marks these as permanently rejected).

| Status | Meaning | Collector reaction |
|---|---|---|
| 400 | Body is not JSON, wrong content type, or no `measurements` array | Splits the batch to find the bad reading; after 3 single-reading refusals in a row it pauses instead (a server bug must not empty the queue) |
| 401 | Missing, invalid or revoked token | Keeps everything, pauses 5 minutes, logs an error |
| 403 | (not used by this endpoint; handled like 401) | Same as 401 |
| 409 | (not used by this endpoint) | Treated as "already stored" |
| 413 | More than 500 measurements or more than 1 MB | Splits the batch |
| 429 | Too many requests with **invalid** tokens from one address (`Retry-After: 600`) | Waits `Retry-After` |
| 5xx | Server or database problem (`503` with `Retry-After: 30`) | Exponential backoff with jitter (5 s … 10 min) |
| network error / timeout | | Exponential backoff with jitter |

Redirects are not followed: configure the final `https://` URL.

## Security

* Tokens are 256-bit random values (`tec_` + base64url). The website stores only their SHA-256 hash, in `energy_collectors`. A token is shown once when it is created.
* A token can only **write** readings for registered devices. It cannot read data, open the dashboard, or act as a member session. Member cookies are not accepted on this endpoint.
* Tokens are created, rotated and revoked on the VM:
  `docker compose exec app node scripts/admin.mjs energy-token create|rotate|revoke <name>` and `energy-token list`.
* Failed token attempts are rate limited per client address (30 per 10 minutes).
* HTTPS is terminated by Caddy on the VM. The collector refuses non-HTTPS endpoints except `localhost`.

## Storage on the website

SQLite tables (migration `drizzle/0010_energy_monitoring.sql`):

* `energy_collectors(id, tokenHash, enabled, createdAt, lastSeenAt)`
* `energy_devices(id, name, machine, category, enabled, createdAt, updatedAt)`
* `energy_measurements(measurementId PK, deviceId, measuredAt ms UTC, online, powerW, energyTodayKwh, energyMonthKwh, voltageV, currentA, collectorId, receivedAt)` with an index on `(deviceId, measuredAt)`

Storage estimate: about 120 bytes per reading. One plug every 10 s is about 8,640 readings, roughly 1 MB, per day, or about 0.4 GB per plug per year. The database is included in the existing daily backups.
