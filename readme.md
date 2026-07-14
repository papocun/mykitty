# Desktop Cat

**Desktop Cat** is a small animated companion that lives directly on the desktop. It brings a little personality to an everyday workspace: the cat rests, notices you, follows your cursor, and can be picked up and placed wherever you like.

![Desktop Cat control panel](.github/images/control-panel.png)

## What it looks like

The cat is shown as pixel-art animation over a transparent background, so it feels like it is sitting naturally on top of the desktop rather than inside a normal application window. Its control panel uses a calm dark interface with purple accents, a cat preview, a live status badge, and a simple on/off switch.

![Desktop Cat on the desktop](.github/images/cat-on-desktop.png)

## What it does

- **Lives on the desktop** — frameless, always visible, and unobtrusive.
- **Moves with personality** — switches between sitting, sleeping, looking, walking, and running animations.
- **Follows the cursor** — turn on follow mode and the cat chases the pointer with smooth changes between walking and running.
- **Responds to interaction** — click, double-click, or drag the cat to wake it, change its behaviour, or move it around.
- **Stays out of the way** — clicks through its transparent background, so only the visible cat is interactive.
- **Works from the tray** — the system-tray menu makes it easy to show, hide, control, or quit the cat.

## Control panel

The control panel is the cat's small command centre. It keeps the important actions in one place without making the app feel complicated:

- A visual preview of the cat using its own sprite asset
- A live **Online / Resting** status
- A **Cat presence** toggle to show or hide the cat
- A button to start or stop cursor-following mode

## How it is made

Desktop Cat is built in Python. PySide6 handles the desktop window, control panel, animations, and tray menu. The cat's PNG sprite frames are organised by animation state and direction, which lets it move naturally between poses. Global mouse and keyboard activity give the cat awareness of what is happening on the desktop.

