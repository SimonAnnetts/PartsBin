#!/usr/bin/env python3
"""PartsBin - catalogue of electronic components in stock (sqlite + tkinter).

Run:  python partsbin.py [path/to/parts.db]

Lookup sources (tried in order, results merged, earlier wins):
  1. Built-in knowledge  - common parts incl. pinouts (offline)
  2. Value parser        - decodes passives like "10k", "100nF", "4k7" (offline)
  3. Mouser API          - needs a free API key (Tools > Settings)
  4. EasyEDA / LCSC      - only for LCSC part numbers such as C2040 (no key)
"""
import contextlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile

import datasheets
import pinout
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(APP_DIR, "parts.db")
CONFIG_PATH = os.path.join(APP_DIR, "partsbin_config.json")

UNKNOWN_QTY = -1   # stock not counted yet; shown as "?"


def qty_text(q):
    return "?" if q == UNKNOWN_QTY else str(q)


def parse_qty(text):
    t = str(text).strip().lower()
    if t in ("?", "unknown", "-1"):
        return UNKNOWN_QTY
    return int(t or 0)


FIELDS = ["name", "type", "value", "package", "description", "pinout",
          "specs", "datasheet", "quantity", "location", "notes"]
TYPES = ["", "Resistor", "Capacitor", "Inductor", "Diode", "LED", "Transistor",
         "MOSFET", "IC", "Regulator", "Op-amp", "Microcontroller", "Connector",
         "Switch", "Crystal", "Relay", "Fuse", "Other"]

