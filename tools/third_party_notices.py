"""Write THIRD-PARTY-NOTICES.txt: the license texts of everything bundled into BRB (run in the build, after pip install).

Walks the dependency tree of requirements.txt for this platform, so it lists exactly what PyInstaller bundles, and copies
each package's own license / notice files (including the notices for libraries those packages bundle themselves).
Usage: python tools/third_party_notices.py OUT.txt
"""
import importlib.metadata as md
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)


def _name(req: str) -> str:
    return re.split(r"[\s;<>=!~\[(]", req.strip(), maxsplit=1)[0]


def _applies(req: str) -> bool:
    if ";" not in req:
        return True
    marker = req.split(";", 1)[1].strip()
    if "extra" in marker:
        return False
    try:
        from packaging.markers import Marker
        return Marker(marker).evaluate()
    except Exception:  # noqa: BLE001
        return True


def bundled() -> list[md.Distribution]:
    """Distributions in the requirements' dependency closure (this platform)."""
    todo = []
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line and _applies(line):
                todo.append(_name(line))
    seen, out = set(), []
    while todo:
        n = todo.pop()
        key = n.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        try:
            d = md.distribution(n)
        except md.PackageNotFoundError:
            continue
        out.append(d)
        todo += [_name(r) for r in (d.requires or []) if _applies(r)]
    return sorted(out, key=lambda d: d.metadata["Name"].lower())


def _license_texts(d: md.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in d.files or []:
        p = str(f).replace("\\", "/")
        if LICENSE_FILE.search(p) and (".dist-info/" in p or "/licenses/" in p.lower() or p.count("/") <= 1):
            try:
                with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                    texts.append((p.split("/")[-1], fh.read().strip()))
            except (OSError, ValueError):
                pass
    return texts


def main(out_path: str) -> None:
    parts = ["BRB (Be Right Back) - third-party notices",
             "",
             "BRB includes the open-source software listed below. Each is used under its own license, reproduced here.",
             "Components under the GNU LGPL may be replaced with modified versions; their source code is available from",
             "the projects' own pages (pypi.org and the URLs below), or on request from support@happyhacks.io.",
             ""]
    py_lic = next((p for p in (os.path.join(sys.base_prefix, n) for n in ("LICENSE.txt", "LICENSE")) if os.path.exists(p)), None)
    sections = []
    if py_lic:
        sections.append(("Python", sys.version.split()[0], "PSF License", "https://www.python.org",
                         [("LICENSE", open(py_lic, encoding="utf-8", errors="replace").read().strip())]))
    softcam = os.path.join(ROOT, "driver", "SOFTCAM-LICENSE.txt")
    if os.path.exists(softcam):
        sections.append(("softcam (HappyHacks Virtual Camera)", "", "MIT", "https://github.com/tshino/softcam",
                         [("LICENSE", open(softcam, encoding="utf-8").read().strip())]))
    for d in bundled():
        m = d.metadata
        lic = m.get("License-Expression") or (m.get("License") or "").split("\n")[0][:80]
        if not lic or lic.upper() == "UNKNOWN":
            lic = "; ".join(c.split("::")[-1].strip() for c in (m.get_all("Classifier") or []) if c.startswith("License"))
        url = m.get("Home-page") or next((u.split(",", 1)[-1].strip() for u in (m.get_all("Project-URL") or [])), "")
        sections.append((m["Name"], d.version, lic, url, _license_texts(d)))
    for name, ver, lic, url, texts in sections:
        parts += ["=" * 100, f"{name} {ver}".strip(), f"License: {lic or 'see below'}", *( [url] if url else [] ), "=" * 100, ""]
        for fname, text in texts or [("", "(license text not included in the package; see the project page above)")]:
            if fname:
                parts += [f"--- {fname} ---", ""]
            parts += [text, ""]
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(parts))
    print(f"{out_path}: {len(sections)} components")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "THIRD-PARTY-NOTICES.txt")
