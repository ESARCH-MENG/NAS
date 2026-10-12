# NAS Tool

## Run

Requires Python 3.10+ with Tkinter. On macOS, use a Python installation with
Tkinter support (for example, the python.org installer).

```sh
python3 -m pip install -r requirements.txt
python3 nas_tool.py
```

Open **Help > User Guide** or press **F1** for an English summary of all
controls, workflows, and interaction rules.

Compare lists files found only in Bank-A or only in Bank-B. Selecting a result
previews that file. Sync copies new or changed files from A to B; it does not
delete extra files in B. Delete Selected permanently deletes selected results
after confirmation.
Each Bank panel keeps its equal-sized **Select Folder** and **Open Folder**
buttons in one row at the bottom-right, with Select on the left and Open on the
right, so their labels remain fully visible.
The bottom controls align with the three UI columns: **Sync** is centered on
the left, **Compare** in the middle, and **Sync Selected** / **Delete Selected**
are centered together on the right, in that order.
The bottom-right **Stop** control matches Storm's 44 px circular emergency
button and triggers on mouse-down. It first signals any active **Sync**,
**Sync Selected**, or folder **Download** to stop. Partial new files are removed,
and an existing destination is replaced only after a complete copy, so stopping
does not leave a truncated replacement. During a macOS Image Capture import,
**Stop** quits Image Capture. Clicking **Stop** while idle does nothing and does
not close NAS Tool.
Both selected-file buttons keep their normal colors at all times. Clicking
either without a selection shows a dialog asking you to select Compare results;
no files are copied or deleted.
**Sync Selected**, beside **Delete Selected**, copies only the selected Compare
results to the opposite bank: A selections go to B, and B selections go to A.
Selections in both banks can be synced together. Relative subfolder paths are
preserved, source files are kept, and the Compare results refresh afterwards.
Unselected files are not synced. As with Sync, a changed same-path destination
file is overwritten if it appeared after Compare.

## Download

The top-left **Select Folder** chooses an upload source for the current session
only; this folder is not persisted. **Download** copies its files, including
subfolders, into Bank-A using their relative paths. Existing same-path files
are skipped, never overwritten. No files are uploaded to Bank-B, and no files
are deleted. The selected source path is displayed beneath the upload controls.
There is no bottom status bar; errors and warnings are shown in dialogs.

If no upload folder is selected, **Download** on macOS opens the system
**Image Capture** app and sets **Import To > Other...** to Bank-A using macOS
UI automation. Connect and select your device first. Grant **Accessibility**
access to NAS Tool (or its Python/terminal host) in **System Settings > Privacy &
Security**, and allow **Automation** access to System Events when prompted.
Use **Privacy & Security > Accessibility** (Chinese: **隐私与安全性 > 辅助功能**),
not the separate top-level Accessibility settings page. NAS Tool opens the
privacy permission list when this permission is missing. Enable the launching
application: **NAS Tool** for an app bundle, **Terminal/iTerm** for a terminal
launch, or **Visual Studio Code** for a VS Code launch; follow the host name in
the macOS permission prompt if it names a different application. If it is not
listed, use **+** to add that application, unlock settings if necessary, and
enable its switch. Quit and reopen the launching application and NAS Tool
before retrying. Automation prompts may name both System Events and Image Capture.
If macOS reports **Not authorized to send Apple events to System Events (-1743)**,
NAS Tool opens **System Settings > Privacy & Security > Automation**; enable
NAS Tool for **System Events**. macOS has no Add button on this page; apps
appear after they request access. NAS Tool requests access when you click
**Download**; launch the newly built app and allow the macOS prompt. This
permission is separate from Accessibility.
Missing permissions, an unselected device, or an unconfirmed destination are
reported as errors, not as a successful setup. Select your device and click
**Download** in NAS Tool again after resolving the error. The destination is
device-specific; repeat this step after switching devices.
Choose the photos and start importing in Image Capture yourself; NAS Tool does
not start an import automatically. Image Capture handles duplicates independently
of the folder-upload skip policy. Click **Compare** after the import completes.
Bank-A must be selected and accessible. This fallback is unavailable on Windows.

## Saved folders

Folder selections are saved immediately and restored at startup. On macOS,
settings are stored in `~/Library/Application Support/NAS Tool/settings.json`.
Unavailable saved folders remain selected; mount the storage before syncing.

## Scheduled sync on macOS

In the compact top-right controls, set **Time** using the HH / MM fields (24-hour
local time), select **Cycle**, and click
the **Scheduled Sync** switch to apply and enable it. Uncheck the switch to
stop it. The slider is green when on and gray when off.
To change an enabled schedule, switch it off, edit the time/cycle,
then switch it on again. Both bank folders
must be selected before enabling the schedule. All settings are persisted.

- `daily`: every day at the selected time.
- `weekly`: every Monday at the selected time.
- `monthly`: on the first day of each month at the selected time.

Enabling the switch does not sync immediately; use **Sync** for an immediate run.
Older minute-interval settings preserve the saved folders but disable the
old schedule. Review the new time/cycle and enable the switch.

The user-level launchd agent is installed at
`~/Library/LaunchAgents/com.marvel.nas-tool.sync.plist`. It runs while you are
logged in, even after the GUI closes, and is loaded again at the next login.
It does not run while logged out or wake a sleeping Mac. Calendar events missed
while asleep may run once on wake; exact wall-clock execution is not guaranteed.

Keep the project and its Python environment at their installed locations.
If either moves, launch the tool from its new location and toggle the schedule
off and on again.
NAS volumes must already be mounted and accessible to your user; the tool
does not mount shares or store credentials. macOS privacy permissions may
need to be granted to the Python executable accessing protected folders.

Background results/errors are recorded in `sync.log` beside the settings.
Launchd output is recorded in `launchd.log` and `launchd-error.log` there.
Only one manual/background sync runs at a time on macOS. Background errors
are logged rather than shown as GUI dialogs.

Windows supports saved folder selections and manual sync, but not this macOS
background scheduler.

## macOS icon

The icon source is `nas icon.png`. Generate `packaging/assets/app_icon.icns`:

```sh
python3 packaging/make_icon.py
```

The generator follows Storm's 1024px canvas, 824px rounded body and artwork
fitting approach. It uses macOS `iconutil` when available, otherwise Pillow's
ICNS encoder. Use this ICNS as the icon when building the macOS app bundle.
The current source is low-resolution; a 1024px original will produce sharper
large icons.

## Tests

```sh
python3 -m unittest -v test_nas_tool
```

The GUI tests require a desktop session. Launchd registration tests use mocks;
actual launchd registration and closed-window execution need macOS validation.
Image Capture destination tests mock macOS commands. Actual destination setup
also needs validation on macOS with a connected device and Accessibility access.
