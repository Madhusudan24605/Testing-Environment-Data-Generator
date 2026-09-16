"""
EMDT Test Data Generator — v2.1
Electric Mobility Digital Twin
LAB University of Applied Sciences — Yeganeh Maleki, 2026

Changes from v2.0:
  - generate_records() now runs on a background thread, so the UI no
    longer freezes while generating large batches (up to max_records).
    Progress updates flow back to the main thread through a Queue and
    tkinter's .after(), which is the only thread-safe way to touch
    widgets from outside the main loop.
  - Generate / Data Type / Record Count are disabled while a generation
    job is running, to prevent overlapping runs.
  - FIELD_RULES extended to cover every numeric column that previously
    had no bounds (actual_power_kw, latitude/longitude, co2_g_per_kwh,
    solar/wind/grid/ev load, capacity_kwh, current_a, charge_cycles,
    motor_rpm, torque_nm, inverter_temp_c, regen_kwh, co2_saved_kg,
    avg_speed_kmh, station_max_kw) so Manual Entry can no longer accept
    physically meaningless values for those fields.
  - Added a logging module (console + emdt.log file) so generation and
    export failures leave a trace instead of only a messagebox.
  - Theme.text switched from a low-contrast pink to a light, readable
    color for body text on the dark background. (Previous value is
    kept as Theme.accent_text for anyone who liked the accent.)

Changes from v1 (unchanged, kept for reference):
  - AppConfig  : all magic numbers in one place
  - DataValidator : field-level rules (SOC stays 0-100 %, etc.)
  - StatsPanel : live min / max / mean for every numeric column
  - actual_power_kw now respects real station ceiling (v1 was hard-capped at 400 kW)
  - Clipboard copy for the selected row
  - Separate "SQLite" export button
  - charging session duration now derived from energy ÷ power (more realistic)
  - Cleaner docstrings, type annotations, and no leftover placeholder comments
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import random
import sqlite3
import statistics
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

__version__ = "2.5.0"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG = logging.getLogger("emdt")


def configure_logging() -> None:
    """Set up console + rotating-free file logging. Safe to call more than once."""
    LOG.setLevel(logging.INFO)
    if LOG.handlers:
        return  # already configured

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    LOG.addHandler(console)

    try:
        log_path = Path(__file__).resolve().parent / "emdt.log"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        LOG.addHandler(file_handler)
    except OSError as err:
        # Non-fatal: fall back to console-only logging (e.g. read-only folder).
        LOG.warning("Could not open log file: %s", err)


# ---------------------------------------------------------------------------
# Configuration — all tunable values live here, nowhere else
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    window_size: str = "1050x900"
    banner_size: tuple[int, int] = (1050, 200)
    max_records: int = 1_000
    min_records: int = 1
    column_width: int = 130
    column_min_width: int = 80
    default_record_count: int = 10
    default_interval_seconds: int = 30
    max_interval_seconds: int = 3_600
    progress_hide_delay_ms: int = 700
    generation_poll_ms: int = 50            # UI poll rate while a background job runs
    gps_jitter_deg: float = 0.0004          # ≈ ±45 m
    min_actual_power_kw: float = 50.0       # lower bound for random power draw
    banner_file_aliases: tuple[str, ...] = ("ev_banner.png", "ev_banner_1.png")


CFG = AppConfig()


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    bg: str = "#0a0f1e"
    panel: str = "#0d1b2e"
    card: str = "#112240"
    accent: str = "#00c9ff"
    accent_secondary: str = "#0066cc"
    btn_hover: str = "#0088ee"
    text: str = "#e8f0fa"          # was #eb34ae (pink) — low contrast on dark bg
    accent_text: str = "#eb34ae"   # kept in case the pink accent is wanted elsewhere
    muted: str = "#6b8caa"
    green: str = "#00e676"
    purple: str = "#7c4dff"
    orange: str = "#ff9800"
    red: str = "#ff5252"
    border: str = "#1a3a5c"
    stats_label: str = "#a0c4e8"
    card_2: str = "#16305c"   # slightly lighter than `card`, used for tooltips/popovers
    muted_2: str = "#4a6a8a"  # dimmer than `muted`, used for subtle map reference markers

    font_family: str = "Consolas"
    font_size: int = 10
    font_size_small: int = 9
    font_size_title: int = 12
    font_size_banner: int = 13

    @property
    def font(self) -> tuple[str, int]:
        return (self.font_family, self.font_size)

    @property
    def font_bold(self) -> tuple[str, int, str]:
        return (self.font_family, self.font_size, "bold")

    @property
    def font_small(self) -> tuple[str, int]:
        return (self.font_family, self.font_size_small)

    @property
    def font_small_bold(self) -> tuple[str, int, str]:
        return (self.font_family, self.font_size_small, "bold")

    @property
    def font_title(self) -> tuple[str, int, str]:
        return (self.font_family, self.font_size_title, "bold")


THEME = Theme()


# ---------------------------------------------------------------------------
# Data validation
# ---------------------------------------------------------------------------

@dataclass
class FieldRule:
    """Optional min/max bounds and an allowed-values list for one field."""
    min_value: float | None = None
    max_value: float | None = None
    allowed: tuple[str, ...] | None = None


# Rules are checked when the user submits a manual entry.
# v2.1: every numeric column across every data type now has a rule —
# previously several Kempower/Grid/Powertrain/Trip fields had none,
# which let Manual Entry accept physically meaningless values.
FIELD_RULES: dict[str, FieldRule] = {
    "soc_percent":     FieldRule(min_value=0.0,   max_value=100.0),
    "start_soc":       FieldRule(min_value=0.0,   max_value=100.0),
    "end_soc":         FieldRule(min_value=0.0,   max_value=100.0),
    "health_percent":  FieldRule(min_value=0.0,   max_value=100.0),
    "efficiency_pct":  FieldRule(min_value=0.0,   max_value=100.0),
    "renewable_pct":   FieldRule(min_value=0.0,   max_value=100.0),
    "voltage_v":       FieldRule(min_value=200.0, max_value=500.0),
    "frequency_hz":    FieldRule(min_value=45.0,  max_value=55.0),
    "temperature_c":   FieldRule(min_value=-40.0, max_value=150.0),
    "motor_temp_c":    FieldRule(min_value=-40.0, max_value=200.0),
    "inverter_temp_c": FieldRule(min_value=-40.0, max_value=200.0),
    "speed_kmh":       FieldRule(min_value=0.0,   max_value=400.0),
    "avg_speed_kmh":   FieldRule(min_value=0.0,   max_value=400.0),
    "duration_min":    FieldRule(min_value=1.0),
    "energy_kwh":      FieldRule(min_value=0.0),
    "regen_kwh":       FieldRule(min_value=0.0),
    "cost_eur":        FieldRule(min_value=0.0),
    "distance_km":     FieldRule(min_value=0.0),
    "capacity_kwh":    FieldRule(min_value=0.0,   max_value=250.0),
    "current_a":       FieldRule(min_value=-1000.0, max_value=1000.0),
    "charge_cycles":   FieldRule(min_value=0.0),
    "motor_rpm":       FieldRule(min_value=0.0,   max_value=25_000.0),
    "torque_nm":       FieldRule(min_value=0.0,   max_value=2_000.0),
    "co2_saved_kg":    FieldRule(min_value=0.0),
    "co2_g_per_kwh":   FieldRule(min_value=0.0,   max_value=2_000.0),
    "solar_kw":        FieldRule(min_value=0.0),
    "wind_kw":         FieldRule(min_value=0.0),
    "grid_load_kw":    FieldRule(min_value=0.0),
    "ev_load_kw":      FieldRule(min_value=0.0),
    "latitude":        FieldRule(min_value=-90.0,  max_value=90.0),
    "longitude":       FieldRule(min_value=-180.0, max_value=180.0),
    "start_latitude":  FieldRule(min_value=-90.0,  max_value=90.0),
    "start_longitude": FieldRule(min_value=-180.0, max_value=180.0),
    "end_latitude":    FieldRule(min_value=-90.0,  max_value=90.0),
    "end_longitude":   FieldRule(min_value=-180.0, max_value=180.0),
    "station_max_kw":  FieldRule(min_value=0.0),
    "actual_power_kw": FieldRule(min_value=0.0),
    "status": FieldRule(allowed=("Completed", "In Progress", "Interrupted",
                                  "Charging", "Driving", "Idle", "Standby")),
    "grid_status": FieldRule(allowed=("Stable", "High Load", "Low Demand", "Peak")),
    "drive_mode":  FieldRule(allowed=("Eco", "Normal", "Sport", "Track")),
    "regen_braking": FieldRule(allowed=("Yes", "No")),
    "route": FieldRule(allowed=("Urban", "Highway", "Mixed", "Rural")),
}


class DataValidator:
    """Validates a single record against FIELD_RULES."""

    @staticmethod
    def validate(record: dict[str, Any]) -> list[str]:
        """Return a list of human-readable error strings (empty = valid)."""
        errors: list[str] = []
        for field_name, value in record.items():
            rule = FIELD_RULES.get(field_name)
            if rule is None:
                continue
            if rule.allowed is not None and isinstance(value, str):
                if value not in rule.allowed:
                    errors.append(
                        f"'{field_name}' must be one of: {', '.join(rule.allowed)}"
                    )
                continue
            if isinstance(value, (int, float)):
                if rule.min_value is not None and value < rule.min_value:
                    errors.append(
                        f"'{field_name}' = {value} is below minimum ({rule.min_value})"
                    )
                if rule.max_value is not None and value > rule.max_value:
                    errors.append(
                        f"'{field_name}' = {value} exceeds maximum ({rule.max_value})"
                    )
        # Cross-field check: start_soc must be less than end_soc
        s, e = record.get("start_soc"), record.get("end_soc")
        if isinstance(s, (int, float)) and isinstance(e, (int, float)) and s >= e:
            errors.append(f"'start_soc' ({s}) must be less than 'end_soc' ({e})")
        return errors


# ---------------------------------------------------------------------------
# Kempower station data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KempowerStation:
    name: str
    city: str
    address: str
    latitude: float
    longitude: float
    max_power_kw: int
    num_plugs: int
    charger_model: str
    network: str


KEMPOWER_STATIONS: tuple[KempowerStation, ...] = (
    KempowerStation(
        name="Kempower HQ Lahti", city="Lahti",
        address="Ala-Okeroistentie 29, 15700 Lahti",
        latitude=60.9583, longitude=25.6561,
        max_power_kw=300, num_plugs=4,
        charger_model="Kempower Satellite 300 kW",
        network="Kempower (Private)",
    ),
    KempowerStation(
        name="ABC Lataus Heinola", city="Heinola",
        address="Route 4, Heinola",
        latitude=61.2016, longitude=26.0326,
        max_power_kw=400, num_plugs=32,
        charger_model="Kempower Satellite 400 kW",
        network="ABC Lataus",
    ),
    KempowerStation(
        name="ABC Renkomäki Lahti", city="Lahti",
        address="Renkomäentie, 15240 Lahti (Route 4)",
        latitude=60.9397, longitude=25.6837,
        max_power_kw=200, num_plugs=12,
        charger_model="Kempower Satellite 200 kW",
        network="ABC Lataus",
    ),
    KempowerStation(
        name="ABC Tiiriö Hämeenlinna", city="Hämeenlinna",
        address="Tiiriöntie, 13130 Hämeenlinna (Route 3)",
        latitude=60.9935, longitude=24.4747,
        max_power_kw=200, num_plugs=8,
        charger_model="Kempower Satellite 200 kW",
        network="ABC Lataus",
    ),
    KempowerStation(
        name="ABC Forssa", city="Forssa",
        address="Route 2, 30100 Forssa",
        latitude=60.8240, longitude=23.6230,
        max_power_kw=150, num_plugs=8,
        charger_model="Kempower Satellite 150 kW",
        network="ABC Lataus",
    ),
    KempowerStation(
        name="K-Lataus Tammisto Vantaa", city="Vantaa",
        address="K-Citymarket Tammisto, 01510 Vantaa",
        latitude=60.3034, longitude=25.0049,
        max_power_kw=400, num_plugs=18,
        charger_model="Kempower Satellite 400 kW",
        network="K-Lataus",
    ),
    KempowerStation(
        name="K-Lataus Länsikeskus Turku", city="Turku",
        address="K-Citymarket Länsikeskus, 20360 Turku",
        latitude=60.4539, longitude=22.2360,
        max_power_kw=400, num_plugs=18,
        charger_model="Kempower Satellite 400 kW",
        network="K-Lataus",
    ),
    KempowerStation(
        name="Oulunbaari", city="Oulu",
        address="Oulunbaari, Neste Station, Oulu",
        latitude=65.0121, longitude=25.4651,
        max_power_kw=150, num_plugs=4,
        charger_model="Kempower Station + Satellite 150 kW",
        network="Neste MY Renewable Charging",
    ),
    KempowerStation(
        name="Port of HaminaKotka MCS", city="Kotka",
        address="Satamakatu, Port of HaminaKotka, 48100 Kotka",
        latitude=60.5650, longitude=26.9430,
        max_power_kw=1200, num_plugs=2,
        charger_model="Kempower Mega Satellite MCS 1.2 MW",
        network="Plugit",
    ),
    KempowerStation(
        name="Kuopio Juustoportti", city="Kuopio",
        address="Juustoportintie, 70460 Kuopio",
        latitude=62.8924, longitude=27.6770,
        max_power_kw=100, num_plugs=18,
        charger_model="Kempower Satellite 100 kW",
        network="Recharge",
    ),
    KempowerStation(
        name="Kempower Helsinki Office", city="Helsinki",
        address="Karvaamokuja 2 A, 00380 Helsinki",
        latitude=60.2226, longitude=24.8854,
        max_power_kw=50, num_plugs=2,
        charger_model="Kempower Station 50 kW",
        network="Kempower (Private)",
    ),
    KempowerStation(
        name="Kempower Tampere Office", city="Tampere",
        address="Korkeakoulunkatu 7, 33720 Tampere",
        latitude=61.4490, longitude=23.8598,
        max_power_kw=100, num_plugs=4,
        charger_model="Kempower Satellite 100 kW",
        network="Kempower (Private)",
    ),
)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Record = dict[str, Any]
GeneratorFunc = Callable[[], Record]

MODELS = ("Model 3 Standard", "Model 3 Long Range", "Model 3 Performance")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vehicle_id() -> str:
    return f"EV-{random.randint(10_000, 999_999)}"


def _make_timestamps(count: int, interval_seconds: int = 30) -> list[str]:
    """Return *count* evenly-spaced UTC timestamps ending at the current moment."""
    now = datetime.now(tz=timezone.utc)
    return [
        (now - timedelta(seconds=(count - 1 - i) * interval_seconds))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(count)
    ]


def _single_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Rough mainland-Finland bounding box, used to give vehicle telemetry a
# plausible GPS fix without tying every record to a fixed charging station
# (that's what generate_kempower_session's station-based jitter is for).
FINLAND_LAT_RANGE: tuple[float, float] = (59.8, 68.9)
FINLAND_LON_RANGE: tuple[float, float] = (21.0, 31.5)


def _random_finland_gps() -> tuple[float, float]:
    """Return a random (latitude, longitude) point within Finland's mainland."""
    lat = round(random.uniform(*FINLAND_LAT_RANGE), 6)
    lon = round(random.uniform(*FINLAND_LON_RANGE), 6)
    return lat, lon


