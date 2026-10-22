"""Read the Radtel RT-950 PRO vendor CPS's saved files (.dat) and compare them with a radiosmith backup.

The CPS (BT-RT950PRO_CPS, .NET) saves a BinaryFormatter object graph of
KDH.RadioData: channelData (990 KDH.Channel in 10 zones of 99, plus zone
names), freqModeData (VFO A/B/C), funConfigData (settings), dtmfData,
modulationData and aprsData. Values are as the CPS shows them (frequency
strings, list indexes), so they are a reference for what the radio's bytes mean.
"""

from __future__ import annotations

from pathlib import Path

from radiosmith import nrbf
from radiosmith.rt950 import codeplug

POWER = {0: "high", 1: "middle", 2: "low"}
PTT_ID = {0: "off", 1: "BOT", 2: "EOT", 3: "both"}


def load(path: Path) -> dict:
    root = nrbf.load(path.read_bytes())
    if root.get("__class__") != "KDH.RadioData":
        raise nrbf.NrbfError(f"{path} is not an RT-950 PRO CPS file (root is {root.get('__class__')})")
    return root


def _mhz(s: str) -> float | None:
    return float(s) if s and s.strip() else None


def _tone(s: str) -> str | None:
    return None if s in ("", "OFF", None) else s


def channels(root: dict) -> list[dict]:
    """The CPS's channels, numbered from 1, in the same shape as rt950.codeplug's."""
    out = []
    for i, c in enumerate(root["channelData"]["channelList"], 1):
        if not c.get("rxFreq"):
            continue
        out.append(
            {
                "number": i,
                "name": (c.get("chName") or "").strip(),
                "rx_mhz": _mhz(c["rxFreq"]),
                "tx_mhz": _mhz(c["txFreq"]),
                "power": POWER.get(c["txPower"], f"unknown({c['txPower']})"),
                "bandwidth_khz": 25.0 if c["bandWide"] == 0 else 12.5,
                "scan_add": bool(c["scanAdd"]),
                "busy_lock": bool(c["busyLockout"]),
                "rx_tone": _tone(c["rxQT"]),
                "tx_tone": _tone(c["txQT"]),
                "ptt_id": PTT_ID.get(c["pttId"], f"unknown({c['pttId']})"),
                "scramble": c["scram"],
                "fhss_learn": bool(c["learnFHSS"]),
                "rx_am": c["rxModulation"] == 1,
            }
        )
    return out


COMPARED = (
    "name", "rx_mhz", "tx_mhz", "power", "bandwidth_khz", "scan_add", "busy_lock", "rx_tone", "tx_tone",
    "ptt_id", "scramble", "fhss_learn", "rx_am",
)  # fmt: skip


def compare(root: dict, image: bytes) -> dict:
    """Channel by channel and VFO differences between a CPS file and a radiosmith image."""
    cps = {c["number"]: c for c in channels(root)}
    ours = {c.number: c.__dict__ for c in codeplug.decode_channels(image)}
    diffs = []
    for n in sorted(set(cps) | set(ours)):
        a, b = cps.get(n), ours.get(n)
        if a is None or b is None:
            diffs.append({"channel": n, "only_in": "cps" if b is None else "radiosmith"})
            continue
        for f in COMPARED:
            x, y = a[f], b[f]
            same = abs(x - y) < 1e-6 if isinstance(x, float) and isinstance(y, float) else x == y
            if not same:
                diffs.append({"channel": n, "field": f, "cps": x, "radiosmith": y})
    vfos = {}
    for i, key in enumerate(("vfoA", "vfoB", "vfoC")):
        digits = image[0x8000 + 32 * i : 0x8000 + 32 * i + 8]
        ours_vfo = f"{int(''.join(str(d) for d in digits)) / 100000:.5f}" if all(d < 10 for d in digits) else digits.hex()
        vfos[key] = {"cps": root["freqModeData"][key]["tB_RxFreq"], "radiosmith": ours_vfo}
    return {
        "channels_cps": len(cps),
        "channels_radiosmith": len(ours),
        "channel_differences": diffs,
        "vfos": vfos,
        "vfos_match": all(v["cps"] == v["radiosmith"] for v in vfos.values()),
    }
