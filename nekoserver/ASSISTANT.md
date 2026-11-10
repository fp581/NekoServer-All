# The AI assistant: Ask Neko (optional)

A private helper inside the dashboard. On its **Ask Neko** page you can ask what a reading means, why a backup failed or what to do about something under **Needs attention**. It explains in plain words, reads the logs of the problem (with passwords and keys taken out first), and can suggest a fix from a short list that runs only when you press **Approve**. The language model runs on the server itself (Ollama): nothing you ask leaves your home.

It is **optional and off unless you choose it**. Setup asks once, and the setup window has a switch for it before setup starts. Everything else in NekoServer works the same without it.

## Choosing it in setup

Setup first checks whether this PC can have it, before anything is downloaded:

- **The processor:** 64-bit Intel or AMD with SSE4.2 (every one made since about 2010), or 64-bit ARM.
- **The memory that is free with your apps running.** Setup keeps a reserve for your other apps to grow into (at least 2 GB, or 20 % of the memory when that is more); only the rest is offered to the assistant.
- **The app SSD:** room for Ollama (about 5 GB) and the model, with 10 GB to spare.
- **Port 11434:** if a program of your own already uses it (an Ollama you installed yourself), NekoServer installs nothing beside it and says so.

These checks read the kernel's own files (`/proc/meminfo`, `/proc/cpuinfo`) and Python's own library, so no extra program is needed to make them.

When the PC can have it, setup shows a choice:

```
=== Optional: AI assistant ("Ask Neko" in the dashboard) ===
This PC has 16 GB of memory. With your apps running and room kept for them to grow, 6.3 GB are free for the assistant.
  0) No assistant (press Enter)
  1) qwen3.5:2b-q4_K_M: short, simple answers, the quickest, but the weakest.
     Downloads 1.9 GB. While it answers it uses about 3 GB of your 16 GB
     (its limit), and up to 4 of your 8 processor cores, so a film Jellyfin converts or
     new photos in Immich go slower meanwhile. 5 minutes after the last question its memory is free again.
  2) qwen3.5:4b (recommended): good everyday answers.
     ...
  Not possible here: qwen3.5:9b needs about 9 GB free; this PC has 6.3 GB free with your apps running.
Which one? (0-2, Enter for no assistant):
```

| Model | Download | Memory while it answers (its limit) | Answers |
|---|---|---|---|
| `qwen3.5:2b-q4_K_M` | 1.9 GB | 3 GB | short and simple, the quickest, but the weakest |
| `qwen3.5:4b` | 3.3 GB | 5 GB | good everyday answers |
| `qwen3.5:9b` | 6.6 GB | 9 GB | the best answers here, the slowest |

The memory is what each model itself used while answering (2.2, 3.7 and 6.7 GB, measured for 0.49.0 with an 8192-token context), with 20 % to spare; it is also that model's hard memory limit. The small one is the 4-bit build of Qwen3.5 2B: the plain `qwen3.5:2b` is an 8-bit build that needs almost as much memory as the 4b.

Answer speed depends on the processor, so these are rough: on the test machine's 4 cores the 4b wrote about 7 words a second; on a 16-core PC the 2b wrote about 30, the 4b 16 and the 9b 9. The first words of the first question take a few seconds more while the model is loaded.

The biggest model that fits is marked recommended. **Enter is "no assistant"**; with the setup window's switch turned on, Enter takes the recommended one. **Ctrl+D** leaves the question for the next setup run. Nobody at the terminal (an upgrade, say): nothing is asked and nothing is installed.

Your choice is saved as `"wanted"` and `"model"` under `"assistant"` in `my-config.json`. Setup run again keeps it, and checks again that it still fits: if your other apps have grown and it does not fit any more, the assistant is stopped and setup says why (its model stays on the SSD).

The graphics card is not used: the model runs on the processor only.

## The model is downloaded, never part of NekoServer

