#!/usr/bin/env python3
"""Generate omtether/ptp_names.json with human readable PTP code names.

The names are taken from libgphoto2's camlibs/ptp2/ptp.h (LGPL-2.1+), which is
the best public catalogue of Olympus/OM System vendor codes.  Only the
identifier strings and their numeric values are stored here.
"""
import json
import re
import sys
from pathlib import Path

src = Path(sys.argv[1] if len(sys.argv) > 1 else
           "ruta/a/libgphoto2/camlibs/ptp2/ptp.h")
out = Path(__file__).resolve().parent.parent / "omtether" / "ptp_names.json"

pat = re.compile(r"^#define\s+(PTP_(OC|DPC|EC)_[A-Za-z0-9_]+)\s+(0x[0-9A-Fa-f]+|\d+)")
names = {"OC": {}, "DPC": {}, "EC": {}}
for line in src.read_text(errors="replace").splitlines():
    m = pat.match(line)
    if not m:
        continue
    name, kind, val = m.group(1), m.group(2), m.group(3)
    code = int(val, 0)
    names[kind].setdefault(str(code), name.replace("PTP_%s_" % kind, ""))

out.write_text(json.dumps(names, indent=0, sort_keys=True))
print("wrote", out, {k: len(v) for k, v in names.items()})
