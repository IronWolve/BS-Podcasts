"""Fail while a component's notice names a licence whose text is not bundled.

The notices file used to open by claiming every text was included in
`licenses/`, while seven components shipped none — and `bs-podcasts.spec`
copies that folder into every Windows build, so the claim reached users. MIT,
BSD and ISC all require the text and copyright notice to be reproduced in a
redistribution, so this is a licence-compliance gap, not a formatting one.

Run before cutting a release. Exits non-zero while anything is missing, and
prints exactly which file each component needs.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging" / "THIRD-PARTY-NOTICES.txt"
LICENSES = ROOT / "packaging" / "licenses"

# Licence name as it appears in the notices -> the file it should be shipped as
# and where the authoritative text comes from.
# Matched as a substring of the notices, because entries are written both as
# "License: X" and inline as "component — X License".
EXPECTED = {
    "ISC": ("ISC.txt", "libass — COPYING in the libass source tree"),
    "MIT License": ("MIT.txt", "urllib3 / charset-normalizer — LICENSE.txt in each sdist"),
    'MIT ("Old MIT")': ("MIT-HarfBuzz.txt", "HarfBuzz — COPYING in the harfbuzz source tree"),
    "FreeType License": ("FTL.txt", "FreeType — docs/FTL.TXT in the freetype source tree"),
    "BSD 3-Clause License": ("BSD-3-Clause.txt", "idna — LICENSE.md in the idna sdist"),
    "Python Software Foundation License": ("PSF.txt", "Python — LICENSE in the CPython distribution"),
}


def main() -> int:
    if not NOTICES.is_file():
        print(f"missing {NOTICES}")
        return 1
    text = NOTICES.read_text()

    present = {path.name for path in LICENSES.glob("*.txt")}
    # Anything the notices reference by path must actually be there.
    referenced = set(re.findall(r"licenses/([A-Za-z0-9._-]+\.txt)", text))
    dangling = sorted(referenced - present)

    missing = []
    for licence, (filename, source) in EXPECTED.items():
        if licence in text and filename not in present:
            missing.append((licence, filename, source))

    if dangling:
        print("Notices reference licence files that are not bundled:")
        for name in dangling:
            print(f"  licenses/{name}")
    if missing:
        print("Components name a licence with no text bundled:")
        for licence, filename, source in missing:
            print(f"  {licence:24} -> packaging/licenses/{filename}")
            print(f"  {'':24}    copy verbatim from: {source}")
    if dangling or missing:
        print(f"\n{len(dangling) + len(missing)} licence text(s) still required before release.")
        return 1

    print(f"licence texts: {len(present)} bundled, every referenced file present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
