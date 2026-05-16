#!/usr/bin/env python3
"""
Solar forecast alert for Bellairpark, Johannesburg.
Fetches tomorrow's hourly GHI from Open-Meteo, applies a simple
orientation/tilt model for East/North/West panel groups, and sends
the result via Telegram.
"""

import os
import sys
import math
import datetime
import urllib.request
import urllib.parse
import json

# ---------- Site & system config ----------
LAT = -26.2
LON = 28.1
TIMEZONE = "Africa/Johannesburg"

PANEL_WP = 545          # Watts-peak per panel
SYSTEM_EFFICIENCY = 0.80  # inverter + wiring losses

# Each group: (count, azimuth_deg, tilt_deg)
# Azimuth: 0=North, 90=East, 180=South, 270=West (meteorological convention)
GROUPS = {
    "East":  {"count": 3, "azimuth": 90,  "tilt": 15},
    "North": {"count": 3, "azimuth": 0,   "tilt": 15},
    "West":  {"count": 3, "azimuth": 270, "tilt": 15},
}

# ---------- Solar geometry helpers ----------

def _deg(r): return math.degrees(r)
def _rad(d): return math.radians(d)


def solar_position(dt_utc: datetime.datetime):
    """Return (elevation_deg, azimuth_deg) for LAT/LON at a UTC datetime."""
    day_of_year = dt_utc.timetuple().tm_yday
    # Hour angle
    hour_utc = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    lon_offset = LON / 15.0
    solar_time = hour_utc + lon_offset
    hour_angle = _rad((solar_time - 12) * 15)

    # Declination
    declination = _rad(23.45 * math.sin(_rad(360 / 365 * (day_of_year - 81))))

    lat_r = _rad(LAT)
    sin_elev = (math.sin(lat_r) * math.sin(declination)
                + math.cos(lat_r) * math.cos(declination) * math.cos(hour_angle))
    elevation = _deg(math.asin(max(-1, min(1, sin_elev))))

    cos_az_num = (math.sin(declination) - math.sin(lat_r) * sin_elev)
    cos_az_den = math.cos(lat_r) * math.cos(_rad(elevation))
    if abs(cos_az_den) < 1e-9:
        azimuth = 0.0
    else:
        cos_az = cos_az_num / cos_az_den
        azimuth = _deg(math.acos(max(-1, min(1, cos_az))))
        if math.sin(hour_angle) > 0:   # afternoon → west half
            azimuth = 360 - azimuth

    return elevation, azimuth


def incidence_factor(elevation_deg, sun_az_deg, panel_az_deg, panel_tilt_deg):
    """
    Fraction of beam irradiance hitting a tilted surface (0–1).
    Uses the standard cos(angle-of-incidence) formula.
    """
    if elevation_deg <= 0:
        return 0.0
    e = _rad(elevation_deg)
    sa = _rad(sun_az_deg)
    pa = _rad(panel_az_deg)
    pt = _rad(panel_tilt_deg)

    cos_aoi = (math.sin(e) * math.cos(pt)
               + math.cos(e) * math.sin(pt) * math.cos(sa - pa))
    return max(0.0, cos_aoi)


# ---------- Open-Meteo fetch ----------

def fetch_hourly_ghi(date: datetime.date) -> dict:
    """
    Returns {hour_utc: ghi_wm2} for the given date using Open-Meteo.
    Uses shortwave_radiation (GHI) as a proxy; free, no key needed.
    """
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "shortwave_radiation,direct_radiation,diffuse_radiation",
        "timezone": "UTC",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())

    times = data["hourly"]["time"]
    ghi   = data["hourly"]["shortwave_radiation"]
    direct = data["hourly"]["direct_radiation"]
    diffuse = data["hourly"]["diffuse_radiation"]

    result = {}
    for t, g, d, df in zip(times, ghi, direct, diffuse):
        hour = int(t[11:13])
        result[hour] = {"ghi": g or 0, "dni_approx": d or 0, "diffuse": df or 0}
    return result


# ---------- Energy calculation ----------

def estimate_kwh(hourly: dict) -> dict:
    """
    Returns {group_name: kwh} for each panel group using tomorrow's hourly data.
    Each hour is treated as 1 hour wide (Wh = W * 1h).
    """
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    totals = {g: 0.0 for g in GROUPS}

    for hour_utc, irr in hourly.items():
        dt_utc = datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                                   hour_utc, 30)  # mid-hour sample
        elev, sun_az = solar_position(dt_utc)
        if elev <= 0:
            continue

        ghi = irr["ghi"]
        if ghi <= 0:
            continue

        for name, cfg in GROUPS.items():
            factor = incidence_factor(elev, sun_az, cfg["azimuth"], cfg["tilt"])
            # Plane-of-array irradiance: beam component + isotropic diffuse
            beam = irr["dni_approx"] * factor
            diffuse_poa = irr["diffuse"] * (1 + math.cos(_rad(cfg["tilt"]))) / 2
            poa = beam + diffuse_poa   # W/m²

            # Power = (poa / 1000) * Wp_per_panel * panels * efficiency
            power_w = (poa / 1000) * PANEL_WP * cfg["count"] * SYSTEM_EFFICIENCY
            totals[name] += power_w / 1000  # Wh → kWh

    return totals


# ---------- Telegram ----------

def send_telegram(token: str, chat_id: str, text: str):
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "parse_mode": "HTML"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


# ---------- Main ----------

def main():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    dry_run = not token or not chat_id

    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    print(f"Fetching forecast for {tomorrow} …")

    hourly = fetch_hourly_ghi(tomorrow)
    kwh    = estimate_kwh(hourly)

    total_kwh = sum(kwh.values())
    peak_wp   = sum(cfg["count"] * PANEL_WP for cfg in GROUPS.values())

    # Simple performance ratio label
    if total_kwh >= peak_wp * 4.5 / 1000:
        label = "Excellent ☀️"
    elif total_kwh >= peak_wp * 3.0 / 1000:
        label = "Good \U0001f324"      # partly sunny
    elif total_kwh >= peak_wp * 1.5 / 1000:
        label = "Moderate ⛅"
    else:
        label = "Poor 🌧"

    msg = (
        f"<b>☀️ Solar Forecast — {tomorrow.strftime('%A, %-d %b %Y')}</b>\n"
        f"📍 Bellairpark, Johannesburg\n\n"
        f"<b>Expected generation:</b>\n"
        f"  ▶ East panels (3×545W):   <b>{kwh['East']:.2f} kWh</b>\n"
        f"  ▶ North panels (3×545W):  <b>{kwh['North']:.2f} kWh</b>\n"
        f"  ▶ West panels (3×545W):   <b>{kwh['West']:.2f} kWh</b>\n"
        f"  ──────────\n"
        f"  ▶ <b>Total: {total_kwh:.2f} kWh</b>\n\n"
        f"Rating: {label}\n"
        f"(System: 4.9 kWp, 80% efficiency assumed)"
    )

    print(msg)

    if dry_run:
        print("\n[DRY RUN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — message not sent.")
        sys.exit(0)

    send_telegram(token, chat_id, msg)
    print("Telegram message sent successfully.")


if __name__ == "__main__":
    main()
