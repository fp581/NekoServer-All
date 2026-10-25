# Hardware video (Jellyfin and Immich)

Converting video ("transcoding") is what lets a phone on mobile data play a big movie, or a browser play a format it does not understand. Done by the processor it is slow and makes the PC hot and loud. Most Intel and AMD processors have a video engine built into their graphics that does it much faster.

## What the installer does

The **Video using the graphics chip** step:

1. Finds your graphics chip and installs its video drivers (Intel: `intel-media-driver`; AMD: `libva-mesa-driver`; both: `libva-utils` for checking).
2. Checks, from inside each app's own container, which video formats the chip decodes there and whether it can encode: `vainfo` lists both, and a one-second test encode on the chip must succeed before encoding counts. Some chips decode but have no encoder (AMD's Radeon RX 6400 and 6500, for example). Only the apps this server runs are checked: Jellyfin if you chose it, Immich if photos are on. With several graphics chips, Intel is tried first, then AMD, and each one of a kind in turn (two Intel chips: the second is tried if the first fails the check). An app where every chip fails the check keeps working in software; that is a result, not a step left to do.
3. Turns on hardware transcoding:
   - **Jellyfin:** Intel uses Quick Sync (QSV), AMD uses VA-API (so do Intel chips older than about 2014, which can't do Quick Sync on Linux: a one-second Quick Sync test encode decides), each set to the chip that passed the check (Jellyfin's *QSV Device* or *VA-API Device*). Hardware decoding for the formats the chip decodes (of H.264, HEVC, MPEG-2, VC-1, VP8, VP9 and AV1); other formats still work in software. Hardware encoding is switched on only when the chip passed the test encode; otherwise Jellyfin decodes on the chip and encodes in software ("decoding only" in the setup log).
   - **Immich:** the same method for converting your videos, set to the chip that passed the check (Immich's setting needs an encoder, so a chip that only decodes leaves Immich converting in software), and the graphics chip is added to Immich's server (kept across monthly updates).

**Intel chips that encode only in low-power mode** (Jasper Lake and Elkhart Lake, for example: many small home servers): when the normal test encode fails and the chip lists only its low-power H.264 encoder, the test is tried once more in low-power mode. If that works, Jellyfin gets hardware encoding with its *Enable Intel Low-Power H.264 hardware encoder* switched on. Immich has no low-power switch for VA-API, so on such a chip it uses Quick Sync if that test passed, and otherwise keeps converting in software.

**If the graphics chip goes away** (taken out, switched off in the BIOS, its driver not loading, or the drives moved to another PC), Immich is started without it and converts video in software, instead of not starting at all: Docker can't start an app whose device is missing. The next setup run with the chip back adds it again.

If you already picked a setting in either app, it is left as it is, with one exception: a setting an older NekoServer made without choosing the device gets the device filled in. If a setting someone else made leaves the app to pick a device by itself and this server has more than one, the step says which device to choose and waits for you (it isn't counted as done). The result is in `reports/hardware-video.json` on the data SSD.

**Not automatic:**
- **NVIDIA graphics** need NVIDIA's own driver and container toolkit; both apps use software until you set that up yourself (Arch wiki: "NVIDIA" and "Docker").
- **Immich's face and object recognition** stays on the processor; it is a one-time job per photo and does not need the graphics chip.

## Check it is working

Play a movie in Jellyfin from your phone on mobile data and lower the quality in the player settings. On the Jellyfin dashboard (Dashboard, then Activity), the stream shows **Transcoding** with the method (for example QSV). Your CPU should stay mostly idle.

## Turn it on or off yourself

- **Jellyfin:** Dashboard, Playback, Transcoding: Hardware acceleration.
- **Immich:** Administration, Settings, Video Transcoding Settings, Hardware Acceleration.

Run `sudo bash install.sh` again after changing the hardware; a setting you chose is kept. That includes switching hardware video **off**: once setup has turned it on, an app you switch off stays off (the setup log says so), and setup doesn't wait for it. This holds even when a run in between couldn't check the app (it wasn't running, or didn't answer in time): setup remembers the last thing it knew for sure.
