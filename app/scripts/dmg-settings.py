"""dmgbuild settings for the Library Cleanup installer DMG.

dmgbuild writes the Finder layout (.DS_Store) directly — no Finder automation,
no permission prompts. Invoked by build-signed-dmg.sh as:

    uvx dmgbuild -s scripts/dmg-settings.py \
        -D app="<path to .app>" -D bg="<path to background.png>" "<volume>" "<dmg>"

(dmgbuild execs this file without __file__, so paths come in via -D.)
"""
import os.path

app = defines.get("app", "build/photocleanup/macos/app/Library Cleanup.app")  # noqa: F821
appname = os.path.basename(app)

format = "UDBZ"                      # compressed, same as the previous hdiutil call
files = [app]
symlinks = {"Applications": "/Applications"}

background = defines.get("bg", "assets/dmg-background.png")  # noqa: F821
window_rect = ((200, 120), (640, 400))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False

icon_size = 100
text_size = 13
icon_locations = {
    appname: (160, 210),
    "Applications": (480, 210),
}
