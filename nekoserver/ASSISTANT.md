# The AI assistant (optional)

A private chat assistant, like ChatGPT, that runs on your own server. You ask it questions in your browser at home, or on your phone through Tailscale. The model runs on the server itself: nothing you ask is sent to an outside service.

It is **optional**. Setup asks once (`=== Optional: AI assistant ===`), and the setup window has a switch for it before setup starts. The answer is **no** unless you say yes, and everything else in NekoServer works the same without it.

## What it installs

- **Ollama** (`ollama/ollama`), which runs the language model.
- **Open WebUI** (`ghcr.io/open-webui/open-webui`), the chat page.
- **The model**, chosen by your server's memory:

| Memory | Model | Download |
|---|---|---|
| 24 GB or more | `qwen3.5:9b` | about 6.6 GB |
| 12 to 24 GB | `qwen3.5:4b` | about 3.3 GB |
| under 12 GB | none: setup says the assistant is not available on this PC | |

The two programs are about 6 GB more. Everything goes on the app SSD, which needs room for it with 10 GB to spare; with less, setup says so and installs nothing.

Both programs are pinned to versions tested with this release (Ollama 0.40.16, Open WebUI v0.13.3). They are not part of the weekly app updates: a newer NekoServer release brings newer ones.

## It never gets in the way

- **On its own.** The assistant is a Docker project of its own (`neko-assistant`) on a network of its own. No other app uses it or waits for it.
- **Within limits.** The model may use at most half the processor cores and a memory limit setup works out (the model's own need, or 40 % of the memory, never more than 60 %). The chat page may use one core and 2 GB. Both have the lowest processor priority, so the other apps come first whenever the server is busy.
- **First to go.** If the server ever runs out of memory, Linux stops the assistant first, never Jellyfin, Immich or a backup. Docker starts it again by itself.
- **Memory back.** The model leaves memory five minutes after the last question.
- **Never fails setup.** If its download is cut off, or it does not start, setup says "AI assistant needs a look", with the reason, and every other step runs as usual. Run `sudo bash install.sh` again once that is fixed.
- **Never undoes an upgrade.** An upgrade of NekoServer never goes back to the previous release because of the assistant.
- **Not in the backups' way.** The nightly app snapshot, the monthly check and the weekly app updates leave it alone, so nothing it does can stop or slow a backup.
- **A quiet alert.** If it stops answering, the dashboard (and your phone alerts, when set up) says so as a warning, never as a critical alert.

## Using it

- **On the server:** <http://127.0.0.1:8090/> (the **AI assistant** tile on the dashboard and its desktop shortcut open it).
- **At home:** the home address setup printed for it (port 8449 on the app menu's address, unless another program had it).
- **On the phone:** the private Tailscale address on port 8449. The phone setup page lists it with a QR code. No app is needed: open it in Safari or Chrome and add it to the home screen.

The first answer after a pause takes a few seconds longer: the model is loaded into memory first. On a server without a graphics card the answers come at reading speed or a little slower.

Right after setup, the model may still be downloading in the background. The chat page works meanwhile and offers the model once the download is in. If the download was cut off, run setup again: it starts it again.

## Its login

Setup makes the assistant's administrator login and keeps it with the other app logins. To see it on the screen (with a QR code for your phone):

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.49.0/account-setup.py show assistant
```

Nobody else can sign up on the chat page. To let someone in your home use it, sign in as the administrator, open **Admin Panel > Users** and add them there.

To check that the saved login still works: `sudo python3 /srv/nekoserver/installer/releases/v0.49.0/account-setup.py signin assistant`.

If setup says the saved login does not work, the chat page has kept an administrator of its own from an earlier install (its data folder is kept when the assistant is switched off). Sign in with that account, or, to start the assistant afresh, move the folder `/srv/nekoserver/appdata/assistant/chat` away and run setup again.

## Changing your mind

- **Switch it off:** in `my-config.json` set `"wanted": false` under `"assistant"`, then run `sudo bash install.sh`. The assistant is stopped and its tile and addresses go; its chats and model stay on the SSD, so switching it on again starts it as it was.
- **Switch it on later:** set `"wanted": true` there and run setup again.
- **Another model:** set `"model"` under `"assistant"` to a model from <https://ollama.com/library>, for example `"qwen3.5:2b"` (smaller, for a server with less memory) or `"gemma4:12b"`, and run setup again. With `"auto"` setup chooses by memory as in the table above. A model you name is taken as named, without the memory check, so pick one that fits.
- **Remove it completely:** switch it off as above, then delete `/srv/nekoserver/appdata/assistant` and `/srv/nekoserver/compose/assistant`. `sudo bash uninstall.sh` removes it with the rest of NekoServer.

Here `/srv/nekoserver` is the app SSD's folder (`data_mount` in the settings).

## Known limits

- It runs on the processor only; a graphics card is not used yet.
- Its chats are not in the backups. They stay on the SSD, but a lost SSD loses them.
- It is updated by NekoServer releases, not by the weekly app updates.
- On a busy server (a monthly check running, a film being converted) answers are slower: the other apps come first by design.