LIST_COLS = "id, created, " + ", ".join(FIELDS)    # everything except the image blob

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT '',
    value TEXT DEFAULT '',
    package TEXT DEFAULT '',
    description TEXT DEFAULT '',
    pinout TEXT DEFAULT '',
    specs TEXT DEFAULT '',
    datasheet TEXT DEFAULT '',
    quantity INTEGER DEFAULT 0,
    location TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    pinout_image BLOB
);
CREATE INDEX IF NOT EXISTS idx_parts_name ON parts(name);
"""


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
class DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(parts)")]
        if "pinout_image" not in cols:            # databases created before pinout images
            self.conn.execute("ALTER TABLE parts ADD COLUMN pinout_image BLOB")
            self.conn.commit()
        self.lock = threading.Lock()

    def search(self, text=""):
        q = f"SELECT {LIST_COLS} FROM parts"
        args = []
        if text.strip():
            like = f"%{text.strip()}%"
            q += (" WHERE name LIKE ? OR type LIKE ? OR value LIKE ? OR package LIKE ?"
                  " OR description LIKE ? OR location LIKE ? OR notes LIKE ?")
            args = [like] * 7
        q += " ORDER BY type, name COLLATE NOCASE"
        with self.lock:
            return self.conn.execute(q, args).fetchall()

    def get(self, pid):
        with self.lock:
            return self.conn.execute(f"SELECT {LIST_COLS} FROM parts WHERE id=?",
                                     (pid,)).fetchone()

    def set_datasheet(self, pid, url):
        with self.lock:
            self.conn.execute("UPDATE parts SET datasheet=? WHERE id=?", (url, pid))
            self.conn.commit()

    def get_image(self, pid):
        with self.lock:
            row = self.conn.execute("SELECT pinout_image FROM parts WHERE id=?",
                                    (pid,)).fetchone()
        return row["pinout_image"] if row else None

    def set_image(self, pid, data):
        with self.lock:
            self.conn.execute("UPDATE parts SET pinout_image=? WHERE id=?", (data, pid))
            self.conn.commit()

    def add(self, d):
        """Insert; if same name+value+location exists, add to its quantity.
        An unknown quantity (-1) never reduces a known one; known replaces unknown."""
        d = {k: d.get(k, "") for k in FIELDS}
        d["quantity"] = parse_qty(d["quantity"])
        with self.lock:
            row = self.conn.execute(
                "SELECT id, quantity FROM parts WHERE lower(name)=lower(?) AND lower(value)=lower(?) "
                "AND lower(location)=lower(?)", (d["name"], d["value"], d["location"])).fetchone()
            if row:
                old, new = row["quantity"], d["quantity"]
                total = (new if old == UNKNOWN_QTY else old if new == UNKNOWN_QTY
                         else old + new)
                self.conn.execute("UPDATE parts SET quantity=? WHERE id=?", (total, row["id"]))
                self.conn.commit()
                return row["id"], False
            cur = self.conn.execute(
                f"INSERT INTO parts ({','.join(FIELDS)}) VALUES ({','.join('?' * len(FIELDS))})",
                [d[k] for k in FIELDS])
            self.conn.commit()
            return cur.lastrowid, True

    def upsert(self, d):
        """Bulk-import add: a part already catalogued (same name+value, any location) is
        updated instead of duplicated. Location and a known quantity replace the old ones
        (a re-import is a fresh count); empty fields are filled, existing data is kept.
        Several matches: prefer the one at the same location, else add a new row.
        Returns (id, True if a new row was created)."""
        d = {k: d.get(k, "") for k in FIELDS}
        d["quantity"] = parse_qty(d["quantity"])
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM parts WHERE lower(name)=lower(?) AND lower(value)=lower(?)",
                (d["name"], d["value"])).fetchall()
        same = [r for r in rows if r["location"].lower() == d["location"].lower()]
        row = same[0] if same else rows[0] if len(rows) == 1 else None
        if row is None:
            return self.add(d)
        new = {k: row[k] for k in FIELDS}
        for k in FIELDS:
            if not new[k] and d[k] not in ("", None):
                new[k] = d[k]
        if d["location"]:
            new["location"] = d["location"]
        if d["quantity"] != UNKNOWN_QTY:
            new["quantity"] = d["quantity"]
        with self.lock:
            self.conn.execute(
                f"UPDATE parts SET {','.join(k + '=?' for k in FIELDS)} WHERE id=?",
                [new[k] for k in FIELDS] + [row["id"]])
            self.conn.commit()
        return row["id"], False

    def batches(self, gap_minutes=10):
        """Group parts into import batches: rows created within gap_minutes of the
        previous one belong together. Returns a list of dicts, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, created, name, location FROM parts ORDER BY created, id").fetchall()
        out, last = [], None
        for r in rows:
            try:
                t = time.mktime(time.strptime(r["created"], "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                t = 0
            if last is None or t - last > gap_minutes * 60:
                out.append(dict(start=r["created"], end=r["created"], ids=[], names=[], locs=set()))
            b = out[-1]
            b["end"] = r["created"]
            b["ids"].append(r["id"])
            b["names"].append(r["name"])
            b["locs"].add(r["location"])
            last = t
        return out

    def locations(self):
        """[(location, part count)] for every distinct location ('' = none)."""
        with self.lock:
            return [(r[0], r[1]) for r in self.conn.execute(
                "SELECT location, COUNT(*) FROM parts GROUP BY location ORDER BY lower(location)")]

    def delete_locations(self, locs):
        """Delete every part whose location is in locs. Returns the number deleted."""
        n = 0
        with self.lock:
            for l in locs:
                n += self.conn.execute("DELETE FROM parts WHERE location=?", (l,)).rowcount
            self.conn.commit()
        return n

    def set_location(self, ids, location, only_blank=False):
        with self.lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.conn.execute(
                    f"UPDATE parts SET location=? WHERE id IN ({','.join('?' * len(chunk))})"
                    + (" AND location=''" if only_blank else ""), [location] + chunk)
            self.conn.commit()

    def update(self, pid, d):
        d = {k: d.get(k, "") for k in FIELDS}
        d["quantity"] = parse_qty(d["quantity"])
        with self.lock:
            self.conn.execute(
                f"UPDATE parts SET {','.join(k + '=?' for k in FIELDS)} WHERE id=?",
                [d[k] for k in FIELDS] + [pid])
            self.conn.commit()

    def delete(self, pid):
        with self.lock:
            self.conn.execute("DELETE FROM parts WHERE id=?", (pid,))
            self.conn.commit()


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# Small offline knowledge base of very common parts.
KNOWN = {
    "LM358": dict(type="Op-amp", description="Dual low-power operational amplifier",
                  package="DIP-8 / SOIC-8",
                  pinout="1 OUT1, 2 IN1-, 3 IN1+, 4 GND/V-, 5 IN2+, 6 IN2-, 7 OUT2, 8 V+",
                  specs="3-32 V supply, 700 kHz GBW, 20 mA output"),
    "LM324": dict(type="Op-amp", description="Quad low-power operational amplifier",
                  package="DIP-14 / SOIC-14",
                  pinout="1 OUT1, 2 IN1-, 3 IN1+, 4 V+, 5 IN2+, 6 IN2-, 7 OUT2, 8 OUT3, "
                         "9 IN3-, 10 IN3+, 11 GND/V-, 12 IN4+, 13 IN4-, 14 OUT4",
                  specs="3-32 V supply, 1.2 MHz GBW"),
    "NE555": dict(type="IC", description="Precision timer", package="DIP-8 / SOIC-8",
                  pinout="1 GND, 2 TRIG, 3 OUT, 4 RESET, 5 CTRL, 6 THRES, 7 DISCH, 8 VCC",
                  specs="4.5-16 V, 200 mA output"),
    "LM7805": dict(type="Regulator", description="5 V 1 A linear voltage regulator",
                   package="TO-220", pinout="1 IN, 2 GND, 3 OUT (front view, left to right)",
                   specs="Vout 5 V, 1 A, Vin 7-25 V"),
    "LM317": dict(type="Regulator", description="Adjustable 1.25-37 V 1.5 A linear regulator",
                  package="TO-220", pinout="1 ADJ, 2 OUT, 3 IN (front view)",
                  specs="1.5 A, Vin up to 40 V"),
    "2N3904": dict(type="Transistor", description="NPN general purpose transistor",
                   package="TO-92", pinout="1 E, 2 B, 3 C (flat face toward you)",
                   specs="Vceo 40 V, Ic 200 mA, hFE 100-300"),
    "2N3906": dict(type="Transistor", description="PNP general purpose transistor",
                   package="TO-92", pinout="1 E, 2 B, 3 C (flat face toward you)",
                   specs="Vceo -40 V, Ic -200 mA"),
    "2N2222": dict(type="Transistor", description="NPN general purpose transistor",
                   package="TO-92 / TO-18", pinout="TO-92: 1 E, 2 B, 3 C",
                   specs="Vceo 40 V, Ic 800 mA"),
    "BC547": dict(type="Transistor", description="NPN general purpose transistor",
                  package="TO-92", pinout="1 C, 2 B, 3 E (flat face toward you)",
                  specs="Vceo 45 V, Ic 100 mA"),
    "BC557": dict(type="Transistor", description="PNP general purpose transistor",
                  package="TO-92", pinout="1 C, 2 B, 3 E (flat face toward you)",
                  specs="Vceo -45 V, Ic -100 mA"),
    "1N4148": dict(type="Diode", description="Small-signal switching diode",
                   package="DO-35", pinout="Cathode at banded end",
                   specs="100 V, 200 mA, trr 4 ns"),
    "1N4007": dict(type="Diode", description="General purpose rectifier diode",
                   package="DO-41", pinout="Cathode at banded end", specs="1000 V, 1 A"),
    "1N5819": dict(type="Diode", description="Schottky rectifier diode", package="DO-41",
                   pinout="Cathode at banded end", specs="40 V, 1 A"),
    "IRLZ44N": dict(type="MOSFET", description="N-channel logic-level power MOSFET",
                    package="TO-220", pinout="1 G, 2 D, 3 S (front view)",
                    specs="55 V, 47 A, Rds(on) 22 mOhm"),
    "IRF540N": dict(type="MOSFET", description="N-channel power MOSFET", package="TO-220",
                    pinout="1 G, 2 D, 3 S (front view)", specs="100 V, 33 A"),
    "LM1881": dict(type="IC", description="Video sync separator", package="DIP-8",
                   specs="Extracts composite/vertical sync, burst/back-porch and odd/even from video"),
    "NE592": dict(type="IC", description="Differential video amplifier", package="DIP-14 / DIP-8",
                  specs="Fixed gain 100 or 400, selectable via pin strapping"),
    "ATMEGA328P": dict(type="Microcontroller", description="8-bit AVR microcontroller, 32 KB flash",
                       package="DIP-28 / TQFP-32", specs="1.8-5.5 V, 20 MHz, 2 KB SRAM"),
    "74HC595": dict(type="IC", description="8-bit shift register with output latches",
                    package="DIP-16 / SOIC-16",
                    pinout="1 QB, 2 QC, 3 QD, 4 QE, 5 QF, 6 QG, 7 QH, 8 GND, 9 QH', 10 SRCLR, "
                           "11 SRCLK, 12 RCLK, 13 OE, 14 SER, 15 QA, 16 VCC", specs="2-6 V"),
    "L293D": dict(type="IC", description="Dual H-bridge motor driver", package="DIP-16",
                  specs="4.5-36 V, 600 mA per channel"),
    "ULN2003": dict(type="IC", description="7-channel Darlington array", package="DIP-16",
                    specs="50 V, 500 mA per channel"),
}

_SI = {"p": "p", "n": "n", "u": "µ", "µ": "µ", "m": "m"}


def _norm_name(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def parse_passive(text):
    """Decode simple passive values: 10k, 4k7, 100nF, 22pF, 10uH, 330R..."""
    t = text.strip()
    low = t.lower()
    hint = ("resistor" if re.search(r"resist|\bres\b", low) else
            "capacitor" if re.search(r"capac|\bcap\b", low) else
            "inductor" if re.search(r"induct|choke", low) else "")
    core = re.sub(r"(?i)\b(resistor|resistance|res|capacitor|cap|inductor|choke|"
                  r"ceramic|electrolytic|film|tantalum|smd|\d{4}|1/\d+w|\d+%)\b", " ", t)
    core = re.sub(r"\s+", "", core).replace("Ω", "R").replace("ohms", "R").replace("ohm", "R")
    # 4k7 / 2R2 style
    m = re.fullmatch(r"(\d+)([kKMrRpPnNuUµ])(\d+)(?:[RFHrfh])?", core)
    if m and len(m[3]) <= 3:
        num, unit = f"{m[1]}.{m[3]}", m[2]
    else:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKMrRpPnNuUµ]?)([RrFfHh]?)", core)
        if not m:
            return {}
        num, unit = m[1], m[2]
        suffix = m[3].upper()
        if not unit and suffix:
            unit = suffix
    ul = unit.lower() if unit not in ("M",) else "M"
    num = num.rstrip("0").rstrip(".") if "." in num else num
    if ul in ("k", "m", "M", "r", "") and hint != "capacitor" and hint != "inductor":
        if unit == "m" and hint != "resistor":
            return {}
        sym = {"k": "kΩ", "M": "MΩ", "m": "MΩ", "r": "Ω", "": "Ω"}[ul if unit != "M" else "M"]
        if not hint and ul in ("", "m"):
            return {}
        return dict(type="Resistor", value=f"{num}{sym}",
                    description=f"{num}{sym} resistor")
    if ul in ("p", "n", "u", "µ"):
        sym = _SI[ul]
        if re.search(r"(?i)\d\s*[pnuµ]?h\b", t) or hint == "inductor":
            return dict(type="Inductor", value=f"{num}{sym}H",
                        description=f"{num}{sym}H inductor")
        return dict(type="Capacitor", value=f"{num}{sym}F",
                    description=f"{num}{sym}F capacitor")
    return {}


def guess_type(name):
    n = name.upper()
    rules = [(r"^1N\d|^1S\d|^BAT\d|^BAS\d|^SS\d", "Diode"),
             (r"^(2N|BC|BD|BF|MPS|TIP|2S[ACDJK])\d", "Transistor"),
             (r"^(IRF|IRL|FQP|BS1|2N7|STP|AO)\w*\d", "MOSFET"),
             (r"^(LM78|LM79|AMS1117|LD1117|MCP17|L78|L79|LM317|LM1117)", "Regulator"),
             (r"^(LM358|LM324|TL07|TL08|NE5532|OP\d|MCP60|LM741|TLC)", "Op-amp"),
             (r"^(ATMEGA|ATTINY|STM32|PIC1|ESP|RP2040|MSP430)", "Microcontroller"),
             (r"^(LM|NE|TL|CD4|74|MAX|MCP|ADS|ULN|L293|SN7|UA7)", "IC")]
    for pat, typ in rules:
        if re.match(pat, n):
            return typ
    return ""


# Types that count as "the same family" when a type hint is given.
IC_FAMILY = {"IC", "Op-amp", "Regulator", "Microcontroller"}
PASSIVES = {"Resistor", "Capacitor", "Inductor"}


FUNC = {  # number (74xx suffix, or 4xxx CMOS) -> description
    "00": "Quad 2-input NAND gate", "02": "Quad 2-input NOR gate", "04": "Hex inverter",
    "08": "Quad 2-input AND gate", "10": "Triple 3-input NAND gate",
    "14": "Hex Schmitt-trigger inverter", "20": "Dual 4-input NAND gate",
    "32": "Quad 2-input OR gate", "74": "Dual D-type flip-flop with set/reset",
    "86": "Quad 2-input XOR gate", "138": "3-to-8 line decoder/demultiplexer",
    "139": "Dual 2-to-4 line decoder/demultiplexer", "157": "Quad 2-input multiplexer",
    "164": "8-bit serial-in parallel-out shift register",
    "165": "8-bit parallel-in serial-out shift register",
    "240": "Octal inverting buffer/line driver, 3-state",
    "244": "Octal buffer/line driver, 3-state",
    "245": "Octal bus transceiver, 3-state", "273": "Octal D-type flip-flop with clear",
    "373": "Octal transparent D-type latch, 3-state",
    "374": "Octal D-type flip-flop, 3-state", "393": "Dual 4-bit binary ripple counter",
    "47": "BCD to 7-segment decoder/driver, open-collector outputs",
    "247": "BCD to 7-segment decoder/driver, open-collector outputs",
    "595": "8-bit shift register with output latches, 3-state",
    "4001": "Quad 2-input NOR gate", "4002": "Dual 4-input NOR gate",
    "4011": "Quad 2-input NAND gate", "4013": "Dual D-type flip-flop",
    "4017": "Decade counter/divider with 10 decoded outputs",
    "4020": "14-stage binary ripple counter", "4040": "12-stage binary ripple counter",
    "4043": "Quad NOR R/S latch with 3-state outputs",
    "4046": "Phase-locked loop with VCO", "4047": "Monostable/astable multivibrator",
    "4049": "Hex inverting buffer/level converter", "4050": "Hex non-inverting buffer",
    "4051": "8-channel analog multiplexer/demultiplexer",
    "4060": "14-stage ripple counter with oscillator",
    "4066": "Quad bilateral analog switch", "4069": "Hex inverter",
    "4071": "Quad 2-input OR gate", "4078": "8-input NOR/OR gate",
    "4081": "Quad 2-input AND gate", "4093": "Quad 2-input Schmitt-trigger NAND gate",
    "4094": "8-stage shift-and-store bus register",
    "4511": "BCD to 7-segment latch/decoder/driver",
    "4538": "Dual precision retriggerable monostable multivibrator",
}
FAMILY = {"": "CMOS", "HC": "High-speed CMOS", "HCT": "High-speed CMOS (TTL levels)",
          "LS": "Low-power Schottky TTL", "ALS": "Advanced low-power Schottky TTL",
          "S": "Schottky TTL", "F": "FAST", "AC": "Advanced CMOS", "ACT": "Advanced CMOS (TTL levels)",
          "AHC": "Advanced high-speed CMOS", "AHCT": "Advanced high-speed CMOS (TTL levels)",
          "LV": "Low-voltage CMOS", "LVC": "Low-voltage CMOS", "C": "CMOS"}


def parse_logic(name):
    """Decode 74-series (74HC04, SN74LS247N) and 4000-series (4017, CD4017B, MC14017B)."""
    n = _norm_name(name)
    m = re.fullmatch(r"(?:SN|DM|MC|M)?74(HCT|HC|LS|ALS|AHCT|AHC|ACT|AC|LVC|LV|F|S|C)?"
                     r"(\d{2,4})[A-Z]{0,4}", n)
    if m and m[2] in FUNC:
        fam = m[1] or ""
        fam_txt = {"": "TTL"}.get(fam, FAMILY.get(fam, ""))
        return dict(type="IC", description=f"{FUNC[m[2]]} ({fam_txt}, 74{fam}{m[2]})",
                    package="DIP / SOIC", source="logic-family")
    m = re.fullmatch(r"(?:CD|HEF|HCF|TC|MC1|M)?(4\d{3})(?:UB|B|BE|BP|BCN|BF)?", n)
    if m and m[1] in FUNC:
        return dict(type="IC", description=f"{FUNC[m[1]]} (4000-series CMOS, {m[1]})",
                    package="DIP / SOIC", source="logic-family")
    return {}


def logic_keywords(name):
    """Search terms to try at a distributor for a (possibly bare) logic part number."""
    n = _norm_name(name)
    m = re.fullmatch(r"(?:CD|HEF|HCF|TC|MC1|M)?(4\d{3})(?:UB|B|BE|BP|BCN|BF)?", n)
    if m:
        return [f"CD{m[1]}B", f"HEF{m[1]}B", f"MC1{m[1]}B", f"CD{m[1]}", name]
    return [name]


def compatible(hint, typ):
    """True if a detected type is consistent with the user's type hint."""
    if not hint or not typ or hint == typ:
        return True
    return hint in IC_FAMILY and typ in IC_FAMILY


def lookup_offline(name, hint=""):
    key = _norm_name(name)
    for k, v in KNOWN.items():
        if (key.startswith(k) or key == k) and compatible(hint, v.get("type")):
            return {**v, "source": "builtin"}
    lg = parse_logic(name)
    if lg and compatible(hint, "IC"):
        return lg
    if hint and hint not in PASSIVES:
        # The user told us what it is: don't let pattern guessing contradict that.
        return dict(type=hint, source="type-hint")
    t = guess_type(name)
    p = {} if (t or (hint and hint not in PASSIVES)) else parse_passive(name)
    if p and compatible(hint, p.get("type")):
        p["source"] = "value-parser"
        return p
    if hint:
        return dict(type=hint, source="type-hint")
    if t:
        return dict(type=t, source="name-guess")
    return {}


def _http_json(url, data=None, headers=None, timeout=12):
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": "PartsBin/1.0", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def lookup_mouser(name, api_key, hint=""):
    want = _norm_name(name)
    for kw in logic_keywords(name):
        data = _http_json(
            f"https://api.mouser.com/api/v1/search/keyword?apiKey={api_key}",
            {"SearchByKeywordRequest": {"keyword": kw, "records": 30, "startingRecord": 0,
                                        "searchOptions": "None"}})
        parts = (data.get("SearchResults") or {}).get("Parts") or []

        def mpn(p):
            return _norm_name(p.get("ManufacturerPartNumber", ""))

        def fits(p):
            if want not in mpn(p):      # the part number must contain what we asked for
                return False
            if not hint:
                return True
            t = _category_to_type(p.get("Category", ""), hint)
            return bool(t) and compatible(hint, t)   # unknown category never passes a hint
        good = [p for p in parts if fits(p)]
        if good:
            best = min(good, key=lambda p: (0 if mpn(p) == want else 1 if mpn(p).startswith(want)
                                            else 2 if mpn(p).endswith(want) else 3, len(mpn(p))))
            break
    else:
        return {}
    attrs = {a["AttributeName"]: a["AttributeValue"]
             for a in best.get("ProductAttributes") or []}
    specs = "; ".join(f"{k}: {v}" for k, v in attrs.items())
    cat = best.get("Category", "")
    out = dict(description=best.get("Description", ""),
               datasheet=best.get("DataSheetUrl", ""), specs=specs,
               package=attrs.get("Package / Case", "") or attrs.get("Package", ""),
               source="mouser")
    out["type"] = _category_to_type(cat, hint) or hint
    if out["type"] in ("Resistor", "Capacitor", "Inductor"):
        out["value"] = (attrs.get("Resistance") or attrs.get("Capacitance")
                        or attrs.get("Inductance") or "")
    notes = [f"Mouser: {best.get('ManufacturerPartNumber', '')} / {cat}"]
    if best.get("Manufacturer"):
        notes.append(f"Manufacturer: {best['Manufacturer']}")
    out["notes"] = "; ".join(notes)
    return out


def _category_to_type(cat, hint=""):
    c = cat.lower()
    NON_IC = (r"resistor|capacitor|inductor|connector|cable|gland|strain|cord|enclosure|"
              r"hardware|wire|terminal|switch(?:es)?\b|led\b|lighting|relay|fuse|crystal|"
              r"transistor|mosfet|diode|rectifier|thyristor|socket|heat ?sink|battery|"
              r"sensor|tool|display|transformer|filter|circuit protection|motor|fan")
    if hint in IC_FAMILY and not re.search(NON_IC, c.replace("analog switch", "").replace(
            "switch ics", "")) and c:
        if "regulator" in c:
            return "Regulator"
        if "amplifier" in c:
            return "Op-amp"
        if "microcontroller" in c or "mcu" in c:
            return "Microcontroller"
        return "IC"
    for key, typ in [("resistor", "Resistor"), ("capacitor", "Capacitor"),
                     ("inductor", "Inductor"), ("mosfet", "MOSFET"), ("transistor", "Transistor"),
                     ("led", "LED"), ("diode", "Diode"), ("regulator", "Regulator"),
                     ("amplifier", "Op-amp"), ("microcontroller", "Microcontroller"),
                     ("connector", "Connector"), ("crystal", "Crystal"),
                     ("relay", "Relay"), ("switch", "Switch"), ("fuse", "Fuse")]:
        if key in c:
            return typ
    return "IC" if "ic" in c.split() or "integrated" in c else ""


def lookup_lcsc(name):
    """EasyEDA public API; only works for LCSC numbers like C2040."""
    if not re.fullmatch(r"C\d{3,9}", name.strip(), re.I):
        return {}
    data = _http_json(f"https://easyeda.com/api/products/{name.strip().upper()}"
                      "/components?version=6.4.19.5")
    res = data.get("result") or {}
    ds = res.get("dataStr", {}).get("head", {}).get("c_para", {}) if isinstance(
        res.get("dataStr"), dict) else {}
    out = dict(description=res.get("description") or res.get("title", ""),
               package=ds.get("package", ""), source="lcsc",
               datasheet=ds.get("link", ""))
    if ds.get("Manufacturer Part"):
        out["notes"] = f"MPN: {ds['Manufacturer Part']}"
    return out


def lookup(name, cfg=None, online=True, hint=""):
    """Merge results from all sources; earlier sources win per field."""
    cfg = cfg if cfg is not None else load_config()
    merged, sources, errors = {}, [], []

    def merge(r):
        if not r:
            return
        sources.append(r.get("source", "?"))
        for k, v in r.items():
            if k != "source" and v and not merged.get(k):
                merged[k] = v

    offline = lookup_offline(name, hint)
    merge(offline)
    # With no explicit hint, trust a type we already identified offline (e.g. 4538 -> IC)
    # so the distributor result must agree with it.
    eff_hint = hint or offline.get("type", "")
    if online:
        for label, fn in [("lcsc", lambda: lookup_lcsc(name)),
                          ("mouser", lambda: lookup_mouser(name, cfg["mouser_api_key"], eff_hint)
                           if cfg.get("mouser_api_key") else {})]:
            try:
                merge(fn())
            except Exception as e:  # network/API problems must never break adding parts
                errors.append(f"{label}: {e}")
    try:
        merge(lookup_kicad(name, hint))
    except Exception as e:
        errors.append(f"kicad: {e}")
    merged["_sources"] = ", ".join(sources) or "none"
    merged["_errors"] = errors
    return merged


# --------------------------------------------------------------------------
# KiCad symbol index: descriptions, datasheet links and pinouts, fully offline.
# kicad_index.db is a snapshot shipped with the app. It does not read any local
# KiCad install. To refresh it:  python partsbin.py --build-kicad-index [file.zip]
# --------------------------------------------------------------------------
INDEX_PATH = os.path.join(APP_DIR, "kicad_index.db")
KICAD_ZIP_URL = ("https://gitlab.com/kicad/libraries/kicad-symbols/-/archive/master/"
                 "kicad-symbols-master.zip")
_Q = r'"((?:[^"\\]|\\.)*)"'
_RE_SYM = re.compile(r'^\t\(symbol ' + _Q)
_RE_PROP = re.compile(r'\(property ' + _Q + ' ' + _Q)
_RE_PNAME = re.compile(r'\(name ' + _Q)
_RE_PNUM = re.compile(r'\(number ' + _Q)
_RE_EXT = re.compile(r'\(extends ' + _Q)


def _unesc(t):
    return re.sub(r"\\(.)", r"\1", t)


def parse_kicad_sym(text):
    """Extract {symbol: {props, pins, extends}} from a .kicad_sym file (line based)."""
    syms, cur, pending, pname = {}, None, False, ""
    for line in text.splitlines():
        m = _RE_SYM.match(line)
        if m:
            cur = syms.setdefault(_unesc(m[1]), dict(props={}, pins={}, extends=""))
            pending = False
            continue
        if cur is None:
            continue
        if line.startswith("\t\t(property "):
            m = _RE_PROP.search(line)
            if m:
                cur["props"].setdefault(_unesc(m[1]), _unesc(m[2]))
        elif line.startswith("\t\t(extends "):
            m = _RE_EXT.search(line)
            if m:
                cur["extends"] = _unesc(m[1])
        else:
            if "(pin " in line:
                pending, pname = True, ""
            if pending:
                m = _RE_PNAME.search(line)
                if m:
                    pname = _unesc(m[1])
                m = _RE_PNUM.search(line)
                if m:
                    cur["pins"].setdefault(_unesc(m[1]), pname)
                    pending = False
    return syms


def _pin_sort(num):
    return (0, int(num), "") if num.isdigit() else (1, 0, num)


def format_pinout(pins, limit=64):
    items = sorted(pins.items(), key=lambda kv: _pin_sort(kv[0]))
    txt = ", ".join(f"{n} {nm}" if nm and nm != "~" else n for n, nm in items[:limit])
    return txt + (f", ... ({len(items)} pins)" if len(items) > limit else "")


def kicad_type(lib):
    if lib.startswith(("Device", "Connector", "Mechanical", "power", "Jumper", "Switch",
                       "Battery", "Fuse", "Relay", "Buzzer", "Speaker", "Motor", "Lamp",
                       "Transformer", "Inductor", "Resistor", "Capacitor", "Crystal",
                       "Oscillator", "Display", "Sensor", "Notes")):
        return ""
    for pre, typ in [("Transistor_FET", "MOSFET"), ("Transistor", "Transistor"),
                     ("Diode", "Diode"), ("LED", "LED"), ("Regulator", "Regulator"),
                     ("Amplifier_Operational", "Op-amp"), ("MCU", "Microcontroller")]:
        if lib.startswith(pre):
            return typ
    return "IC"


def build_kicad_index(sources, log=print):
    """sources: .zip files (e.g. the GitLab archive) and/or directories of .kicad_sym."""
    tmp = INDEX_PATH + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    with contextlib.closing(sqlite3.connect(tmp)) as conn:
        conn.executescript(
            "CREATE TABLE symbols(lib TEXT, name TEXT, norm TEXT, description TEXT, "
            "keywords TEXT, datasheet TEXT, pinout TEXT, npins INTEGER, PRIMARY KEY(lib,name));"
            "CREATE INDEX idx_norm ON symbols(norm);"
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);")

        def add(lib, syms):
            for name, sym in syms.items():
                props, pins, par, hops = dict(sym["props"]), sym["pins"], sym["extends"], 0
                while par in syms and hops < 5:      # derived symbols inherit from parent
                    for k, v in syms[par]["props"].items():
                        if not props.get(k):
                            props[k] = v
                    pins = pins or syms[par]["pins"]
                    par, hops = syms[par]["extends"], hops + 1
                clean = lambda v: "" if v.strip() in ("~", "") else v.strip()
                conn.execute("INSERT OR REPLACE INTO symbols VALUES (?,?,?,?,?,?,?,?)", (
                    lib, name, _norm_name(name), clean(props.get("Description", "")),
                    clean(props.get("ki_keywords", "")), clean(props.get("Datasheet", "")),
                    format_pinout(pins), len(pins)))
            return len(syms)
        def lib_name(path):
            """'x/4xxx.kicad_symdir/4017.kicad_sym' and 'x/4xxx.kicad_sym' both -> '4xxx'."""
            parts = path.replace("\\", "/").split("/")
            for p in reversed(parts):
                if p.endswith(".kicad_symdir"):
                    return p[:-len(".kicad_symdir")]
            return parts[-1][:-len(".kicad_sym")]
        total = 0
        for src in sources:
            libs, nfiles = {}, 0
            if os.path.isdir(src):
                paths = [os.path.join(dp, f) for dp, _, fs in os.walk(src)
                         for f in sorted(fs) if f.endswith(".kicad_sym")]
                read = lambda p: open(p, encoding="utf-8", errors="replace").read()
                zf = None
            else:
                zf = zipfile.ZipFile(src)   # read members in memory; nothing is extracted
                paths = sorted(n for n in zf.namelist() if n.endswith(".kicad_sym"))
                read = lambda p: zf.read(p).decode("utf-8", "replace")
            for p in paths:
                # symbols in one library may extend each other across files, so group first
                libs.setdefault(lib_name(p), {}).update(parse_kicad_sym(read(p)))
                nfiles += 1
            for lib, syms in libs.items():
                total += add(lib, syms)
            log(f"{src}: {len(libs)} libraries from {nfiles} files")
            if zf:
                zf.close()
        conn.executemany("INSERT INTO meta VALUES (?,?)", [
            ("source", ", ".join(sources)), ("built", time.strftime("%Y-%m-%d")),
            ("symbols", str(total))])
        conn.commit()
    os.replace(tmp, INDEX_PATH)
    log(f"Indexed {total} symbols -> {INDEX_PATH}")


def download_and_build(log=print):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "kicad-symbols.zip")
        log("Downloading " + KICAD_ZIP_URL)
        req = urllib.request.Request(KICAD_ZIP_URL, headers={"User-Agent": "PartsBin/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        build_kicad_index([path], log)
        # record the real origin rather than the temp path
        with contextlib.closing(sqlite3.connect(INDEX_PATH)) as c:
            c.execute("UPDATE meta SET value=? WHERE key='source'", (KICAD_ZIP_URL,))
            c.commit()


_PREFIXES = ("MC1", "HEF", "HCF", "NJM", "CD", "SN", "DM", "MC", "TC", "UA", "KA", "MM", "M")


def kicad_candidates(name):
    """Normalised names to try, best first: as typed, prefix removed, package suffix trimmed."""
    n = _norm_name(name)
    bases = [(0, n)]
    for p in _PREFIXES:
        if n.startswith(p) and len(n) - len(p) >= 3 and n[len(p)].isdigit():
            bases.append((1, n[len(p):]))
            break
    out = []
    for pre, b in bases:
        out.append((0, pre, b))
        for k in range(1, 4):               # trim trailing package/grade letters
            if len(b) > 4 and b[-1].isalpha():
                b = b[:-1]
                out.append((k, pre, b))
            else:
                break
    return list(dict.fromkeys(c for _, _, c in sorted(out)))


def lookup_kicad(name, hint=""):
    if not os.path.exists(INDEX_PATH):
        return {}
    with contextlib.closing(sqlite3.connect(INDEX_PATH)) as conn:
        for cand in kicad_candidates(name):
            for lib, desc, ds, pins in conn.execute(
                    "SELECT lib, description, datasheet, pinout FROM symbols WHERE norm=? "
                    "ORDER BY (description = ''), npins DESC, lib", (cand,)):
                t = kicad_type(lib)
                if hint and t and not compatible(hint, t):
                    continue
                return dict(type=t, description=desc, datasheet=ds, pinout=pins,
                            source="kicad")
    return {}


# --------------------------------------------------------------------------
# Bulk-line parsing
# --------------------------------------------------------------------------
def parse_bulk(text):
    """Each line: name[, qty[, location]]  (separators: comma, tab, semicolon).
    A line like '10x 100nF @ drawer 3' also works.  Lines starting with # skipped."""
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        qty, loc, hint = UNKNOWN_QTY, "", ""   # not given = not counted yet
        m = re.match(r"^(%s)\s*:\s*(.+)$" % "|".join(re.escape(t) for t in TYPES if t),
                     line, re.I)
        if m:
            hint = next(t for t in TYPES if t.lower() == m[1].lower())
            line = m[2]
        m = re.match(r"^(\d+)\s*[xX×]\s*(.+)$", line)
        if m:
            qty, line = int(m[1]), m[2]
        if "@" in line:
            line, loc = [s.strip() for s in line.split("@", 1)]
        cols = [c.strip() for c in re.split(r"[,\t;]", line)]
        name = cols[0]
        if len(cols) > 1 and cols[1].isdigit():
            qty = int(cols[1])
            if len(cols) > 2:
                loc = ", ".join(c for c in cols[2:] if c) or loc
        elif len(cols) > 1:
            loc = ", ".join(c for c in cols[1:] if c) or loc
        if name:
            item = dict(name=name, quantity=qty, location=loc)
            if hint:
                item["hint"] = hint
            items.append(item)
    return items


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
# (label, url template, narrow the query with manufacturer + "datasheet pdf"?)
DATASHEET_SEARCHES = [
    ("datasheetarchive.com", "https://www.datasheetarchive.com/?q={q}", False),
    ("alldatasheet.com", "https://www.alldatasheet.com/view.jsp?Searchword={q}", False),
    ("the web", "https://duckduckgo.com/?q={q}", True),
]


def datasheet_terms(name, notes=""):
    """(part number, manufacturer) to search for. A distributor lookup records the exact
    manufacturer part number in Notes ('Mouser: CD4538BE / Counter ICs; Manufacturer: TI'),
    which is far better than a bare name like '4538'."""
    mpn = mfr = ""
    m = re.search(r"Mouser:\s*(.+?)\s+/\s", notes or "") or re.search(
        r"MPN:\s*([^;\s]+)", notes or "")
    if m:
        mpn = m[1].strip()
    m = re.search(r"Manufacturer:\s*([^;]+)", notes or "")
    if m:
        mfr = m[1].strip()
    return mpn or name.strip(), mfr


def datasheet_query(name, notes, narrow):
    mpn, mfr = datasheet_terms(name, notes)
    if not mpn:
        return ""
    return f'"{mpn}" {mfr} datasheet pdf'.replace("  ", " ") if narrow else mpn


class App(tk.Tk):
    def __init__(self, db_path):
        super().__init__()
        self.title(f"PartsBin - {os.path.basename(db_path)}")
        self.geometry("1150x650")
        self.db = DB(db_path)
        self.current_id = None
        self.vars = {}
        self.texts = {}
        self._build()
        self.refresh()

    # ---- layout
    def _build(self):
        menu = tk.Menu(self)
        tools = tk.Menu(menu, tearoff=0)
        tools.add_command(label="Bulk import...", command=self.bulk_dialog)
        tools.add_command(label="Locations by import date...", command=self.batches_dialog)
        tools.add_command(label="Settings...", command=self.settings_dialog)
        menu.add_cascade(label="Tools", menu=tools)
        self.config(menu=menu)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        top = ttk.Frame(left)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="Search:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(top, textvariable=self.search).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Bulk import", command=self.bulk_dialog).pack(side="left")

        pf = ttk.LabelFrame(left, text="Pinout")
        pf.pack(side="bottom", fill="x", padx=6, pady=(0, 6))
        self.pin_canvas = tk.Canvas(pf, height=270, bg="white", highlightthickness=0)
        self.pin_canvas.pack(fill="x", padx=4, pady=4)
        pb = ttk.Frame(pf)
        pb.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(pb, text="Export PNG...", command=self.export_png).pack(side="left", padx=2)
        ttk.Button(pb, text="Export SVG...", command=self.export_svg).pack(side="left", padx=2)
        ttk.Button(pb, text="Capture from datasheet...",
                   command=self.capture_image).pack(side="left", padx=2)
        ttk.Button(pb, text="Remove captured image",
                   command=self.remove_image).pack(side="left", padx=2)
        self.pin_canvas.bind("<Configure>", lambda e: self._schedule_pinout())
        self.current_image = None
        self._pin_job = None
        self._photo = None

        cols = ("name", "type", "value", "package", "quantity", "location")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, w in zip(cols, (140, 90, 80, 90, 55, 130)):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=(0, 6))
        self.tree.pack(fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        right = ttk.Frame(paned, padding=8)
        paned.add(right, weight=2)
        right.columnconfigure(1, weight=1)
        r = 0
        for f in FIELDS:
            ttk.Label(right, text=f.title()).grid(row=r, column=0, sticky="nw", pady=2)
            if f == "type":
                v = tk.StringVar()
                w = ttk.Combobox(right, textvariable=v, values=TYPES)
                self.vars[f] = v
            elif f in ("description", "pinout", "specs", "notes"):
                w = tk.Text(right, height=4 if f != "description" else 3, width=40, wrap="word")
                self.texts[f] = w
            else:
                v = tk.StringVar()
                w = ttk.Entry(right, textvariable=v)
                self.vars[f] = v
            w.grid(row=r, column=1, sticky="ew", pady=2, padx=4)
            r += 1
        self.vars["name"].trace_add("write", lambda *_: None)

        btns = ttk.Frame(right)
        btns.grid(row=r, column=0, columnspan=2, pady=8, sticky="w")
        ttk.Button(btns, text="Look up", command=self.do_lookup).pack(side="left", padx=2)
        ttk.Button(btns, text="Add as new", command=self.add_new).pack(side="left", padx=2)
        ttk.Button(btns, text="Save changes", command=self.save).pack(side="left", padx=2)
        ttk.Button(btns, text="Delete", command=self.delete).pack(side="left", padx=2)
        ttk.Button(btns, text="Clear", command=self.clear).pack(side="left", padx=2)
        row2 = ttk.Frame(right)
        row2.grid(row=r + 1, column=0, columnspan=2, sticky="w")
        mb = ttk.Menubutton(row2, text="Find datasheet")
        menu = tk.Menu(mb, tearoff=0, postcommand=lambda: self._fill_datasheet_menu(menu))
        mb["menu"] = menu
        mb.pack(side="left", padx=2)
        self.auto_btn = ttk.Button(row2, text="Auto-find datasheet link",
                                   command=self.auto_find_datasheet)
        self.auto_btn.pack(side="left", padx=2)
        ttk.Button(row2, text="Use local PDF...", command=self.use_local_pdf).pack(
            side="left", padx=2)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom")
        self.texts["pinout"].bind("<KeyRelease>", lambda e: self._schedule_pinout())
        self.vars["name"].trace_add("write", lambda *_: self._schedule_pinout())
        self.bind("<Control-Return>", lambda e: self.add_new())

    # ---- form helpers
    def get_form(self):
        d = {k: v.get().strip() for k, v in self.vars.items()}
        d.update({k: t.get("1.0", "end").strip() for k, t in self.texts.items()})
        try:
            d["quantity"] = parse_qty(d.get("quantity"))
        except ValueError:
            raise ValueError("Quantity must be a whole number, or ? if not counted")
        return d

    def set_form(self, d, only_empty=False):
        for k, v in self.vars.items():
            if k in d and (not only_empty or not v.get().strip()):
                v.set(qty_text(d[k]) if k == "quantity" else
                      d[k] if d[k] is not None else "")
        for k, t in self.texts.items():
            if k in d and d[k] and (not only_empty or not t.get("1.0", "end").strip()):
                t.delete("1.0", "end")
                t.insert("1.0", d[k])

    def clear(self):
        self.current_id = None
        self.current_image = None
        self.tree.selection_remove(self.tree.selection())
        for v in self.vars.values():
            v.set("")
        for t in self.texts.values():
            t.delete("1.0", "end")
        self.vars["quantity"].set("1")

    # ---- actions
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for row in self.db.search(self.search.get()):
            self.tree.insert("", "end", iid=str(row["id"]), values=(
                row["name"], row["type"], row["value"], row["package"],
                qty_text(row["quantity"]), row["location"]))
        if self.current_id and self.tree.exists(str(self.current_id)):
            self.tree.selection_set(str(self.current_id))

    def on_select(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        self.current_id = int(sel[0])
        row = self.db.get(self.current_id)
        if row:
            self.clear_fields_only()
            self.set_form(dict(row))
            self.current_image = self.db.get_image(self.current_id)
            self._schedule_pinout()

    def clear_fields_only(self):
        for v in self.vars.values():
            v.set("")
        for t in self.texts.values():
            t.delete("1.0", "end")

    def do_lookup(self):
        name = self.vars["name"].get().strip()
        if not name:
            messagebox.showinfo("Look up", "Enter a component name first.")
            return
        hint = self.vars["type"].get().strip()
        self.status.set(f"Looking up {name}...")

        def work():
            res = lookup(name, hint=hint)
            self.after(0, lambda: self._lookup_done(res))
        threading.Thread(target=work, daemon=True).start()

    def _lookup_done(self, res):
        errs = res.pop("_errors", [])
        src = res.pop("_sources", "none")
        self.set_form(res, only_empty=True)
        self._schedule_pinout()
        msg = f"Lookup sources: {src}"
        if errs:
            msg += f"  (errors: {'; '.join(errs)})"
        self.status.set(msg)

    def add_new(self):
        try:
            d = self.get_form()
        except ValueError as e:
            return messagebox.showerror("Error", str(e))
        if not d["name"]:
            return messagebox.showerror("Error", "Name is required")
        # auto-fill anything missing before saving (offline only here, fast)
        filled = lookup(d["name"], online=False, hint=d.get("type", ""))
        for k, v in filled.items():
            if not k.startswith("_") and not d.get(k):
                d[k] = v
        pid, new = self.db.add(d)
        self.current_id = pid
        self.refresh()
        self.status.set("Added." if new else "Already in stock at that location - quantity increased.")
        self.clear()

    def save(self):
        if not self.current_id:
            return messagebox.showinfo("Save", "Select a part first (or use 'Add as new').")
        try:
            self.db.update(self.current_id, self.get_form())
        except ValueError as e:
            return messagebox.showerror("Error", str(e))
        self.refresh()
        self.status.set("Saved.")

    def delete(self):
        if self.current_id and messagebox.askyesno("Delete", "Delete this part?"):
            self.db.delete(self.current_id)
            self.clear()
            self.refresh()

    # ---- pinout viewer
    def _schedule_pinout(self):
        if self._pin_job:
            self.after_cancel(self._pin_job)
        self._pin_job = self.after(80, self.draw_pinout)

    def _current_diagram(self):
        pins = pinout.parse_pinout(self.texts["pinout"].get("1.0", "end"))
        title = self.vars["name"].get().strip() or "?"
        return pinout.build_diagram(title, pins) if pins else None

    def draw_pinout(self):
        self._pin_job = None
        cv = self.pin_canvas
        cv.delete("all")
        cw, ch = max(cv.winfo_width(), 200), max(cv.winfo_height(), 100)
        if self.current_image:
            data = pinout.fit_png(self.current_image, cw - 8, ch - 8)
            try:
                self._photo = tk.PhotoImage(data=data)
                f = max(1, -(-self._photo.width() // (cw - 8)), -(-self._photo.height() // (ch - 8)))
                if f > 1:
                    self._photo = self._photo.subsample(f)
                cv.create_image(cw / 2, ch / 2, image=self._photo)
                return
            except tk.TclError:
                pass
        diagram = self._current_diagram()
        if diagram:
            sc = min((cw - 8) / diagram["w"], (ch - 8) / diagram["h"], 1.6)
            pinout.draw_on_canvas(cv, diagram, sc)
            cv.move("all", (cw - diagram["w"] * sc) / 2, (ch - diagram["h"] * sc) / 2)
        else:
            has = self.texts["pinout"].get("1.0", "end").strip()
            cv.create_text(cw / 2, ch / 2, fill="gray", justify="center", text=(
                "Pin data can't be drawn as a package diagram.\n"
                "Edit the pinout as '1 NAME, 2 NAME, ...' or capture the figure from the datasheet."
                if has else "No pinout data for this part."))

    def _save_dialog(self, ext, kinds):
        from tkinter import filedialog
        name = re.sub(r"[^\w.-]+", "_", self.vars["name"].get().strip() or "pinout")
        return filedialog.asksaveasfilename(
            defaultextension=ext, initialfile=f"{name}_pinout{ext}", filetypes=kinds)

    def export_png(self):
        try:
            if self.current_image:
                data = self.current_image
            else:
                diagram = self._current_diagram()
                if not diagram:
                    return messagebox.showinfo("Export", "There is no pinout to export.")
                data = pinout.diagram_png(diagram)
        except RuntimeError as e:
            return messagebox.showinfo("Export PNG", str(e))
        path = self._save_dialog(".png", [("PNG image", "*.png")])
        if path:
            with open(path, "wb") as f:
                f.write(data)
            self.status.set(f"Saved {path}")

    def export_svg(self):
        if self.current_image:
            return messagebox.showinfo("Export SVG", "A captured datasheet image is a bitmap; "
                                       "use Export PNG. SVG is for generated diagrams.")
        diagram = self._current_diagram()
        if not diagram:
            return messagebox.showinfo("Export", "There is no pinout to export.")
        path = self._save_dialog(".svg", [("SVG image", "*.svg")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(pinout.diagram_svg(diagram))
            self.status.set(f"Saved {path}")

    def capture_image(self):
        if not self.current_id:
            return messagebox.showinfo("Capture", "Select or save the part first.")
        pid = self.current_id

        def done(png):
            self.db.set_image(pid, png)
            if self.current_id == pid:
                self.current_image = png
                self._schedule_pinout()
            self.status.set("Pinout image stored.")
        pinout.capture_from_datasheet(self, self.vars["datasheet"].get().strip(),
                                      self.vars["name"].get().strip(), done, self.status.set)

    def remove_image(self):
        if self.current_id and self.current_image and messagebox.askyesno(
                "Remove", "Remove the captured image and go back to the generated diagram?"):
            self.db.set_image(self.current_id, None)
            self.current_image = None
            self._schedule_pinout()

    def use_local_pdf(self):
        """Pick a downloaded datasheet; its path becomes the datasheet link, then capture."""
        path = filedialog.askopenfilename(
            parent=self, title="Choose the datasheet PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*")])
        if not path:
            return
        self.vars["datasheet"].set(path)
        if self.current_id:
            self.status.set("Local PDF set - drag a box round the pinout, then Save changes.")
            self.capture_image()
        else:
            self.status.set("Local PDF set. Save the part, then use Capture from datasheet.")

    def open_datasheet(self):
        url = self.vars["datasheet"].get().strip()
        if url.startswith("http"):
            webbrowser.open(url)
        elif os.path.isfile(pinout.local_path(url)):
            path = pinout.local_path(url)
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                webbrowser.open("file://" + urllib.parse.quote(os.path.abspath(path)))
        else:
            self.status.set("No datasheet link stored for this part.")

    def auto_find_datasheet(self):
        name = self.vars["name"].get().strip()
        if not name:
            return messagebox.showinfo("Datasheet", "Enter a component name first.")
        mpn, mfr = datasheet_terms(name, self.texts["notes"].get("1.0", "end"))
        pid = self.current_id
        self.auto_btn.config(state="disabled")

        def progress(msg):
            self.after(0, lambda: self.status.set(msg))

        def work():
            try:
                url, note = datasheets.find_datasheet(
                    name, mpn if mpn != name else "", mfr, progress,
                    serper_key=load_config().get("serper_api_key", ""))
            except Exception as e:
                url, note = None, f"Search failed: {e}"
            self.after(0, lambda: done(url, note))

        def done(url, note):
            self.auto_btn.config(state="normal")
            if not url:
                return self.status.set(f"No datasheet link stored. {note}")
            if self.vars["name"].get().strip() == name:       # user hasn't moved on
                self.vars["datasheet"].set(url)
            if pid:                                           # already in the database
                self.db.set_datasheet(pid, url)
            self.status.set(f"Datasheet link {'saved' if pid else 'set (add/save the part to keep it)'}: "
                            f"{url}  ({note})")
        threading.Thread(target=work, daemon=True).start()

    def _fill_datasheet_menu(self, menu):
        """Rebuilt each time it opens so it shows what will actually be searched for."""
        menu.delete(0, "end")
        menu.add_command(label="Open stored datasheet link", command=self.open_datasheet)
        menu.add_separator()
        name = self.vars["name"].get()
        notes = self.texts["notes"].get("1.0", "end")
        for label, tmpl, narrow in DATASHEET_SEARCHES:
            q = datasheet_query(name, notes, narrow)
            menu.add_command(label=f"Search {label}: {q}"[:90] if q else f"Search {label}",
                             state="normal" if q else "disabled",
                             command=lambda t=tmpl, q=q: webbrowser.open(
                                 t.format(q=urllib.parse.quote_plus(q))))

    # ---- dialogs
    def batches_dialog(self):
        """List parts grouped by import time and set a location for whole batches."""
        w = tk.Toplevel(self)
        w.title("Locations by import date")
        w.geometry("860x700")
        w.transient(self)
        ttk.Label(w, justify="left", text=(
            "Parts are grouped by when they were added (rows added within 10 minutes of each "
            "other are one import).\nSelect one or more imports, type a location, and apply.")
                  ).pack(anchor="w", padx=8, pady=6)
        cols = ("imported", "parts", "locations", "examples")
        tree = ttk.Treeview(w, columns=cols, show="headings", selectmode="extended")
        for c, t, wd in (("imported", "Imported", 150), ("parts", "Parts", 50),
                         ("locations", "Current location(s)", 170), ("examples", "Examples", 440)):
            tree.heading(c, text=t)
            tree.column(c, width=wd, anchor="e" if c == "parts" else "w")
        tree.pack(fill="both", expand=True, padx=8)
        data = {}

        def load():
            tree.delete(*tree.get_children())
            data.clear()
            for b in self.db.batches():
                locs = ", ".join(sorted(l or "(none)" for l in b["locs"]))
                ex = ", ".join(b["names"][:8]) + (" ..." if len(b["names"]) > 8 else "")
                iid = tree.insert("", "end", values=(b["start"], len(b["ids"]), locs, ex))
                data[iid] = b
            ltree.delete(*ltree.get_children())
            for l, n in self.db.locations():
                ltree.insert("", "end", iid=l or "__none__", values=(l or "(none)", n))

        row = ttk.Frame(w)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Label(row, text="Location:").pack(side="left")
        loc = tk.StringVar()
        ttk.Entry(row, textvariable=loc, width=26).pack(side="left", padx=4)
        blank = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="only parts with no location", variable=blank).pack(side="left")

        def apply():
            sel = tree.selection()
            if not sel:
                return messagebox.showinfo("Locations", "Select at least one import.", parent=w)
            ids = [i for s in sel for i in data[s]["ids"]]
            self.db.set_location(ids, loc.get().strip(), blank.get())
            self.refresh()
            load()
            self.status.set(f"Location set on up to {len(ids)} parts.")
        ttk.Button(row, text="Apply to selected imports", command=apply).pack(side="left", padx=8)

        ttk.Separator(w).pack(fill="x", padx=8, pady=4)
        ttk.Label(w, text="Delete parts by location (select one or more):").pack(anchor="w", padx=8)
        ltree = ttk.Treeview(w, columns=("loc", "n"), show="headings", height=6,
                             selectmode="extended")
        ltree.heading("loc", text="Location")
        ltree.heading("n", text="Parts")
        ltree.column("loc", width=300)
        ltree.column("n", width=60, anchor="e")
        ltree.pack(fill="both", expand=True, padx=8)

        def delete_locs():
            sel = ltree.selection()
            if not sel:
                return messagebox.showinfo("Delete", "Select at least one location.", parent=w)
            locs = ["" if i == "__none__" else i for i in sel]
            total = sum(int(ltree.set(i, "n")) for i in sel)
            names = ", ".join(l or "(none)" for l in locs)
            if not messagebox.askyesno(
                    "Delete parts", f"Permanently delete {total} parts at: {names}?\n"
                    "This cannot be undone.", icon="warning", default="no", parent=w):
                return
            n = self.db.delete_locations(locs)
            self.current_id = None
            self.refresh()
            load()
            self.status.set(f"Deleted {n} parts.")
        ttk.Button(w, text="Delete all parts at selected locations",
                   command=delete_locs).pack(pady=8)
        load()

    def settings_dialog(self):
        cfg = load_config()
        w = tk.Toplevel(self)
        w.title("Settings")
        w.transient(self)
        ttk.Label(w, text="Mouser API key (free from mouser.com, optional):").pack(
            padx=10, pady=(10, 2), anchor="w")
        v = tk.StringVar(value=cfg.get("mouser_api_key", ""))
        ttk.Entry(w, textvariable=v, width=50).pack(padx=10)
        ttk.Label(w, justify="left", text=(
            "Serper API key (serper.dev, free signup, optional) - used to find datasheet\n"
            "links; without it the app falls back to scraping DuckDuckGo/Bing, which is\n"
            "often throttled:")).pack(padx=10, pady=(12, 2), anchor="w")
        sv = tk.StringVar(value=cfg.get("serper_api_key", ""))
        ttk.Entry(w, textvariable=sv, width=50).pack(padx=10)

        def ok():
            cfg["mouser_api_key"] = v.get().strip()
            cfg["serper_api_key"] = sv.get().strip()
            save_config(cfg)
            w.destroy()
        ttk.Button(w, text="Save", command=ok).pack(pady=10)

    def bulk_dialog(self):
        w = tk.Toplevel(self)
        w.title("Bulk import")
        w.geometry("700x620")
        w.minsize(560, 480)
        w.transient(self)
        ttk.Label(w, justify="left", text=(
            "One component per line:  name, quantity, location\n"
            "Also accepted:  10x 100nF @ Drawer 3   |   LM358 ; 5 ; Box A\n"
            "No quantity given = unknown (shown as ?). Lines starting with # are ignored.")).pack(
            anchor="w", padx=8, pady=6)
        txt = scrolledtext.ScrolledText(w, wrap="none", height=10)
        txt.pack(fill="both", expand=True, padx=8)
        hint_row = ttk.Frame(w)
        hint_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(hint_row, text="Component type for all lines:").pack(side="left")
        hint_var = tk.StringVar(value="Auto-detect")
        ttk.Combobox(hint_row, textvariable=hint_var, state="readonly", width=18,
                     values=["Auto-detect"] + [t for t in TYPES if t]).pack(side="left", padx=4)
        ttk.Label(hint_row, text="(narrows the search; a line can override with 'type: name')",
                  foreground="gray").pack(side="left")
        loc_row = ttk.Frame(w)
        loc_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(loc_row, text="Location for all lines:").pack(side="left")
        loc_var = tk.StringVar()
        ttk.Entry(loc_row, textvariable=loc_var, width=24).pack(side="left", padx=4)
        ttk.Label(loc_row, text="(used when a line gives no location)",
                  foreground="gray").pack(side="left")
        online = tk.BooleanVar(value=True)
        ttk.Checkbutton(w, text="Look up online data (Mouser / LCSC) as well as offline",
                        variable=online).pack(anchor="w", padx=8)
        web = tk.BooleanVar(value=False)
        ttk.Checkbutton(w, text="Also search the web for missing datasheet links "
                        "(slow: ~5-15 s per part)", variable=web).pack(anchor="w", padx=8)
        prog = ttk.Progressbar(w)
        prog.pack(fill="x", padx=8, pady=4)
        log = tk.StringVar()
        ttk.Label(w, textvariable=log).pack(anchor="w", padx=8)
        btn = ttk.Button(w, text="Import")
        btn.pack(pady=8)

        def run():
            items = parse_bulk(txt.get("1.0", "end"))
            if not items:
                return
            default_loc = loc_var.get().strip()
            for it in items:
                if not it["location"]:
                    it["location"] = default_loc
            btn.config(state="disabled")
            prog.config(maximum=len(items), value=0)
            cfg = load_config()
            hint = "" if hint_var.get() == "Auto-detect" else hint_var.get()

            def work():
                cache, added, merged, unknown = {}, 0, 0, []
                for i, it in enumerate(items, 1):
                    h = it.pop("hint", "") or hint
                    key = (it["name"].lower(), h)
                    if key not in cache:
                        cache[key] = lookup(it["name"], cfg, online.get(), h)
                    info = {k: v for k, v in cache[key].items() if not k.startswith("_")}
                    if not info.get("description") and not info.get("type"):
                        unknown.append(it["name"])
                    if web.get() and not info.get("datasheet"):
                        mpn, mfr = datasheet_terms(it["name"], info.get("notes", ""))
                        self.after(0, lambda n=it["name"]: log.set(f"Searching datasheet: {n}"))
                        try:
                            url, _ = datasheets.find_datasheet(
                                it["name"], mpn if mpn != it["name"] else "", mfr,
                                serper_key=cfg.get("serper_api_key", ""))
                        except Exception:
                            url = None
                        if url:
                            info["datasheet"] = url
                            cache[key]["datasheet"] = url
                    rec = {**info, **it}
                    _, new = self.db.upsert(rec)
                    added += new
                    merged += (not new)
                    self.after(0, lambda i=i, n=it["name"]: (
                        prog.config(value=i), log.set(f"{i}/{len(items)}: {n}")))
                self.after(0, lambda: done(added, merged, unknown))
            threading.Thread(target=work, daemon=True).start()

        def done(added, merged, unknown):
            self.refresh()
            msg = f"Imported: {added} new, {merged} updated existing."
            if unknown:
                msg += f"\nNo data found for: {', '.join(dict.fromkeys(unknown))}"
            messagebox.showinfo("Bulk import", msg, parent=w)
            w.destroy()
        btn.config(command=run)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--build-kicad-index":
        if len(sys.argv) > 2:
            build_kicad_index(sys.argv[2:])
        else:
            download_and_build()
        sys.exit(0)
    App(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB).mainloop()
