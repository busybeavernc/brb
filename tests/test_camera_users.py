"""Which other programs have a camera (Windows' camera-use records), and what BRB tells you."""
from doppleganger import camera_users as cu

NOW = 133000000000000000


def test_programs_using_a_camera_now():
    entries = [
        ("C:#Program Files#Zoom#bin#Zoom.exe", NOW, 0),                         # using it now
        ("C:#Users#me#AppData#Local#Programs#BRB#BRB.exe", NOW, 0),              # that's us
        ("C:#Program Files#Google#Chrome#Application#chrome.exe", NOW, NOW + 5),  # used it earlier
        ("MSTeams_8wekyb3d8bbwe", NOW, 0),                                        # Store app, now
        ("C:#Tools#SomeRecorder.exe", NOW, 0),
        ("Microsoft.WindowsCamera_8wekyb3d8bbwe", None, None),
    ]
    assert cu.users(entries) == ["Zoom", "Microsoft Teams", "Somerecorder"]


def test_never_breaks_and_empty_off_windows():
    def boom():
        raise OSError("no registry")
    assert cu.current(boom) == []


def test_advice_names_the_app_and_the_camera_to_pick():
    msg = cu.advice("Logitech BRIO", ["Zoom"])
    assert "Zoom is using a camera" in msg and "Zoom's video settings" in msg
    assert "HappyHacks Virtual Camera" in msg and "Logitech BRIO" in msg


def test_advice_for_a_meeting_in_a_browser():
    assert "the meeting's video settings in Google Chrome" in cu.advice("Cam", ["Google Chrome"])


def test_advice_without_knowing_the_app_and_when_unplugged():
    assert "If a meeting app" in cu.advice("Cam", [])
    assert "OBS Virtual Camera" in cu.advice("Cam", [], virtual="OBS Virtual Camera")
    assert "disconnected" in cu.advice("Cam", ["Zoom"], unplugged=True)
