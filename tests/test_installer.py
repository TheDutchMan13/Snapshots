"""The installer script (built on Windows by the workflow) agrees with the rest of the project."""

import re
from pathlib import Path

from snapshots import startup

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "installer" / "snapshots.iss").read_text(encoding="utf-8")


def test_the_files_the_installer_names_exist_or_are_built():
    assert (ROOT / "assets" / "icon.ico").is_file()
    assert (ROOT / "LICENSE").is_file()
    sources = re.findall(r'^Source: "\.\.\\([^"]+)"', ISS, re.M)
    assert set(sources) == {"dist\\Snapshots.exe", "LICENSE"}       # the exe is built just before the installer
    assert re.search(r"^SetupIconFile=\.\.\\assets\\icon\.ico$", ISS, re.M)
    assert re.search(r"^OutputBaseFilename=Snapshots-Setup$", ISS, re.M)


def test_the_installer_makes_the_icon_the_app_expects():
    # Settings > Start menu and desktop looks for "Snapshots.lnk" directly in the Start menu's Programs folder.
    assert startup.LINK_NAME == "Snapshots.lnk"
    assert re.search(r'^Name: "\{autoprograms\}\\Snapshots"', ISS, re.M)
    # And the uninstaller clears the "Start with Windows" entry under the name the app writes.
    assert f"'{startup.VALUE_NAME}'" in ISS and startup.RUN_KEY.replace("\\", "\\") in ISS


def test_the_installer_never_needs_administrator_rights_by_default():
    assert re.search(r"^PrivilegesRequired=lowest$", ISS, re.M)
    assert "{commonappdata}" not in ISS and "HKLM" not in ISS


def test_the_workflow_builds_and_publishes_the_installer():
    flow = (ROOT / ".github" / "workflows" / "build-exe.yml").read_text(encoding="utf-8")
    assert "installer\\snapshots.iss" in flow
    assert "dist/Snapshots-Setup.exe" in flow
    # The installer is built after the exe's own tests and before it is attached to a release.
    assert flow.index("Build exe") < flow.index("Build the installer") < flow.index("Attach to the release")
