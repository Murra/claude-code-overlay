# Media

These images are generated from the real widget, not mocked up, so they stay honest when the
design changes.

| File | Used for |
|---|---|
| `screenshot.png` | README header. The overlay composited into a taskbar strip. |
| `thresholds.png` | The four display states: green, amber, red, and no-CLI-data. |

## Regenerating them

Both are produced by rendering the actual `OverlayWindow` and calling `QWidget.grab()`, then
compositing the result. The surrounding taskbar in `screenshot.png` is drawn — abstract rounded
squares standing in for pinned icons, no logos and nothing imitating a real product — so that no
part of a real desktop ends up in the repository.

If you change the layout or palette, re-render rather than editing the PNGs by hand. The widget
renders at the display's device pixel ratio, so a 2x screen produces the 524x80 images checked in
here for a 262x40 window.