# ---------------------------------------------------------------------------
# Data generators
# ---------------------------------------------------------------------------

def generate_battery() -> Record:
    """Tesla Model 3 battery sensor snapshot."""
    lat, lon = _random_finland_gps()
    return {
        "vehicle_id":     _vehicle_id(),
        "timestamp":      _single_timestamp(),
        "model":          random.choice(MODELS),
        "latitude":       lat,
        "longitude":      lon,
        "soc_percent":    round(random.uniform(20.0, 80.0), 1),
        "voltage_v":      round(random.uniform(340.0, 400.0), 1),
        "current_a":      round(random.uniform(-200.0, 200.0), 1),
        "capacity_kwh":   random.choice([50, 62, 75, 82]),
        "temperature_c":  round(random.uniform(-10.0, 45.0), 1),
        "health_percent": round(random.uniform(70.0, 100.0), 1),
        "charge_cycles":  random.randint(0, 1_200),
        "status":         random.choice(["Charging", "Driving", "Idle", "Standby"]),
    }


def generate_charging_session() -> Record:
    """
    Tesla Model 3 DC/AC charging session.

    Duration is derived from energy and charger power so that
    the three fields are mutually consistent instead of independent
    random draws.
    """
    start_soc    = round(random.uniform(10.0, 40.0), 1)
    end_soc      = round(random.uniform(max(start_soc + 5.0, 60.0), 90.0), 1)
    energy_kwh   = round(random.uniform(10.0, 75.0), 1)

    charger_type, power_kw = random.choice([
        ("AC Level 2 - 11kW",         11),
        ("DC Supercharger V2 - 150kW", 150),
        ("DC Supercharger V3 - 250kW", 250),
    ])

    # Realistic duration: energy / power, with ±15 % efficiency jitter.
    efficiency   = random.uniform(0.85, 1.0)
    duration_min = max(5, round(energy_kwh / (power_kw * efficiency) * 60))

    lat, lon = _random_finland_gps()

    return {
        "session_id":   f"CHG-{random.randint(1_000, 9_999)}",
        "vehicle_id":   _vehicle_id(),
        "timestamp":    _single_timestamp(),
        "model":        random.choice(MODELS),
        "latitude":     lat,
        "longitude":    lon,
        "charger_type": charger_type,
        "location":     random.choice([
            "Lahti Supercharger", "Helsinki Supercharger",
            "Home Charger",       "Shopping Mall",
        ]),
        "start_soc":    start_soc,
        "end_soc":      end_soc,
        "energy_kwh":   energy_kwh,
        "duration_min": duration_min,
        "cost_eur":     round(energy_kwh * random.uniform(0.25, 0.45), 2),
        "status":       random.choice(["Completed", "In Progress", "Interrupted"]),
    }


def generate_powertrain() -> Record:
    """Tesla Model 3 powertrain and motor telemetry."""
    lat, lon = _random_finland_gps()
    return {
        "vehicle_id":      _vehicle_id(),
        "timestamp":       _single_timestamp(),
        "model":           random.choice(MODELS),
        "latitude":        lat,
        "longitude":       lon,
        "speed_kmh":       round(random.uniform(0.0, 261.0), 1),
        "motor_rpm":       random.randint(0, 19_000),
        "torque_nm":       round(random.uniform(0.0, 493.0), 1),
        "motor_temp_c":    round(random.uniform(20.0, 120.0), 1),
        "inverter_temp_c": round(random.uniform(20.0, 90.0), 1),
        "efficiency_pct":  round(random.uniform(75.0, 98.0), 1),
        "regen_braking":   random.choice(["Yes", "No"]),
        "drive_mode":      random.choice(["Eco", "Normal", "Sport", "Track"]),
    }


def generate_grid() -> Record:
    """Regional power-grid and energy-mix snapshot for Lahti / Päijät-Häme."""
    location = random.choice(["Lahti Zone A", "Lahti Zone B", "Päijät-Häme"])

    if location == "Päijät-Häme":
        load    = round(random.uniform(50_000.0, 250_000.0), 1)
        solar   = round(random.uniform(0.0, 15_000.0), 1)
        wind    = round(random.uniform(0.0, 30_000.0), 1)
        ev_load = round(random.uniform(500.0, 8_000.0), 1)
    else:
        load    = round(random.uniform(5_000.0, 50_000.0), 1)
        solar   = round(random.uniform(0.0, 2_000.0), 1)
        wind    = round(random.uniform(0.0, 5_000.0), 1)
        ev_load = round(random.uniform(100.0, 2_000.0), 1)

    renewable_pct = round(min((solar + wind) / max(load, 1) * 100, 100.0), 1)

    return {
        "timestamp":      _single_timestamp(),
        "location":       location,
        "solar_kw":       solar,
        "wind_kw":        wind,
        "grid_load_kw":   load,
        "ev_load_kw":     ev_load,
        "renewable_pct":  renewable_pct,
        "co2_g_per_kwh":  round(random.uniform(20.0, 200.0), 1),
        "frequency_hz":   round(random.uniform(49.8, 50.2), 3),
        "grid_status":    random.choice(["Stable", "High Load", "Low Demand", "Peak"]),
    }


