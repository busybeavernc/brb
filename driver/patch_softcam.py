"""Turn a softcam checkout into the "Doppleganger" camera.

Run on CI before building:  python driver/patch_softcam.py <softcam-checkout> [toolset, e.g. v145]

Changes (each is checked, so an upstream change fails the build loudly instead of silently):
- Camera name shown in Zoom/Teams/browsers: "HappyHacks Virtual Camera"
- Its own COM class ID, mutex and shared-memory names, so it never collides with
  another softcam-based camera installed on the same PC
- Opens even before the Doppleganger app is running (1280x720 @ 30), so Zoom can
  select it at any time; without frames it shows a neutral "Camera starting..."
  picture (driver/make_placeholder.py -> doppleganger_placeholder.h) instead of
  reading a saved screenshot that may not exist (which crashed Zoom)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

GUID_LINE = "0x548a881e, 0x7657, 0x4231, 0x8f, 0xea, 0xa9, 0x87, 0x7d, 0x97, 0x43, 0xc9);"
DEFAULT_W, DEFAULT_H, DEFAULT_FPS = 1280, 720, 30

PATCHES = {
    "src/softcam/softcam.cpp": [
        ("0xaef3b972, 0x5fa5, 0x4647, 0x95, 0x71, 0x35, 0x8e, 0xb4, 0x72, 0xbc, 0x9e);", GUID_LINE),
        ('const wchar_t FILTER_NAME[] = L"DirectShow Softcam";', 'const wchar_t FILTER_NAME[] = L"HappyHacks Virtual Camera";'),
    ],
    "src/softcamcore/DShowSoftcam.cpp": [
        ('CSource(NAME("DirectShow Softcam"), lpunk, clsid),', 'CSource(NAME("HappyHacks Virtual Camera"), lpunk, clsid),'),
        ('L"DirectShow Softcam Stream"', 'L"HappyHacks Virtual Camera Stream"'),
        ('NAME("DirectShow Softcam Stream")', 'NAME("HappyHacks Virtual Camera Stream")'),
        ("    m_valid(m_frame_buffer ? true : false),\n"
         "    m_width(m_frame_buffer.width()),\n"
         "    m_height(m_frame_buffer.height()),\n"
         "    m_framerate(m_frame_buffer.framerate())",
         "    m_valid(true),\n"
         f"    m_width(m_frame_buffer ? m_frame_buffer.width() : {DEFAULT_W}),\n"
         f"    m_height(m_frame_buffer ? m_frame_buffer.height() : {DEFAULT_H}),\n"
         f"    m_framerate(m_frame_buffer ? m_frame_buffer.framerate() : {DEFAULT_FPS}.0f)"),
    ],
    # No picture from the app (not running / just quit): show a neutral "Camera starting..." frame.
    # Upstream copies a saved screenshot here, which doesn't exist if the app never ran -
    # with the camera opening early (above) that crashed the calling app (Zoom).
    "src/softcamcore/DShowSoftcam.cpp#placeholder": [
        ('#include "DShowSoftcam.h"\n', '#include "DShowSoftcam.h"\n#include "doppleganger_placeholder.h"\n'),
        ("                    // Darken the image to indicate that the source is inactive.\n"
         "                    for (std::size_t i = 0; i < size; i++)\n"
         "                    {\n"
         "                        pData[i] /= 4;\n"
         "                    }\n",
         "                    // The app stopped: show the neutral placeholder, not a frozen frame of you.\n"
         "                    dopplegangerPlaceholder(pData, m_width, m_height);\n"),
        ("            const std::size_t size = calcDIBSize(m_width, m_height);\n"
         "            std::memcpy(pData, m_screenshot.get(), size);\n",
         "            dopplegangerPlaceholder(pData, m_width, m_height);\n"),
    ],
    "src/softcamcore/FrameBuffer.cpp": [
        ('"DirectShow Softcam/NamedMutex"', '"Doppleganger/NamedMutex"'),
        ('"DirectShow Softcam/SharedMemory"', '"Doppleganger/SharedMemory"'),
    ],
}


def set_toolset(base: pathlib.Path, toolset: str) -> None:
    """Match the C++ toolset to whatever Visual Studio the build machine has (v143 = VS2022, v145 = VS2026)."""
    for proj in base.joinpath("src").rglob("*.vcxproj"):
        if "vs2019" in proj.name:
            continue
        text = proj.read_text(encoding="utf-8")
        new = text.replace("<PlatformToolset>v143</PlatformToolset>", f"<PlatformToolset>{toolset}</PlatformToolset>")
        if new != text:
            proj.write_text(new, encoding="utf-8")
            print(f"toolset {toolset}: {proj.relative_to(base)}")


def main(root: str, toolset: str | None = None) -> None:
    base = pathlib.Path(root)
    import make_placeholder  # noqa: E402 - same folder
    make_placeholder.main(str(base / "src" / "softcamcore" / "doppleganger_placeholder.h"))
    if toolset and toolset != "v143":
        set_toolset(base, toolset)
    for key, edits in PATCHES.items():
        rel = key.split("#")[0]  # several patch groups may target one file
        path = base / rel
        text = path.read_text(encoding="utf-8")
        for old, new in edits:
            if text.count(old) != 1:
                sys.exit(f"patch_softcam: expected exactly one match in {rel} for:\n{old}")
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
        print(f"patched {rel} ({len(edits)} edits)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
