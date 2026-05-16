#!/usr/bin/env python3
"""
Solar forecast for Bellairpark, Johannesburg.
Can be run directly (sends tomorrow's forecast) or imported by bot_poll.py.
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

PANEL_WP = 500
SYSTEM_EFFICIENCY = 0.80

GROUPS = {
    "East":  {"count": 3, "azimuth": 90,  "tilt": 15},
    "North": {"count": 3, "azimuth": 0,   "tilt": 15},
    "West":  {"count": 3, "azimuth": 270, "tilt": 15},
}

# ---------- Solar geometry ----------

def _deg(r): return math.degrees(r)
def _rad(d): return math.radians(d)


def solar_position(dt_utc: datetime.datetime):
    day_of_year = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    solar_time = hour_utc + LON / 15.0
    hour_angle = _rad((solar_time - 12) * 15)
    declination = _rad(23.45 * math.sin(_rad(360 / 365 * (day_of_year - 81))))
    lat_r = _rad(LAT)
    sin_elev = (math.sin(lat_r) * math.sin(declination)
                + math.cos(lat_r) * math.cos(declination) * math.cos(hour_angle))
    elevation = _deg(math.asin(max(-1, min(1, sin_elev))))
    cos_az_num = math.sin(declination) - math.sin(lat_r) * sin_elev
    cos_az_den = math.cos(lat_r) * math.cos(_rad(elevation))
    if abs(cos_az_den) < 1e-9:
        azimuth = 0.0
    else:
        cos_az = cos_az_num / cos_az_den
        azimuth = _deg(math.acos(max(-1, min(1, cos_az))))
        if math.sin(hour_angle) > 0:
            azimuth = 360 - azimuth
    return elevation, azimuth


def incidence_factor(elevation_deg, sun_az_deg, panel_az_deg, panel_tilt_deg):
    if elevation_deg <= 0:
        return 0.0
    e, sa, pa, pt = _rad(elevation_deg), _rad(sun_az_deg), _rad(panel_az_deg), _rad(panel_tilt_deg)
    cos_aoi = math.sin(e) * math.cos(pt) + math.cos(e) * math.sin(pt) * math.cos(sa - pa)
    return max(0.0, cos_aoi)


# ---------- Open-Meteo ----------

def fetch_hourly(date: datetime.date) -> dict:
    params = {
        "latitude": LAT, "longitude": LON,
        "hourly": "shortwave_radiation,direct_radiation,diffuse_radiation",
        "timezone": "UTC",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    result = {}
    for t, g, d, df in zip(data["hourly"]["time"],
                            data["hourly"]["shortwave_radiation"],
                            data["hourly"]["direct_radiation"],
                            data["hourly"]["diffuse_radiation"]):
        result[int(t[11:13])] = {"ghi": g or 0, "dni_approx": d or 0, "diffuse": df or 0}
    return result


# ---------- Energy calculation ----------

def estimate_kwh(hourly: dict, date: datetime.date) -> dict:
    totals = {g: 0.0 for g in GROUPS}
    for hour_utc, irr in hourly.items():
        dt_utc = datetime.datetime(date.year, date.month, date.day, hour_utc, 30)
        elev, sun_az = solar_position(dt_utc)
        if elev <= 0 or irr["ghi"] <= 0:
            continue
        for name, cfg in GROUPS.items():
            factor = incidence_factor(elev, sun_az, cfg["azimuth"], cfg["tilt"])
            poa = irr["dni_approx"] * factor + irr["diffuse"] * (1 + math.cos(_rad(cfg["tilt"]))) / 2
            power_w = (poa / 1000) * PANEL_WP * cfg["count"] * SYSTEM_EFFICIENCY
            totals[name] += power_w / 1000
    return totals


# ---------- Message builder ----------

def build_message(date: datetime.date) -> str:
    hourly = fetch_hourly(date)
    kwh = estimate_kwh(hourly, date)
    total_kwh = sum(kwh.values())
    peak_wp = sum(cfg["count"] * PANEL_WP for cfg in GROUPS.values())

    if total_kwh >= peak_wp * 4.5 / 1000:
        label = "Excellent ☀️"
    elif total_kwh >= peak_wp * 3.0 / 1000:
        label = "Good \U0001f324"
    elif total_kwh >= peak_wp * 1.5 / 1000:
        label = "Moderate ⛅"
    else:
        label = "Poor 🌧"

    today = datetime.date.today()
    if date == today:
        date_label = f"Today · {date.strftime('%-d %b %Y')}"
    elif date == today + datetime.timedelta(days=1):
        date_label = f"Tomorrow · {date.strftime('%-d %b %Y')}"
    else:
        date_label = date.strftime('%A, %-d %b %Y')

    return (
        f"<b>☀️ {total_kwh:.2f} kWh — {label}</b>\n"
        f"Solar Forecast · {date_label}\n"
        f"📍 Bellairpark, Johannesburg\n\n"
        f"<b>Breakdown:</b>\n"
        f"  ▶ East panels (3×500W):   <b>{kwh['East']:.2f} kWh</b>\n"
        f"  ▶ North panels (3×500W):  <b>{kwh['North']:.2f} kWh</b>\n"
        f"  ▶ West panels (3×500W):   <b>{kwh['West']:.2f} kWh</b>\n\n"
        f"(System: 4.5 kWp, 80% efficiency assumed)"
    )


# ---------- Telegram ----------

def send_telegram(token: str, chat_id: str, text: str):
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


# ---------- Main (daily scheduled run → tomorrow's forecast) ----------

def main():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    dry_run = not token or not chat_id

    date = datetime.date.today() + datetime.timedelta(days=1)
    print(f"Fetching forecast for {date} …")
    msg = build_message(date)
    print(msg)

    if dry_run:
        print("\n[DRY RUN] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        sys.exit(0)

    send_telegram(token, chat_id, msg)
    print("Telegram message sent successfully.")


if __name__ == "__main__":
    main()
