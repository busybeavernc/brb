"""Earlier versions kept data in a "Doppleganger" folder and the camera in %ProgramData%\\Doppleganger."""
import os
from unittest import mock

from doppleganger import paths, vcam


def test_old_data_folder_is_renamed(tmp_path):
    old, new = tmp_path / "Doppleganger", tmp_path / "BRB"
    (old / "loops").mkdir(parents=True)
    (old / "loops" / "loop1.mp4").write_bytes(b"x")
    (old / "config.json").write_text("{}")
    (old / "doppleganger.log").write_text("old run")
    assert paths.move_old_folder(str(old), str(new)) == str(new)
    assert not old.exists()
    assert (new / "loops" / "loop1.mp4").read_bytes() == b"x" and (new / "config.json").exists()
    assert not (new / "doppleganger.log").exists()


def test_new_folder_wins_and_old_is_left_alone(tmp_path):
    old, new = tmp_path / "Doppleganger", tmp_path / "BRB"
    old.mkdir(); new.mkdir()
    assert paths.move_old_folder(str(old), str(new)) == str(new)
    assert old.exists()


def test_no_old_folder(tmp_path):
    assert paths.move_old_folder(str(tmp_path / "Doppleganger"), str(tmp_path / "BRB")) == str(tmp_path / "BRB")


def test_old_folder_used_if_it_cannot_be_moved(tmp_path):
    old, new = tmp_path / "Doppleganger", tmp_path / "BRB"
    old.mkdir()
    with mock.patch("os.rename", side_effect=PermissionError("in use")):
        assert paths.move_old_folder(str(old), str(new)) == str(old)


def test_camera_folders_and_names():
    with mock.patch.dict(os.environ, {"ProgramData": r"C:\ProgramData"}):
        assert vcam.install_dir().endswith("BRB") and vcam.old_install_dir().endswith("Doppleganger")
    assert vcam.DLL64 == "brb_cam64.dll" and vcam.DLL32 == "brb_cam32.dll"


def test_install_script_cleans_up_old_folder_without_unregistering():
    lines = vcam._remove_old_folder_lines()
    text = "\n".join(lines)
    assert "doppleganger_cam64.dll" in text and "rmdir" in text
    assert "regsvr32" not in text   # same CLSID: unregistering the old DLL would remove the new camera


def test_old_camera_folder_cleanup_runs_on_windows(tmp_path):
    """The real batch commands (no admin needed in a temporary folder): old DLLs go, the folder goes."""
    import subprocess
    import sys
    import pytest
    if sys.platform != "win32":
        pytest.skip("Windows only")
    old = tmp_path / "Doppleganger"
    old.mkdir()
    for n in (vcam.OLD_DLL64, vcam.OLD_DLL32, "version.txt", "SOFTCAM-LICENSE.txt"):
        (old / n).write_bytes(b"x")
    with mock.patch.dict(os.environ, {"ProgramData": str(tmp_path)}):
        lines = vcam._remove_old_folder_lines()
    bat = tmp_path / "t.bat"
    bat.write_text("@echo off\r\n" + "\r\n".join(lines) + "\r\nexit /b 0\r\n")
    assert subprocess.run(["cmd.exe", "/c", str(bat)]).returncode == 0
    assert not old.exists()