def generate_trip() -> Record:
    """Tesla Model 3 trip telemetry with consistent energy and CO₂ figures."""
    distance = round(random.uniform(1.0, 600.0), 1)
    energy   = round(distance * random.uniform(0.12, 0.20), 2)

    # Start/end points are independent random draws within Finland — they
    # are NOT guaranteed to be exactly `distance_km` apart. Good enough for
    # test/demo data, but don't rely on them for real distance calculations.
    start_lat, start_lon = _random_finland_gps()
    end_lat, end_lon     = _random_finland_gps()

    return {
        "trip_id":         f"TRIP-{random.randint(1_000, 9_999)}",
        "vehicle_id":      _vehicle_id(),
        "timestamp":       _single_timestamp(),
        "vehicle_type":    random.choice(["Passenger Car", "Van", "Bus", "Cargo"]),
        "start_latitude":  start_lat,
        "start_longitude": start_lon,
        "end_latitude":    end_lat,
        "end_longitude":   end_lon,
        "distance_km":     distance,
        "duration_min":    random.randint(5, 300),
        "avg_speed_kmh":   round(random.uniform(20.0, 130.0), 1),
        "energy_kwh":      energy,
        "regen_kwh":       round(energy * random.uniform(0.05, 0.25), 2),
        "co2_saved_kg":    round(distance * 0.12, 3),   # ICEV baseline: 120 g CO₂/km
        "route":           random.choice(["Urban", "Highway", "Mixed", "Rural"]),
    }


def generate_kempower_session() -> Record:
    """
    Charging session at a real Kempower site in Finland.

    actual_power_kw draws from the full range
    [min_actual_power_kw, station.max_power_kw] so the 1.2 MW HaminaKotka
    MCS charger is distinguishable from a standard 400 kW unit.

    A small GPS jitter (≤ ±45 m) simulates different spots within the
    same station car park.
    """
    station  = random.choice(KEMPOWER_STATIONS)
    start_soc = round(random.uniform(10.0, 40.0), 1)
    end_soc   = round(random.uniform(max(start_soc + 5.0, 50.0), 90.0), 1)
    energy    = round(random.uniform(10.0, 75.0), 1)

    # Use real station ceiling — no artificial 400 kW cap.
    actual_power = round(
        random.uniform(CFG.min_actual_power_kw, float(station.max_power_kw)), 1
    )
    # Derive duration from energy ÷ power with a small efficiency factor.
    efficiency   = random.uniform(0.85, 1.0)
    duration_min = max(5, round(energy / (actual_power * efficiency) * 60))

    jitter = CFG.gps_jitter_deg
    lat    = round(station.latitude  + random.uniform(-jitter, jitter), 6)
    lon    = round(station.longitude + random.uniform(-jitter, jitter), 6)

    return {
        "session_id":      f"KMP-{random.randint(10_000, 99_999)}",
        "timestamp":       _single_timestamp(),
        "station_name":    station.name,
        "network":         station.network,
        "city":            station.city,
        "address":         station.address,
        "latitude":        lat,
        "longitude":       lon,
        "charger_model":   station.charger_model,
        "station_max_kw":  station.max_power_kw,
        "actual_power_kw": actual_power,
        "start_soc":       start_soc,
        "end_soc":         end_soc,
        "energy_kwh":      energy,
        "duration_min":    duration_min,
        "cost_eur":        round(energy * random.uniform(0.25, 0.45), 2),
        "status":          random.choice(["Completed", "In Progress", "Interrupted"]),
    }


# ---------------------------------------------------------------------------
# Data-type registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DataTypeSpec:
    label: str
    generator: GeneratorFunc
    columns: tuple[str, ...]


DATA_TYPES: tuple[DataTypeSpec, ...] = (
    DataTypeSpec(
        "Battery Sensor", generate_battery,
        ("vehicle_id", "timestamp", "model", "latitude", "longitude", "soc_percent",
         "voltage_v", "current_a", "temperature_c", "health_percent", "charge_cycles",
         "status"),
    ),
    DataTypeSpec(
        "Charging Session", generate_charging_session,
        ("session_id", "vehicle_id", "timestamp", "latitude", "longitude", "charger_type",
         "location", "start_soc", "end_soc", "energy_kwh", "duration_min", "cost_eur",
         "status"),
    ),
    DataTypeSpec(
        "Powertrain & Motor", generate_powertrain,
        ("vehicle_id", "timestamp", "model", "latitude", "longitude", "speed_kmh",
         "motor_rpm", "torque_nm", "motor_temp_c", "inverter_temp_c", "efficiency_pct",
         "regen_braking", "drive_mode"),
    ),
    DataTypeSpec(
        "Grid & Energy", generate_grid,
        ("timestamp", "location", "solar_kw", "wind_kw", "grid_load_kw",
         "ev_load_kw", "renewable_pct", "co2_g_per_kwh", "frequency_hz", "grid_status"),
    ),
    DataTypeSpec(
        "Vehicle Trip", generate_trip,
        ("trip_id", "vehicle_id", "timestamp", "vehicle_type",
         "start_latitude", "start_longitude", "end_latitude", "end_longitude",
         "distance_km", "duration_min", "avg_speed_kmh", "energy_kwh", "regen_kwh",
         "co2_saved_kg", "route"),
    ),
    DataTypeSpec(
        "Kempower Station Session", generate_kempower_session,
        ("session_id", "timestamp", "station_name", "network", "city", "address",
         "latitude", "longitude", "charger_model", "station_max_kw", "actual_power_kw",
         "start_soc", "end_soc", "energy_kwh", "duration_min", "cost_eur", "status"),
    ),
)

DATA_TYPE_LABELS: tuple[str, ...] = tuple(s.label for s in DATA_TYPES)
DATA_TYPE_MAP:    dict[str, DataTypeSpec] = {s.label: s for s in DATA_TYPES}


# ---------------------------------------------------------------------------
# Utility: record helpers
# ---------------------------------------------------------------------------

def normalize_record(record: Record, columns: tuple[str, ...]) -> Record:
    """Return only the fields relevant to the active data type."""
    return {col: record[col] for col in columns}


class FieldParseError(ValueError):
    """Raised when manual-entry text cannot be converted to a field's type."""


def parse_field_value(raw: str, original: object) -> object:
    """Coerce manual-entry text to the same Python type as the original value.

    Raises FieldParseError when the text cannot be converted to the type
    implied by *original*. Previously this silently fell back to returning
    the raw string on a failed conversion, which let bad input slip past
    DataValidator entirely: a non-numeric string skips the min/max checks
    (they only run for int/float values), and a numeric-looking string
    typed into an enum field (e.g. "status") could get coerced to int by
    the "unknown type" fallback, which skips the allowed-values check
    (it only runs for str values). Raising here forces the caller to
    reject the entry instead of admitting an unvalidated value.
    """
    value = raw.strip()
    if not value or value == str(original):
        return original
    if isinstance(original, bool):
        return value.lower() in {"1", "true", "yes"}
    if isinstance(original, int):
        try:
            return int(value)
        except ValueError:
            raise FieldParseError(f"must be a whole number (got '{value}')") from None
    if isinstance(original, float):
        try:
            return float(value)
        except ValueError:
            raise FieldParseError(f"must be a number (got '{value}')") from None
    # Unknown/string type — this covers the enum fields (status, route,
    # drive_mode, ...). Keep it as plain text; do NOT try to coerce it to
    # int/float here, or a value like "123" typed into "status" would
    # silently become the int 123 and skip the allowed-values check.
    return value


# ---------------------------------------------------------------------------
# Utility: SQL helpers
# ---------------------------------------------------------------------------

def table_name_from_label(label: str) -> str:
    name = label.lower().replace(" & ", "_").replace(" ", "_").replace("-", "_")
    return "".join(ch for ch in name if ch.isalnum() or ch == "_")


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{str(value).replace(chr(39), chr(39)*2)}'"


def infer_sql_type(value: object) -> str:
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


_SQL_RANK: dict[str, int] = {"INTEGER": 0, "REAL": 1, "TEXT": 2}


def infer_column_types(records: list[Record], columns: tuple[str, ...]) -> dict[str, str]:
    """Infer SQL types across all records; wider type always wins."""
    if not records:
        return {col: "TEXT" for col in columns}
    types = {col: infer_sql_type(records[0][col]) for col in columns}
    for rec in records[1:]:
        for col in columns:
            candidate = infer_sql_type(rec[col])
            if _SQL_RANK[candidate] > _SQL_RANK[types[col]]:
                types[col] = candidate
    return types


def build_sql_script(table: str, records: list[Record], columns: tuple[str, ...]) -> str:
    col_types  = infer_column_types(records, columns)
    col_defs   = ",\n    ".join(f"{c} {col_types[c]}" for c in columns)
    col_list   = ", ".join(columns)
    lines = [
        "-- EMDT Test Data Generator v2",
        f"-- Table: {table}  |  Records: {len(records)}",
        "",
        f"CREATE TABLE IF NOT EXISTS {table} (",
        f"    {col_defs}",
        ");",
        "",
    ]
    for rec in records:
        vals = ", ".join(sql_literal(rec[c]) for c in columns)
        lines.append(f"INSERT INTO {table} ({col_list}) VALUES ({vals});")
    lines.append("")
    return "\n".join(lines)


def write_sqlite_db(
    path: Path, table: str, records: list[Record], columns: tuple[str, ...]
) -> None:
    """Write records to a SQLite database, replacing any existing file."""
    path.unlink(missing_ok=True)
    col_types = infer_column_types(records, columns)
    col_defs  = ", ".join(f"{c} {col_types[c]}" for c in columns)
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {table} ({col_defs})")
        placeholders = ", ".join("?" * len(columns))
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [[rec[c] for c in columns] for rec in records],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Statistics panel
# ---------------------------------------------------------------------------

