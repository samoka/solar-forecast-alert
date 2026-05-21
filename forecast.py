#!/usr/bin/env python3
"""
Solar forecast for Bellairpark, Johannesburg.
Can be run directly (sends tomorrow's forecast) or imported by bot_poll.py.

Uses Open-Meteo's global_tilted_irradiance (GTI) per panel group for accuracy,
plus temperature derating and a conservative system efficiency.
"""

import os
import sys
import datetime
import urllib.request
import urllib.parse
import json

# ---------- Site & system config ----------
LAT = -26.2
LON = 28.1

PANEL_WP = 500
SYSTEM_EFFICIENCY = 0.75   # conservative: inverter, wiring, soiling losses
TEMP_COEFF = -0.0035       # power loss per °C above 25°C (typical = -0.35%/°C)
NOCT = 45                  # nominal operating cell temp (°C) — raises cell temp above ambient

# Open-Meteo azimuth convention: 0=South, -90=East, 90=West, 180=North
# Our panels face North (equator-facing in southern hemisphere), East, West
GROUPS = {
    "East":  {"count": 3, "tilt": 15, "om_azimuth": -90},
    "North": {"count": 3, "tilt": 15, "om_azimuth": 180},
    "West":  {"count": 3, "tilt": 15, "om_azimuth":  90},
}


# ---------- Open-Meteo — one GTI call per panel group + temperature ----------

def fetch_group_gti(date: datetime.date, tilt: int, azimuth: int) -> dict:
    """
    Returns {hour_utc: gti_wm2} for a specific tilt/azimuth using the
    Open-Meteo solar radiation API (global_tilted_irradiance).
    This accounts for cloud cover, diffuse sky, and ground reflection properly.
    """
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "global_tilted_irradiance",
        "tilt": tilt,
        "azimuth": azimuth,
        "timezone": "UTC",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    return {
        int(t[11:13]): (v or 0)
        for t, v in zip(data["hourly"]["time"],
                        data["hourly"]["global_tilted_irradiance"])
    }


def fetch_temperature(date: datetime.date) -> dict:
    """Returns {hour_utc: temp_celsius} for the day."""
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": "temperature_2m",
        "timezone": "UTC",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read())
    return {
        int(t[11:13]): (v or 25)
        for t, v in zip(data["hourly"]["time"],
                        data["hourly"]["temperature_2m"])
    }


# ---------- Energy calculation ----------

def estimate_kwh(date: datetime.date) -> dict:
    """
    Fetches GTI per group and temperature, applies temperature derating,
    returns {group_name: kwh}.
    """
    temps = fetch_temperature(date)
    totals = {}

    for name, cfg in GROUPS.items():
        gti_hourly = fetch_group_gti(date, cfg["tilt"], cfg["om_azimuth"])
        group_kwh = 0.0

        for hour_utc, gti in gti_hourly.items():
            if gti <= 0:
                continue

            # Cell temperature rises above ambient due to irradiance heating
            t_ambient = temps.get(hour_utc, 25)
            t_cell = t_ambient + (NOCT - 20) * (gti / 800)

            # Temperature derating factor
            temp_factor = 1 + TEMP_COEFF * (t_cell - 25)
            temp_factor = max(0.5, temp_factor)  # cap losses at 50%

            # Power per hour: (GTI/1000) × Wp × panels × efficiency × temp_factor
            power_w = (gti / 1000) * PANEL_WP * cfg["count"] * SYSTEM_EFFICIENCY * temp_factor
            group_kwh += power_w / 1000  # Wh → kWh

        totals[name] = group_kwh

    return totals


# ---------- Message builder ----------

def build_message(date: datetime.date) -> str:
    kwh = estimate_kwh(date)
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
        f"(System: 4.5 kWp · 75% efficiency · temp derating applied)"
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