The model is downloaded from the Ollama library (<https://ollama.com/library>) while setup runs, after you chose it, so setup needs the internet then. The download is the size in the table above. It is kept on the app SSD in `/srv/nekoserver/appdata/assistant/models`, and setup notes which build it downloaded (its ID) in `/srv/nekoserver/reports/assistant.json`.

NekoServer itself never contains a model: its download from GitHub holds the source only, and the copy of NekoServer setup keeps on the SSD, the nightly app backup and the monthly snapshot never take the models folder. A test fails if a model file ever gets into NekoServer's repository.

If the download is cut off (no network, a restart), Ask Neko says the model is not downloaded yet; run `sudo bash install.sh` again and it starts the download again. While a download runs, it says so instead.

## It never gets in the way

- **Its own project, on this PC only.** Ollama runs as a Docker project of its own (`neko-assistant`), reachable on `127.0.0.1:11434` only: nothing on your home network or Tailscale can reach it. Only the dashboard's own server asks it.
- **Hard limits.** The model's memory limit is the figure in the table, with no swap: a model that needs more is stopped instead of the server running short. It may use at most half the processor cores, at the lowest processor priority, and it is the first thing Linux stops if memory ever runs out. One question and one model at a time; the model leaves memory five minutes after the last question.
- **Never fails setup.** If its download is cut off or it does not start, setup says "AI assistant needs a look", with the reason, and every other step runs as usual. A PC that cannot have it is listed under "Not available on this system".
- **Never part of anything else.** No other setup step, service or timer needs it. The backups, the monthly check, the weekly app updates and the recovery menu never call it. An upgrade of NekoServer never goes back to the previous release because of it.
- **When it is broken.** Stopped, its model missing, out of memory, too slow or answering nonsense: Ask Neko says "Neko is not available right now" with the reason, and the rest of the dashboard works as always.

## Ask Neko

The **Ask Neko** page is right under **Home** in the menu, and only on a server with the assistant. Each item under **Needs attention** has an **Ask Neko about this** button, which opens the page with the question filled in.

- **The PIN.** Asking uses the server's processor, so it needs the dashboard PIN on a phone or another computer, like every other change; on the server itself it does not.
- **What Neko reads.** Your question, a short summary of the dashboard's readings (what needs attention, the backups, the drives, the apps), and for an attention item its own text and the last 40 lines of its service's log. Before the model sees any of it, the server takes out passwords, keys, tokens, cookies, Tailscale keys, e-mail addresses, and every app password and secret NekoServer keeps.
- **Nothing is kept.** The chat lives in the open page only: reloading or closing it clears it. Nothing is stored on the server.
- **On the phone** it works through the dashboard's own Tailscale address.

### Fixes Neko may suggest

Neko can name one fix from this list, and nothing else:

| Fix | What it does |
|---|---|
| Restart an app | Its containers stop and start again, as **Restart app** on Your apps does |
| Look for updates | The daily check for new app versions, now; it changes nothing |
| Update an app now | A version that is waiting: a snapshot first, the update, a check, undone if it fails |
| Run a backup now | The media apps' snapshot, this PC's settings, the Immich database copy or the USB copy, as its timer would |

The server checks every suggestion against this list and against what is installed here. The page then shows exactly what will happen, with a warning where there is one (the media apps' snapshot pauses the apps for a few minutes), and it runs only when you press **Approve**. A backup is refused while setup, an upgrade, another backup or an unfinished job is running. Anything outside the list is refused and noted in the control service's log (`journalctl -u nekoserver-control`). Neko never runs a command of its own.

### When it looks like a bug in NekoServer

Neko then writes a **bug report** for you: the NekoServer version, your question, what the server showed (with the secrets taken out), and what it thinks went wrong, with a fix in words. Read it, then copy it to report the problem. Nothing is sent anywhere, and nothing on the server is changed.

## Changing your mind

- **Switch it off:** set `"wanted": false` under `"assistant"` in `my-config.json` and run `sudo bash install.sh`. The assistant is stopped and Ask Neko leaves the menu; its model stays on the SSD.
- **Another model:** set `"model"` under `"assistant"` to one from the table, or to another from <https://ollama.com/library> (for example `"gemma4:12b"`), and run setup again. A model from the table is checked against its memory need; another one gets all the memory left for a model (at least 1 GB), so pick one that fits.
- **Remove it completely:** switch it off as above, then delete `/srv/nekoserver/appdata/assistant` and `/srv/nekoserver/compose/assistant`. `sudo bash uninstall.sh` removes it with the rest of NekoServer.

Here `/srv/nekoserver` is the app SSD's folder (`data_mount` in the settings).

## Known limits

- The model runs on the processor only; a graphics card is not used.
- On a busy server (a monthly check, a film being converted) answers are slower: the other apps come first by design.
- Ollama is updated by NekoServer releases, not by the weekly app updates.