class StatsPanel:
    """
    Shows min / max / mean for every numeric column in the current dataset.
    Displayed in a compact scrollable frame below the main table.
    """

    def __init__(self, parent: tk.Widget, theme: Theme) -> None:
        self.theme = theme
        t = theme

        self.frame = tk.LabelFrame(
            parent,
            text=" Column Statistics ",
            bg=t.panel, fg=t.accent,
            font=t.font_small_bold,
            bd=1, relief="flat",
        )

        self._canvas   = tk.Canvas(self.frame, bg=t.panel, height=70, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(
            self.frame, orient="horizontal", command=self._canvas.xview
        )
        self._canvas.configure(xscrollcommand=self._scrollbar.set)
        self._scrollbar.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=t.panel)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind(
            "<Configure>",
            lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._empty_label = tk.Label(
            self._inner, text="No data yet.",
            bg=t.panel, fg=t.muted, font=t.font_small,
        )
        self._empty_label.pack(padx=10, pady=8)

    def update(self, records: list[Record], columns: tuple[str, ...]) -> None:
        """Recompute and redisplay statistics for all numeric columns."""
        for widget in self._inner.winfo_children():
            widget.destroy()

        numeric_cols = [
            col for col in columns
            if records and isinstance(records[0].get(col), (int, float))
        ]

        if not records or not numeric_cols:
            tk.Label(
                self._inner, text="No numeric columns to summarise.",
                bg=self.theme.panel, fg=self.theme.muted, font=self.theme.font_small,
            ).pack(padx=10, pady=8)
            return

        t = self.theme
        for col in numeric_cols:
            values = [rec[col] for rec in records if isinstance(rec[col], (int, float))]
            if not values:
                continue
            mn   = min(values)
            mx   = max(values)
            mean = statistics.mean(values)

            cell = tk.Frame(self._inner, bg=t.card, padx=8, pady=4)
            cell.pack(side="left", padx=4, pady=6)

            tk.Label(cell, text=col, bg=t.card, fg=t.accent,
                     font=t.font_small_bold).pack()
            tk.Label(
                cell,
                text=f"min {mn:.2g}  mean {mean:.2g}  max {mx:.2g}",
                bg=t.card, fg=t.stats_label, font=t.font_small,
            ).pack()

    def clear(self) -> None:
        for widget in self._inner.winfo_children():
            widget.destroy()
        self._empty_label = tk.Label(
            self._inner, text="No data yet.",
            bg=self.theme.panel, fg=self.theme.muted, font=self.theme.font_small,
        )
        self._empty_label.pack(padx=10, pady=8)


# ---------------------------------------------------------------------------
# Map panel
# ---------------------------------------------------------------------------

class MapPanel:
    """
    Lightweight, fully offline GPS visualization of the current dataset —
    a schematic position plot, not a georeferenced/surveyed map (no map
    tiles, no network access, no extra dependencies beyond Tkinter).

    Two record shapes are recognised:
      - a single point per record: "latitude" + "longitude"
        (Battery Sensor, Charging Session, Powertrain & Motor,
        Kempower Station Session)
      - a start/end pair per record: "start_latitude" + "start_longitude" +
        "end_latitude" + "end_longitude" (Vehicle Trip), drawn as a short
        line between the two points.

    Data types with no GPS columns at all (currently only Grid & Energy)
    show a neutral placeholder instead of an empty canvas.

    Interaction:
      - mouse wheel  : zoom in/out, centered on the cursor
      - click + drag : pan
      - hover         : tooltip with the exact coordinates under the cursor
      - click (no drag): pin a highlight ring + push coordinates to the
        optional on_point_click callback
      - "Reset View" button: re-fit the view to the current dataset

    A very rough Finland outline and a handful of reference cities are
    drawn as fixed background context, purely to make relative positions
    easier to read — they are simplified free-hand polygons/points, not
    survey-grade geographic data.
    """

    _PAD = 40  # canvas padding in px, leaves room for corner coordinate labels
    _PICK_RADIUS_SQ = 225   # 15px pick radius for click/hover point detection
    _DRAG_THRESHOLD_SQ = 16  # 4px — below this, a mouse-up is treated as a click
    _ZOOM_FACTOR = 0.85      # per wheel notch
    _MIN_SPAN_DEG = 0.02     # smallest allowed lat/lon span, prevents over-zoom

    # Very rough, simplified outline of mainland Finland (lat, lon), traced
    # clockwise from the southwest. Intended purely as a recognisable
    # silhouette for orientation on an offline schematic map — not
    # survey-accurate border data.
    _FINLAND_OUTLINE: tuple[tuple[float, float], ...] = (
        (59.8, 22.9), (60.1, 21.4), (61.0, 21.3), (61.5, 21.4),
        (62.0, 21.2), (62.6, 21.2), (63.1, 21.6), (63.7, 22.7),
        (64.1, 23.5), (64.9, 24.6), (65.8, 24.1), (66.5, 23.6),
        (67.5, 23.6), (68.0, 22.0), (68.6, 21.0), (69.0, 21.1),
        (69.9, 27.0), (69.5, 28.5), (69.1, 29.0), (68.0, 29.5),
        (66.9, 30.0), (65.8, 30.0), (64.9, 30.5), (63.9, 31.0),
        (62.9, 31.5), (62.0, 31.3), (61.3, 29.8), (60.9, 29.0),
        (60.5, 28.0), (60.2, 26.9), (60.15, 25.7), (60.17, 24.94),
        (60.0, 23.5), (59.8, 22.9),
    )

    # A handful of reference cities for orientation (not exhaustive).
    _REFERENCE_CITIES: tuple[tuple[str, float, float], ...] = (
        ("Helsinki", 60.1699, 24.9384),
        ("Tampere", 61.4978, 23.7610),
        ("Turku", 60.4518, 22.2666),
        ("Oulu", 65.0121, 25.4651),
        ("Rovaniemi", 66.5039, 25.7294),
        ("Kuopio", 62.8924, 27.6770),
        ("Jyväskylä", 62.2415, 25.7209),
        ("Lahti", 60.9827, 25.6612),
    )

    # Priority order of enum-like columns to color-code point-mode markers
    # by, when present in the active dataset's columns.
    _CATEGORY_FIELDS: tuple[str, ...] = (
        "status", "drive_mode", "route", "grid_status", "network",
    )
    _CATEGORY_PALETTE: tuple[str, ...] = (
        "#00c9ff", "#7c4dff", "#00e676", "#ff9800",
        "#ff5252", "#ffd54f", "#26c6da", "#ec407a",
    )

    def __init__(
        self,
        parent: tk.Widget,
        theme: Theme,
        on_point_click: Callable[[str], None] | None = None,
    ) -> None:
        self.theme = theme
        self._on_point_click = on_point_click
        t = theme

        self.frame = tk.LabelFrame(
            parent,
            text=" GPS Map (schematic — not to scale) ",
            bg=t.panel, fg=t.accent,
            font=t.font_small_bold,
            bd=1, relief="flat",
        )

        # --- toolbar: reset button, usage hint, dynamic category legend ---
        toolbar = tk.Frame(self.frame, bg=t.panel)
        toolbar.pack(fill="x", padx=8, pady=(6, 0))

        tk.Button(
            toolbar, text="Reset View", bg=t.card, fg=t.text,
            activebackground=t.btn_hover, font=t.font_small,
            relief="flat", padx=8, pady=2, cursor="hand2",
            command=self._reset_view,
        ).pack(side="left")

        tk.Label(
            toolbar, text="scroll = zoom  \u00b7  drag = pan  \u00b7  click = select",
            bg=t.panel, fg=t.muted_2,
            font=t.font_small,
        ).pack(side="left", padx=10)

        self._legend_frame = tk.Frame(toolbar, bg=t.panel)
        self._legend_frame.pack(side="right")

        self.canvas = tk.Canvas(self.frame, bg=t.card, height=220, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)

        self.canvas.bind("<Configure>", lambda _e: self._render())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda _e: self._hide_tooltip())
        # Mouse wheel: Windows/Mac send <MouseWheel> with event.delta;
        # X11 sends <Button-4>/<Button-5> instead.
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)

        self._records: list[Record] = []
        self._mode: str | None = None            # "point" | "trip" | None
        self._category_field: str | None = None
        self._category_colors: dict[str, str] = {}

        # Current visible bounds as (lat_min, lat_max, lon_min, lon_max).
        # None until the first dataset is loaded.
        self._view: tuple[float, float, float, float] | None = None

        # (canvas_x, canvas_y, lat, lon) for every plotted marker, used by
        # the click/hover handlers to find the nearest point.
        self._plotted: list[tuple[float, float, float, float]] = []

        # Drag-vs-click bookkeeping.
        self._press_xy: tuple[int, int] | None = None
        self._last_drag_xy: tuple[int, int] | None = None
        self._dragged: bool = False

        # The record currently pinned by a table-row selection, so it can
        # be re-highlighted after a pan/zoom/reset redraw.
        self._selected_record: Record | None = None

        self._render()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, records: list[Record], columns: tuple[str, ...]) -> None:
        """Load a new dataset and reset the view to fit it."""
        self._records = records
        self._selected_record = None

        if "latitude" in columns and "longitude" in columns:
            self._mode = "point"
        elif all(
            c in columns
            for c in ("start_latitude", "start_longitude", "end_latitude", "end_longitude")
        ):
            self._mode = "trip"
        else:
            self._mode = None

        self._category_field = None
        if self._mode == "point":
            self._category_field = next(
                (f for f in self._CATEGORY_FIELDS if f in columns), None
            )
        self._build_category_colors()
        self._build_legend_widgets()

        self._view = self._compute_data_bounds()
        self._render()

    def clear(self) -> None:
        self._records = []
        self._mode = None
        self._category_field = None
        self._category_colors = {}
        self._selected_record = None
        self._view = None
        self._build_legend_widgets()
        self._render()

    def highlight_record(self, record: Record | None) -> None:
        """
        Highlight the given record's point(s) on the map, e.g. in response
        to a table-row selection. Pans/zooms back out to the full dataset
        first if the record isn't currently visible.
        """
        self._selected_record = record
        if record is not None and self._mode is not None and self._view is not None:
            lat_min, lat_max, lon_min, lon_max = self._view
            for lat, lon in self._record_points(record):
                if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                    self._view = self._compute_data_bounds()
                    break
        self._render()

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _record_points(self, record: Record) -> list[tuple[float, float]]:
        if self._mode == "point":
            return [(record["latitude"], record["longitude"])]
        if self._mode == "trip":
            return [
                (record["start_latitude"], record["start_longitude"]),
                (record["end_latitude"], record["end_longitude"]),
            ]
        return []

    def _compute_data_bounds(self) -> tuple[float, float, float, float] | None:
        if not self._records or self._mode is None:
            return None

        lats: list[float] = []
        lons: list[float] = []
        for rec in self._records:
            for lat, lon in self._record_points(rec):
                lats.append(lat)
                lons.append(lon)

        lat_min, lat_max = min(lats), max(lats)
        lon_min, lon_max = min(lons), max(lons)

        # Guard against a degenerate (near-zero-span) bounding box — a
        # single point, or several nearly-identical ones, would otherwise
        # divide by (close to) zero when projecting to canvas coordinates.
        if lat_max - lat_min < 0.05:
            mid = (lat_max + lat_min) / 2
            lat_min, lat_max = mid - 0.5, mid + 0.5
        if lon_max - lon_min < 0.05:
            mid = (lon_max + lon_min) / 2
            lon_min, lon_max = mid - 0.5, mid + 0.5

        # 8% padding around the data so markers don't sit flush on the edge.
        lat_span = lat_max - lat_min
        lon_span = lon_max - lon_min
        lat_min -= lat_span * 0.08
        lat_max += lat_span * 0.08
        lon_min -= lon_span * 0.08
        lon_max += lon_span * 0.08
        return (lat_min, lat_max, lon_min, lon_max)

    def _canvas_size(self) -> tuple[float, float]:
        w = self.canvas.winfo_width() or 760
        h = self.canvas.winfo_height() or 220
        return max(w, 2 * self._PAD + 20), max(h, 2 * self._PAD + 20)

    def _project(self, lat: float, lon: float) -> tuple[float, float]:
        if self._view is None:
            return (0.0, 0.0)
        lat_min, lat_max, lon_min, lon_max = self._view
        w, h = self._canvas_size()
        pad = self._PAD
        x = pad + (lon - lon_min) / (lon_max - lon_min) * (w - 2 * pad)
        # Screen y grows downward, latitude grows upward — invert.
        y = pad + (1 - (lat - lat_min) / (lat_max - lat_min)) * (h - 2 * pad)
        return x, y

    def _unproject(self, x: float, y: float) -> tuple[float, float]:
        if self._view is None:
            return (0.0, 0.0)
        lat_min, lat_max, lon_min, lon_max = self._view
        w, h = self._canvas_size()
        pad = self._PAD
        lon = lon_min + (x - pad) / (w - 2 * pad) * (lon_max - lon_min)
        lat = lat_min + (1 - (y - pad) / (h - 2 * pad)) * (lat_max - lat_min)
        return lat, lon

    # ------------------------------------------------------------------
    # Category colors / legend
    # ------------------------------------------------------------------

    def _build_category_colors(self) -> None:
        self._category_colors = {}
        if not self._category_field:
            return
        values = sorted(
            {
                str(rec.get(self._category_field))
                for rec in self._records
                if rec.get(self._category_field) is not None
            }
        )
        for i, value in enumerate(values):
            self._category_colors[value] = self._CATEGORY_PALETTE[
                i % len(self._CATEGORY_PALETTE)
            ]

    def _build_legend_widgets(self) -> None:
        for widget in self._legend_frame.winfo_children():
            widget.destroy()
        if not self._category_field or not self._category_colors:
            return
        t = self.theme
        tk.Label(
            self._legend_frame, text=f"{self._category_field}:",
            bg=t.panel, fg=t.muted, font=t.font_small,
        ).pack(side="left", padx=(0, 6))
        for value, color in self._category_colors.items():
            chip = tk.Frame(self._legend_frame, bg=t.panel)
            chip.pack(side="left", padx=4)
            tk.Label(chip, text="\u25cf", bg=t.panel, fg=color, font=t.font_small).pack(side="left")
            tk.Label(chip, text=value, bg=t.panel, fg=t.text, font=t.font_small).pack(side="left")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _draw_placeholder(self, text: str) -> None:
        w, h = self._canvas_size()
        self.canvas.create_text(
            w / 2, h / 2, text=text,
            fill=self.theme.muted, font=self.theme.font_small,
        )

    def _render(self) -> None:
        self.canvas.delete("all")
        self._plotted = []

        if not self._records:
            self._draw_placeholder("No data yet.")
            return
        if self._mode is None or self._view is None:
            self._draw_placeholder("This data type has no GPS columns.")
            return

        t = self.theme
        w, h = self._canvas_size()
        pad = self._PAD

        # --- frame + gridlines ---
        self.canvas.create_rectangle(pad, pad, w - pad, h - pad, outline=t.border)
        for frac in (0.25, 0.5, 0.75):
            gx = pad + frac * (w - 2 * pad)
            gy = pad + frac * (h - 2 * pad)
            self.canvas.create_line(gx, pad, gx, h - pad, fill=t.border, dash=(2, 3))
            self.canvas.create_line(pad, gy, w - pad, gy, fill=t.border, dash=(2, 3))

        # --- clip drawing to the plot area so pan/zoom can't paint over the UI ---
        self.canvas.create_rectangle(
            0, 0, w, pad, fill=t.card, outline="", tags="chrome"
        )

        # --- rough Finland outline, purely for orientation ---
        outline_pts: list[float] = []
        for lat, lon in self._FINLAND_OUTLINE:
            x, y = self._project(lat, lon)
            outline_pts.extend([x, y])
        if len(outline_pts) >= 6:
            self.canvas.create_polygon(
                outline_pts, outline=t.border, fill=t.panel, width=1
            )

        # --- reference cities ---
        for name, lat, lon in self._REFERENCE_CITIES:
            x, y = self._project(lat, lon)
            if pad <= x <= w - pad and pad <= y <= h - pad:
                self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=t.muted_2, outline="")
                self.canvas.create_text(
                    x + 6, y, anchor="w", fill=t.muted, font=("Consolas", 8), text=name
                )

        # --- data points ---
        if self._mode == "point":
            for rec in self._records:
                x, y = self._project(rec["latitude"], rec["longitude"])
                color = t.accent
                if self._category_field:
                    color = self._category_colors.get(
                        str(rec.get(self._category_field)), t.accent
                    )
                self.canvas.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5,
                                         fill=color, outline="")
                self._plotted.append((x, y, rec["latitude"], rec["longitude"]))
            legend = f"{len(self._records)} point(s)"
        else:
            for rec in self._records:
                sx, sy = self._project(rec["start_latitude"], rec["start_longitude"])
                ex, ey = self._project(rec["end_latitude"], rec["end_longitude"])
                self.canvas.create_line(sx, sy, ex, ey, fill=t.muted, width=1)
                self.canvas.create_oval(sx - 3.5, sy - 3.5, sx + 3.5, sy + 3.5,
                                         fill=t.accent, outline="")
                self.canvas.create_oval(ex - 3.5, ey - 3.5, ex + 3.5, ey + 3.5,
                                         fill=t.purple, outline="")
                self._plotted.append((sx, sy, rec["start_latitude"], rec["start_longitude"]))
                self._plotted.append((ex, ey, rec["end_latitude"], rec["end_longitude"]))
            legend = f"{len(self._records)} trip(s)  \u00b7  \u25cf start   \u25cf end"

        # --- selection ring, if a table row is currently selected ---
        if self._selected_record is not None:
            for lat, lon in self._record_points(self._selected_record):
                x, y = self._project(lat, lon)
                self.canvas.create_oval(
                    x - 8, y - 8, x + 8, y + 8, outline=t.orange, width=2
                )

        # --- corner labels ---
        lat_min, lat_max, lon_min, lon_max = self._view
        self.canvas.create_text(pad, pad - 12, anchor="w", fill=t.muted,
                                 font=t.font_small, text=f"{lat_max:.2f}\u00b0N")
        self.canvas.create_text(pad, h - pad + 14, anchor="w", fill=t.muted,
                                 font=t.font_small,
                                 text=f"{lat_min:.2f}\u00b0N \u00b7 {lon_min:.2f}\u00b0E")
        self.canvas.create_text(w - pad, h - pad + 14, anchor="e", fill=t.muted,
                                 font=t.font_small, text=f"{lon_max:.2f}\u00b0E")
        self.canvas.create_text(w - pad, pad - 12, anchor="e", fill=t.muted,
                                 font=t.font_small, text=legend)

    def _reset_view(self) -> None:
        self._view = self._compute_data_bounds()
        self._render()

    # ------------------------------------------------------------------
    # Interaction: pan, zoom, click, hover
    # ------------------------------------------------------------------

    def _on_press(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._press_xy = (event.x, event.y)
        self._last_drag_xy = (event.x, event.y)
        self._dragged = False

    def _on_drag(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._view is None or self._last_drag_xy is None or self._press_xy is None:
            return
        px, py = self._press_xy
        if (event.x - px) ** 2 + (event.y - py) ** 2 > self._DRAG_THRESHOLD_SQ:
            self._dragged = True

        lx, ly = self._last_drag_xy
        dx_px, dy_px = event.x - lx, event.y - ly
        if dx_px == 0 and dy_px == 0:
            return

        lat_min, lat_max, lon_min, lon_max = self._view
        w, h = self._canvas_size()
        pad = self._PAD
        lon_per_px = (lon_max - lon_min) / max(w - 2 * pad, 1)
        lat_per_px = (lat_max - lat_min) / max(h - 2 * pad, 1)

        # Dragging right/down should move the *view* left/up (grab-and-pull).
        d_lon = -dx_px * lon_per_px
        d_lat = dy_px * lat_per_px

        self._view = (
            lat_min + d_lat, lat_max + d_lat,
            lon_min + d_lon, lon_max + d_lon,
        )
        self._last_drag_xy = (event.x, event.y)
        self._hide_tooltip()
        self._render()

    def _on_release(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        was_drag = self._dragged
        self._press_xy = None
        self._last_drag_xy = None
        self._dragged = False
        if not was_drag:
            self._select_nearest(event.x, event.y)

    def _on_mousewheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._view is None:
            return
        # Normalise the wheel direction across platforms.
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            factor = 1 / self._ZOOM_FACTOR  # zoom out
        else:
            factor = self._ZOOM_FACTOR      # zoom in

        anchor_lat, anchor_lon = self._unproject(event.x, event.y)
        lat_min, lat_max, lon_min, lon_max = self._view
        new_lat_span = max((lat_max - lat_min) * factor, self._MIN_SPAN_DEG)
        new_lon_span = max((lon_max - lon_min) * factor, self._MIN_SPAN_DEG)

        # Keep the point under the cursor fixed while the view scales
        # around it, so zooming feels anchored to the mouse.
        lat_frac = (anchor_lat - lat_min) / max(lat_max - lat_min, 1e-9)
        lon_frac = (anchor_lon - lon_min) / max(lon_max - lon_min, 1e-9)

        new_lat_min = anchor_lat - lat_frac * new_lat_span
        new_lon_min = anchor_lon - lon_frac * new_lon_span

        self._view = (
            new_lat_min, new_lat_min + new_lat_span,
            new_lon_min, new_lon_min + new_lon_span,
        )
        self._hide_tooltip()
        self._render()

    def _select_nearest(self, x: int, y: int) -> None:
        if not self._plotted:
            return
        px, py, lat, lon = min(
            self._plotted, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2
        )
        if (px - x) ** 2 + (py - y) ** 2 > self._PICK_RADIUS_SQ:
            return  # click was too far from any plotted marker
        self.canvas.create_oval(
            px - 6, py - 6, px + 6, py + 6, outline=self.theme.green, width=2
        )
        if self._on_point_click is not None:
            self._on_point_click(f"{lat:.5f}, {lon:.5f}")

    def _on_hover(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if self._press_xy is not None:
            return  # a drag is in progress; don't fight it with tooltip churn
        if not self._plotted:
            self._hide_tooltip()
            return
        px, py, lat, lon = min(
            self._plotted, key=lambda p: (p[0] - event.x) ** 2 + (p[1] - event.y) ** 2
        )
        if (px - event.x) ** 2 + (py - event.y) ** 2 > self._PICK_RADIUS_SQ:
            self._hide_tooltip()
            return
        self._show_tooltip(event.x, event.y, f"{lat:.5f}, {lon:.5f}")

    def _show_tooltip(self, x: int, y: int, text: str) -> None:
        t = self.theme
        self.canvas.delete("tooltip")
        w, h = self._canvas_size()
        pad_x = 8
        text_w = 7 * len(text) + 14  # rough monospace width estimate
        box_x = min(x + 12, w - text_w - 4)
        box_y = max(y - 24, 2)
        self.canvas.create_rectangle(
            box_x, box_y, box_x + text_w, box_y + 20,
            fill=t.card_2,
            outline=t.accent, tags="tooltip",
        )
        self.canvas.create_text(
            box_x + text_w / 2, box_y + 10, text=text,
            fill=t.text, font=("Consolas", 9), tags="tooltip",
        )

    def _hide_tooltip(self) -> None:
        self.canvas.delete("tooltip")



# ---------------------------------------------------------------------------
# Banner helper
# ---------------------------------------------------------------------------

def find_banner_path(aliases: tuple[str, ...]) -> Path | None:
    search_dirs: list[Path] = []
    for directory in (Path(__file__).resolve().parent, Path.cwd()):
        if directory not in search_dirs:
            search_dirs.append(directory)
    return next(
        (
            candidate
            for d in search_dirs
            for name in aliases
            if (candidate := d / name).is_file()
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class EMDTApp:
    """Desktop UI for generating and exporting EMDT test datasets."""

    def __init__(self, theme: Theme = THEME) -> None:
        self.theme   = theme
        self.config  = CFG
        self.generated_data: list[Record] = []
        self.active_spec:    DataTypeSpec | None = None
        self._banner_image:  Any = None          # ImageTk.PhotoImage when PIL present
        self.buttons: dict[str, tk.Button] = {}
        self._gen_queue: "queue.Queue[tuple[str, Any]] | None" = None
        self._generation_seq: int = 0            # bumped each time a job starts
        self._closing: bool = False              # set once the window is closing

        self.root = tk.Tk()
        self.root.title(f"EMDT Data Generator v{__version__}")
        self.root.geometry(CFG.window_size)
        self.root.configure(bg=theme.bg)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(
            value="Ready — select a data type and click Generate or Manual."
        )

        self._configure_styles()
        self._build_banner()
        self._build_controls()
        self._build_status_bar()
        self._build_progress_bar()
        self._build_table()
        self._build_stats_panel()
        self._build_map_panel()
        self._build_footer()

    def run(self) -> None:
        self.root.mainloop()

    def _on_close(self) -> None:
        """Stop any pending .after() callbacks from touching dead widgets."""
        self._closing = True
        self.root.destroy()

    # ------------------------------------------------------------------
    # Style setup
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        t = self.theme
        style.configure("TCombobox",
            fieldbackground=t.card, background=t.card, foreground=t.text,
            selectbackground=t.accent, bordercolor=t.border, arrowcolor=t.text)
        style.configure("Treeview",
            background=t.card, foreground=t.text, fieldbackground=t.card,
            rowheight=26, bordercolor=t.border)
        style.configure("Treeview.Heading",
            background=t.accent_secondary, foreground=t.text, font=t.font_small_bold)
        style.map("Treeview", background=[("selected", t.accent_secondary)])
        style.configure("Vertical.TScrollbar",
            background=t.card, troughcolor=t.panel, arrowcolor=t.accent)
        style.configure("EMDT.Horizontal.TProgressbar",
            troughcolor=t.panel, background=t.accent,
            bordercolor=t.border, lightcolor=t.accent, darkcolor=t.accent)

    # ------------------------------------------------------------------
    # Widget builders
    # ------------------------------------------------------------------

    def _build_banner(self) -> None:
        t = self.theme
        banner_path = find_banner_path(CFG.banner_file_aliases)

        if banner_path and _PIL_AVAILABLE:
            try:
                img = Image.open(banner_path).resize(CFG.banner_size, Image.LANCZOS)
                self._banner_image = ImageTk.PhotoImage(img)
                tk.Label(self.root, image=self._banner_image, bg=t.bg).pack(fill="x")
                return
            except OSError as err:
                LOG.warning("Banner found but could not load: %s", err)
                self.status_var.set(f"Banner found but could not load: {err}")

        tk.Label(
            self.root,
            text="EMDT — Electric Mobility Digital Twin — Test Data Generator",
            bg=t.accent_secondary, fg=t.text,
            font=(t.font_family, t.font_size_banner, "bold"),
            pady=18,
        ).pack(fill="x")

    def _build_controls(self) -> None:
        t = self.theme

        # ── Row 1: dropdowns and spinboxes ──
        ctrl = tk.Frame(self.root, bg=t.panel, pady=10)
        ctrl.pack(fill="x")

        def _lbl(text: str, col: int) -> None:
            tk.Label(ctrl, text=text, bg=t.panel, fg=t.text,
                     font=t.font).grid(row=0, column=col, padx=(12, 4))

        _lbl("Data Type:", 0)
        self.data_type_combo = ttk.Combobox(
            ctrl, width=24, state="readonly", font=t.font, values=DATA_TYPE_LABELS)
        self.data_type_combo.current(0)
        self.data_type_combo.grid(row=0, column=1, padx=(0, 16))
        self.data_type_combo.bind("<<ComboboxSelected>>", self._on_data_type_changed)

        _lbl("Records:", 2)
        self.record_count_spinbox = tk.Spinbox(
            ctrl, from_=CFG.min_records, to=CFG.max_records, width=6,
            bg=t.card, fg=t.text, insertbackground=t.text,
            buttonbackground=t.accent_secondary, font=t.font, relief="flat")
        self.record_count_spinbox.delete(0, tk.END)
        self.record_count_spinbox.insert(0, str(CFG.default_record_count))
        self.record_count_spinbox.grid(row=0, column=3, padx=(0, 12))

        _lbl("Interval(s):", 4)
        self.interval_spinbox = tk.Spinbox(
            ctrl, from_=1, to=CFG.max_interval_seconds, width=6,
            bg=t.card, fg=t.text, insertbackground=t.text,
            buttonbackground=t.accent_secondary, font=t.font, relief="flat")
        self.interval_spinbox.delete(0, tk.END)
        self.interval_spinbox.insert(0, str(CFG.default_interval_seconds))
        self.interval_spinbox.grid(row=0, column=5, padx=(0, 12))

        _lbl("Seed:", 6)
        self.seed_entry = tk.Entry(
            ctrl, width=8,
            bg=t.card, fg=t.text, insertbackground=t.text,
            font=t.font, relief="flat")
        self.seed_entry.grid(row=0, column=7, padx=(0, 12))

        # ── Row 2: action buttons ──
        btn_row = tk.Frame(self.root, bg=t.panel, pady=6)
        btn_row.pack(fill="x")

        buttons = (
            ("Generate",   t.accent,          self.generate_records,    0),
            ("Manual",     t.accent_secondary, self.open_manual_entry,   1),
            ("Copy Row",   t.muted,            self.copy_selected_row,   2),
            ("Delete Row", t.orange,           self.delete_selected_row, 3),
            ("Clear",      t.red,              self.clear_records,       4),
            ("CSV",        t.green,            self.export_csv,          5),
            ("JSON",       t.purple,           self.export_json,         6),
            ("SQL",        t.orange,           self.export_sql,          7),
            ("SQLite",     t.accent_secondary, self.export_sqlite,       8),
        )
        for text, color, command, col in buttons:
            self.buttons[text] = self._make_button(btn_row, text, color, command, col)

    def _make_button(
        self, parent: tk.Frame, text: str, color: str, command: Any, column: int
    ) -> tk.Button:
        t = self.theme
        btn = tk.Button(
            parent, text=text, bg=color, fg=t.bg,
            activebackground=t.btn_hover, activeforeground=t.text,
            font=t.font_bold, relief="flat",
            padx=12, pady=6, cursor="hand2", command=command)
        btn.grid(row=0, column=column, padx=5)
        return btn

    def _build_status_bar(self) -> None:
        t = self.theme
        tk.Label(
            self.root, textvariable=self.status_var,
            bg=t.card, fg=t.muted, font=t.font_small,
            anchor="w", padx=16, pady=5,
        ).pack(fill="x")

    def _build_progress_bar(self) -> None:
        self.progress_var = tk.IntVar(value=0)
        self.progress_bar = ttk.Progressbar(
            self.root, variable=self.progress_var, maximum=100,
            style="EMDT.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", padx=12, pady=(2, 0))
        self.progress_bar.pack_forget()

    def _build_table(self) -> None:
        t = self.theme
        frame = tk.Frame(self.root, bg=t.bg)
        frame.pack(fill="both", expand=True, padx=12, pady=(6, 0))

        self.tree   = ttk.Treeview(frame, show="headings", selectmode="browse")
        scroll_y    = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        scroll_x    = ttk.Scrollbar(frame, orient="horizontal",  command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side="right",  fill="y")
        scroll_x.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _on_tree_select(self, _event: object = None) -> None:
        """Mirror the selected table row as a highlight ring on the map."""
        selected = self.tree.selection()
        if not selected:
            self.map_panel.highlight_record(None)
            return
        index = self.tree.index(selected[0])
        if 0 <= index < len(self.generated_data):
            self.map_panel.highlight_record(self.generated_data[index])

    def _build_stats_panel(self) -> None:
        self.stats_panel = StatsPanel(self.root, self.theme)
        self.stats_panel.frame.pack(fill="x", padx=12, pady=(4, 0))

    def _build_map_panel(self) -> None:
        self.map_panel = MapPanel(
            self.root, self.theme, on_point_click=self._on_map_point_click
        )
        self.map_panel.frame.pack(fill="x", padx=12, pady=(4, 0))

    def _on_map_point_click(self, coord_text: str) -> None:
        self.status_var.set(f"Map point selected: {coord_text}")

    def _build_footer(self) -> None:
        t = self.theme
        tk.Label(
            self.root,
            text=(
                f"EMDT-TDG v{__version__}  •  "
                "LAB University of Applied Sciences  •  Yeganeh Maleki  •  2026"
            ),
            bg=t.panel, fg=t.muted, font=(t.font_family, 8), pady=6,
        ).pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_data_type_changed(self, _event: object = None) -> None:
        if not self.generated_data or not self.active_spec:
            return
        new_label = self.data_type_combo.get()
        if new_label == self.active_spec.label:
            return
        if messagebox.askyesno(
            "Change Data Type",
            "Switching data type will clear the current table. Continue?",
        ):
            self.clear_records()
        else:
            self.data_type_combo.set(self.active_spec.label)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_spec(self) -> DataTypeSpec:
        return DATA_TYPE_MAP[self.data_type_combo.get()]

    def _read_record_count(self) -> int | None:
        try:
            count = int(self.record_count_spinbox.get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Record count must be a whole number.")
            return None
        if not CFG.min_records <= count <= CFG.max_records:
            messagebox.showerror(
                "Invalid Input",
                f"Record count must be between {CFG.min_records} and {CFG.max_records}.",
            )
            return None
        return count

    def _read_interval(self) -> int:
        try:
            value = int(self.interval_spinbox.get())
        except ValueError:
            return CFG.default_interval_seconds
        # Clamp to the spinbox's advertised range — previously only the
        # lower bound was enforced, so typing a value above the spinbox's
        # displayed max (3600) was silently accepted instead of clamped.
        return max(1, min(value, CFG.max_interval_seconds))

    def _configure_tree_columns(self, columns: tuple[str, ...]) -> None:
        self.tree["columns"] = columns
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(
                col, width=CFG.column_width, anchor="center",
                minwidth=CFG.column_min_width)

    def _populate_tree(self, records: list[Record], columns: tuple[str, ...]) -> None:
        self.tree.delete(*self.tree.get_children())
        for rec in records:
            self.tree.insert("", "end", values=[rec[c] for c in columns])

    def _ensure_data(self) -> bool:
        if self.generated_data:
            return True
        messagebox.showwarning("No Data", "Please generate or enter data first.")
        return False

    def _set_generation_controls_enabled(self, enabled: bool) -> None:
        """Lock the widgets that could start/interfere with a background job."""
        state = "normal" if enabled else "disabled"
        combo_state = "readonly" if enabled else "disabled"
        for name in ("Generate", "Manual", "Clear"):
            btn = self.buttons.get(name)
            if btn is not None:
                try:
                    btn.configure(state=state)
                except tk.TclError:
                    pass
        try:
            self.data_type_combo.configure(state=combo_state)
            self.record_count_spinbox.configure(state=state)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Core actions
    # ------------------------------------------------------------------

    def generate_records(self) -> None:
        """Kick off record generation on a background thread.

        Tkinter widgets may only be touched from the main thread, so the
        worker thread only computes plain-Python data and reports back
        through a Queue; _poll_generation_queue() drains that queue on
        the main thread via .after() and is the only place UI state changes.
        """
        count = self._read_record_count()
        if count is None:
            return

        # Re-running Generate used to silently overwrite generated_data —
        # including any rows added by hand via Manual Entry — with no
        # confirmation at all (only switching Data Type asked first).
        if self.generated_data and not messagebox.askyesno(
            "Replace Data",
            f"This will replace the {len(self.generated_data)} record(s) "
            "currently in the table. Continue?",
        ):
            return

        seed_text = self.seed_entry.get().strip()
        seed_value: int | None = None
        if seed_text:
            try:
                seed_value = int(seed_text)
            except ValueError:
                messagebox.showerror("Invalid Seed", "Seed must be a whole number.")
                return

        interval = self._read_interval()
        spec     = self._selected_spec()
        self.active_spec = spec
        self._configure_tree_columns(spec.columns)

        self.progress_bar.pack(fill="x", padx=12, pady=(2, 0))
        self.progress_var.set(0)
        self._set_generation_controls_enabled(False)
        self.status_var.set(f"Generating {count} {spec.label} records…")

        # Tag this run so a stale .after() callback from a previous job
        # (e.g. its delayed pack_forget()) can't affect a newer one that
        # started before the delay elapsed.
        self._generation_seq += 1
        job_id = self._generation_seq

        self._gen_queue = queue.Queue()
        worker = threading.Thread(
            target=self._generate_worker,
            args=(count, interval, spec, seed_value, self._gen_queue),
            daemon=True,
        )
        worker.start()
        self.root.after(
            CFG.generation_poll_ms, self._poll_generation_queue, spec, seed_text, job_id
        )

    def _generate_worker(
        self,
        count: int,
        interval: int,
        spec: DataTypeSpec,
        seed_value: int | None,
        out_queue: "queue.Queue[tuple[str, Any]]",
    ) -> None:
        """Runs on a background thread. MUST NOT touch any tkinter widget."""
        try:
            random.seed(seed_value) if seed_value is not None else random.seed()

            timestamps = _make_timestamps(count, interval)
            records: list[Record] = []
            step = max(1, count // 20)

            for i in range(count):
                rec = normalize_record(spec.generator(), spec.columns)
                if "timestamp" in rec:
                    rec["timestamp"] = timestamps[i]
                records.append(rec)
                if i % step == 0:
                    out_queue.put(("progress", int((i + 1) / count * 100)))

            out_queue.put(("done", records))
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI thread
            LOG.exception("Record generation failed")
            out_queue.put(("error", str(exc)))

    def _poll_generation_queue(
        self, spec: DataTypeSpec, seed_text: str, job_id: int
    ) -> None:
        # The window may have been closed while this callback was pending;
        # touching any widget after that raises TclError.
        if self._closing:
            return
        assert self._gen_queue is not None
        try:
            while True:
                kind, payload = self._gen_queue.get_nowait()
                if kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "done":
                    self._on_generation_done(payload, spec, seed_text, job_id)
                    return
                elif kind == "error":
                    self._on_generation_error(payload)
                    return
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if not self._closing:
            self.root.after(
                CFG.generation_poll_ms, self._poll_generation_queue, spec, seed_text, job_id
            )

    def _on_generation_done(
        self, records: list[Record], spec: DataTypeSpec, seed_text: str, job_id: int
    ) -> None:
        self.generated_data = records
        self._populate_tree(records, spec.columns)
        self.stats_panel.update(records, spec.columns)
        self.map_panel.update(records, spec.columns)

        self.progress_var.set(100)
        self.root.after(
            CFG.progress_hide_delay_ms, self._hide_progress_if_current, job_id
        )
        self._set_generation_controls_enabled(True)

        seed_info = f" | Seed: {seed_text}" if seed_text else ""
        self.status_var.set(
            f"{len(records)} records generated | Type: {spec.label} | "
            f"Columns: {len(spec.columns)}{seed_info}"
        )
        LOG.info("Generated %d '%s' records%s", len(records), spec.label,
                  f" (seed={seed_text})" if seed_text else "")

    def _hide_progress_if_current(self, job_id: int) -> None:
        """Only hide the progress bar if no newer job has started meanwhile.

        Without this guard, starting a second generation within
        progress_hide_delay_ms of the first one finishing would let the
        first job's delayed pack_forget() hide the *second* job's
        freshly-shown progress bar mid-run.
        """
        if self._closing:
            return
        if job_id != self._generation_seq:
            return
        try:
            self.progress_bar.pack_forget()
        except tk.TclError:
            pass

    def _on_generation_error(self, message: str) -> None:
        if self._closing:
            return
        self.progress_bar.pack_forget()
        self._set_generation_controls_enabled(True)
        self.status_var.set("Generation failed — see emdt.log for details.")
        messagebox.showerror("Generation Failed", message)

    def delete_selected_row(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("No Selection", "Select a row to delete.")
            return
        item_id = selected[0]
        index   = self.tree.index(item_id)
        self.tree.delete(item_id)
        if 0 <= index < len(self.generated_data):
            self.generated_data.pop(index)
        if self.active_spec:
            self.stats_panel.update(self.generated_data, self.active_spec.columns)
            self.map_panel.update(self.generated_data, self.active_spec.columns)
        self.status_var.set(
            f"Row {index + 1} deleted | Total records: {len(self.generated_data)}"
        )

    def copy_selected_row(self) -> None:
        """Copy the selected row to the system clipboard as tab-separated values."""
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("No Selection", "Select a row to copy.")
            return
        values = self.tree.item(selected[0], "values")
        self.root.clipboard_clear()
        self.root.clipboard_append("\t".join(str(v) for v in values))
        self.status_var.set("Row copied to clipboard.")

    def clear_records(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = ()
        self.generated_data.clear()
        self.active_spec = None
        self.stats_panel.clear()
        self.map_panel.clear()
        self.status_var.set("Cleared.")

    def open_manual_entry(self) -> None:
        spec = self._selected_spec()
        if self.active_spec and self.active_spec.label != spec.label:
            messagebox.showwarning(
                "Data Type Mismatch",
                f"The table contains {self.active_spec.label} data.\n"
                f"Clear first or switch back before adding {spec.label} records.",
            )
            return
        ManualEntryDialog(self, spec)

    def add_manual_record(self, record: Record, spec: DataTypeSpec) -> None:
        if self.active_spec and self.active_spec.label != spec.label:
            messagebox.showerror(
                "Data Type Mismatch",
                "This record does not match the data currently in the table.",
            )
            return
        record = normalize_record(record, spec.columns)
        if not self.tree["columns"]:
            self._configure_tree_columns(spec.columns)
            self.active_spec = spec
        self.generated_data.append(record)
        self.tree.insert("", "end", values=[record[c] for c in spec.columns])
        self.stats_panel.update(self.generated_data, spec.columns)
        self.map_panel.update(self.generated_data, spec.columns)
        self.status_var.set(
            f"Manual entry added | Total records: {len(self.generated_data)}"
        )
        LOG.info("Manual '%s' record added.", spec.label)

    # ------------------------------------------------------------------
    # Export actions
    # ------------------------------------------------------------------

    def export_csv(self) -> None:
        if not self._ensure_data() or not self.active_spec:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")], title="Save as CSV")
        if not path:
            return
        columns = self.active_spec.columns
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=columns)
                writer.writeheader()
                writer.writerows(self.generated_data)
        except OSError as err:
            LOG.error("CSV export failed: %s", err)
            messagebox.showerror("Export Failed", f"Could not save CSV:\n{err}")
            return
        LOG.info("CSV exported to %s (%d rows)", path, len(self.generated_data))
        messagebox.showinfo("Saved", f"CSV saved:\n{path}")
        self.status_var.set(f"CSV exported → {path}")

    def export_json(self) -> None:
        if not self._ensure_data():
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")], title="Save as JSON")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.generated_data, fh, ensure_ascii=False, indent=2)
        except OSError as err:
            LOG.error("JSON export failed: %s", err)
            messagebox.showerror("Export Failed", f"Could not save JSON:\n{err}")
            return
        LOG.info("JSON exported to %s (%d rows)", path, len(self.generated_data))
        messagebox.showinfo("Saved", f"JSON saved:\n{path}")
        self.status_var.set(f"JSON exported → {path}")

    def export_sql(self) -> None:
        """Export a plain-text SQL script (CREATE TABLE + INSERTs)."""
        if not self._ensure_data() or not self.active_spec:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".sql",
            filetypes=[("SQL script", "*.sql")], title="Save as SQL script")
        if not path:
            return
        table = table_name_from_label(self.active_spec.label)
        try:
            script = build_sql_script(table, self.generated_data, self.active_spec.columns)
            Path(path).write_text(script, encoding="utf-8")
        except OSError as err:
            LOG.error("SQL export failed: %s", err)
            messagebox.showerror("Export Failed", f"Could not save SQL script:\n{err}")
            return
        LOG.info("SQL script exported to %s (%d rows)", path, len(self.generated_data))
        messagebox.showinfo("Saved", f"SQL script saved:\n{path}")
        self.status_var.set(f"SQL exported → {path}")

    def export_sqlite(self) -> None:
        """Export directly to a SQLite database file."""
        if not self._ensure_data() or not self.active_spec:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".db",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
            title="Save as SQLite database")
        if not path:
            return
        table = table_name_from_label(self.active_spec.label)
        try:
            write_sqlite_db(
                Path(path), table, self.generated_data, self.active_spec.columns)
        except (OSError, sqlite3.Error) as err:
            LOG.error("SQLite export failed: %s", err)
            messagebox.showerror("Export Failed", f"Could not save SQLite database:\n{err}")
            return
        LOG.info("SQLite DB exported to %s (%d rows)", path, len(self.generated_data))
        messagebox.showinfo("Saved", f"SQLite database saved:\n{path}")
        self.status_var.set(f"SQLite exported → {path}")


# ---------------------------------------------------------------------------
# Manual entry dialog
# ---------------------------------------------------------------------------

class ManualEntryDialog:
    """Modal form for adding a single record by hand."""

    _W, _H = 440, 560

    def __init__(self, app: EMDTApp, spec: DataTypeSpec) -> None:
        self.app  = app
        self.spec = spec
        t         = app.theme

        self.window = tk.Toplevel(app.root)
        self.window.title(f"Add Manual Entry — {spec.label}")
        self.window.geometry(f"{self._W}x{self._H}")
        self.window.configure(bg=t.bg)
        self.window.resizable(False, True)
        self.window.transient(app.root)

        if (app.active_spec and app.active_spec.label == spec.label
                and app.generated_data):
            self.base_row = app.generated_data[-1].copy()
            help_text     = "Previous row values loaded. Edit as needed."
        else:
            self.base_row = normalize_record(spec.generator(), spec.columns)
            help_text     = "Sample values loaded. Edit as needed."

        # Header
        tk.Label(self.window, text=f"Manual Entry — {spec.label}",
                 bg=t.bg, fg=t.accent, font=t.font_title).pack(pady=(16, 4))
        tk.Label(self.window, text=help_text,
                 bg=t.bg, fg=t.muted, font=t.font_small).pack(pady=(0, 8))

        # Scrollable field area
        scroll_frame = tk.Frame(self.window, bg=t.bg)
        scroll_frame.pack(fill="both", expand=True, padx=20)

        canvas    = tk.Canvas(scroll_frame, bg=t.bg, highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._canvas   = canvas
        inner          = tk.Frame(canvas, bg=t.bg)
        win_id         = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))

        def _scroll(event: tk.Event) -> None:  # type: ignore[type-arg]
            canvas.yview_scroll(-1 if (event.num == 4 or event.delta > 0) else 1, "units")

        canvas.bind("<MouseWheel>", _scroll)
        canvas.bind("<Button-4>",   _scroll)
        canvas.bind("<Button-5>",   _scroll)

        # Build one label+Entry pair per column, stacked vertically.
        # (Previously label and entry sat side-by-side in two grid columns —
        # a long rule hint like "[≥-1000.0, ≤1000.0]" made the row wider
        # than the dialog, pushing the entry box off-screen with no
        # horizontal scrollbar to reach it. Stacking removes that failure
        # mode entirely: the entry always spans the visible width.)
        inner.grid_columnconfigure(0, weight=1)
        self.entries: dict[str, tk.Entry] = {}
        first_entry: tk.Entry | None = None
        for idx, col in enumerate(spec.columns):
            rule_hint = ""
            rule      = FIELD_RULES.get(col)
            if rule:
                parts: list[str] = []
                if rule.min_value is not None:
                    parts.append(f"≥{rule.min_value}")
                if rule.max_value is not None:
                    parts.append(f"≤{rule.max_value}")
                if rule.allowed:
                    parts.append(f"one of: {', '.join(rule.allowed)}")
                if parts:
                    rule_hint = f"  [{', '.join(parts)}]"

            row = idx * 2
            tk.Label(
                inner, text=f"{col}{rule_hint}:", bg=t.bg, fg=t.text,
                font=t.font_small, anchor="w", justify="left",
                wraplength=self._W - 60,
            ).grid(row=row, column=0, sticky="w", pady=(6, 2), padx=(4, 4))

            entry = tk.Entry(
                inner, bg=t.card, fg=t.text,
                insertbackground=t.accent, font=t.font_small, relief="flat")
            entry.grid(row=row + 1, column=0, sticky="ew", padx=(4, 4), pady=(0, 2), ipady=4)
            entry.insert(0, str(self.base_row.get(col, "")))
            self.entries[col] = entry
            if first_entry is None:
                first_entry = entry

        # Buttons
        btn_frame = tk.Frame(self.window, bg=t.bg)
        btn_frame.pack(pady=14)

        tk.Button(btn_frame, text="Add to Table", bg=t.green, fg=t.bg,
                  activebackground=t.btn_hover, font=t.font_bold,
                  relief="flat", padx=16, pady=8, cursor="hand2",
                  command=self.submit).pack(side="left", padx=8)

        tk.Button(btn_frame, text="Cancel", bg=t.card, fg=t.red,
                  font=t.font_small, relief="flat", padx=10, pady=6,
                  cursor="hand2", command=self._close).pack(side="left", padx=8)

        self.window.protocol("WM_DELETE_WINDOW", self._close)

        # NOTE: this dialog is intentionally NOT modal (no grab_set()).
        # grab_set()/focus_force() force keyboard focus programmatically,
        # and on some platforms/window-manager combinations that forcing
        # can fail silently, leaving the dialog open but unable to receive
        # any typed input. A plain Toplevel with only transient() behaves
        # exactly like every other Tk window: click a field, type in it —
        # it cannot get stuck in a "can't type anything" state.
        self.window.lift()
        if first_entry is not None:
            first_entry.focus_set()

    def _close(self) -> None:
        try:
            self._canvas.unbind("<MouseWheel>")
            self._canvas.unbind("<Button-4>")
            self._canvas.unbind("<Button-5>")
        except tk.TclError:
            pass
        self.window.destroy()

    def submit(self) -> None:
        row = self.base_row.copy()

        for col, entry in self.entries.items():
            raw = entry.get().strip()
            if not raw:
                messagebox.showwarning("Empty Field", f"Field '{col}' cannot be empty.")
                entry.focus_set()
                return
            try:
                row[col] = parse_field_value(raw, row[col])
            except FieldParseError as err:
                # This is the actual fix for the manual-entry bypass: a bad
                # numeric value now gets rejected here instead of being
                # silently kept as a string that skips DataValidator's
                # min/max checks (which only run on int/float values).
                messagebox.showerror("Invalid Value", f"'{col}' {err}.")
                entry.focus_set()
                return

        # Validate all field rules before accepting the record.
        errors = DataValidator.validate(row)
        if errors:
            LOG.debug("Manual entry rejected: %s", "; ".join(errors))
            messagebox.showerror(
                "Validation Error",
                "Please fix the following:\n\n" + "\n".join(f"• {e}" for e in errors),
            )
            return

        row = normalize_record(row, self.spec.columns)
        self.app.add_manual_record(row, self.spec)
        self._close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    configure_logging()
    LOG.info("EMDT Test Data Generator v%s starting", __version__)
    EMDTApp().run()


if __name__ == "__main__":
    main()