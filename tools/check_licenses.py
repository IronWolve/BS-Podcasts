"""Fail while a component's notice names a licence whose text is not bundled.

The notices file used to open by claiming every text was included in
`licenses/`, while seven components shipped none — and `bs-podcasts.spec`
copies that folder into every Windows build, so the claim reached users. MIT,
BSD and ISC all require the text and copyright notice to be reproduced in a
redistribution, so this is a licence-compliance gap, not a formatting one.

All seven texts are now bundled, copied verbatim: urllib3, charset-normalizer,
idna and Python from the exact installed distributions in this project's venv,
and libass/HarfBuzz/FreeType from each project's own canonical repository.
Every MIT/BSD-family component ships its own file because the copyright holder
differs per component — a shared generic MIT.txt would name the wrong holder.

This gate keeps that true: each licence named in the notices must carry an
inline `licenses/<file>` reference, and every referenced file must exist.
Run before cutting a release; exits non-zero while anything is missing.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging" / "THIRD-PARTY-NOTICES.txt"
LICENSES = ROOT / "packaging" / "licenses"

# Component -> the per-component file its notice must reference, and where the
# authoritative text comes from if it ever needs re-copying. Matched as a
# substring of the notices so renaming an entry without its file fails loudly.
EXPECTED = {
    "libass": ("ISC-libass.txt", "COPYING in the libass source tree"),
    "urllib3": ("MIT-urllib3.txt", "LICENSE.txt in the urllib3 sdist"),
    "charset-normalizer": ("MIT-charset-normalizer.txt", "LICENSE in the charset-normalizer sdist"),
    "HarfBuzz": ("MIT-HarfBuzz.txt", "COPYING in the harfbuzz source tree"),
    "FreeType": ("FTL.txt", "docs/FTL.TXT in the freetype source tree"),
    "idna": ("BSD-3-Clause-idna.txt", "LICENSE.md in the idna sdist"),
    "Python Software Foundation License": ("PSF.txt", "LICENSE in the CPython distribution"),
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
    for component, (filename, source) in EXPECTED.items():
        if component not in text:
            continue
        if filename not in present or filename not in text:
            missing.append((component, filename, source))

    if dangling:
        print("Notices reference licence files that are not bundled:")
        for name in dangling:
            print(f"  licenses/{name}")
    if missing:
        print("Components missing a bundled + referenced licence text:")
        for component, filename, source in missing:
            print(f"  {component:24} -> packaging/licenses/{filename} (must exist and be named in the notices)")
            print(f"  {'':24}    copy verbatim from: {source}")
    if dangling or missing:
        print(f"\n{len(dangling) + len(missing)} licence text(s) still required before release.")
        return 1

    print(f"licence texts: {len(present)} bundled, every referenced file present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
