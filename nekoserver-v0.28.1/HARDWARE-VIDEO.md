# Hardware video (Jellyfin and Immich)

Converting video ("transcoding") is what lets a phone on mobile data play a big movie, or a browser play a format it does not understand. Done by the processor it is slow and makes the PC hot and loud. Most Intel and AMD processors have a video engine built into their graphics that does it much faster.

## What the installer does

The **Video using the graphics chip** step:

1. Finds your graphics chip and installs its video drivers (Intel: `intel-media-driver`; AMD: `libva-mesa-driver`; both: `libva-utils` for checking).
2. Checks, from inside Jellyfin, that the chip really decodes video. If it does not, nothing is changed and both apps keep working in software.
3. Turns on hardware transcoding:
   - **Jellyfin:** Intel uses Quick Sync (QSV), AMD uses VA-API. Hardware decoding for H.264, HEVC, MPEG-2 and VC-1; other formats still work in software.
   - **Immich:** the same method for converting your videos, and the graphics chip is added to Immich's server (kept across monthly updates).

If you already picked a setting in either app, it is left as it is. The result is in `reports/hardware-video.json` on the data SSD.

**Not automatic:**
- **NVIDIA graphics** need NVIDIA's own driver and container toolkit; both apps use software until you set that up yourself (Arch wiki: "NVIDIA" and "Docker").
- **Immich's face and object recognition** stays on the processor; it is a one-time job per photo and does not need the graphics chip.

## Check it is working

Play a movie in Jellyfin from your phone on mobile data and lower the quality in the player settings. On the Jellyfin dashboard (Dashboard, then Activity), the stream shows **Transcoding** with the method (for example QSV). Your CPU should stay mostly idle.

## Turn it on or off yourself

- **Jellyfin:** Dashboard, Playback, Transcoding: Hardware acceleration.
- **Immich:** Administration, Settings, Video Transcoding Settings, Hardware Acceleration.

Run `sudo bash install.sh` again after changing the hardware; a setting you chose is kept.
