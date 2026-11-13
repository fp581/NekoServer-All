# Changelog

## v0.51.2 — (in progress)

Fixes only, from a wider review of 0.51.0's card engine, setup and dashboard. Nothing changes in how NekoServer is used. Each fix is listed here as it lands.

### Fixed: SD-card backup sets (the engine, not switched on yet)
- **The catalogue's copy for the host backup is private.** The catalogue (which files are on which card: the paths of your files) is the server's own, readable only by root, but its copy for the host backup was left readable by every user of the server, and a new catalogue was readable by others for a moment before it was narrowed. Both files are now made private before anything is written into them.

## v0.51.1 — 2026-11-13

Fixes only, found by a code review of 0.51.0 and by trying the restore tool on real card images: mostly in the tool that gets files back from a backup card. Nothing changes in how NekoServer is used.

### Fixed: getting files back from a backup card
- **The newest version of a file is the one restored.** A file that changed between two backups has a copy of each version, on one card or on several. The restore tool restored the oldest under the file's name and skipped the newer one as "a file is there already"; restoring several cards with one progress file did the same whichever card came first. The newest version now gets the file's name and each older one is listed as `older` (with `--rename` it is restored under `_renamed/` too). With a progress file, a newer version found on a later card replaces the older one the tool put there itself, unless that file was changed since. A second copy of the same version (made because the first was damaged) is tried when the first fails, and is not counted as a failure. Tested on cards the backup engine wrote itself (a file backed up, edited, backed up again), not yet on a real SD card.
- **A big folder restores in minutes, not hours.** For every file it restored, the tool listed the whole destination folder again, to find a name that differs only in upper and lower case. A folder of 8,000 photos took 23 seconds and one of 100,000 about an hour; 32,000 now take 24 seconds (measured on test cards). Each folder is listed once per restore, and names that differ only in case are still refused, also between two files of the same restore.
- **A restore with a progress file is as quick as one without.** The progress file (`--progress`, for several cards or after a stop) was written again in full after every file, so 8,000 files took seven minutes; they now take 10 seconds. Each restored file adds one line to it, flushed to the disk at once; a line cut off by a stop or a power cut is not counted. A progress file of the earlier tool is read and carried on.
- **A card mounted on `/mnt` itself is read.** `sudo mount /dev/sdb1 /mnt` is a usual way to mount a card, but the tool took only a card below `/mnt/`, `/media/` or `/run/media/`, and refused this one with "Mount the card under /media or /mnt". Those folders themselves count now; a folder elsewhere is still refused. Tried on an ext4 card image mounted read-only at `/mnt`: info, verify, and a restore with a progress file.
- **Restoring onto a USB stick formatted exFAT works as the guide says.** A name such a stick cannot hold (`meeting 10:30.jpg`, `what?.png`) ended as "failed: No such file or directory", with `--rename` too; it is now skipped, saying the stick cannot hold the name, or with `--rename` put under `_renamed/`. Running the same restore again without a progress file skipped every file it had restored before (or with `--rename` made a second copy of each); a file that is there with exactly the same bytes now counts as `same file`, under `_renamed/` too. Found by restoring a card onto an exFAT image (exFAT through FUSE: Linux's own exFAT driver was not there to try).

### Fixed: Ask Neko
- **A password that holds another is kept out of the question whole.** Before a question goes to the model, NekoServer takes out the apps' passwords and logins. When one of them held another (an app's login inside another app's password), the shorter was taken out first and the rest of the longer one was left in: `<removed>-Fern-Oak`. The longest are now taken out first.

### Tests
- **A setup test no longer waits for the keyboard.** The test of "none for now" at the offline-backup question meant to answer it itself, but the question had taken `input` when its file was loaded, before the test put its answer in place: the test read the real terminal, hung when it was run from one, and its check that the question is not asked a second time could never fail. The three drive questions that took `input` that way look it up when they ask (setup's own, which reaches the window, as before).
- **No test reads the terminal.** The whole suite now passes with the terminal made unreadable (every read fails, naming the test). One more test needed it: the drive size check ran into the media-apps question that 0.50.0 put before the drives, and answered it with whatever the terminal gave.
- **The lock an upgrade or a drive move holds is named once.** Ask Neko's "run this backup now" checked `/run/nekoserver-operation.lock`, written out in the runtime as well as in setup; it is in `shared.json` now, with setup's lock, and the test that keeps journals and locks from being written out twice covers it.

## v0.51.0 — 2026-11-13

Two new parts and the first fixes from the 0.50.0 sweep. One of them is not switched on yet: read "SD-card backup sets" before you count on it.

### New: SD-card backup sets (the engine and the restore tool; not switched on yet)
- **The copy engine** that puts photos and personal videos from Immich onto a numbered set of SD cards, USB sticks or external drives, and asks for the next card when one is full. Every file is checked against its checksum before it counts as backed up; a card pulled out, a power cut or a crash at any step leaves nothing counted that is not on the card, and the next run finishes or undoes what was half done. The catalogue of what is on which card is kept in SQLite on the server, and each card carries an inventory of the whole set, so the catalogue can be rebuilt from the cards.
- **Not switched on yet:** no service, timer or dashboard button runs the engine in 0.51.0; setting it up comes in a later release. Nothing is copied to a card yet.
- **The restore tool goes onto every card** (`neko_restore.py`, with its guide `CARD-RECOVERY.md`): on any Linux computer, with Python 3 only, it lists, checks and restores the files on one card, and says which card of the set holds a file it does not have. It never overwrites a file of yours.
- **The runtime is built statically** (musl, SQLite compiled in), so the same binary runs on every supported system.
- **How it was tested:** unit and crash tests, and the 13 loop-device cases on real ext4 cards (`tests/cardset-loop-tests.sh`: killed at each of the 9 points between the commit steps, the card pulled, the card back). Not yet on a physical SD card.

### New: Torrentio as a download source
- **Torrentio** (a Stremio add-on, not in Prowlarr's catalogue) is a download source NekoServer adds to Prowlarr itself, as a custom definition. Setup's source picker offers it after the install and never ticks it for you. It searches the way Prowlarr 2.7.0 runs it: one path per search, movies or series chosen by category.

### Changed: Ask Neko
- **The model's download and loading get the lowest disk priority**, so pulling a model does not slow down the apps or the backups.

## v0.50.0 — 2026-11-12

Setup was rebuilt around one idea: you choose once, approve once, and setup does the rest. It no longer needs three drives (one is enough), asks far fewer questions, asks nothing after the approval but what needs the running server, and never asks you to vouch for something it can check itself. A server that is already set up keeps exactly the apps, drives, logins and backups it has.

### New

**Setup's pages**
- **One first page, "What would you like this server to do?"** Every part is a switch, chosen once: Photos and phone backup (Immich), Movies and shows (Jellyfin), Music (Navidrome), Personal files (Nextcloud); then a TV connected to this PC, alerts on your phone, a warning when the server stops checking in, a local AI assistant; and whether your drives are in a network storage box. Any combination works, none of the apps included.
- **"Also find and download new ones"** is a small switch inside the Movies and shows card and the Music card, on unless you switch it off. On adds Seerr, Sonarr, Radarr; Lidarr; Prowlarr and qBittorrent. Off leaves Jellyfin, or Navidrome, alone.
- **The storage page, "Where should NekoServer store your apps and files?"**: a line for the apps, a line for your media and the backups, an optional offline backup, and a line for where Immich's photos, the films and shows, the music and Nextcloud's files are kept. Each line is a list of drives; a drive that cannot be used for a line says why (too small, already chosen, the disk Linux runs from for the offline copy, a network share for the apps).
- **"Media you already have"**: films, shows or music on another drive or a NAS are used where they are (nothing copied), with new ones saved there too if you like, or copied into the library. A music folder on a network share is asked about once, "Back this up too?" (yes unless you say no), and the nightly music copy then takes it.
- **The accounts page**: strong passwords made for you (Enter takes this), or your own logins, one row per app, with the dashboard PIN and the shared folders' password made for you or typed. **Show login** shows any of them.
- **The backups page**: when the backups run and how many nightly backups are kept, and a table of what is backed up, where, when and how far back, which also lists what is not backed up (films and shows, music while its copy is off, your own folders).
- **One approval.** "Ready to install NekoServer" lists what will be installed, what is left out, what is always set up and what setup will still stop to ask. One button, **Install selected apps**, starts it; in the terminal `yes`. On a server that exists the page is "Review your changes", the welcome says nothing changes until you approve, and the review says what is kept.
- **After the approval** only the Tailscale sign-in, a drive short of room, the download sources and the phone page stop setup. Lidarr's and Seerr's profiles are picked without asking.
- **Download sources from a list you can search**, once Prowlarr runs: Prowlarr's whole catalogue with a search box and tick boxes, the recommended ones (The Pirate Bay, YTS, EZTV, those the catalogue has) ticked, so Continue (or Enter alone) takes them. One Prowlarr refuses gets a Retry page; the rest still go in. A server whose Prowlarr has sources already keeps them and is not asked: an update or a rerun stops at nothing after the approval.
- **The phone page, "Use your apps away from home"**: status rows for what setup checked itself, each passed, not yet or not known, with the reason and the next step.
- **The finishing page**: what is ready, what a program checked, what you tried yourself and what is still waiting, each with its own Retry, and the dashboard's address and PIN in words.

**Drives**
- **One drive is enough.** A drive of its own, or the disk Linux runs from, can hold the apps, the media and the backups; any number of drives works, in either order. The same-drive warning is asked once.
- **An app's files can be kept on a drive of their own**: Immich's photos, the films and shows, the music, Nextcloud's files. Such a drive is checked, mounted and guarded like the others; while it is away its apps stay stopped and nothing is made in its place, and music on it is in the nightly music copy.
- **Everything connected is shown** before you choose: each drive's size, kind, model, label and how full it is, network shares mounted on this PC too, and for one that cannot be used, the reason.
- **The offline backup is optional.** "None for now" is saved and the weekly copy left out; the next setup run offers it again. A drive under 16 GB says it is too small.
- **A NAS can share folders over NFS** (experimental), beside iSCSI disks. Each folder is tried out before it is used. A NAS that is off never keeps the PC from starting; the apps that use one of its folders stay stopped until it answers, then its folder is mounted again and they start.

**While setup runs**
- **"Retry this step".** A step that does not finish says why and what usually fixes it (no internet, another program installing packages, Docker not running, a full drive, a drive not connected, a download that timed out, Tailscale still getting its certificate), and can be tried again without starting setup over.
- **A test alert and a test check-in**, each answer of the server said in its own words (a wrong address, busy, a server error), and a retry passes only on a good answer.

**The setup window**
- **Night and Lamplight** looks, with a button to switch.
- **Every question has its own words**: a short title, one plain line, and buttons that say what they do.
- **The recovery menu and the uninstaller** open the window too. The terminal alone still does everything.

**Recovery**
- **The recovery menu has three sections**: restore something from a backup; check or repair a drive; fix damaged backup copies. A restore is chosen first, then shown whole and approved once, and always goes into a new folder. A replaced drive is noticed and asked for; Enter takes the recommended answer.

### Changed

- **Backups kept: 3, from 1 to 7.** A new server keeps the newest 3 nightly app backups and 3 nightly settings backups (it was 7). A server that keeps a count from 1 to 7 keeps it; one outside is reviewed and nothing is removed until you choose.
- **The extras are off on a new server** unless you switch them on. A server set up before keeps what it has.
- **Music no longer needs Movies and shows**: Lidarr comes with Music's own download switch.
- **The home network is not asked about.** With several connections setup takes the cable before Wi-Fi and says which.
- **Addresses stay off until the logins are verified.**
- **qBittorrent's login is made and used by setup.**
- **A drive is named the same way everywhere**, by its number and description in the list, and by what this server keeps on it.
- **A group switched off on a server that has it** is stopped and keeps everything.
- **An update keeps the logins** of the apps whose settings were kept, and NekoServer's own Tailscale routes.

### Fixed

From the 0.50.0 gate on virtual machines (a fresh install in the window, one drive in the terminal, two drives with a network share and films already there, an update of a 0.48.2 server):
- The one-drive layout could not be chosen, and the runtime refused two roles on one drive.
- One drive was offered twice in the photos question ("the media drive" and "the SSD"), and the photos line spoke of a USB copy that was not chosen.
- Questions were asked after the approval: the accounts page, the backup times, the PIN, the shared folders' password, "Add a media source?", Lidarr's and Seerr's profiles, "Did you finish what was left in the apps?"; on an update, the films question and "Change any of these answers?" a second time.
- An update was told as a first install ("Nothing is installed yet", "Apps to install", "about an hour").
- The logins question refused Enter alone, though its first answer is the recommended one; so did the recovery menu's.
- The welcome asked for three data drives.
- The backup-times page named the music copy with Music off, and asked the weekly USB copy's day and time with no copy to make.
- The offline drive: an 8 GB drive was offered with nothing said; a drive just chosen for music was offered too; a drive was asked for with nothing to copy; "none" said to edit my-config.json.
- Wording named apps the server does not have ("your apps, photos and music" with Photos off).
- The films, shows and music were in no backup and in no list; music on a drive of its own was in no backup.
- Docker stopped during the connecting step was told as each app failing, not as Docker down.
- The monthly update and app updates refused every server with the new existing-media folders ("Uncovered container bind mount").
- With a group's drive away at the start, Docker made its folders on the system disk and Lidarr started there; the dashboard's stopped list was incomplete.
- A share away at the start: its apps started on the empty folder and the dashboard said nothing.
- The alerts test said a busy server (429) was a wrong topic, a server error (500) a wrong address, and a retry passed with no request sent.
- After a restore, an app whose settings were kept was given new logins; NekoServer's Tailscale routes were lost.
- A replaced drive was not noticed by setup's first page, and a retry forgot the fix.
- A group's drive replaced needed an edit of the settings.
- The uninstaller did not list a group's own drive or the person's folders and shares.
- A network share mounted on the PC was not in the drive list.
- The fresh install stopped at the welcome on a PC with no settings yet (a change made during the gate).
- With every app off, the home-addresses step failed and setup ended "almost ready".
- "Continue unfinished setup" showed the first page, the offline drive question and "Change any of these answers?" again before going on.
- The finishing page offered tries for apps this server does not have.
- `sudo bash uninstall.sh --dry-run` crashed without root.
- Ask Neko's memory said "0.0 GB free" where it meant free memory less the 2 GB kept for the apps; each number is said now.

**The setup window**
- Choosing something no longer moves the page.
- Three steps whose questions reached only the terminal now ask in the window too.
- A question is no longer missed because of when its file was loaded; an answer that was not delivered can be sent again.
- A long list is shown whole with a search box.

**Ask Neko** (from the review of 0.49.0)
- Approve installs only the version the card showed.
- An assistant that needs a look no longer stops the first checkup.
- Changing the model counts its download in the space check; a model named without a tag is not downloaded again at every run.
- An answer cut off part-way is marked as such; no fix or bug report is read from it.
- Leaving the page stops an answer that is still streaming.
- Missing, old or future-dated readings are said as such.
- Drive free space reaches the assistant.
- A copied bug report never carries a password typed into the question.
- Copy works, or says so, on a page without a clipboard.

### Removed

- **Three drives are no longer required**, and the disk Linux runs from is no longer refused.
- **Typing a drive's name to confirm a choice**, and **typing SECURED** to switch the apps' addresses on.
- **"Ready to start? [yes/no]"**, "Install the media apps too?" and "Have you completed this step successfully?".
- **The question about which connection is your home network.**
- **LinuxTracker** as a suggested download source.

### Not in this release

- **An installation drive for one group's apps** (its settings and database on a drive of its own): built, but its gate failed with the drive away, so it is not offered; it moves to 0.51.x. The apps are installed together on one drive, as in 0.49.0.
- **Torrentio** is not in Prowlarr's catalogue (it is a Stremio add-on), so it is not a recommended source; to 0.51.0.
- **"Back this up too?" for films and shows** on a share: NekoServer has no backup job for films; they are listed as not backed up.
- **Spin-down** covers only the media HDD; other drives keep their own power settings.

### How it was tested

On the release commit: 2409 Python tests (19 skipped) and 559 Rust tests pass, `cargo clippy --all-targets -D warnings` is clean, and no function this release made longer is over a cognitive complexity of 15. The gate ran on three virtual machines on 2026-11-12: a fresh install in the setup window, one drive in the terminal, two drives with a network share and films already there, and an update of an installed 0.48.2 server (with an update cut off midway and put right at the next start). Its failures are fixed above; the rows and what they looked at are in the gate results. Not tested on the gate: the assistant's model on the small VMs, the phone on mobile data, a real NAS or ZFS box, and real hardware (a TV, a controller, removable media).

### Known limits

- NFS was tried on virtual machines and with unit tests, not yet on a real NAS box. Folders shared the Windows way (SMB) are not supported.
- A NAS used over iSCSI and NFS at once is not offered: choose one for both drives.
- Restore and repair are in the terminal menu (the window follows it); they have no pages of their own yet.

## v0.49.0 — 2026-11-10

One new, optional part, the one you asked for: an AI assistant inside the dashboard, "Ask Neko". Nothing else changes how NekoServer is used: without the assistant a server works as on 0.48.2, and the settings, timers and units setup writes are the same apart from the release number.

### New: Ask Neko, an AI assistant in the dashboard (optional)
- **What it is.** A page of the dashboard, **Ask Neko**, right under Home. You ask what a reading means or what to do about a problem; each item under **Needs attention** has **Ask Neko about this**. The language model runs on the server itself (Ollama), so nothing you ask leaves your home. ASSISTANT.md says all of this in more detail.
- **Off unless you choose it.** Setup asks once, after the dashboard step, and the setup window has a switch for it. Asking Neko needs the dashboard PIN, so without one setup asks for it there, and without a PIN nothing is downloaded. Before anything is downloaded it also checks the processor (64-bit, with SSE4.2 on Intel and AMD), the memory free with your apps running, the SSD space, and that port 11434 is free (an Ollama of your own keeps it, and nothing is installed). These checks read kernel files and Python's own library, so no extra tool is needed. A run with nobody at the terminal never installs it.
- **A choice of model, with what each costs this PC.** "No assistant" (Enter), then each model that fits, the biggest marked recommended: its download, the memory it uses while it answers ("about X GB of your Y GB"), up to half the processor cores and what that slows (a film Jellyfin converts, new photos in Immich), and that its memory is free again five minutes after the last question. A model that does not fit gets one line with the memory it needs. `qwen3.5:2b-q4_K_M` (3 GB), `qwen3.5:4b` (5 GB), `qwen3.5:9b` (9 GB), each the model's own memory while it answers (measured: 2.2, 3.7 and 6.7 GB) with 20 % to spare, and its hard limit; the memory is only what is left after a reserve for the other apps (at least 2 GB, or 20 % of the memory), measured again on every setup run, and a running assistant is stopped when it no longer fits.
- **What Neko reads, with secrets out.** Your question, a short summary of the readings, and for an attention item its text and the last 40 lines of its service's log. Passwords, keys, tokens, cookies, Tailscale keys, e-mail addresses and every app password and secret NekoServer keeps are taken out before the model sees anything.
- **Fixes only from a short list, approved by you.** Neko may suggest restarting an app, looking for updates, updating an app that has a version waiting, or running one of four backups now. The server checks each suggestion against the list and what is installed, the page says exactly what will happen (the app snapshot pauses the apps for a few minutes), and it runs only when you press **Approve**, with the PIN on every device, the server too. A backup is refused while setup, an upgrade, another backup or an unfinished job runs. Anything else is refused and logged; Neko never runs a command of its own.
- **A bug report, never a change.** When the cause looks like a bug in NekoServer, Neko writes a report (version, question, what the server showed with the secrets out, a fix in words) to read and copy. Nothing is sent anywhere.
- **NekoServer works the same without it.**
  - Ollama runs as a project of its own on 127.0.0.1:11434 only, with a hard memory limit and no swap (a model that needs more is stopped, not the server), half the cores at most, the lowest priority, and the first thing Linux stops when memory runs out. One model and one question at a time.
  - Its setup step never fails setup ("AI assistant needs a look", or "not available on this system", with the reason). No other step, service or timer needs it; the backups, updates and recovery menu never call it; an upgrade never goes back because of it.
  - Stopped, its model missing, out of memory, too slow or answering nonsense: Ask Neko says "Neko is not available right now" with the reason, and nothing else changes. TESTING.md: safety rule S08, controlled failures F07 to F11.

### Found while trying it on the test machine
- **The first design froze a 6 GB server.** The assistant's memory limits were first worked out from the total memory (the model's need or 40 %, and 2 GB for a chat page), without counting what the other apps already used. On the test machine they came to 5 GB with 1.6 GB free, and the first question stalled the whole server under memory pressure for twelve minutes (no SSH, no out-of-memory kill). Now the model gets only what is free with the other apps running, less their reserve, as a hard limit; on that machine setup says there is not enough free memory and installs nothing.
- **An assistant with an address of its own held up every app's address.** The first design gave the chat page a home and Tailscale address; the gateway's check of the running apps found no row for it and refused all of them. With the chat now inside the dashboard, the assistant has no address of its own.
- **Asking needs the PIN on every device, the server too.** The page first asked for the PIN only away from the server, and the server refused the question there. Asking is an app action like Restart app, so the page now asks for the PIN everywhere, and setup sets the assistant up only once a PIN exists.
- **"Not downloaded yet" with the model there.** The check looked for the model's files where an older Ollama kept them. It now asks Ollama itself which models it has.
- **No answer within the time limit.** Qwen3.5 thinks before it answers unless told not to; on four cores that took the whole three minutes. Neko now asks for the answer at once.
- **Answers at a crawl.** Ollama started one thread per core while it may use half of them, and its threads waited for each other: about one word in two seconds. Each question now names the threads of its processor share; the same question then took 4 to 16 seconds.
- **A suggested fix was missed.** The small model wrote its ACTION line in bold with a colon after it. The server and the page now read past such dressing; a fix outside the list is still refused.
- **"an error was encountered while running the model: unexpected".** That is what Ollama says when the kernel ends the model for the apps' sake. Neko now says the model was stopped because your apps needed the memory, and to ask again in a while.
- **Stars and ticks in the answer.** The small model writes Markdown, and the page showed it as it came ("**running too slow**"; a bug report that began with a line of stars). The page now shows plain words, with a dot for a list item. When the browser refuses copying, the bug report is selected and the page says so.

### How it was tested
- Python tests (1574, on openSUSE Tumbleweed with Python 3.13; 67 in tests/test_assistant.py and 8 for the page in tests/test_dashboard_page.py) and Rust tests (510, Rust 1.99; 28 in assist.rs) pass; ruff, rustfmt, clippy and Prettier are clean. Among them: planted secrets never reaching the model, Ollama stopped, the model missing, killed for its memory, too slow, a garbage answer, repairs outside the list, the PIN, Ollama on 127.0.0.1 only, the choice list per memory size, and no checking tool run.
- The Ask Neko page was tried in a browser against a stand-in server, on a desktop and at phone width: the question from an attention item, the answer as it came, the repair card and Approve, the bug report.
- The golden-file tests changed only by the release number and the new `"assistant"` settings in the settings copies.
- **A fresh install** from the clean Debian 13 desktop snapshot of the test machine (6 GB of memory, 4 cores), answered as a person would: 31 of 32 steps, the phone step pending as on every release. With every app running there was 0.4 GB for a model, so setup offered none and said why; the six private addresses answered over HTTPS with valid certificates.
- **A model named by hand** (`qwen3.5:2b-q4_K_M`): refused with the apps running; installed once they were stopped by hand, with its 3 GB limit, no swap, and port 11434 on 127.0.0.1 only. Through the dashboard's own interface: a question without a PIN session is refused; with one, the answer came in 4 to 16 seconds with a redacted excerpt of the app's log; a second question meanwhile is told Neko is busy; a fix outside the list is refused and logged; the approved update check ran.
- **Ollama stopped (TESTING.md F08):** Ask Neko said "Neko is not available right now: the AI assistant does not answer", and the dashboard went on as before. (The media apps were still stopped by hand from the step above, so their own pages were not part of this drill.)
- **Memory needed elsewhere (F09):** with all 16 app containers and the desktop running, the model still fitted and answered in 16 seconds: no harm, no restart. Then Immich was made to load its own models until the PC ran out: the kernel ended Neko's model first (its out-of-memory score is the highest), the question ended with a plain reason, every app's page kept answering, and Ollama came back once and stayed idle (no restart loop). A second, far bigger Immich model then did not fit even without Neko and was ended too; that was the test's doing. A model killed inside its own hard limit was seen in the measurements: the 9b under a 4.5 GB limit, with the machine fine.
- **Setup run again with every app running:** it stopped the assistant ("not enough free memory: qwen3.5:2b-q4_K_M needs about 3 GB free, and this PC has 1.0 GB free with your apps running and room kept for them"), listed it under "Not available on this system", and finished every other step. The model stays on the SSD.
- **The real page on the installed server**, in a browser (apps stopped by hand so the model fitted): "Ask Neko about this" on an attention item, the PIN dialog, the answer arriving, a repair card and **Approve** (Jellyfin restarted, "Done.", one item fewer under Needs attention), **No, thanks** ("Not done."), the bug report box and Copy, and the page at phone width.
- **On a phone** (the Android emulator over Tailscale): the phone reached the server and opened all six private addresses; the dashboard showed 10 of 10 apps and no Ask Neko entry, as it should with the assistant stopped. Ask Neko itself was not tried on the phone: this test machine has no room for a model beside its apps.

### Known limits
- The test machine could run Ask Neko only with its apps stopped, so asking from a real phone was not tried; the page was tried at phone width in a desktop browser.
- The smallest model's answers are sometimes wrong, and the fix it suggests is not always the fitting one. The card says exactly what a fix does, and nothing runs without Approve.
- The model runs on the processor only; a graphics card is not used.
- Ollama is updated by NekoServer releases, not by the weekly app updates.
- On a busy server answers are slower: the other apps come first by design.

## v0.48.2 — 2026-11-09

A cleanup release: less repeated code, the tests that freeze what setup writes, and the written standard every release is now tested by. Three fixes carried from the 0.48.1 review come with it. Nothing changes how NekoServer is used; the new report lines are named below.

### Cleanup
- **Seven places that did the same thing now share one.** The setup window and the terminal start a desktop app through one function; the upgrade and the drive move read their records and check each other's locks through two shared helpers; Debian and the Fedora family settle half-installed packages through one method; two of the four file-checksum functions became the shared one (the other two read in blocks and check the path first, so they stay); the runtime's app port lookup has one copy; the app, host and music backups write their nightly timer through one template; the setup window enables and disables its answer controls in one place. Every unit, timer and settings file setup writes is byte for byte the same as in 0.48.1 (the new golden-file tests compare them).
- **The search found nothing else to remove.** Every file in the repository is used, no test duplicates another or tests code that is gone, and the dead-code tools report nothing.

### Tests
- **Golden-file tests of everything setup writes for systemd and the runtime.** For four fixed setups (the defaults, libraries on the media drive with no USB drive, a settings file from before 0.48.0, and backup times that cross midnight), every unit, timer, drop-in and job settings file (224 files) is rendered and compared byte for byte with a kept copy; a change shows as a diff. A release bump regenerates them with one command (tests/golden/README.md).
- **The test standard.** TESTING.md is how every release is tested: nine fixed user journeys with concrete expected results, seven safety rules with the guard that enforces each and the tests that assert it, six controlled failures, compatibility fixtures, one record format for every case, and a gate report that says passed, failed, skipped or not tested. tests/COVERAGE-MAP.md says what the 0.48.1 gate proved and what it did not, and links each 0.48.1 fix to its regression test. tests/golden/BASELINE-0.48.1.md keeps the numbers this release is measured against.

### How it was tested
- Python tests (1495, on Ubuntu 24.04 with Python 3.12; five tests of the removed 0.21.0 path are gone, fourteen for the Tailscale certificate check and the mutation-pass gaps are new) and Rust tests (480, on Arch Linux with Rust 1.99.1, the one CI uses) pass; ruff, rustfmt and clippy are clean, and Prettier and the setup window's page tests ran on Arch Linux with node 26. The golden-file tests passed unchanged across the cleanup; the version bump changed only the release number in their paths. A mutation pass over the code the 0.48.1 and 0.48.2 fixes added caught 198 of 198 Python and 29 of 32 Rust mutants; the one real gap has a test now.
- The release gate ran on a clean Debian 13 PC with a desktop (a virtual machine), following TESTING.md: a fresh install with the default answers (31 of 32 steps, the phone step left for the person), every backup once with test data in the apps (all pass, and every test file, photo and song is byte for byte the same afterwards), two timers moved two minutes ahead (both started their jobs), setup run again (32 of 32; the first checkup passes 79 checks and no longer flags Immich), and the restore menu for photos from the USB drive, Nextcloud files and app settings (every restored file matches its checksum). The phone reached all six addresses over Tailscale, but the pages could not be checked: Let's Encrypt refused the server's certificate after the test machine's many reinstalls this week, which is how the Tailscale step's missing check was found. The Tailscale step's fix was then run on that same machine: it now stops with the certificate reason where it used to pass. The gateway code is otherwise unchanged since 0.48.1, whose gate opened the dashboard on the phone.

### Setup
- **The Tailscale step checks that the phone addresses really open.** It ended as done once the private routes were in place and HTTPS was switched on for the tailnet, without checking that Tailscale had got the HTTPS certificate for this machine. Without one, every app address on the phone hangs and shows a blank page, and nothing said why (seen on the 0.48.2 gate, where Let's Encrypt had refused the certificate: it gives a machine name at most five a week). Setup now opens the dashboard's address with the certificate checked, trying for up to 90 seconds since Tailscale fetches it on the first connection. When it does not open, the step waits for you and says why, with the time Let's Encrypt allows the next certificate when Tailscale's log names it; the checkup's Tailscale row says the same. New text: "Tailscale has not got the HTTPS certificate for <name> yet, so the app addresses on the phone do not open. ... Run  sudo bash install.sh  again later; it checks again." A phone you confirmed before stays confirmed.
- **The first checkup waits for the containers its own backups restarted.** The checkup runs the Immich database backup, which pauses and restarts Immich's containers, and inspected the server two seconds later: their health checks were still running, so the finishing screen said "something needs a look" and the dashboard's checklist kept "Immich needs a look" until the next setup run (seen on the 0.48.1 gate; present since 0.41.1). The checkup now looks again every 10 seconds for up to two minutes while a failing check is only a container still starting, as the gateway step already did; anything else wrong is reported at once, and so is a container still not up after the wait.

### Backups and updates
- **The weekly USB copy takes the newest database copy and password list that pass their checksum.** When the newest Immich (or Nextcloud) database copy on the HDD, or the newest sealed password list, failed its checksum, the week's copy reported the damage and copied nothing, leaving the USB drive without a fresh copy for as long as the damaged one stayed the newest. It now passes the damaged one over and copies the newest one that still matches, and the report names both: the copy that failed and was skipped, and the one copied instead. With no sound copy at all it behaves as before (nothing copied, the damage reported).

### Upgrade
- **Upgrading straight from 0.21.0 or 0.16.0 is no longer handled.** Setup recognised the exact Compose files those 2025 releases wrote and rewrote them; nobody runs them any more (0.41.2 already said so). Upgrade to 0.41 or later first.
- **Tidy: the release folders an upgrade protects.** `upgrade_needs` also protected the name of the folder the new release's setup runs from, which is always `nekoserver` and never a release folder, so the line did nothing; it is gone, with a comment saying why. Nothing changes in what is kept or removed.

## v0.48.1 — 2026-11-09

Fixes only, from an outside review of 0.48.0: nineteen things in setup, the upgrade tool and the backup jobs, most of them gaps the new backup-times question left. Nothing changes how NekoServer is used; a few lines setup says are new, each named below.

### How it was tested
- Python tests (1476, on Ubuntu 24.04 with Python 3.12 and on Arch Linux with Python 3.14, where the runtime was built and node is present so the setup window's and the dashboard's page tests ran too) and Rust tests (477, on Arch Linux with Rust 1.99.1, the one CI uses) pass; rustfmt, clippy and shfmt are clean. ruff and Prettier were not run on this PC; CI runs them. Each fix has a unit test that fails without it.
- The release gate, a fresh install with a desktop on a clean Debian 13 PC (a virtual machine) with the phone checks, is run before this release is merged; it had not run when this entry was written.

### Setup
- **Immich's weekly database copy lands on the right day when its 45 minutes cross midnight.** The copy runs 45 minutes before the weekly USB copy, but it kept the USB copy's weekday: a USB copy at Sunday 00:20 put the database copy at Sunday 23:35, 23 hours after it instead of 45 minutes before. The database copy's timer is now written for the day before when the subtraction wraps past midnight (Saturday 23:35). A server set up before 0.48.0, where the database copy has a time of its own, keeps the USB copy's day as before.
- **On a server set up before 0.48.0, Enter at the backup times no longer moves them without a word.** Such a server gave the music copy, the copy of this PC's own settings and Immich's database copy times of their own. The drives step showed only the daily, weekly and monthly times and said Enter keeps them, then saved the three, from which the other jobs are placed at fixed distances from the daily time: they moved silently. Now, when every answer keeps the time shown and saving would move one of them, nothing is saved and the log says "Backup times kept as they were"; when an answer changes a time, setup says which jobs move before it saves ("With these times the music copy moves from 03:45 to 22:30"). The setup window's page goes through the same code.
- **Setup run again shows the backup times the settings really hold.** The "Last time, the optional questions were answered this way" line took the backup times from the answers kept on the SSD, and `no` left the settings as they were: with a settings file made again from the template (04:15, Sun 03:00, 02:30), the terminal showed the old times, you kept them in words only, and every timer was written at the template's times. The list now shows the times the settings hold, and when they differ from the remembered answer the drives step says so ("The backup times in the settings (...) are not the ones chosen last time (...), so they are asked again") and asks the questions instead of offering to keep them.
- **Enter never keeps a monthly time the maintenance step refuses.** The monthly question offered the server's current time as its default, and Enter, Ctrl+D or the window's "Skip for now" took it unchecked. When `maintenance.night_end_hour` had been lowered since (to 2, say, with the monthly check at 02:30), the maintenance step then refused the time. The default is now the latest time with the same minutes that is still before the night's end (01:30), the prompt says why ("02:30 is not before 02:00"), and the setup window's page shows the same default.
- **A weekday typed in lowercase no longer blanks the setup window's day selector.** "sat" typed in the terminal reached the window as typed; its selector knows only "Sat" and showed nothing. The answer is recorded in its capitalised form.
- **Only a full setup run changes the backup times.** The drives step asked them in every run that reached it, and nearly every step needs the drives step: `sudo bash install.sh --module host_backup --apply` (or `--profile`, or `--retry-failed`) saved new times and rewrote only that step's timer at them, leaving the other jobs at the old ones. A run that does not cover every step that writes a timer now says "Backup times are changed only by a full setup run (sudo bash install.sh); this run keeps ..." and asks nothing.
- **A NAS disk that cannot be inspected stops setup instead of passing as empty.** When `lsblk` failed, or gave an answer that could not be read, or listed no row for the disk, the LUN was described as "empty: no filesystem yet" and the step offered the command that formats it. Setup now stops with "LUN n could not be inspected (reason); nothing was changed. Run setup again.", and a disk whose size is not known is said to be "size unknown" rather than shown as "?".
- **Ctrl+C during the first checkup still reaches the finishing screen.** The checkup says it may be stopped, but stopping it left setup before the finishing screen, and the setup window never heard that setup had finished. One line now says "First checkup stopped" and how to run it later (`sudo bash install.sh` again), and the finishing screen follows.
- **The setup window's Start button comes back after a refused start.** When setup's server answered Start with 409 (the settings file cannot be read right now, or setup has started already) the button stayed disabled at "Starting…" and nothing said why. The reason is now shown in the page's note and the button is itself again, to press once more.
- **The setup window's Send works again after the backup-times page.** The page's three rows are required inputs of the same form as the plain answer field; when a later question came they were only hidden, and hidden required inputs still stopped the form from sending, so Send did nothing from then on. Off the page the rows are removed.
- **The setup window's header wraps in a narrow window.** Below about 600 px the logo, the name, the progress bar and the badge stayed on one line and the badge was clipped; in the narrow layout the header wraps and the progress bar takes a line of its own.
- **Documentation.** The README links the wiki's Architecture page (how the parts connect) under the documentation links, and GUIDED-SETUP names the backup times among the optional questions a later run offers to ask again.

### Upgrade
- **A runtime copy hidden by a way back that was cut off gets its name back.** The way back to an older release hides the newer release's runtime copy under another name, with a link at its own, while the older setup runs, and only the end of that run gave the name back. When the way back was cut off while the older `install.sh` was still starting (a power cut, the PC out of memory, `systemctl stop` of the upgrade unit), the next start found the server whole and closed the record without touching the copy: the link stayed for good, and every later setup of that release failed with "Refusing symlink path". Closing the record, at a start or after a setup run by hand, now gives every hidden copy its own name back first.
- **A failed step and an `install.sh` refusal no longer pass for a step that waits for you.** Setup's exit code 1 meant three things: a step that only waits for you (Tailscale's sign-in, say), a step that failed, and `install.sh`'s own refusal before setup ran (the setup lock held by another run, its preflight, a missing prerequisite). The upgrade took every 1 for the first: a step new in the release that failed went unseen and the upgrade was reported installed and checked, and a refusal, on the upgrade or on the way back, was accepted and the later check blamed the services while the real cause was only in the log. Setup now ends with 5 when a step failed and 1, as before, when a step only waits; the upgrade also reads the run's log, where a FAILED line is a failed step (also from a release before this one) and a run with no Finished line never reached setup, so `install.sh` refused, and the log's last words are the reason.

### Backups and updates
- **A weekly USB copy that gives way to the monthly run is no longer recorded as a failure.** When the 1st fell on the weekly day, the monthly run copied the USB drive itself and the weekly copy found it at work. That was right (0.47.10's known limit), but the weekly copy's "did not start" message was recorded like a real error: the USB backup read "fail" over the monthly run's pass, and the dashboard showed the external backup failed for a week. The weekly copy now records nothing, as when it gives way to setup, and the weekly USB service exits 0 with its message instead of ending in error. The message is unchanged.
- **A USB drive the monthly check could not check no longer overwrites the weekly report.** The monthly run's checksum check of the drive runs the weekly copy with every file compared and recorded its result as the weekly job's. When the drive was full or would not mount, the monthly run tolerated it ("no damage found, the snapshot and updates go on") and said so in its own report, but the weekly job's last pass had already been replaced by a "fail" with that reason, which the dashboard showed against the USB backup. A problem the monthly run tolerates now leaves the weekly report as it was; a pass, files left out and damage found by the checksums are still recorded, since they are results of the copy itself.
- **The nightly app snapshot says when room was made by cutting the history short.** On a tight HDD it removes complete snapshots, oldest first, until the new one fits, keeping the newest as the one good copy. The number the settings keep (`app_backup.keep_snapshots`, 7 by default) was never consulted, the only trace was a line in the journal, and the job ended "pass": you believed you had a week of history when one or two nights were left. The job now ends "partial" when fewer than the kept number remain, saying how many were removed and why, and the dashboard shows it like the other partial results. Nothing changes when the drive had room.
- **The monthly update's retry after the autumn clock change is set for tomorrow, not today.** A monthly run that gave way to setup, or failed, tries again the next night; tomorrow was worked out as now plus 24 hours, as a local date. On the night the clocks go back the day has 25 hours, so from any moment before the repeated hour that was still today: the timer was set for a time already passed, never fired, and the report said the update would try again that night. Tomorrow is now the calendar day after today in the server's zone.
- **The host snapshot's protected complete snapshot is chosen in the pruning's own order.** A host snapshot made with Steam open leaves Steam's files out, so the newest snapshot that has them is protected from pruning. It was chosen by sorting the snapshot names, while the pruning counts a snapshot dated more than a day ahead (made while the clock was wrong) as the oldest and looks only at this user's private snapshots: a future-dated snapshot became the protected one, and the real newest complete snapshot could be pruned away. Both now count the snapshots the same way.

## v0.48.0 — 2026-11-08

Setup now asks when the backups run: one daily, one weekly and one monthly time, in the terminal and on one page of the setup window. That is the one change in how NekoServer is used. Before it, four fixes from a second outside review of 0.47.10, each a gap one of 0.47.10's own fixes left.

### How it was tested
- Python and Rust tests pass; ruff, Prettier, shfmt, rustfmt and clippy are clean. Each of the four fixes has a unit test that fails without it; the way back is tested against a copy of 0.47.9's own guard.
- A fresh install with a desktop on a clean Debian 13 PC (a virtual machine): the setup window showed the three times on one page, refused a monthly time after 06:00, and its answers reached setup and the settings; the timers were written from them, and the daily snapshot, the weekly USB copy and the monthly check (at 01:00, with the clock moved) each started from its own timer and passed. Setup run again showed the times among the kept answers and did not ask them.
- The way back from a stuck upgrade, on a fresh 0.47.9 install: 0.47.9's own upgrade tool, upgrading to 0.48.0 with its check made to fail, stopped at its own guard ("this server already runs 0.48.0"), as reviewed. 0.48.0's `upgrade.py resume` then put 0.47.9 back past that guard: the services and the dashboard run 0.47.9, the check passed, the record is closed, and 0.48.0's runtime copy has its own name back. When 0.48.0 itself was whole and passed its check, the same command closed the record and kept 0.48.0 instead.

### Known limits
- An upgrade from the dashboard is run by the upgrade tool of the release the server runs. From 0.47.9 that is 0.47.9's own tool, which a newer release cannot change: if its check fails after the new setup has rewritten the services, its way back still meets 0.47.9's guard, the dashboard says putting 0.47.9 back did not pass, and the upgrade waits. Then run `sudo python3 /srv/nekoserver/installer/releases/v0.48.0/upgrade.py resume` (`/srv/nekoserver` is the SSD folder, `data_mount` in your settings): it uses this release's fixed code, and either keeps 0.48.0 when it is whole and passes its check, or puts 0.47.9 back (both tried on a VM). Upgrades from 0.48.0 on, and the resume after a power cut, have the fix.

### Fixes
- **Putting an older release back is no longer stopped by that release's own guard.** 0.47.10 let the way back past the guard of 0.47.10 and later, but the guard that runs is the one of the release being put back, and a release before 0.47.10 knows nothing of the upgrade's record: with the services still pointing at the newer runtime it stopped with "this server already runs ...". While the previous release's setup runs, the newer release's runtime copy now has a hidden name with a link at its own name: the guard and the cleanup count only real folders, every service still finds its program, and afterwards the copy gets its own name back (one hidden when the power was cut is put back first).
- **A drive on a Btrfs subvolume is checked the same with or without `findmnt`.** When `findmnt` did not answer in time (0.47.10 reads the kernel's mount table then), the answer lacked the subvolume that `findmnt` shows as `/dev/sdb1[/@data]`: a drive set up on `/@data` was refused as "mounted with another Btrfs subvolume", and one on another subvolume accepted where the settings name none. The mount table's answer now shows it the same way.
- **An older media report is never shown as the acceptance check's result.** Acceptance started the media check, ignored how it ended and read its last report: when the check stopped before writing (a drive its guard refused, its lock, broken settings), the last run's PASS rows were shown as current. The report now counts only when it was written after the check was started; otherwise one failed row says "The media check did not write a new report", with the reason when the start gave one.
- **A replaced USB drive shows no old check result.** 0.47.10 stopped a new drive's first copy from taking over an entry without a drive id; the dashboard still showed that entry for the new drive until a copy rewrote it. With a USB drive set, a USB copy's result is now shown only when it names that drive.

- **An upgrade's files are tidied also when the way back removed its settings.** Found while trying the way back above: the resume, run from 0.48.0's copy on the SSD, put 0.47.9 back, whose setup removed that copy with its settings file; the tidy after it could not read them, and the download and the build folder (gigabytes) stayed in `installer/upgrades`. The folder is now read before the work starts.

### New: setup asks when the backups run
- **Three questions at the drives step: the daily, the weekly and the monthly time.** Before, every job had its time deep in the settings file (`app_backup.time`, `host_backup.time`, `music.backup_time`, `photos.backup_time`, `photos.backup_weekday`, `photos.database_backup_time`, `maintenance.time`), and nobody was asked. Now setup asks, right after the drives: "When should the daily backups run? (HH:MM, Enter for 04:15)", the day and time of the weekly USB copy (Enter for Sunday 03:00) and the time of the monthly check on the 1st (Enter for 02:30; it must start at night). The answers are `"backup_times"` in your settings, the one place every timer is written from: the music copy runs 30 minutes before the daily time, the media apps' snapshot at it and this PC's own settings an hour after it, so the nightly jobs never start in the same minute; Immich's database copy runs 45 minutes before the USB copy, which takes it along; the weekly app updates and the monthly update's "again next night" retry use the monthly time. Enter keeps every time as it was, so nothing changes unless you ask. Setup run again shows the last answer with the other optional questions and asks again only on yes; a setup without a person at the terminal (an upgrade) never asks, and a server set up before 0.48.0 keeps the times its settings had until setup asks you. The setup window shows the same questions.
- **The setup window asks the same on one page.** Three rows, the daily backups, the weekly USB copy (day and time) and the monthly check, each with a time picker and a line saying what runs then, filled with the times setup would keep; "Save these times" sends all three at once, "Skip for now" keeps the times shown, as Ctrl+D does in the terminal. The terminal still asks them one at a time, and either place can answer.

## v0.47.10 — 2026-11-08

Fixes only, from two sources: a round of backup and installation drills on a clean Debian 13 PC (a virtual machine with small test data), and an outside review of 0.47.9 that found twelve things, each named below with "finding N". Nothing changes how NekoServer is used.

### How it was tested
- Python tests (1427) and Rust tests (473) pass; ruff, Prettier, shfmt, rustfmt and clippy are clean.
- A fresh 0.47.9 install from the clean snapshot, then on it: all four backup jobs by hand and the monthly check in its night window; restores of photos, Nextcloud files and a monthly snapshot into new folders with matching checksums; two damage drills (a damaged copy healed by the monthly check, a copy and its original both damaged repaired from the USB drive by the recovery menu); every timer started by its own schedule with the clock set before it, and the kept counts (7 nightly, 7 host, 3 monthly, 4 database copies); a USB drive filled to the last byte; the USB drive swapped for an empty one and the documented route to the new drive; the media drive remounted read-only under the running apps.
- Installs from the clean snapshot in less common shapes: without a USB drive (added on a later run), stopped with Ctrl+C at a prompt and run again (the saved drive answers kept), and the PC's power cut while the media apps were being deployed, then setup run again (it carried on at that step; the damaged Jellyfin image it left is the fix below).
- Restores onto a second clean VM from the carried-over USB drive and a monthly snapshot: photos, Nextcloud files, the monthly snapshot with its app images, and `monthly-recovery.py --stage`, every checksum matching.
- The twelve review findings each have a unit test that fails without the fix.

### Known limits
- The monthly check peaked at 4.7 to 5 GB of memory and took 14 to 21 minutes on the 6 GB VM (it was swapping; 2 minutes on a quiet run). A server with little memory will see the same.
- A weekly USB copy whose Sunday 03:00 falls while the monthly run is still at work (a Sunday the 1st) refuses with "The monthly update is running, so this week's USB copy did not start" and shows as a failed run; the monthly run makes its own copy of the drive.
- "SSD only" and "HDD only" setups were not tested: setup requires both drives.
- The music library is not in the monthly snapshot (the weekly jobs cover it); the music restore tool is covered by its new unit tests.

### Dashboard and alerts
- **A drive kept busy by a backup is no longer shown as missing.** While the USB drive was being filled and an Immich database copy ran on a small VM (under a load partly made by the test rig itself), one dashboard reading took two minutes and its `findmnt` call for the media HDD was killed after 8 seconds (it reads the filesystem's label block, which the saturated drive held back). The page then said "Media HDD: Missing", every backup row "Can't run: drive missing", and the apps counted as held, until the next reading a minute later. When `findmnt` does not answer in time, the collector (and the drive check every backup job makes) now reads the kernel's own mount table and the drive links, which needs no access to the drive.
- **An app check that could not run no longer leaves the last PASS as current** (finding 3). When the hourly check stopped early (a drive its guard refused, its lock, broken settings) it wrote no report and the dashboard showed the last PASS for up to two intervals; now the collector reads the check service's last failure and marks the report stale with the reason.
- **An app counts as recovered only when its check got an answer** (finding 11). A RECOVERED phone alert went out while the app's answer was not known yet (right after a restart, or a check that did not reach it); now recovery waits for a check that reached the app.

### Setup
- **An app whose downloaded image was damaged by a power loss is noticed and downloaded again.** The PC lost power while setup was downloading Jellyfin's image; Docker listed the image as complete, but the files of its last layer were lost with the unwritten disk cache. Setup run again said "Deployed jellyfin" and went on, and the container restarted for ever with "exec /init: exec format error" (the accounts step then waited three minutes for an answer that never came). Now, after starting an app, setup looks at a container that is not running: when its output says its program cannot run, the image is removed and downloaded anew and the app started again; when that does not help, the step fails and says so instead of passing.

- **The media apps step waits for Docker instead of failing after 20 seconds.** On a PC still busy right after building the runtime (a small VM, under a load partly made by the test rig itself), `docker info` took longer than the step's single 20-second try, the step failed with "docker info --format {{.ServerVersion}} did not finish within 20 seconds", and setup asked to be run again. Docker now gets up to two minutes to answer, as the photos step already gave it.

- **The closing screen no longer points at a list it did not show.** With every step done and only the first checkup finding something ("Needs a look: …"), the screen said "1. Fix the first item under "What needs you" (its line says how)", though no "What needs you" list was printed. It now says to look at the items under "First checkup and backup" (the dashboard's checklist explains each one) and that backups and updates run on their own. A step that still needs you keeps the old wording.

- **The Tailscale step keeps its sign-in link when Tailscale answers late.** The installer asked `tailscale up` for the link with a 15-second bound; when Tailscale's control server took longer (16 seconds on the test VM, when a stopped setup was run again) the step said "No authorization link available. Run sudo tailscale up in another terminal window" although the link arrived a second later. Now the bound is 45 seconds, and when the command still gives up first the link is read from `tailscale status`, which keeps it while the sign-in flow is open.

- **The old-release guard lets the upgrade tool's own way back run** (review of 0.47.9, finding 1). An upgrade that wrote its unit files and then failed was put back by running the previous release's setup from its copy on the SSD; that setup's guard saw the services pointing at the newer runtime and stopped with "this server already runs ...", so the way back never ran and the record stayed open with app updates and the monthly check waiting. The guard now stands down only while the upgrade's own record names the folder being set up or put back; a person's setup from any other folder is still stopped.

- **A narrower setup run keeps the profile the server was set up with** (review of 0.47.9, finding 6). A repair such as `sudo bash install.sh --profile foundation --apply` on a full installation recorded "foundation" as the server's profile, so the next upgrade from the dashboard set up the foundation alone and left the installed apps out. Now the profile is recorded only when the run covers every step already done; otherwise the log says the recorded profile was kept and which steps the run left out.

### Backups
- **A replaced USB drive inherits nothing from a report without a drive id** (finding 2). A previous integrity entry with no drive id (written before drive ids were recorded) carried its full-check date and findings over to a new USB drive; now a missing id counts as another drive.
- **An app whose files live on the media drive is not started without it** (finding 7). After a weekly copy, Immich's or Nextcloud's resume checked the SSD alone; with the photos or Nextcloud's files on the media drive and that drive gone (or another one mounted there) while Docker still ran, the app was started against an empty folder on the system disk and the recovery record cleared. Now the resume also requires that drive and otherwise leaves the record for drive-return.
- **The drive move refuses a copied NAS LUN and a timer that did not stop** (findings 5 and 8). `migrate.py` knew the old drive by UUID, filesystem and subvolume alone, so a restored or cloned LUN with the same filesystem UUID on another NAS disk was accepted as the source; now the disk's WWN is checked the way every backup checks it, at the start and before each step. A timer whose stop failed was passed over, so a backup could start during the quiet copy; now it ends the move with "These did not stop: ...".
- **Two approvals at once both survive** (finding 12). "Install at the next update night" and the cleanup after an update rewrote approved.json and pending.json with no lock, so only one of two overlapping writes survived; now one lock in the updates folder covers each read-modify-write.

### Recovery
- **A copy that fails part-way is removed and the real error shown** (finding 4). When the private copy of a snapshot archive stopped part-way (a backup drive that failed to read, no room left), the partial file stayed in the hidden work folder, removing the folder failed with "Directory not empty", that error hid the real one and every retry left another `.neko-images-*` folder beside the snapshot. Now the copy is removed on any failure and the first error is the one reported.
- **The music restore writes every file through the folder it made** (finding 9). `music-recovery.py` wrote each file by a fresh lookup of its path, so a folder swapped in mid-restore got the rest of the files while the count said all were restored; now everything goes through one descriptor, as the monthly staging does, and a swapped folder ends the restore with a clear message. The tool gained its first unit tests.
- **`monthly-recovery.py --stage` says plainly when the folder above the destination is missing** instead of a traceback.

### Uninstaller
- **Nextcloud's files on the SSD and the USB password lists go with their group** (finding 10). Both were only listed as staying; the top-level folder of each place now comes from the shared locations table and is looked for on both drives, and `passwords/` is one of the USB copy's own folders.
- **"Check a drive that turned read-only" in the recovery menu now runs.** It always answered "Setup (sudo bash install.sh) is running: check the drive once it has finished. Nothing was changed.", with no setup running: the menu is started by `install.sh`, which takes setup's lock first, so the menu counted itself as the running setup. The lock the menu holds itself is now left out of that check; a setup really running elsewhere is still waited for. Found on a VM with the media drive remounted read-only under the running apps: the check now stops the apps, runs `e2fsck`, mounts the drive again and starts the apps.

## v0.47.9 — 2026-11-08

Fixes from upgrading a clean Debian 13 PC (a virtual machine) from 0.47.6 to 0.47.8 from the dashboard and then using it, plus two things you asked for: a "Skip for now" button in the setup window, and a rerun of setup that does not ask the optional questions again unless you want it to.

### How it was tested
- Python tests (1411) and Rust tests (469) pass; ruff, Prettier, shfmt, rustfmt and clippy are clean.
- A fresh install of the published 0.47.6 from a clean Debian 13 snapshot, sample photos, music, Nextcloud files and app settings added, then the upgrade to 0.47.8 from the dashboard: the data was there afterwards, and every restore (photos, Nextcloud files, the password list, app settings, host settings) came back into a new folder with matching checksums.
- A final fresh install from the same snapshot with every change in: setup finished every step but the phone test in one run, the server joined Tailscale through setup's own step, the feature check passed (45 checks, plus the monthly check in its night window), an emulated Android phone opened the six app addresses and the dashboard over Tailscale, and `sudo bash install.sh` run again showed the kept optional answers, asked nothing again on `no`, and the window's "Skip for now" left the phone step pending.
- The git-pull guard was tried on the upgraded VM: a 0.47.6 folder is stopped with the message, a 0.47.8 folder passes.
- The Python suite also ran on the five test distros (Ubuntu 24.04, Arch, Fedora 44, AlmaLinux 10, openSUSE Tumbleweed).

### Known limits
- The guard against setting an old download up over an upgraded server is part of this release's `install.sh`: a folder still at 0.47.8 or older does not have it.
- Running setup again with `no` at "Ask these questions again?" also skips the copy of sources you already added into the libraries (it only runs with the source questions); `yes` runs it as before.

### Improvements
- **The setup window has "Skip for now" at the steps you confirm yourself.** At "Have you completed this step successfully?" (the phone, a TV test) the window had only Yes and No; leaving the step for later needed Ctrl+D in the terminal. The button leaves the step pending and setup goes on, exactly as Ctrl+D does; the terminal shows "(left for now, from the setup window)".
- **Running setup again does not ask the optional questions again unless you want to.** Every `sudo bash install.sh` asked about download sources, the indexer choice and the playback test anew. Setup now keeps the last answer to each (how many sources were added, which indexers were chosen, whether the playback test was done or skipped; no secrets) in `config/onboarding-answers.json` on the SSD, shows them, and asks "Ask these questions again? [yes/no]". `no` keeps them; `yes` asks them as before. The first run after this release still asks them once, since nothing was kept before. A question with no kept answer is asked either way.

### Setup
- **Setup from an old download no longer sets the old release up over an upgraded server.** After an upgrade from the dashboard (0.47.6 to 0.47.8 on a VM), `sudo bash install.sh` in the `NekoServer-All` folder downloaded for 0.47.6 ran the 0.47.6 setup again over the 0.47.8 server. `install.sh` now stops first: "This folder has NekoServer 0.47.6, and this server already runs 0.47.8 (upgraded from the dashboard). Run  git pull  here first, then  sudo bash install.sh  again." The dashboard's NekoServer tab and the upgrade's finishing note say the same after an upgrade. The guard is part of this release's `install.sh`, so it protects from 0.47.9 onward: a folder still at 0.47.8 or older does not have it.
- **Skipping outage alerts is not later reported as switching off a monitor.** With outage alerts skipped at setup, every later run (an upgrade included) said "This server no longer checks in, so pause (or delete) the check at your outside monitor", for a monitor that was never made. That note now comes only when an outage monitor was set up; otherwise it says "Outage monitor: skipped by choice", as setup did. Seen upgrading a VM from 0.47.6 to 0.47.8.
- **An upgrade no longer marks unfinished app connections as done.** An upgrade runs setup without a terminal, and there the step that connects the media apps only checks that the apps answer. On a server where connecting them was not finished (0.47.6 with the music on the media drive), the step passed, Lidarr still had no music folder, and the monthly check then failed on it every night ("Live automated media connections are not ready"). Without a terminal, an unfinished step now stays unfinished and says to run `sudo bash install.sh` in a terminal.
- **The monthly check says which part failed.** A failure of its app-connection check was reported "while checking free space", the part before it. It now says "while checking the connections between the media apps".

### Dashboard
- **After Upgrade, the NekoServer tab says an upgrade is at work right away.** For up to a minute after the click (until the next reading), the tab still offered "Upgrade NekoServer", as if nothing had started, and a second click was possible. It now shows "An upgrade is at work" from the click on. Seen upgrading a VM from 0.47.6 to 0.47.8 from the dashboard.
- **After an upgrade, the release now running is not offered again.** The NekoServer tab said "This server runs NekoServer 0.47.8", then "Available: v0.47.8-beta" with the Upgrade button, until the next daily check. It now offers only a release newer than the one running.

## v0.47.8 — 2026-11-06

Fixes from answering setup in the setup window and stopping it halfway, on a clean Debian 13 PC (a virtual machine). After a wrong answer the window lost the list to choose from; a password or PIN question did not say which one it was; and each step you confirm yourself said Ctrl+C leaves it pending, when Ctrl+C stops setup.

### How it was tested
- Python tests (1404) and Rust tests (469) pass; ruff, Prettier, shfmt, rustfmt and clippy are clean.
- A fresh install from a clean Debian 13 snapshot, answered in the setup window over an SSH tunnel: Start setup from the window, closing and reopening it, wrong drive numbers and names, a mismatched PIN, Ctrl+C at the playback test and at the phone step, Ctrl+D at the phone step, then `sudo bash install.sh` again, which went on where it stopped and finished. Photos and Nextcloud's files on the SSD and music on the media drive ended up where chosen.
- A final fresh install from the same snapshot with every fix in: setup finished every step but the phone test, an emulated Android phone opened the six app addresses and the dashboard over Tailscale, and the feature check passed (45 checks, plus the monthly check in its night window).
- Covered by tests only: what the window shows for a question asked again, and the key words at the playback test.

### Known limits
- The setup window has only Yes and No at the steps you confirm yourself; leaving one for later needs Ctrl+D in the terminal.
- Running setup again asks the optional playback test and the indexer question again.

### Setup
- **The steps you confirm yourself say what Ctrl+C does.** Each one ended with "Ctrl+C leaves it pending", but Ctrl+C stops setup (on purpose since 0.39, as at every question); Ctrl+D is what leaves a step pending while setup goes on. Seen on a VM: Ctrl+C at the phone step ended setup with six steps still to run. The words now say "Ctrl+D leaves it pending and setup goes on; Ctrl+C stops setup" (at the optional playback test, which Ctrl+C does skip: "Ctrl+C or Ctrl+D skips it and setup goes on"), and the same for `acceptance --manual` and in GUIDED-SETUP.md and ACCEPTANCE.md. Nothing else changed: the keys do what they did.

### Setup window
- **After a wrong answer, the list to choose from stays in the window.** The setup window showed what the terminal had said since the last answer, so after a wrong drive number it showed only "Choose another one from the list", without the list. A question asked again keeps its list, with the error under it.
- **A password or PIN question in the window says which one it is.** The window showed every hidden entry as "a hidden entry", so "Same PIN again" looked like the first PIN question. It now shows the question's own words ("PIN:", "Same PIN again:"); the answer is still never shown or kept.

## v0.47.7 — 2026-11-06

Fixes from installing NekoServer from scratch on a clean Debian 13 PC (a virtual machine) with blank drives, as a new user would, and using it from an Android phone over Tailscale. The biggest one: with the music on the media drive, which setup recommends when that drive has more room, connecting the apps never finished. The rest makes setup and the dashboard say clearly what is happening, and stops warnings about things that are fine.

### How it was tested
- Python tests (1400, the dashboard page included) and Rust tests (469) pass; ruff, Prettier, shfmt, rustfmt and clippy are clean.
- Four fresh installs from a clean Debian 13 snapshot with blank drives, following the README: setup's recommended choices with a USB drive (three times, the last one with every fix in), and photos, music and Nextcloud's files on the SSD without a USB drive. The last one finished every step but the phone test in one run, and its feature check passed (45 checks, plus the monthly check in its night window).
- The server joined Tailscale through setup's own step, and an emulated Android phone opened all six app addresses and the dashboard (also from a link) over Tailscale.
- Every backup was run, and app settings, host settings, photos from the USB drive, Nextcloud files and the password list were restored into new folders, with their checksums matching.
- Covered by tests only: the messages for an ARM board or a Linux without systemd, and the one-network-connection case through a whole setup run (checked on the VM with setup's own chooser and one network card turned off).

### Known limits
- Not tried yet: signing in to the apps from the phone, the setup window on a real desktop, a monthly snapshot restored onto a fresh install, and an upgrade from 0.47.6 to this release.
- A monthly check started by hand during the day is refused (it waits for its night), and then shows on the dashboard as needing attention.

### Restore
- **The app and host settings restores end in plain words.** They said "live application startup remains untested" and "symbolic links remain inert text". Now they say the files are in a new folder and nothing on the server was changed or restarted.

### Setup
- **Without a USB drive, setup says how to add one.** With the USB drive skipped, the weekly USB copy waited with the message "Connect the external photo disk and rerun photo_backup interactively to confirm its UUID". It now says no USB drive is chosen yet and to run setup again to choose it.
- **One network connection is used without asking.** Setup asked which network links the PC to your router even when there was only one to choose, so you had to type 1. With one, it now uses it and says which; with several it asks as before. The same holds without a keyboard. (Your decision, after the VM installs.)
- **A skipped quality profile is named as the reason connecting the apps is unfinished.** Pressing Enter at Lidarr's profile questions, or at the quality profile for requests, skips them, as offered. The end screen then blamed "Prowlarr, the download client or the library folders". It now names the skipped profile and says to run setup again and choose one.
- **When setup stops at a question, its last screen says what to do.** It promises that each item "says how", yet said only "Drives: Drive confirmation failed" (the drive's name typed wrong three times), "Selection aborted", or, for Tailscale, "authorization pending; reapply interactively". Each now says what happened and to run `sudo bash install.sh` again.
- **The guides say where your files are, wherever you chose.** STORAGE.md said Nextcloud's files always go on the media drive, though setup offers the SSD too. It and the table above it named the SSD as the place for photos and music "by default", though setup offers whichever drive suits the server. RECOVERY.md's "Rebuild Immich from the USB drive" copied the photos back to the SSD only. Each now says where the files go for either choice.
- **Music on the media drive works.** Setup lets you keep the music on the media drive, and recommends it when that drive has more room. Three parts still looked for the music on the SSD: the library check, Lidarr's music folder, and the import of music you already have. So connecting the apps never finished ("Host library directory missing"), and imported music went where neither Navidrome nor Lidarr look. All three now use the drive you chose.
- **The USB drive question reads like the other drive questions.** It listed each drive as `/dev/sdc 32G ext4 UUID=… mounted=-`. It now shows size, name and model, as the SSD and HDD questions do.

### Installation
- **Installing git on a fresh Debian or Ubuntu works as the README says.** `sudo apt install git` fails there with "Unable to locate package git" until the package lists have been fetched once. The README now says `sudo apt update && sudo apt install git`.
- **Formatting a drive: the terminal command is given too.** When a drive has no filesystem yet (or one Linux apps can't use), setup suggested only the Disks app or KDE Partition Manager. A server often has no desktop, so it now also gives the command, for example `sudo mkfs.ext4 /dev/sdb`.
- **Setup's last screen no longer points at things that aren't there yet.** When setup stopped early (at unformatted drives, say), it still gave the dashboard's address and the command that shows the app passwords, though neither existed yet. Each is shown once its step has run.
- **`sudo bash install.sh --help` explains itself.** It began with "usage: installer.py", a file nobody runs, and called itself a "safe phase-one installer framework". It now names the command you typed, says that with no options it runs the guided setup, and explains every option.
- **Clearer messages on a PC NekoServer can't run on.** A Raspberry Pi or another ARM board was told only "An x86_64 systemd host is required". It now hears that NekoServer needs a 64-bit Intel or AMD PC, and which kind of machine this is. A Linux without systemd (WSL with systemd off, say) is told how to turn it on.

### Dashboard
- **After an upgrade was put back, its note says what to do.** It said only that the server ran the earlier release again. It now starts with "Nothing was lost", says to try the upgrade again when you're ready, and that the note goes away once one succeeds. (It takes effect from the next upgrade on a server running 0.47.7.)
- **The dashboard opens from a link on your phone.** Opened from the phone setup page's QR code, a link in a chat or a shortcut, it answered "E41: Open the dashboard from its own address", though it was its own address: the phone's browser marks such a visit as coming from another site. Opening the page itself is now allowed from a link, on the first visit and every later one; its readings and buttons still answer only to the page itself. Found on an emulated Android phone over Tailscale.
- **A new server says "Getting started", not "needs a look".** Home said "2 things need a look" on a new server with nothing wrong: its backups had not run yet, and the server check was not done. Those are now a note (for example "First backups due Sat 7 Nov, 04:15 · finish checking your server"), Home stays green, and the Backups tile says when the first run is due instead of "Not all checked yet" (once some backups have run, it says their copies are fine and how many are still to run). A backup that ran and failed, or a server check that found a problem, is still listed. (Your decision, after the VM installs.)
- **No more "This dashboard is not running" on the dashboard itself.** Since 0.46.3 the dashboard starts when someone opens it and rests in between. Its reading, taken while nobody had it open, called it stopped, so Home opened with an urgent warning about the very page showing it. A dashboard that will start when asked now counts as running.
- **A server never signed in to Tailscale is told so plainly.** On a new server, Home said, as urgent, that its Tailscale sign-in "ran out" and to sign in again. Now it says phone access is not set up yet and how to do it, as something to review. A server whose sign-in did run out still gets the urgent warning.
- **"All recent copies OK" when your backups are fine.** Home said "Backups: Not all checked yet", and listed "Weekly app updates has not succeeded yet", until the first weekly update night had run, which takes up to a week on a new server. With a backup switched off (the music backup, say), Home never said all was fine at all. Only the backups you use count now. A failed update night is still listed with the other things to look at.

### Tests
- **A test keeps paths to the photos, music and Nextcloud files from being written by hand.** Their folders are named once (shared.json) and every path is built from setup's choice of drive. The music-on-the-media-drive bug came from three places that built "SSD + library/music" themselves; the test fails if that happens again.
- **The acceptance tests no longer touch the server they run on.** They asked the real Docker, systemd and Tailscale, and could start one of the server's own services. On a server where Docker could not start, the test run took over half an hour. They now run the same everywhere, in about a minute.

## v0.47.6 — 2026-11-06

More fixes, found after 0.47.5.

### Fixed
- **A drive with no temperature sensor no longer shows "0 °C".** A virtual disk, and some USB and SAS drives, answer 0 for a temperature they do not measure; the Server health page showed it as a reading.
- **An upgrade that was put back no longer leaves a failed service.** Seen on the rig, with an upgrade cut off part-way (as by a power cut) and put right at the next start: the server was back on its release and passed its check, but the upgrade's service ended with an error. It stayed in the list of failed services, and the dashboard showed, as urgent, "NekoServer upgrade needs attention: Its service could not run (exit-code, exit 1)" in place of what had happened. The service now fails only when the upgrade needs you (putting the release back did not work either). The report says what was put right: "The upgrade to v0.48.3-beta was cut off before it was checked". Like the other upgrade fixes, this works once the server runs this release.
- **The Updates page says a job's state in plain words.** It showed the report's own ("Update night: unknown (No recorded successful run)" before the first update night, "last upgrade: pass"); it says "has not run yet", "passed", "incomplete" now, as the Backups page does. On the Backups page the update night, the daily check and the upgrade say "Last good run", not "Last good copy": they copy nothing.
- **The dashboard no longer says the NekoServer upgrade's schedule is off.** After an upgrade that worked, Home listed "NekoServer upgrade schedule is off: no new copies are made" for good, the Backups page said "Not all checked", and the upgrade finishing was celebrated as a backup copy. The upgrade runs when you choose it and has no schedule. 0.47.4 fixed this in the server's alerts; the page kept its own copy of the rule. The server now says which job has no schedule, and the page reads that.
- **The media apps can write to a library Docker made first.** Seen on the rig after the media drive was replaced: the apps started before setup ran on the new drive, so Docker made the folders it binds (`library/movies`, `tv`, `downloads`) itself, owned by root and closed to the apps. Setup made a folder only when it was missing, so every later run left them, and Radarr, Sonarr, qBittorrent and Lidarr could write nothing there. Setup now hands over such a folder (root's, as Docker makes it, with no file anywhere in it). A folder with files in it, or with another owner or access, is left as it is.
- **A release whose tag was written another way is offered.** 0.47.4 was published as `0.47.4-beta` (no `v`) and 0.47.0 as `v0.47.0-Beta`: neither matched the tag rule, so no server was offered them, and nothing said so. A tag without the `v`, and a beta written `Beta`, `beta`, `-Beta` or `-beta`, is read as the release it is. The dashboard's check and the upgrade read one rule now (`shared.json`), where each had its own copy. Only servers running this release or later read tags this way: until then, publish tags as `vX.Y.Z-beta`.

## v0.47.5 — 2026-11-05

More fixes from the test rig, found after 0.47.4. Each has a test that fails on 0.47.4.

### Fixed
- **The acceptance check passes Docker's drive guard on a newer systemd.** systemd 262 prints a unit name with an escaped dash in quotes (`"mnt-nekoserver\\x2dmedia.mount"`, the usual media drive's), so the guard never matched and the whole check said "failed". Both ways of printing are read now.
- **A failed upgrade no longer pushes out the release that worked.** Two releases are kept on the SSD: this one and the one before. The files of an upgrade that was put back counted as one of the two, so the earlier release that worked was removed and the failed one stayed. The two are now counted from the last release that passed its check, and the files of a release that was put back go when the earlier release is set up again (they stay if they hold settings no other kept release has). This works from this release on: an upgrade put back by 0.47.4 or earlier still leaves its files.
- **After an upgrade is put back, the dashboard says why.** Its alert read "NekoServer upgrade needs attention: This server runs 0.47.5 again, as before the upgrade, and passed its check", with the reason only in the log. The reason is said after it now.

## v0.47.4 — 2026-11-05

Fixes from trying the releases for real on the test rig (an Arch server with the apps running, taken from 0.41.3 through 0.46.2 and 0.46.3 to 0.47.3). All of them are still in 0.47.3. Each fix has a test that fails on 0.47.3.

### Fixed: setup
- **The home addresses' step works again on a server with Immich** (P1). The check of Immich's running containers read a list that 0.47.1 had removed. The error counted as a failed container, so `gateway_lan` failed with "not verified: Immich / immich-server, Immich / database, …" on every setup run and every upgrade, and the apps' home addresses stayed off. The list is in `shared.json` now, read by the snapshot's rules and this check alike. Since 0.47.1.

### Fixed: backups
- **The nightly app backup works with music on the media drive** (P1). qBittorrent's downloads folder (`library/downloads/music` on the media drive) was taken for "unmanaged writable container state", and the backup failed every night. Since 0.46.0.

### Fixed: the dashboard
- **No false "turned read-only" alerts for the SSD and the media drive** (P1). The collector runs with the system read-only to it, so Linux reported every drive as read-only in its view, and both drives showed a critical alert on every server with local drives. The drive's own state is read now. Since 0.46.0.
- **An upgrade that worked no longer shows "NekoServer upgrade needs attention".** The dashboard looked for the upgrade's schedule, found none (you start it yourself) and said "its schedule is gone, run setup again".

### Fixed: upgrades
- **An upgrade from 0.46.0, 0.46.1 or 0.46.2 no longer fails on a copy kept beside a unit file** (P1).
  - 0.46.3 taught its own check to skip the gateway's `.before-<time>` copy. But an upgrade is run and checked by the release the server has, so a 0.46.2 server with such a copy still failed its check, then failed to go back.
  - Setup now keeps a unit's copy in `/etc/nekoserver-backups/units-before/`, and moves the copies already in systemd's folder there. The older release's check then passes.
- **A release put back by an older upgrader no longer loses its dashboard** (P1). 0.46.2's record has no list of the units it found, so the newer release's `nekoserver-dashboard.socket` stayed, held the port, and the 0.46.2 dashboard restarted for ever with "Address already in use". Going back from such a record now removes the units a newer release is known to have added.
- **A release you set up by hand after a failed upgrade stays** (P1). The upgrade's record stayed open, so app updates and the monthly check waited, and at the next start the resume service put the old release back over the new one. Setup run by hand now closes the record and ends the upgrade's report when the server passes the upgrade's check (every step that worked before, the dashboard and every service on this release), and the resume service does the same before it puts anything back.

### Fixed: smaller things
- The question where an app's files go calls the media drive "the most room" only when it has more free space than the SSD.
- A test of the dashboard's port no longer reads this PC's own dashboard (it failed on a server whose dashboard uses port 8788).

## v0.47.3 — 2026-11-05

A stabilization release: fixes from the owner's review of 0.47.2, and a sweep for more of the same kind. Each fix has a test that fails on 0.47.2.

### Fixed: upgrades
- **An upgrade sets up only what this server has.** Every upgrade, and the way back, ran setup with the `full` profile, so an installation set up with a smaller profile gained Immich, Nextcloud and the rest. Setup now records the profile it runs with, and the upgrade runs the same one. For a server set up before 0.47.3, it uses the smallest profile that covers every step already done.
- **An upgrade no longer leaves gigabytes behind.** Each upgrade's download, the release unpacked from it and its build folder stayed in `installer/upgrades` for good. Once an upgrade has finished, whether it succeeded or was put back, they are removed; the ten newest logs stay. The release it set up keeps its own copy, as before.
- **Putting a release back stops before the older setup** when a service of the newer release did not stop. Before, it ran the older setup first and only then said so. Now it changes nothing more, says which services to stop, and `sudo python3 upgrade.py resume` finishes it.

### Fixed: moving a drive
- **A move never copies another Btrfs subvolume than the settings name.** It took whichever subvolume was mounted, even when the settings named another one. Now it stops and says which one to mount. When the settings name none, it notes the mounted one, as before.
- **A move never unmounts what someone else mounted.** It refused a staging folder that was already in use, then its clean-up unmounted that folder anyway. Only the move's own new drive is unmounted now.
- **A drive move record that can't be read is not reported as done.** `migrate.py resume` said all was well and left the record, which still kept upgrades and setup waiting. It now says the record can't be read and what to check.

### Fixed: restoring a monthly snapshot
- **A staged copy is marked checked only if it came from the archive that was checked.** The archives were checked by checksum, then read again by name for unpacking and for the comparison. An archive changed in between was unpacked, compared with itself, and the copy marked as checked. Each archive is now copied once into a private file, checked by checksum as it is copied, and everything after reads only that copy, which is then removed. Loading the saved app versions (`--load-images`) does the same. Unpacking needs room for one archive's copy more.

### Fixed: the Immich snapshot (recovery menu)
- **Immich is not left off by an earlier stop that was still on its way.** When a snapshot's stop of Immich timed out, Docker could still finish it after the restart, and the restart had already cleared its record, so nothing started Immich again. If a recorded service is still running when the restart begins, it now waits longer than the stop's own limit, starts everything again and checks once more before the record goes.
- **A snapshot checks the drives before it starts Immich from an earlier record.** With a record left from a snapshot that was cut off, it started Immich first and only then rejected a wrong drive.

### Fixed: drives and the acceptance check
- **No backup result is read from a drive the acceptance check refused.** The check could reject the SSD and still pass the backup reports on it. It now asks for the backups' states only once both drives passed the drive check. The runtime's `job-states` also counts a drive only when it passes the check every job makes.
- **Moving data to the other drive makes earlier acceptance runs outdated.** Moving the photos from the SSD to the HDD (or the music, or Nextcloud's files) changed neither of the acceptance check's fingerprints, so a restore witnessed before the move still passed. Both now include where the data is, and the USB copy's NAS disk. A server that never moved anything keeps its fingerprints and its witnessed tests.
- **The dashboard reads nothing from a drive every job refuses.** It judged the SSD and HDD by UUID and filesystem only, so a drive on the wrong Btrfs subvolume, or a copied NAS disk, showed its backup reports as current. Such a drive now shows as the wrong drive.
- **The acceptance check verifies the USB backup drive as the weekly copy does,** including its NAS disk. The UUID alone passed a restored or copied NAS disk mounted in its place.
- **A new USB drive inherits no checksum result from the old one.** After you switch to a new USB drive, the dashboard no longer shows the old drive's "every copy matched", and the new drive's weekly copies no longer carry the old full check's date forward.

### Not changed
- The acceptance check keeps two fingerprints of the settings: one decides whether earlier witnessed tests carry over, the other whether the dashboard shows the run as outdated. Each lists the settings it covers, and the two lists differ. Merging them would reset every witnessed test once, so it is left for the owner to decide.
- Setup's estimate of the room the weekly USB copy needs still follows the copy's rules by itself (see 0.47.2).

## v0.47.2 — 2026-11-05

A stabilization release. It has three parts: the owner's review of 0.47.1's dashboard (the cats, the theme switch), the owner's cat placement package, and another sweep for rules written twice. Each fix has a test that fails on 0.47.1.

### Fixed: the dashboard
- **Every page has its cat where it belongs** (the owner's cat placement package):
  - Each page shows its own cat beside its main section, and Home no longer repeats the cats of the panels it shares with Your apps and Needs attention.
  - Server health's grooming cat moved from the drives to "Checks & monitoring". Updates (grooming) and Help (curious) have cats now.
  - Needs attention is calm when its list is empty, and worried only beside real issues (then on Home too).
  - The cats have larger boxes, with room for the pounce and its yarn.
  - Listening, breeze and grooming now have their full animations too. An animation that can't be loaded shows the same cat's other size before its still picture.
- **A page's cat is the page's own, not a verdict on it.** Before, Fans & lights showed the curious cat whenever the fan service couldn't be reached, so its breeze cat never appeared in the preview. A problem now outlines the page's panel, and its own words say what is wrong.
- **The light/dark switch works in the live preview.** The page that hosts the preview keeps its own `data-theme` on the root, which undid every click. The switch now uses its own name (`data-neko-theme`) and sets the colour scheme on the root itself.
- **The apps' names, descriptions and ports come from the one shared list** (`shared.json`, through the readings). The page kept a copy that already differed from setup's wording. The gateway's Tailscale ports are shared too.
- **The app portal on your home network** uses the dashboard's colours and follows the device's light or dark setting (it kept the colours of before 0.47.0).
- **New README recording** of the dashboard, with each cat where it belongs.

### Fixed: rules written twice
- **The acceptance check asks the runtime whether the drives are the right ones** (`check-drives`, the check every job makes). It compared only the UUID and filesystem, so it passed a drive on the wrong Btrfs subvolume, a copied NAS disk, or one with a copy connected, all of which every backup refuses.
- **The acceptance check reads the backups', the outage monitor's and the phone alerts' states from the runtime** (new command `job-states`, the dashboard's own rules). Its copy of those rules passed a backup whose worker systemd couldn't start, or a run cut off part-way, and gave the outage monitor no grace after a restart. The copy is gone.
- **An acceptance run of the `accounts` profile shows on the dashboard.** The collector kept its own list of profiles, without it. Setup now writes the profiles into the dashboard's settings. The collector also no longer works out the settings fingerprint itself: it compares the one setup wrote.
- A copy of the dashboard's acceptance summary in Python, used only by tests, is gone.
- install.sh spells out setup's lock and the monthly journal itself, since it runs before Python may be there. A test now checks them against `shared.json`.

### Corrected
- The 0.47.1 notes said a drive move now stops the gateway too. It already did (it stops every running NekoServer service). That line has been corrected.

### Known, not changed
- Setup's estimate of the room the weekly USB copy needs still follows the copy's rules by itself (`modules/capacity.py`). It only warns, with 20% to spare. Moving it into the runtime means changing the backup copy itself, so it is left for a release of its own.

## v0.47.1 — 2026-11-05

A stabilization release: fixes from a review of 0.47.0, and a sweep for rules written twice. Setup (Python) decides once and writes it down; the runtime (Rust) reads it and checks it; a fact both use is in `runtime/src/shared.json`. Where two copies of a rule already disagreed, that was a bug, and it is fixed here. Each fix has a test that fails on 0.47.0.

### Fixed
- **A USB backup disk on a NAS is checked where it is mounted.** The WWN check looked at a disk by its UUID, but two LUNs can share a UUID. The check now looks at the device that is mounted (or about to be), and both the recovery menu and the weekly copy mount exactly the device they checked. A NAS disk that shows no WWN is refused.
- **While the NAS check holds the apps, nothing else starts them.** An update, the hourly retry or "Update now" could start Docker on the drive that was failing. Docker, Samba and the gateway now refuse to start while the hold is on (for an app drive on the NAS). The check lifts the hold just before it starts them, and puts it back if Docker doesn't start.
- **A broken gateway no longer keeps the apps held for good.** When a drive came back, one service that failed stopped the rest and skipped every resume job. Every service is tried now, and the resume jobs run whenever Docker is up.
- **The NAS check is no longer cut off part-way** (it had one overall time limit; each step has its own now). A check stuck on its test write shows the drive as waiting.
- **A NAS hold is one alert, not one per app.** Each app the hold stopped raised "needs attention, choose Restart app" (on the page and on your phone), advice that couldn't help since the restart is refused, and the hold itself had no alert. Now one urgent alert says the apps are held and names them; the collector decides which apps are held, and the page shows its word.
- **App snapshots no longer refuse a file with setuid/setgid bits.** The bits are not recorded, and a restore clears them instead of refusing the whole snapshot.
- **A cut-off Immich snapshot is finished at boot even when another job is running** (it waits up to 3 hours for the lock instead of giving up).
- **Putting a release back switches off a newer release's template services** (`name@.service`) and checks that none of their instances still run.
- **Systems with dnf4 (older Fedora and RHEL-like):** an update whose long package name wraps onto its own line is counted.
- **A drive move reads the list of services that write to the drives from the shared facts** (no behaviour change: the gateway, the one service its own list lacked, was already stopped with the other NekoServer services; this line said otherwise until 0.47.2).
- The restore report's folder is flushed to disk after the report is put in place.

### Changed: one copy of each rule
- **The drive check is in the runtime only** (`check-drives`): which drive is mounted, that no copy of it is connected, and that the SSD and HDD are two filesystems. Setup asks it. Setup's own copy and `storage-guard.py` are gone; they already disagreed with it (they took a RAID member for a copy).
- **The snapshot's Compose rules are in the runtime only** (`check-compose --upload --database`). Setup asks it. Its own copy refused `privileged: false`, which the update itself installs.
- **The media check is in the runtime only.** The acceptance check runs it and reads its report; `media-health.py`, which already disagreed (a Prowlarr link, a root folder), is gone.
- **The runtime reads the folders setup wrote down** instead of building them again from the drive and a fixed name, so a folder you moved is checked where it is.
- **When readings count as late is decided once**, by the program that collects them (two intervals plus 30 seconds, never under 3 minutes). The dashboard, the acceptance check and the phone alerts had three rules.
- **One list of apps** (names, ports, which are media apps), in `shared.json`, read by setup and the runtime; there were ten copies. Also shared now: the recovery records (journals) of each job and their resume services, setup's lock, and the NAS check's service and timer names.

## v0.47.0 — 2026-11-05

A feature release: the dashboard's new look, **Moonlit Neko**.

### New: the cat
- **Neko, a small black cat, sits at the top of Home** and shows how the server is doing. It is afraid when something needs a look, curious when the readings are not current, playful while a backup runs, happy for a moment when one has just finished, and resting when all is well. The words beside it say the same thing and are what counts; the cat never decides anything (DASHBOARD.md, "The cat").
- **Every page has its own animated cat:** sitting on Home, peeking into Your apps, sleeping between backups, lying back on Hardware, listening on Sensors, enjoying a fan's breeze on Fans & lights, pouncing at yarn on Processes, grooming on Server health, and looking over Needs attention. When something on a page is wrong, its cat is afraid instead; a problem on one page never scares the others, and unreadable or old readings make a cat curious.
- **Less motion:** every cat is a still picture when the device asks for less motion or Settings → *Cat animation* is *Still pictures*. The page cats use small animations (about 60 KB each), so a page loads little.
- A finished job is celebrated once, when the page sees its last good copy move on; opening the page never replays an old one, and a warning always wins.

### Changed
- **The night palette is warmer** (plum-charcoal), and the light theme is now **Lamplight**: soft parchment with no bright white, so switching to it never dazzles. The setup window uses the same colours.
- **A night/Lamplight switch at the top right** (☾ / ☀). The page crossfades into the other theme; with less motion asked for, it simply switches. The choice is kept in the browser, like the other settings.
- **One set of colours for the dashboard and the setup window** (`dashboard/theme.css`, each colour written once for both themes), so the two can't drift apart.
- **The dashboard loads less:** the page's files carry a fingerprint, so the browser keeps them and asks only whether they changed; a page loads only its own cat; offline, the still cats show.
- **Every text colour is easy to read in both themes:** 5:1 or better against every surface it can sit on, tinted badges included (the common standard, WCAG AA, asks for 4.5:1). Lamplight's accents keep the night's hues, made darker, so lavender, green, amber and red mean the same in both.
- **A moonlit Home:** a faint night sky behind the page, and a glow behind the cat in the colour of its mood (lavender at rest, mint while a job runs, peach on success, amber or red on a problem). A new mood fades in; nothing moves when motion is turned down. The four summary facts are a compact line instead of four boxes.
- **New logo and app icons:** Neko peeking in front of a full moon, in the menu, the browser tab and on a phone's home screen (they replace the pink cat face).
- **New README recordings** of the dashboard and the setup window. Both were made from the real programs: the dashboard program serving example readings (the hardware readings are those of the test machine; Fans & lights is not in it, as there was no fan hardware to control), and the setup window with its real steps while setup's progress was replayed (no real installation ran).

## v0.46.4 — 2026-11-05

A stabilization release: fixes only, from the project owner's reviews of v0.46.3-beta. Each fix has a test that fails on 0.46.3.

### Fixed
- **Nextcloud's trusted domains are never overwritten after a silent failure.** A read that was killed (exit 137, or any other failure that printed nothing) was taken for "not set", so the first trusted domain could still be replaced. Only occ's own "not set" answer (exit 1 with no output) counts as empty now; anything else stops the step and changes nothing.
- **The apps on a NAS drive are started again when Docker fails the first time.** After a NAS outage the check lifted its hold even when Docker (or Samba, or the gateway) did not start, so nothing tried again. The hold now stays until every service that was switched on runs, and the check tries again every minute.
- **A USB backup drive that is already mounted is checked to be the NAS disk setup chose.** The WWN check ran only before mounting, so a copy of the LUN mounted there before was taken as it was.
- **Debian 13 servers are told when they need a restart for a new kernel.** Debian 13 names its kernels like `6.12.48+deb13-amd64`, and the version check didn't read that form.
- **Putting a release back no longer reports success while a newer service still runs.** A service the newer release added was switched off without checking that it stopped, and its file was removed, so the check after it couldn't see it. A service that doesn't stop now keeps its file, and the way back says which one to look at.
- **Two restore tests started at the same time no longer break each other.** The clearing of a left test's network ran before the recovery lock, so a second test could remove the first one's new network and containers before they started. It now runs only while the test holds the lock.
- **Repairing the dashboard keeps its port while the dashboard is stopped.** systemd's socket keeps holding the port then, and setup took that for another program, moved the dashboard to a spare port, or failed when none was free.
- **A NAS backup disk's identity is saved in your settings as soon as setup learns it.** The disk's WWN was kept only in memory and in the runtime's drives file, so a later setup could take a replacement disk for its first look at the drive. It is now written to `my-config.json` right away.
- **A restore stays tied to its folder until it has finished.** The folder opened at the start was closed before the report, the owners and the times were written. A folder put at the same path then got them, along with a report saying everything was restored and verified. All three now go through the folder opened at the start, and the restore stops if the path no longer leads there.

## v0.46.3 — 2026-11-04

A stabilization release: fixes only, from a second audit of 0.46.2. It had four separate reviews: 0.46.2's own changes, the dashboard and control service, installing and upgrading, and the apps and restores. Each fix has a test that fails on 0.46.2.

### Fixed: the dashboard
- **No other program can take the dashboard's port and catch the PIN** (P1).
  - The dashboard bound its port itself, after waiting up to three minutes for its first readings at start-up. Any account on the PC could take `127.0.0.1:8787` first (a `@reboot` job, say), serve a copy of the page to the phones and read the PIN they type.
  - systemd now holds the port from the start of the computer (`nekoserver-dashboard.socket`) and hands it to the dashboard. The dashboard also checks that it got the right port.
- **Close app and Force close never stop a program started with sudo from the app.** Setup or a system update run with sudo in a terminal sits in the terminal's group, and Force close killed it. Only the desktop user's own processes are closed now.
- **Slow connections hold the dashboard less.** A request's 3 seconds now count from when it was accepted, so one that waited in the queue is closed at once instead of taking a worker for 3 seconds more.
- **The Processes page lists a process whose name has a byte that isn't UTF-8.** It was left out before.
- **An upgrade from a `-beta` tag shows as running** while it runs.

### Fixed: upgrades
- **A copy kept beside a unit file no longer fails every upgrade.** The gateway keeps a `.before-<time>` copy of a unit it rewrites. That copy named an old release, so the check after an upgrade failed, and so did the check after putting the release back. Only real unit files are looked at now.
- **Putting a release back removes the units the newer one added.** The older setup rewrites only its own units, so a new one kept running the newer release's program.
- **An upgrade waits for a pending restart.** When a system update waits for a restart, setup stops at once, and so does the old release's setup. The upgrade then failed, and then failed to go back. It now says to restart first and changes nothing.
- The upgrade's description now says what it checks: the release's version, from this project's GitHub tag, over HTTPS. Releases are not signed yet.

### Fixed: apps and restores
- **A Nextcloud setting that can't be read is never overwritten.** If occ timed out, or printed a PHP warning before its answer, the list of trusted domains was taken as empty, and its first entry (`localhost`, or the other gateway's address) was replaced. Only "not set" counts as empty now; any other failure stops the step.
- **A restore test left behind no longer blocks the next one on its port.** A failed or stopped test kept its network, and Docker refused the next test's network of the same name. A left network is now removed, with its stopped containers, before the next test, and the advice says `down`.
- **A restore test's own network is never offered as the home network**, neither in setup's choice of network nor at start-up.
- The Nextcloud fallback install's comment now says that its passwords are briefly in its process's arguments, as with the image's own first start.

### Fixed: drives on a NAS (0.46.2's own changes)
- **With only the backup drive on the NAS, the apps start again after a repair.** The menu left the start to the NAS check, which has no drive to look at in that case, so its hold was never lifted. The menu now starts the apps itself then, and the check lifts a hold left from before.
- **Drive-return doesn't start the apps on a NAS drive that fails writes.** It holds them as the NAS check would, and that check starts them once the drive takes writes. A drive that is only waiting for the NAS is no reason to wait.
- **Quitting the recovery menu doesn't wait for a stuck test write.**
- **The recovery menu checks the backup drive's NAS disk identity (WWN) too**, as the weekly copy does.
- **A busy USB drive (the weekly copy) never hides the HDD's password lists.**
- The NAS check no longer claims to keep a failed start "for the dashboard": the failure is reported in the check's log.

### Fixed: Linux families
- **A Fedora derivative that also names `rhel` (Nobara, say) is not taken for RHEL**, whose repositories have no release of its number. It is refused as not supported.

### Tried for real
- **The dashboard with its port handed over by systemd:** run as an ordinary user under `systemd-socket-activate`, it answered on the handed port. Handed another port than its settings name, it refused to start.
- Everything else is covered by tests only (1319 Python, 449 Rust).

### Not changed
- **Releases are not signed.** Whoever can push a tag to the project's GitHub can publish a release. Signed checksums would need a signing key and a step in the release process.
- **Slow connections:** a few hundred slow local connections can still keep the dashboard busy. Each one now holds a worker for much less time.

## v0.46.2 — 2026-11-04

A stabilization release: fixes only. They come from a new audit of 0.46.1 (four separate reviews: the NAS code, the Compose checks, backups and restores, drive moves and setup) and the project owner's two reviews of `v0.46.1-beta`. Each fix has a test that fails on 0.46.1.

### Fixed: Compose files can't reach further than the app
- **Networks are checked** (P1). The check looked at a model's networks only as a key name. These passed:
  - an external network named `host`, which shares the PC's network and makes the loopback-only ports meaningless
  - a network bridged straight onto the home network (`macvlan`)
  - another app's private network, which reaches its database
  - a container name or alias on the shared network that answers for another app (seerr calling itself `sonarr`)

  Now a model may use only its project's own network, made by Compose with no driver, options or addresses of its own. The media apps may also use the shared `nekoserver-media` network, but with no name or alias of their own there.
- **Only the download apps see the whole media library** (P1). Every app may bind `library` before; now only Radarr, Sonarr and qBittorrent may. Jellyfin keeps its movies and TV folders, read-only.
- **A bind's own options** are limited to `create_host_path` and `selinux`. Propagation could carry mounts out of the container.
- **An Immich update can't install a file its snapshot refuses.** The update now also checks what a snapshot needs:
  - the photos and the database bound where Immich looks for them
  - no other user
  - Immich's server using its own database and cache

  The snapshot check refuses unsafe security options, as the update does. Both checks take a cache volume for Redis.
- **The existing-volume check runs on every path:** also when a waiting update is installed, and on the fallback to the current Immich release.

### Fixed: drives on a NAS
- **A Btrfs NAS drive keeps its network mount options** when its default subvolume is pinned. The second fstab write left them out, so at the next start the drive was mounted before the network and the login.
- **A failed start after the NAS returns no longer keeps the apps held.** One app that wouldn't start left "held" set, so every minute everything was restarted again. Once the drives check out, the hold ends; what failed is said and kept for the dashboard.
- **Drive-return starts the apps only when every NAS drive takes writes**, not only when the NAS check holds them.
- **The recovery menu and the NAS check take turns.** The menu's note that the apps should start again could be written over by a check already at work, and the apps stayed stopped. The menu now says when the check refused, instead of always "started again".
- **The menu's test write gives up after 20 s** with a message when the NAS isn't answering, instead of freezing.
- **CHAP logins saved by 0.46.0 are noted**, so a login lost later is noticed. Settings from 0.46.0 didn't say which target had one.
- **A NAS holding only the backup drive gets the minute check**, and a target without a session is logged in to again, also the backup drive's while the media drive is fine.
- **The backup drive on a NAS is checked by its NAS disk identity (WWN)**, like the SSD and the HDD. Setup notes it; the weekly copy refuses a restored or cloned LUN with the same UUID.
- **A drive repair cut off is never called a success.** e2fsck killed part-way returns a negative code, which passed as "repaired"; the drive was mounted again and the apps started. Only 0, 1 and 2 count now.
- **The repair checks it has the drive the settings name** before stopping anything: with another drive mounted at the folder (or settings out of date), it unmounted one filesystem and checked another.
- STORAGE.md says the read-only protection and the repair are for ext4.

### Fixed: backups and recovery
- **The recovery menu offers every password list, newest first:** the monthly snapshots', the ones setup makes, and the USB drive's copies when it is plugged in. It looked only at the monthly snapshots, so a newer list from setup (and the only one before the first monthly snapshot) was never offered, and the USB drive was looked at only when the HDD had none.
- **A password list dated in the future (a wrong clock) is never the newest for good**, and one cut off before its manifest no longer takes the place of a good one.
- **Photos on the media drive:** setup and STORAGE.md say that their weekly archive copy is on the same drive, so the USB backup is the only copy on another drive.
- STORAGE.md says a missed week is caught up when the PC starts again, as the timers do. It said the opposite.

### Fixed: drive moves and setup
- **A move offers only drives setup would take.** An encrypted drive opened by hand (not in `/etc/crypttab`) or a loop device was offered. After the switch, setup refused it, and the move could go neither on nor back.
- **Setup never offers the USB backup drive as the SSD or the HDD.**
- **A server set up before 0.46.0, updated with no one at the keyboard, keeps its layout.** An empty music or photo folder was quietly put on the media drive.
- A cut-off `back` is told to go back again, not to start a move, and STORAGE.md says when a move cut off during its switch starts over.

### Fixed: the System tab
- **Debian shows a restart is needed when a newer kernel waits.** Debian keeps the running kernel's modules when a newer kernel is installed, and writes `/run/reboot-required` only with update-notifier, so "no restart required" was shown. A newer installed kernel of the same kind now counts.

### Fixed: releases
- **A beta tagged `v0.46.1-beta`** (as 0.46.1 was) is read as a beta by the updates page, the upgrade and the release-channel check. The updater accepted only `v0.46.1Beta`, and the check failed, leaving that beta marked as stable on GitHub. Both spellings are betas now (README.md, Release channels).

### Tried for real
- NekoServer's own Compose files pass the new checks, each run through `docker compose config` and the runtime's `check-compose`: the eight media apps, Nextcloud's, and Immich's own release file.
- The rest is covered by tests only. The NAS lab of 0.46.1 still stands; it was not run again for this release.

### Not changed
- `migrate.py back` still refuses while a move's record is unfinished. The move that could get stuck that way (a drive setup refuses) is no longer offered.

## v0.46.1 — 2026-11-04

A stabilization release: fixes only, no new features. It follows the four rules for NAS support:
- a list of what is tested, experimental or not supported (the new **SUPPORT.md**)
- the same guarantees for every kind of drive: the right drive, ready before the apps, and a way back after a fault
- interruption tests kept for good (the new **lab/nas_lab.py**)
- releases like this one, made only of fixes

Setup now also refuses what it can't manage safely, instead of guessing.

The fixes come from the project owner's reviews of 0.46.0 (H01 to H08, then three more lists), each with a test that fails on 0.46.0.

### How the work is split now
- **Setup decides once and writes the decision down; the runtime only checks it against reality.** Setup writes the folders it chose for each app (`paths` in the drives file). The runtime reads them and never works them out again, so the two can't disagree.
- **The facts both use are in one file**, `runtime/src/shared.json`: where each app's files go by default, the NAS check's files, the services that write to the drives, and the Compose keys an app may use. The runtime builds it in; setup reads it.
- **The Compose rules live once, in the runtime.** Setup's first Immich install asks the runtime (`check-compose`) instead of keeping its own copy of the rules.

### Fixed: drives on a NAS
- **A CHAP login is never lost** (H05, P1). Each setup run discovered the targets again in a way that rewrote their saved records, CHAP login included. Discovery now only lists them. Setup reads the login back after saving it and checks at every run that it is still there. If it is gone, setup asks for it again, or stops with a message when no one is there to type it.
- **CHAP logins work for records named `host,port`.** Setup didn't find a record of that form, so it couldn't save the login to it.
- **A wrong CHAP password is named.** The NAS refusing the login gets "the NAS refused the login", and setup asks for the CHAP login once more.
- **After Linux is installed again, the NAS still knows this PC.** Setup keeps this PC's iSCSI name in the settings (`network_storage.initiator`) and puts it back, so a new Linux doesn't get a name the NAS refuses. The host backups keep the name too. The CHAP password is never in a backup: setup asks for it again.
- **A setting that didn't stick stops setup** (H01). Logging in at every start, and the 10-minute wait: each update's result was ignored. Now each one is checked and read back.
- **A chosen target the NAS no longer lists stops setup** (H02). Before, it was silently dropped from the settings.
- **No network wait means no NAS** (H03). The start-up must wait for the network before it logs in. When this PC has no network manager that can say the network is up, or the wait can't be switched on, setup refuses. Before, it warned, and the drives went missing after each restart.
- **A NAS that starts after the PC is logged in to again.** The minute check sees the NAS answer and logs in, and the drive-return service mounts the drives and starts the apps. Before, the drive stayed missing until the next restart.
- **The minute check retries a stop that failed** (H06). A refused stop was recorded as done and never tried again. While the apps are held, any that started again is stopped again.
- **The hold lasts until every NAS drive takes writes.** Plugging the SSD in again (drive-return) started the apps while a NAS drive still didn't take writes. A drive that was gone or waiting while held let a restarted app carry on.
- **A drive with filesystem errors is shown.** ext4 counts its errors; the dashboard reads the count, alerts on it, and calls the drive recovered only once it is back to zero (after a check).
- **The drive repair waits for other work** (H07). "Check a drive that turned read-only" stopped the apps and unmounted the drive with no look at a drive move, an upgrade, an app update or a backup at work. It now holds the operation lock and checks for running NekoServer jobs first.
- **The runtime checks the NAS disk's identity (WWN)**, as setup does. A restored or cloned LUN carrying the same filesystem UUID is refused by the backups, Docker's start check and drive-return.

### Fixed: backups and recovery
- **The weekly USB copy takes Nextcloud's files from where setup put them** (H04, P1). With Nextcloud's files on the SSD, the USB copy refused the folder.
- **The Immich rebuild uses the library Immich really uses** (H08). RECOVERY.md always copied the photos into the SSD's library. The restore menu now ends with the exact command for your server, and the guide gives both folders.
- **The newest password list is the one copied to the USB drive** (P1). Setup named its lists in seconds and the monthly snapshots in microseconds, so an older monthly list outranked setup's newer one. The USB drive's retention could then remove the new copy at once. Lists are compared as times now, and setup's new ones are named in microseconds. The recovery menu sorts and dates them the same way.
- **A damaged password list is made again.** Setup called a list "current" when the passwords hadn't changed, without checking its files. Every weekly USB copy was then incomplete.
- **The weekly USB copy is switched on again with its app.** When no app was left to copy, the copy was switched off. Setting the app up again didn't switch it back on, and a new day or time was never installed.
- **A drive move recovered after a restart never switches to an out-of-date copy on a guess** (P1). A move record without the boot and the time (written before 0.45.8), an unreadable boot ID, or systemd not saying when a service last started now count as "the apps may have run": the move starts over. Before, they counted as "nothing ran".
- **A drive move never offers the USB backup drive, nor a NAS disk for the SSD.** Setup refuses both. The trouble showed only after the copy and the switch.

### Fixed: Compose files
- **More ways to get access are refused** (P1). The checks refused named keys, so new ones passed: `use_api_socket` (Docker's own socket), `post_start`/`pre_stop` hooks running with every right, and `secrets` or `configs` read from this PC's files. Both checks now take only the keys NekoServer knows (`shared.json`), so a key Docker adds later is refused until it is reviewed. The restore-test conversion gets only checked files.
- **A named volume is the app's own.** Any volume name passed, and Compose uses an existing volume of that name as it is, with whatever storage or files it has. The name must now be the app's own (`neko-<app>_<key>`), and an existing volume must be the one Compose made for that app, on Docker's own storage.

### Fixed: setup
- **An app's files on both drives stop setup.** Setup took the SSD's folder without a word. It now asks you to say which one the app uses.
- **The preserved release copy keeps `runtime/src/shared.json`.** Without it, a reinstall from the copy kept on the SSD would not start.

### Documentation
- **SUPPORT.md** says what is tested, experimental or not supported. Setup says "experimental" when the NAS switch is on.
- STORAGE.md, MUSIC.md and NEXTCLOUD.md describe the chosen locations instead of "on the SSD". With music on the media drive, the optional music backup is on the same drive: it keeps older versions, not a copy safe from that drive (or its NAS).
- STORAGE.md's NAS section covers the new refusals, the retries, the error count and reconnecting after Linux is installed again.
- **The front page** (README.md) mentions NAS drives and SUPPORT.md, and its drive table follows the chosen locations. Its pictures show smaller (720 px wide; the overview 480 px), and the dashboard, setup-window and overview images are 20–60% lighter (429, 396 and 108 KB). The two terminal recordings stay as they are: re-encoding saved under 3%.

### Tried for real
The new lab (`sudo python3 lab/nas_lab.py run`) builds an Ubuntu 24.04 VM with the real open-iscsi, and a software iSCSI target stands in for the NAS. NekoServer's own NAS step, drive step and units set the VM up. A stand-in service plays the apps' writer. The lab ran in QEMU without KVM, so slowly.
- **All six scenarios passed** in one clean run, with this release's runtime:
  - **A restart:** the drive was mounted after the network and the login, the writer started, and the waits setup sets were in place again (120 s, 600 s).
  - **A 30-second network cut and a 200-second network cut:** no write failed; the longest waited 204 s.
  - **A NAS power cut:** no write failed.
  - **The NAS switched on four minutes after the PC:** the drive was missing at start. The minute check logged in again once the NAS answered, and drive-return mounted the drive (passing the new WWN check) and started the writer.
  - **An outage longer than the waits:** the minute check stopped the writer and held it, the recovery menu repaired the drive, and the writer started again.
- **CHAP** against a real login (wrong password refused, right one accepted, kept when setup runs again), in the VM, earlier in this release's work.
- **The lab itself had bugs, now fixed:** it put the kernel modules in the wrong folder (no 9p share in the VM), chose disks by `sdX` names (they change between starts; now by label), kept one run's state for the next (each run now starts from a clean disk), and asked for KVM where there was none.

Not tried: a real EX4100, Docker apps on a NAS drive (the lab VM has no Docker), and the password-list and USB-schedule fixes outside their tests.

## v0.46.0 — 2026-11-04

Two new things, built for a NAS-based server (a WD My Cloud EX4100 with two mirrored pools, an i3 laptop running NekoServer, a 250 GB SSD on the laptop by USB):
- **The media drive and the backup drive can be on a network storage box (NAS) over iSCSI.**
- **Setup asks where each app keeps your files**, and every backup follows the choice.

**Tried for real**, in a lab VM (Ubuntu 24.04 with the real open-iscsi), against a software iSCSI target standing in for the NAS:
- Setup's NAS step logged in to two targets and listed both LUNs by their stable names. It refused to format them and gave the commands instead.
- The drive step mounted the NAS LUN with the network options and never offered it for the SSD.
- After a restart, the LUN came back by itself: network, then login, then mount.
- A 30-second outage and a 30-second target power cut only paused the writes.
- With Linux's default waits, a 200-second outage aborted ext4's journal. With the waits setup now sets, the same outage only paused the writes.
- A longer outage, with the waits shortened for the test: the new check stopped the apps once the drive stopped taking writes. The recovery menu's new check repaired the drive, and the apps started again.

Not tried: a real EX4100, Docker apps on a NAS drive (the lab VM has no Docker), and CHAP logins (the lab target had none). The setup window shows these questions the way it shows every setup question; that part wasn't tried in a browser.

### New: drives on a NAS
- **A switch on the setup window's first screen:** *My media and backup drives are in a network storage box (NAS)*. Off by default.
- **The new step "Drives on the network storage box":**
  - Installs the iSCSI tools (open-iscsi; iscsi-initiator-utils on Fedora and RHEL).
  - Shows this PC's initiator name, to allow on the NAS.
  - Asks for the NAS's address, checks that it answers, and lists its targets for you to choose.
  - Asks for a CHAP login if the NAS needs one. The password is written in the target's own settings, readable by root only, never on a command line or in `my-config.json`.
  - Logs in, again at every start, at the address you gave, even when the NAS lists itself at another (found in the lab: the login failed otherwise).
  - Lists each LUN with its size, filesystem and identity (WWN).
- **Never formats.** An empty LUN gets the exact `mkfs.ext4` command, with a label of its own, to check and run yourself.
- **The drive step** marks NAS disks *on the network storage box*, never offers them for the SSD, and keeps their identity (WWN), so a different disk with the same filesystem ID is refused. They are mounted once the network is up and this PC is logged in (`_netdev`, and the login service required). An ext4 one turns read-only on write errors rather than carry on half-written.
- **Longer waits for the NAS.** Setup raises iSCSI's wait to 10 minutes, and the disk driver's to 120 s per try (a udev rule for iSCSI disks only), so a NAS away for up to ten minutes only pauses reads and writes. Linux's defaults give up after two to three minutes, and ext4 then aborted its journal (seen in the lab).
- **A check every minute (`nekoserver-nas-watch`)** tries a small write on each NAS drive. Linux still shows an aborted ext4 as mounted "rw" (statvfs says so too), so only a write tells.
  - When a drive stops taking writes, it stops the apps that write there (Docker, the shared folders, the home gateway), once.
  - It starts them again only when every NAS drive takes writes, through the same checks as when a drive comes back.
  - A NAS that is only slow to answer gets no action: its writes wait.
- **The recovery menu's new "Check a drive that turned read-only":** it stops the apps and unmounts the drive (never by force). It checks and repairs the filesystem with your yes (`e2fsck`; anything it won't fix on its own is left to you), mounts the drive again, makes sure it takes writes, and starts the apps.
- **The dashboard** shows the connection to the NAS, a drive that turned read-only or waits for the NAS, and "health watched by the NAS" instead of SMART, which this PC can't read through iSCSI. A NAS alert recovers only once Linux says the session is logged in again, and a read-only drive is never "back". A NAS connection no longer used is dropped quietly.
- **The guide** (STORAGE.md, "Drives on a network storage box") covers the EX4100's own steps: a target with a LUN on each pool, allowing only this PC, fixed addresses.

### New: where each app keeps your files
- **After the drives, setup asks** where Immich's photos, the music library (with its downloads) and Nextcloud's files go: the SSD (faster) or the media drive (more room). The better one comes first:
  - photos on the SSD from 500 GB, music from 400 GB
  - Nextcloud's files always on the media drive

  The apps' settings and databases stay on the SSD.
- **Everything follows the choice:**
  - the apps' folders
  - the photo archive copy and the weekly USB copy
  - the music backup
  - the repair and restore menus
  - the update's checks
  - the drive space and acceptance checks
  - the media health checks

  All of them read the paths from one place (`neko_common` and the runtime's `common`). A test checks that no path is written out by hand any more.
- **Servers set up before 0.46.0 keep their layout:** a place that already holds files stays where it is, and the apps' definitions come out exactly as before. A setting changed by hand while the files are still on the other drive stops setup instead of starting an app on an empty folder.

## v0.45.8 — 2026-11-03

From a second full audit of every part of the code (the runtime, setup and its window, the gateway, the backups' reports, the dashboard and its alerts), each finding checked against the code before it was fixed. Again no finding allowed getting past the PIN, attacking the page from another site, or injecting commands. Each fix has a test, and each test was run against 0.45.7 and fails there.

### Fixed: from the project owner's review of 0.45.7
- **A drive move cut off before its switch never switches to a copy that is out of date.** 0.45.7 started over when the apps ran again on the old drive, but only if they were running when `resume` looked. Apps that ran after a restart and were then stopped (for a recovery, say) went unseen, and the move switched to the new drive's older copy. The move's record now notes the boot and the time; a restart since, or any of its services started since (still running or not), means starting over.
- **"Start the move again" works.** The copy the first move made is on the new drive, so the move refused it at once as "not empty". A move now notes the copy it makes; started again with the same two drives, it goes on with that copy (its last pass removes what is no longer on the old drive, and every file is compared after it). Any other drive must still be empty.
- **Staging a monthly snapshot can't report success for a split copy.** `monthly-recovery.py --stage` named its folder by path for each part. A folder swapped in at that path between the parts got the second part and the "checked" record. Everything is now written through the folder it made, opened once; if the path no longer leads to it, it says so and writes no record.
- **0.45.5's restore-test firewall rule is removed.** The corrected rule was added beside the old one, which still dropped the traffic between the test's own apps until the PC restarted. Once the right rules are in, each copy of the old one is taken out. Tried on a real iptables.
- **A downloaded Compose file can't reach this PC's files through a named volume, or get devices beyond the graphics chip.** The update's check refused a direct bind of `/`, but took a named volume defined as a bind (`driver_opts: o: bind, device: /`), and any `devices` or `device_cgroup_rules`. A named volume must now be on Docker's own storage: no driver options, no other driver, not external. The only device allowed is the graphics chip (`/dev/dri`, or one of its card or render nodes, at the same place), for Jellyfin and Immich's server. `gpus` and `runtime` are refused too. Setup's first install of Immich now runs the same check on the release's Compose file before starting it. Before, nothing checked it there.
- **Damaged database copies never push out good ones.** The weekly Immich and Nextcloud database copies were kept by date alone. With the default four, three newer damaged copies and a fresh good one removed the older good copy, leaving one good copy of four. Copies without their note took places too. Now only copies that match the checksum in their note count toward the four. A damaged or unchecked copy stays only while it is newer than every good one, and with no good copy at all the newest stay, as before. The copy just made is always kept.
- **Nextcloud names such as `.alice`, `_admin` or `@home` work for the update's login check.** Setup accepted them, but the command filter refused a name that didn't start with a letter or digit. It now takes every first character a Nextcloud name may have, except `-`. Setup also stops taking names that Nextcloud itself can't have.

### Fixed: privacy and safety
- **Your file names no longer reach the dashboard's status file.** That file can be read by every account on the server. A damaged backup copy, or a weekly USB copy that left files out, put up to five of your Nextcloud or photo file names in it, and in the dashboard's alerts. It now gives the counts only; the repair menu (`sudo bash install.sh --recover`) and the backup's own report (readable by root only) still name the files.
- **The race in the backup copy is closed.** 0.45.7 listed a narrow race (a folder in the path swapped for a link between the check and the open). The runtime now opens each file with the kernel refusing a link anywhere in its path (openat2 with `RESOLVE_NO_SYMLINKS`); on a kernel too old for that, it keeps the earlier check.
- **A file on the backup drive can't fill the memory.** The list of files of each weekly copy is unpacked from the drive; a small file that unpacked to gigabytes was read whole. It is now read up to 1 GB, then left.
- **A broken reply from OpenRGB can't make a list of billions of colours.**
- **Setup's window never writes over a settings file it can't read.** When `my-config.json` couldn't be read (a typo made while editing it by hand), a switch pressed in the window replaced it with the defaults. It now says the file can't be read and changes nothing.

### Fixed: the dashboard and alerts
- **The NekoServer tab shows how the last upgrade went.** Its "last upgrade" line read a report nothing wrote, so it always said "unknown". The upgrade now writes it: passed, put back on the release you had (a warning, your apps work), or needs you (with what to do).
- **An upgrade at work is no longer shown as cut off.** The dashboard asked systemd about a service called `nekoserver-upgrade`, which doesn't exist (each upgrade runs as `nekoserver-upgrade@<release>`).
- **No false "RECOVERED" for Tailscale or the outside monitor.** When their state couldn't be read, an open alert was taken as fixed and "RECOVERED" was sent to your phone.
- **A two-drive Btrfs pool whose error counters can't be read stays listed** as "unknown". It was left out, and its open alert was then dropped as if the pool had been taken out on purpose.
- **The log commands name the service that ran.** With weekly photo backups the archive runs as the Immich database copy's service, and the weekly updates, the daily check and upgrades had no service named at all.
- **Setup says "running now" while an app update runs.** The weekly updates, "Update now" and the monthly retry weren't counted, so setup said the update "did not finish" and told you to start the service that finishes it, while it was still working. The uninstaller waits for them too.
- **The acceptance check doesn't ask for the photo jobs without Immich.** With Immich switched off they are off too, so the check could never pass.

### Fixed: setup
- **A chip that decodes 10-bit video gets Jellyfin's 10-bit decoding.** Jellyfin's 10-bit HEVC and VP9 decoding stayed off even when the graphics chip supports them.
- **Nextcloud keeps the trusted proxy you added.** Setup wrote its own proxies over the first two entries of the list, removing one you had added.
- **Drive sizes are never rounded up.** A drive with 19.96 GB free was shown as "20 GB" while being refused as too small. A new drive sold as "1 TB" (931 GiB) is also no longer called smaller than recommended.
- **The music port is described as it is chosen:** 8446 unless another program has it; the phone setup page shows the one in use.

### Tried for real
- The firewall cleanup, on a real iptables: with two copies of 0.45.5's rule in place, both were removed and only the right rules stayed.
- Monthly staging with real tar archives (the swap test runs real `tar`).
- The rest is covered by tests only (the Rust ones on a real Linux kernel, the dashboard's in Node). The lab checks of 0.45.5 to 0.45.7 still stand.

## v0.45.7 — 2026-11-03

From a full audit of the code: five separate reviews (the dashboard and control service, the backups, app updates and rollback, setup, recovery and drive moves), each finding checked against the code before it was fixed. No finding allowed getting past the PIN, attacking the page from another site, or injecting commands. Each fix below has a test; the Python ones fail on 0.45.6.

### Fixed: the more serious ones
- **The daily check now pins each app by image ID, never by tag.** On a new server the check pinned every app by tag (`jellyfin:latest`). Its download then moved the tag, so running `sudo bash install.sh` before that app's first update night started the new image untested, with no snapshot; the dashboard also showed the new version as installed. It now pins what the containers run, by image ID, and re-pins pins made by tag. It downloads nothing for an app it can't pin.
- **A monthly check whose restore test runs past the night is done, not run again.** It installs nothing, but it stopped at the night's end like an update would, and came back every night (all apps stopped for hours each time). Each such run counted as complete, so with `keep_snapshots` 3 the earlier months' snapshots were all removed within three nights.
- **App updates and the monthly check no longer wait hours on each other.** The monthly run holds the backups' lock while it waits for the USB drive; an update held the USB drive's lock while it waited for the backups'. An update on the same night as a monthly retry, or "Update now" during a monthly run, stalled both for up to 3 hours. An update now never holds one of the two while waiting for the other.
- **"Install at the next update night" works on a new server too.** 0.45.6 let the control service write the folder that stores your choice, but systemd skips a folder that doesn't exist when the service starts, and on a new server the first daily check creates it only hours later. Setup now makes the folder before starting the service.
- **A drive move cut off before its switch, whose apps ran again, starts over.** If the switch failed (the old drive was busy) and the computer then restarted, the apps ran on the old drive. `resume` then switched to the new drive's older copy, so what was saved in between was missing from the server. Now `resume` sees the apps ran again and stops the move, keeping the old drive; run the move again.
- **A Nextcloud user can't block the weekly USB copy by naming a file like the copy's own records.** A file called `notes.neko-latest`, `.neko-names.json` or `film.neko-part`, or a user called `neko-damaged`, was copied, then reported "missing" every week. That failed the copy and kept the monthly snapshot and updates waiting. Such names are now named in the report and not copied.

### Fixed: smaller ones
- **Notes on a USB drive are read without following links, and within a size.** This covers the current-version notes and the folder name records: a drive someone else prepared could link them to `/dev/zero` or a file outside the drive.
- **A damaged copy that can't be repaired during the live Nextcloud or photo pass** (the file changed meanwhile) counts as a file in use; it no longer fails the whole copy.
- **Setup never changes a file's owner through a link.** It wrote into folders the desktop user owns (the Steam launcher, the desktop link), then changed owners by path. A link swapped in meanwhile would have been followed.
- **A NekoServer upgrade cut off part-way leaves no record behind.** A download cut short, or a check that got half a reply, ended the upgrade with an error and left its record, so app updates and the monthly check waited until the next restart. The download now just fails ("this server stays on …"); a failed check puts the previous release back.
- **The update's login check works for a Nextcloud administrator whose name has a space** ("Mary Smith"); the command filter refused it.
- **Each account on the server has its own PIN lockout.** A program of another account typing wrong PINs locked out the person at the server for up to a day.
- **More room for slow connections.** A few dozen connections sending a byte now and then kept the dashboard's four workers busy; it now has 16, each held at most about 4 s.
- **On a phone, "Open" shows only for apps the Tailscale gateway publishes.** It showed for every app with a Tailscale port, and those links led nowhere.
- **A lighting change while OpenRGB hangs no longer says "unavailable" when it was saved.** The page and the web server now wait longer than the control service can take.
- **The record that an update went in is written after its commit.** A run cut off in between left a record saying installed though it was undone.
- **An update cut off after its commit is noted installed** (by resume), so it isn't installed again.
- **An update's media check waits for the hourly one** instead of failing at once and undoing a good update.
- **A downloaded Compose file can't gain rights NekoServer's apps don't have.** `volumes_from`, `cap_add`, a security option other than no-new-privileges, `userns_mode` and `cgroup` are refused.

### Not changed
- A narrow race in the backup copy (a parent folder swapped for a link between the check and the open) is still possible for someone who controls the app's files and wins it. A lost race stops the run, loudly. Closing it needs the copy rewritten to open every folder through the one above it; that's for a later release.

## v0.45.6 — 2026-11-03

Found by using the Updates page in the lab (made-up versions waiting, a PIN set), and fixes for the project owner's review of 0.45.5.

**Tried for real** (the lab, with Docker): the restore test's firewall rules with two containers on the test network. With bridge netfilter on (Docker turns it on), 0.45.5's rule also dropped the restored Immich's connection to its own database; with the fixed rule that connection works, while this PC and the network stay unreachable and the published port answers. In the lab dashboard, "Install at the next update night" now saves the choice (it failed before), and each waiting version's line was read in the browser.

### Fixed
- **`migrate.py back --drive media_hdd` takes back the HDD.** `back` ignored `--drive` and took back the first drive with an earlier one in the settings (the SSD). It now takes the drive named; when both drives were moved and none is named, it asks which and changes nothing.
- **A move whose final check failed can be finished.** After setup or the check failed, the apps run again on the new drive; `resume` then tried to unmount that drive, failed (it is in use) and stopped before running setup again. When the new drive is already the one mounted there, `resume` now runs setup and the check again without unmounting it.
- **The restore test's apps reach each other.** The rule that stops the restored Immich reaching this PC and the network also dropped its connections to its own database, Redis and ML service when bridge netfilter is on, as Docker sets it. Traffic that stays on the test network now passes.
- **A drive move never offers a partition of the boot disk or of the system's Btrfs pool.** Only the disk holding `/` counted as the system's. Now the disks holding `/boot`, `/boot/efi`, `/efi`, `/usr`, `/var`, `/home` and swap count too, as does every member of a Btrfs system spread over several drives (lsblk shows its folder on one of them only).
- **"Install at the next update night" works.** The control service may write only a few folders, and the one holding the version you choose (`backups/updates`) was not one of them, so the button always failed ("Read-only file system"). Found by clicking it in the lab with a PIN set; it now saves your choice.
- **Each waiting app version says when it really installs.** A version that had waited long enough said "installs from Nov 1" on Nov 3, and every waiting version said it would install, though an update night installs one: the one you chose, else the newest that has waited long enough. Each now says which it is.
- **The PIN window names updates and upgrades**, not only restarting or closing apps.
- **A drive that is too small says the size it needs.** A 20 GB drive was refused as "too small: it needs at least 20 GB" (a formatted drive keeps some of itself, so 20 GB free needs a 22 GB drive). The messages now name the drive size needed, with the free space it gives.

### Tests
- A test for each; the move ones fail on 0.45.5, the firewall rule's was tried on a live Linux bridge.

## v0.45.5 — 2026-11-03

Found by installing 0.45.4 in the lab and opening its dashboard in a browser.

**Tried for real** (the lab: Ubuntu 24.04 with systemd, two file-backed drives; this lab can't download Docker or app images, so it ran the dashboard profile): setup with `--check`, then `--profile dashboard --apply`; every step passed and the dashboard, its collector and the control service ran. Every dashboard page and the three Updates tabs were opened in Chromium, with no page errors, and at phone width with nothing wider than the screen. The System check ran for real: 116 Ubuntu updates, security fixes marked, the restart "not known" with its reason. The drive move list, a move refused while the operation lock was held, and an upgrade refused for the same release, for an unfinished move and for a release that isn't there were tried for real. Setup started by hand during a move stopped and named the command that finishes it.

### Fixed
- **The dashboard showed lists of words as empty.** Since 0.41.1 the page's list helper kept only rows (objects), and these lists hold plain words, so it dropped every one of them:
  - the System tab said "whether a restart is needed is not known: ." with no reason, and never named the reasons a restart is needed;
  - an update never said it restarts Docker, Samba, Tailscale or SSH;
  - a RAID array or Btrfs pool was never shown with the drives it holds, nor named after them.
- **The System check says why it did not run in one line.** With the SSD missing, it refused correctly but printed a Python traceback; it now prints one line and exits with code 1.

## v0.45.4 — 2026-11-03

Fixes for the project owner's three reviews of 0.45.3 (18 findings). Built on 0.45.3 (merge PR #56 first).

**Tried for real:**
- **The Immich restore test's isolation, with Docker in the lab:** a container on the test network reached a service on this PC, at the network's own address and at the PC's address. With the new firewall rules it reached neither, and the published port still answered. Adding the rules twice adds them once.
- **openSUSE's Python repair step** was run in bash with stand-ins for `zypper` and `python3`: during an upgrade it stops (exit 4) without running zypper; run by hand, it still runs `zypper update`.

Everything else was tested with the automated tests only.

### Fixed: the two most serious
- **A nightly restore stops when its folder's drive goes.** If the drive with the restore folder was unplugged or replaced part-way, the restore made its folders again at the same path and went on writing there: on the system disk, or on another drive. Now the folder is opened once and every file is made from that open folder; before each one, the path must still lead to it, and a folder on another drive is refused. That is how the photo restore already worked; both now share the same code.
- **Failed app updates no longer fill the SSD.** Each update that was undone kept its failed app data (`appdata/<app>.neko-failed-<run>`), and nothing weekly ever removed it. Now it goes with its run's snapshot, and of the undone updates of an app only the newest is kept (to look into).

### Fixed: backups and app updates
- **Nextcloud users whose names start with a dot (`.alice`) are backed up.** Nextcloud allows those names; the USB copy skipped them without a word. Only a name the copy's own records use (`.neko-…`) can't be copied; that user is named in the report, and the copy is not called complete.
- **The daily update check checks the room on the SSD before it downloads.** It downloads a new version's images only when the SSD keeps its reserve afterwards. Images of versions no longer waiting (replaced by a newer one, now running, or changed by setup) are removed after each check, not only after an update installs; an update's own clean-up keeps noting those still waiting, so they are removed later.
- **Old app-update snapshots go only while the newer ones are intact**, as for the monthly ones. A newer snapshot that fails its checksum check keeps the older ones and is named.
- **An update that does not fit can free its own undone updates' snapshots** (on the HDD, with their data on the SSD), oldest first; a complete snapshot is never removed for room.
- **Update now and a weekly USB copy no longer wait on each other.** An update held the backups' lock while it waited for the USB drive's lock, and the USB copy then waited half an hour for the backups' lock and left out its fresh Nextcloud database copy. An update now takes the USB drive's lock first and keeps it to the end; a copy that starts meanwhile waits at its start, holding nothing.
- **Nextcloud with two-factor sign-in is "not known" in the update's login check**, never "signed in": its saved password can't be tried.
- **An app update's record no longer says its restore test passed** (`restore_test: "not_run"`); only the monthly check runs it.
- **A NekoServer upgrade keeps its way back.** Setup's clean-up of earlier releases could remove the release an upgrade at work would put back (with a newer release that failed still kept, say), before the upgrade checked the result. Releases the upgrade's record names are kept, and every release when that record can't be read.
- **The manual Immich restore test can't reach this PC or the network.** Its network only had IP masquerading off, which isn't a firewall: the restored app, with your production settings, could reach services on this PC. Two firewall rules now drop every connection it starts itself; without them the test does not start.

- **Nextcloud accounts with a space or an apostrophe get their folders back on a new HDD.** After the files folder was given back on a new HDD, accounts like `Mary Smith` or `o'brien` were left out (the name check allowed neither, though Nextcloud does), and the file scan's errors went unseen, because `occ files:scan` ends with exit code 0 anyway. So setup said uploads worked again and dropped its note to finish the job. Now every account Nextcloud lists gets its folder, checked to be there, and its own scan, which must report 0 errors. Until every account is done, the note stays and the next setup tries again. An account name Nextcloud itself would not accept is named, never skipped.

### Fixed: drive moves, the System tab and upgrades
- **A move never offers a partition on the system's disk when the system is encrypted.** With `/` on an encrypted (LUKS) partition, the system's disk was taken to be that partition, so another partition of the same disk could be offered. Each filesystem is now traced down to its physical disks, through encryption, LVM and RAID.
- **A backup at work stops a move** instead of being cut off half-way. A running backup is "activating" to systemd, not "active", so the move did not see it. Now it stops the timers first; if a job is still at work, it stops, and the apps run as before.
- **openSUSE's system stays as it is during a NekoServer upgrade.** If Python needed newer libraries, setup brought the whole system up to date even during an upgrade from the dashboard. Now it stops and says to run setup in a terminal, as on Arch.
- **The System check never writes onto the system disk.** With the SSD missing it wrote its answer under the SSD's empty folder. It now checks that the SSD is mounted first.
- **Security fixes on Fedora, RHEL and Arch.** They were always shown as "not a security fix". Fedora and RHEL now ask `dnf updateinfo`; Arch asks `arch-audit` when it is installed. Where the system can't say (openSUSE, Arch without arch-audit), the tab says it is not known.
- **The stable release of the version you run is not offered.** The check offered it, and the upgrade refused it, because both keep their files in the same folder and one can't be the way back to the other. Only a release with a newer version number is offered now.

### Tests
- A setup window test failed on CI by chance: a timestamp's digits held the PIN it checks is never recorded. It now leaves the times out of that check.
- A test for each finding. The Python ones fail on 0.45.3; the Rust ones use functions 0.45.3 doesn't have, so they weren't run against it. The restore that loses its folder, the openSUSE step and the firewall rules are exercised for real (a real folder, bash, iptables).

## v0.45.3 — 2026-11-03

Fixes for the project owner's two reviews of 0.45.2: the drive move's end check, a move overlapping an upgrade, its /etc/fstab switch, finishing one whose apps did not start, an app update's rollback, and the login check of a held-back Immich or Nextcloud.

**Tried for real** (the lab: Ubuntu 24.04 with systemd, two loop-device drives): a move with a commented-out /etc/fstab line naming the new drive (the real line was switched, the new drive mounted, data, links, owners and attributes kept); a `back` refused while another process held the operation lock (nothing changed). Docker (Compose 5.5) was asked what it records for a port it picks (`"0"` asked, the real one under NetworkSettings), which is what the login check now reads. Setup and the server check in the move, the rollback and a real Immich or Nextcloud update were tested with the automated tests only.

### Fixed
- **A drive move is checked at the end the way an upgrade is.** It took setup's exit code 1 as success, but that can also mean a step failed, so a move could say "Done" with broken apps. Now it notes which setup steps work before the move, and after setup and the restart it runs the upgrade's own check: those steps work again, the dashboard answers and every NekoServer service runs this release. If not, it says what, keeps its record, and `migrate.py resume` runs setup and the check again. `back` is checked the same way.
- **A drive move and a NekoServer upgrade can't overlap any more, also from a terminal.** `upgrade.py` didn't look for an unfinished move's record. It does now, and both hold one operation lock (`/run/nekoserver-operation.lock`) from start to end, including when they finish a cut-off run (`resume` waits for the lock).
- **Only the active `/etc/fstab` line counts.** The move skipped the switch when the new drive's UUID appeared anywhere in the file, even in a comment, and left the real line naming the old drive. Now it reads the one active line that mounts the folder, and after mounting it checks that the new drive is the one mounted there; if not, it stops and says so.
- **Finishing a move whose apps did not start no longer talks about the old drive.** A restart failure after the switch was recorded like one before it, so `resume` said "the old drive, unchanged" while the new drive was in use. They are now recorded apart (`started_partly` after the switch), and `resume` starts them on the new drive and runs the check. A 0.45.2 record is told apart by which drive is mounted.

- **A failed app update rolls back only that app.** An update snapshots only the app it updates, but the rollback asked for the folders of every app set up, found the others missing and stopped half-way ("Rollback state incomplete"), which left the update unfinished and every later one waiting. It now puts back only what that run's snapshot holds (its manifest) for the apps that run changed (its record).
- **A healthy Immich or Nextcloud update passes its login check.** While an update is checked they run on a port Docker picks, on this computer only, but the login check accepted only their usual port, so it refused them, and the update was rolled back. The check now reads the held-back container's real port from Docker (on 127.0.0.1 only, never below 1024) and signs in there, with the same identity checks (its project, service, folder and that it runs). Only the update's login check accepts it.

### Tests
- A test for each fails on 0.45.2: setup's step failures found by the end check; a move refused while the operation lock is held, and an upgrade refused while a move is unfinished; a commented line naming the new drive; a mount of the wrong drive; apps that do not start after the switch, finished on the new drive (also from a 0.45.2 record). A rollback asks only for its own app's saved paths; the login check takes the held-back container's port and refuses a low or fixed other one, another project, or a check that is not the update's. The test stand-in for `mount` now mounts what /etc/fstab's active line names, as Linux does.

## v0.45.2 — 2026-11-02

Fixes for the project owner's two reviews of 0.45.1: going back after a drive move, the settling of app updates, the restart check on the System tab, an Immich major's approval, an upgrade's setup that runs out of time, and Update now's version. Built on 0.45.1 (merge PR #54 first).

**Tried for real** (the lab: Ubuntu 24.04 with systemd, two loop-device drives): a drive move, then going back while setup held its lock (refused, nothing changed), then going back for real (fstab and the settings name the old drive again and remember the newer one, the service writing to the drive runs again, no record left). The app updates, the upgrade and the System tab were tested with the automated tests only.

### Fixed
- **Going back after a drive move (`migrate.py back`) has the move's checks and a record that survives a restart.** It did not check that setup, an upgrade, an app update or the monthly check was at work, and the list of what to start again lived in `/run`, which a restart empties. Now it checks the same as a move (also the backups' shared lock), refuses to run from the drive it moves, and keeps its record in `/etc/nekoserver-backups/migrate.json`, so `sudo python3 migrate.py resume` finishes it after a restart, as for a move. It stops and starts the apps the same way, and remembers the newer drive, so you can switch again. A setup started by hand while a move is unfinished now stops and says to run `migrate.py resume` (the move's own setup runs are not affected).
- **Daily app builds now become ready to install.** The daily check kept only the last four versions it found for an app, but a version must wait 7 days (the default) before it is installed. An image that publishes a build every day pushed each one out before it had waited long enough, so it was never updated. Now it keeps the newest version, the oldest one still settling, the newest one that has settled and one you approved (never more than four), so the oldest settling one becomes ready on time.
- **Taking back an Immich major upgrade's approval now stops it.** The approval (`maintenance.immich_major_update`) was checked when the daily check downloaded the new major, but not again when it was installed. Removing it afterwards left the downloaded version to be installed anyway. Now it is checked again when it would be installed, as Nextcloud's already was.
- **A NekoServer upgrade whose setup ran out of time is put back at once.** A setup that ran past its six hours (or could not be started) ended the upgrade with an error before the previous release was put back; the record stayed, so it was put back only at the next start. Now that is a failure like any other: it is noted in the record, and the previous release is set up again and checked straight away (the record stays when that fails too).
- **Update now installs the version shown next to the button.** It used the update night's choice, which prefers a version you chose earlier for the night, so it could install an older version than the one you pressed it for. The dashboard now names the version, the control service checks it is still waiting, and the worker installs exactly that one (`nekoserver-update-now@<app>:<version>`).
- **"Restart not needed" only when the system's check said so.** On Fedora, RHEL and openSUSE a missing `needs-restarting` or `zypper needs-rebooting`, or one that failed, showed as "no restart needed". The System tab now shows needed, not needed or **not known**, with the reason.

### Tests
- Each fix has a test that fails on 0.45.1: going back while another job is at work, a going back cut off part-way and finished by `resume`; daily builds over 12 days with 7 days to settle; a restart check that is missing or fails; an Immich major whose approval was taken back after it was downloaded; an upgrade setup that times out or cannot start; Update now with an older version chosen for the night.

## v0.45.1 — 2026-11-02

Fixes for the project owner's two reviews of 0.45.0: the NekoServer upgrade and the drive move.

**Tried for real** (the lab: Ubuntu 24.04 with systemd, two loop-device drives):
- The drive move again, with its new checks: done, as in 0.45.0.
- The old drive unmounted while the apps were being stopped: the move stopped before its last copy, and the new drive's copy was still there.

### Fixed
- **The upgrade now sees which setup steps worked before.** It looked for the status "complete", which the installer never writes ("success"). So the list of steps that worked before was always empty, and a step the new release broke went unnoticed. The tests used "complete" too, which hid it; they now check the installer's own word.
- **Putting the previous release back is checked before it is called done.** Its setup ending with exit code 1 (which can mean a step failed) was taken as success, the record removed and "runs again as before" said. It now gets the same check as an upgrade: every step that worked before, the dashboard and every service on that release. Until that passes, the record stays, and app updates and the monthly check keep waiting. After a restart, the check uses the steps the upgrade noted when it began.
- **The dashboard is asked on its own port.** The check always asked port 8787. Setup picks another port when 8787 is taken, so a good upgrade could be undone. It now uses the port that release's setup saved.
- **A drive move checks both drives before each step.** It copied from the drive's folder without checking it was still the drive. A drive that dropped out would leave that folder an empty one on the system disk: the last copy (which deletes from the new drive what is not on the old one) would then erase the copy, and the empty-to-empty comparison would pass. Now, before each copy, before the comparison and before the switch, the old drive must still be the one mounted there (UUID, filesystem and Btrfs subvolume), and the new one at its own folder. Otherwise the move stops with nothing changed.
- **A move whose stopping of the apps failed part-way starts again all it had stopped.** It started again an empty list and removed its record, while it said everything ran as before. It now starts again what its record lists. The record goes only once all of it runs; otherwise it stays, names what is left, and `migrate.py resume` finishes once you have started those.
- **Going back to an old Btrfs drive mounts its own subvolume again.** The move remembered only the old drive's UUID and filesystem, so `back` could mount the drive's top level instead of `/@media`, say. It now keeps the whole record, the subvolume as mounted included, and `back` puts it in `/etc/fstab` and the settings again.
- **Every NekoServer service must run the new release, not only the dashboard.** The check looked at the dashboard's service alone. Now any installed `nekoserver-*` service or timer whose definition names another release's folder fails the check: the backups, the monthly check, the updates and the rest.

## v0.45.0 — 2026-11-02

The Updates page's System tab, first step: it shows, it changes nothing. Also two faults in 0.44.0's drive move, found by trying it for real.

**Tried for real** (the lab: Ubuntu 24.04 with systemd, two loop-device drives):
- The System check on real apt: 122 updates waiting, the same count as apt's own.
- A full drive move: a 1 GB ext4 media drive moved to a 2 GB one while a NekoServer service kept writing to it.
  - Owners, permissions, hard links and extended attributes came across.
  - The checksum comparison passed.
  - `/etc/fstab` and the settings named the new drive, and the old fstab was kept aside.
  - The writing service ran again on the new drive.
- `migrate.py back` went back to the old drive. NekoServer's setup run at the end of each was left out: the lab machine has no NekoServer set up.

### Added
- **The System tab** shows:
  - the Linux package updates waiting (from `apt-get -s`, `dnf check-update`, `zypper list-updates`, or `checkupdates` on Arch), each marked when it is a security fix, needs a restart of the computer, or restarts Docker (every app), Samba, Tailscale or SSH;
  - whether the computer needs a restart now;
  - the system's release.
  
  A daily, read-only job (`nekoserver-system-check.timer`, `system_updates.py`) reads them; it never installs, removes or refreshes a package. Installing system updates from the dashboard comes later, with a recovery backup of the system first.

### Fixed
- **0.44.0's drive move always stopped before its last copy.** It asks systemd for NekoServer's running units by the pattern `nekoserver-*`, and its own argument check refused the `*`. The move then stopped as designed, with nothing changed and the apps on the old drive, but it never got through. The pattern is allowed now.
- **`migrate.py list` stopped with an error** when one of the two drives' folders could not be read (not mounted). It now says so for that drive and lists the other.
- **The System check no longer marks `systemd-dev` (or `libc6-dev`) as needing a restart.** Only the packages themselves (systemd, the C library, the kernel, microcode, firmware) do.

## v0.44.0 — 2026-11-02

Moving a working server to bigger drives, from the project owner's review of 0.41.5.

### Added
- **`migrate.py`: the SSD or the HDD moved to a new drive while both are connected.**
  - `list` shows where each drive can move.
  - `move --drive data_ssd|media_hdd --to <UUID>`:
    1. Copies everything while the apps run, keeping owners, permissions, hard links, ACLs and extended attributes.
    2. Stops the apps, Docker, Samba and NekoServer's jobs, and copies what changed meanwhile.
    3. Compares every file on both drives by checksum. Any difference stops the move, with the apps back on the old drive.
    4. Switches: the new drive takes the old one's place in `/etc/fstab` (the old file is kept beside it) and in the settings, and setup runs again without questions or a system update.
  - The old drive is never changed. `back` goes back to it, and `resume` puts a move cut off by a power cut right.
  - App updates, the checks, the monthly check and app restarts wait while a move is at work.
- `rsync` is among the packages setup installs (the move uses it).

### Not tried for real
- The move was tested with stand-ins for the drives, Docker and setup, and the copy and checksum comparison on real folders. It was not done on real drives.

## v0.43.0 — 2026-11-02

The Updates page's NekoServer tab: NekoServer upgrades itself when you approve, as one coordinated operation.

### Added
- **NekoServer finds its own new releases.** The daily check looks on GitHub for releases on your channel (`maintenance.nekoserver_channel`): `"beta"` by default, while every release is a beta, or `"stable"`. It skips drafts and tags that follow neither channel's form, and on the stable channel anything marked a pre-release. A release found is shown, never installed by itself.
- **Upgrade NekoServer from the dashboard** (Updates page, NekoServer tab, with the PIN). A background service (`nekoserver-upgrade@<release>`, which runs `upgrade.py`) goes on if the page is closed:
  - It checks that nothing else is at work.
  - It downloads the release and checks it is that release.
  - It runs that release's setup with your saved settings, without questions, and never updates the system packages.
  - It then checks every step that was done before is done again, that the services run the new release, and that the dashboard answers with the new version.
  - If anything fails, it runs the previous release's setup again from its copy on the SSD.
  - A restart in the middle is put right at the next start (`nekoserver-upgrade-resume.service`).
- **App updates, the checks, the monthly check and app restarts wait** while an upgrade is at work or was cut off, so they never overlap.
- **`NEKOSERVER_KEEP_SYSTEM`**: `install.sh` and `host.py` leave the system packages alone when an upgrade runs setup. On Arch, a release that needs a new package stops and says to run `sudo bash install.sh` in a terminal, which updates the whole system first. Running setup in a terminal is unchanged.

### Not tried for real
- The upgrade was tested with stand-ins for the download and for setup, not end to end on a server: it can only be tried for real once a newer release than this one is published.

## v0.42.1 — 2026-11-02

Follow-ups to 0.42.0: SonarCloud's findings, and superseded and duplicated code taken out.

### Changed
- **The update run's code is split into its steps** (SonarCloud: too complex to follow, at 67 where 30 is allowed): the checks first, room on the drives, the running apps, the snapshot, the new files, the image clean-up. What it does, and in which order, is unchanged.
- **Major-update notices come only from the daily check.** The old way of reading them from the newest monthly snapshot is gone. The Health page no longer repeats the Updates page's box.
- **Duplicated code merged:** one check, in four places, of whether an unfinished run still needs a snapshot folder; one way to write the three night jobs' service files; no nested conditions in the Updates page.
- **Wording:** the next daily check, not "next month's run", looks again at a new Immich release it could not check. A run cut off by a power cut is "tried again at its next time": that covers the update night as well as the monthly check.

### Fixed
- **A cut-off update night is finished at the next start.** The resume service only looked at the monthly run's report when deciding whether there was anything to finish. It now looks at the update night's too.

## v0.42.0 — 2026-11-02

App updates move out of the monthly run: a daily check, weekly protected updates of one app at a time, and a monthly check of every backup copy. The project owner's design for the Updates page.

### Changed
- **App updates are weekly, each app on its own, after a snapshot of it.**
  - A daily check (`nekoserver-update-check`, while the apps run) finds and downloads new versions, and notes the day it first saw each.
  - A version waits `maintenance.settle_days` days (7 by default) before an update night installs it. The update night is Wednesday by default (`maintenance.update_day`), at `maintenance.time`, never on the 1st.
  - On that night each app with a settled version goes on its own: a snapshot of that app, checked against its checksums; the new version, whole stack at once; then its checks. If the app took its saved login before the update, it must take it after. A failure is undone from the snapshot: data, settings and the old images together, since Immich cannot downgrade a database.
  - One app's failed update does not hold back the others. Each app keeps the snapshots of its newest two updates (`backups/updates/` on the HDD).
- **The monthly run is now a check, not an update.** On the 1st it compares every backup copy with its checksum, takes a snapshot of every app, tests that it restores, and starts the apps again as they were. It no longer downloads or installs anything.
- **Major versions** (Immich, Nextcloud) and releases with an unknown layout still wait for your approval. The daily check now finds them.

### Added
- **An Updates page on the dashboard, with Apps, NekoServer and System tabs.**
  - **Apps** shows each app's version, the versions waiting and the day each installs, its last snapshot and how its last update ended. It has **Check for updates**, **Update now**, **Install at the next update night** (each with the PIN) and a link to each app's own release notes.
  - Both update buttons start a background service (`nekoserver-update-now@<app>`) that goes through the same steps as the update night. Closing the page stops nothing, and reopening it shows the progress.
  - **NekoServer** and **System** say what they will cover in later releases, and how to upgrade by hand until then.
- `account-setup.py signin APP`: checks, read-only, that an app takes its saved login (exit code 0, 1, or 3 when there is none).

### Not tried for real
- The update night, Update now and the daily check were tested with stand-ins, not on a real server. The lab can't download app images (Docker Hub and ghcr.io are blocked there).

## v0.41.8 — 2026-11-02

### Changed
- **The weekly USB copy's code is split into parts** (SonarCloud: one function was far too complex to follow, at 85 where 30 is allowed). Checking the drive, the copy of the photos, of Nextcloud and of the password list, and the mounting are now each a part of their own; what the copy does, and in which order, is unchanged.

## v0.41.7 — 2026-11-02

The project owner's second review of 0.41.5.

### Fixed
- **Nextcloud's weekly database copy and files agree.** The weekly USB copy made Nextcloud's database copy first and copied the files later, with Nextcloud running: a file renamed or deleted in between left a database copy naming a file the USB drive did not have, and the checksums could not tell. The copy now works in two passes like Immich's: the files first, with Nextcloud running, then Nextcloud paused (its web app and cron job; the database keeps running), the database copy made and the files that changed meanwhile copied, then Nextcloud started again, whatever happens. A note (`/etc/nekoserver-backups/nextcloud-resume.json`), the job's own stop step and the new `nekoserver-nextcloud-resume.service` at boot start it again after a stop or a power cut; the drive-return service runs it too. When the monthly update is under way, or another job holds the backups' lock for 30 minutes, that week's files are still copied and the backup says incomplete (no database copy).
- **The USB drive gets the app passwords from the first week on.** Its copy of the encrypted password list came only from a monthly snapshot, so for the weeks before the first monthly run the USB backup said it was done without one. Setup now makes an encrypted list of its own on the HDD after every run (`backups/password-lists/`, only when a password or the backup key changed; the newest 3 kept), and the weekly copy takes the newest list, setup's or a monthly snapshot's. Until the HDD has one, the USB backup says it is incomplete.
- **The monthly update's retry the next night survives a restart.** It was a one-off `systemd-run` timer, which a restart forgot while the report still promised the retry. It is now a timer file of its own (`nekoserver-maintenance-next-night.timer`), and the report also gives the time as `next_attempt`.

### Added
- **Release channels.** A tag `vX.Y.Z` is a stable release and `vX.Y.ZBeta` a beta (README.md, "Release channels"). A new GitHub job marks each release as its tag says (a beta as Pre-release, so GitHub's latest release is never a beta), and fails on a tag that is neither.

## v0.41.6 — 2026-11-02

Fixes for the project owner's review of 0.41.5.

### Fixed
- **Setup run again no longer updates your apps.** Choosing the backup app (or any app that needs the media apps) runs the media step again, and that downloaded a newer image for every app on a moving tag (`lscr.io/...:latest`, say) and started it: an app update outside the monthly update, so without its snapshot, restore test or rollback. Setup now downloads only the images not on this PC yet (an app set up for the first time); an app already there starts again on the image it has. Only the monthly update updates apps. (A new NekoServer release whose app templates name a different version still downloads that version when setup runs.)
- **A replacement drive keeps nothing of the drive it replaces.** RECOVERY.md says to take the old drive's `"uuid"` out of `my-config.json`; its Btrfs `"subvolume"` (`/@data`, say) stayed, so an ext4 replacement was refused and a new Btrfs one was looked for in a subvolume it may not have. Picking the new drive now clears the old drive's settings; a Btrfs drive's own subvolume is found when it is mounted.

## v0.41.5 — 2026-11-02

Fixes for the project owner's review of 0.41.4.

### Fixed
- **A restore stops if its folder's drive goes away or changes mid-copy.** The restore folder was checked once, then each file was written by its path again: with the drive unplugged or another one mounted there part-way, the rest of the files went to the empty folder on the system disk or onto the other drive, and the restore still counted them all as restored and verified. The restore folder is now opened once and every folder and file of the restore is made from that open folder (never on another filesystem); before each file the path is checked to still lead to it. If it doesn't, the restore stops, says after how many files, and writes nothing more; NOT-RESTORED.txt and handing the files over to your account are done only through that same folder. This covers photo and Nextcloud restores in every mode.
- **An interrupted Nextcloud recovery finishes at the next setup run.** After a new HDD, setup gives Nextcloud its files folder back and, once Nextcloud answers, makes each account's folder again. If setup stopped in between (Docker slow to answer, the terminal closed, a restart), the next run saw the folder's marker files, took it for one in use and never made the accounts' folders, so uploads stayed refused. Setup now writes a note on the SSD, naming the new HDD, before it gives the folder back, and removes it only once the accounts' folders exist and Nextcloud's file list was refreshed; the next run finishes the job. A note about another HDD is dropped.
- **Nextcloud's accounts that can't be listed are an error, not "no accounts".** A failed `occ user:list` (an error, an answer that is not a list of accounts, or no answer within 2 minutes) used to count as zero accounts, and setup said their folders were made "so uploads work again". It now stops with what went wrong and how to finish it, and the note above stays for the next run.
- STORAGE.md and ACCOUNT-SETUP.md now say that the weekly USB copy keeps the newest 3 encrypted password lists on the USB drive (they still open only with the backup password); they said the list never went there.

## v0.41.4 — 2026-11-02

The sixth round: RECOVERY.md's two drive-replacement rows followed word for word, the 0.41.3 update on a second family, and the application SSD mirrored.

**Tried for real** (WSL rig): on openSUSE Tumbleweed, RECOVERY.md's RAID row on the HDD mirror (`cat /proc/mdstat`, `mdadm --add`, the dashboard's Drives page showing the rebuild, then whole; the app backup ran on it) and its Btrfs row on a two-drive pool missing one (`btrfs replace`, the balance, the ordinary mount again, the dashboard healthy), then the 0.41.3 update (27 of 29 steps, the two left wait for the Tailscale sign-in). On Arch, the same update over a 0.41.2 server (27 of 29, feature check 47 of 47, the fault suite 30 of 30), then the HDD replaced by an empty one: setup refused with the gone drive named and the way out, the rerun rewrote fstab, made every Nextcloud account's folder and refreshed the file list, and the feature check was 47 of 47; the drive-return service run by hand under a lock held install.sh's way said setup was running and ended without an error. The application SSD made an mdadm RAID-1 mirror in place (the filesystem shrunk first, a second drive synced; Docker ran from the array), one drive failed and taken off the bus: writes landed, the storage guard restarted Docker, the drive-return service was fine, every backup and the monthly run passed, the dashboard said degraded with the critical alert; the drive replaced by the row (`mdadm /dev/md1 --add`), the dashboard showing the rebuild and then healthy; the SSD a plain drive again. The Python suite, the Rust suite (394) and clippy pass.

### Fixed
- **After a new HDD, Nextcloud takes uploads again.** Since 0.41.1 setup gives Nextcloud an empty files folder back on a replaced HDD, but Nextcloud's database still listed what the old drive held: it took each account's `files` folder for present, never made it again, and refused every upload ("Could not create path"), the monthly run's test file with it ("Nextcloud could not write a test file after 120 seconds, though Nextcloud answers", so no verified snapshot and no app updates until the folder was made by hand). Setup now makes the folder of every account Nextcloud names on the new drive and refreshes Nextcloud's file list, so the old drive's listing is gone and uploads work; the log says so.

- **The drive-return service leaves a drive that "returns" under a running setup to setup.** On a PC given a new HDD, setup's storage step mounts the drive and rewrites its fstab line; the drive-return service (woken by Docker's socket) took that for a drive plugged back in, started Docker and the gateway at once, and the gateway refused because its record still named the old drive (setup's gateway step brings it up to date a minute later): the service ended as failed and stayed so, though setup itself finished. It now says that setup is running and starts the apps itself, and ends without an error. Seen on the rig: Arch given the drives of the Tumbleweed server.

### Changed
- **RECOVERY.md's RAID row names the whole drive or its partition**, whichever the array's other drive is (`mdadm /dev/md0 --add /dev/sdX`, or `sdX1`), and says a restart mid-rebuild is fine: the rebuild goes on by itself. Tried: the WSL rig's kernel went down in the middle of the rebuild, and on the next start the array resumed it and finished, the dashboard showing it whole.

## v0.41.3 — 2026-11-02

The fifth round: a mirror (RAID-1) that lost a drive, tried for real. The backups keep going on the half of the mirror that is left, and a Btrfs pool missing a drive is opened on the drives left instead of staying unmounted.

**Tried for real** (WSL rig, openSUSE Tumbleweed with its 0.41.2 server on the shared drives): the HDD made an mdadm RAID-1 mirror in place (the array's superblock at the drive's end, the filesystem untouched, a second drive added and synced), then the update over it (27 of 29 steps, the two left wait for the Tailscale sign-in; feature check 47 of 47). With one member taken away mid-run: every backup ran as usual (the weekly USB copy, the nightly app and host copies, the Immich database copy, the monthly verified snapshot with the app updates), the drive policy and the drive-return service were fine, the dashboard's Drives page said degraded with the critical alert, the rebuilding alert came and went when the member was put back, and the array was healthy again. The same HDD as a Btrfs RAID-1 pool over two drives, chosen by setup: with one drive taken offline the ordinary mount refused, the drive-return service opened the pool degraded, Docker and the apps came up and the backups passed; the drive back, the ordinary mount again. The Python suite (1153 tests), the Rust suite (393) and clippy pass.

### Fixed
- **A Btrfs pool that lost a drive is opened on the drives left.** A Btrfs HDD or SSD mirrored over two drives refuses an ordinary mount once one of them is gone (only `degraded` opens it), so after a restart nothing was mounted there: Docker and every app stayed down and the backups said the drive was missing, although the other half of the mirror was fine. The drive-return service now opens such a pool on its remaining drives, says so in its log, and starts Docker and the backups as usual; the dashboard's Drives page shows the pool as degraded and the alert goes out. A rerun of setup on such a server mounts it the same way and says so. RECOVERY.md shows how to replace the drive (`btrfs replace`), after which a restart mounts the pool the ordinary way again. An mdadm mirror never needed this: Linux keeps the array running on one drive, and every backup, the drive policy and the start guard were tried on one.
- **A drive made a RAID-1 member in place no longer stops Docker as a "copy".** Mirroring the existing HDD without reformatting it (the array's superblock at the drive's end, `mdadm --metadata=1.0`) leaves the member showing the filesystem's UUID beside the array's until Linux looks at it again, and the start guard refused to start Docker with "Two drives look the same (one is probably a copy)". A drive under an array is the array's own now, never a copy of it.
- **The gateway step waits for a container that is still starting.** Run a minute after Docker started the apps again (a drive came back, or a step before it restarted Docker), setup's gateway step failed at once with "Selected containers must use the verified persistent mounts..." and named Immich, whose health check simply had not finished; every step after it was skipped. A container that is in place but still on its health check is now waited for (up to two minutes); one that is really wrong is still named at once.

## v0.41.2 — 2026-11-01

What the third round's drills left open: a monthly run that fails no longer costs a whole month, the password list has a copy off the SSD and HDD, two paths nobody runs any more are gone, and two smaller findings.

**Tried for real** (WSL rig, openSUSE Tumbleweed with its 0.41.1 server on the shared drives): the update over it (27 of 29 steps, the two left wait for the Tailscale sign-in; feature check 47 of 47), then the three fixes driven as a person would meet them: the weekly copy put the newest sealed password list on the USB drive (the four files the same bytes as the HDD's, each private, the newest ones kept) and said so; with every sealed list on the HDD set aside, the recovery menu's **App password list** said it looks on the USB drive, offered the drive's copy and restored the current list from it, and unmounted the drive afterwards; with the HDD filled, the monthly run failed on room, its report carried `retries` 1 and the time of the next try, the next night's timer existed, and the next run (space freed) passed and carried no retry note. Then the fault suite (30 of 30). The Python suite (1091 tests), the Rust suite (391) and clippy pass.

### Fixed
- **A monthly run that fails comes back the next night, not next month.** An app not ready in time, the HDD short of room, a damaged copy found: before, the run was left to the next month, so a passing trouble cost a month without a verified snapshot and without the app updates. Now it is tried again the next night at the maintenance time, three nights running, before it is left to the next month (the way a run postponed by setup already came back). The dashboard's "The update was undone" says when the next try is, and the result's reason ends with it ("night 1 of 3"). A run that stopped because the night ran out before the apps were stopped comes back the next night too. Only the hourly retry of an update cut off part-way was already there; it is unchanged.
- **The password list has a copy on the USB drive.** It lived only in the HDD's monthly snapshots (encrypted); with the SSD and the HDD both gone, the photos and Nextcloud files survived on the USB drive and the passwords did not. The weekly copy now puts the newest monthly snapshot's encrypted list on the USB drive with the key copy, certificate and manifest that open it (`passwords/<run>/`, the newest three kept, each file checked against the snapshot's checksums on the HDD and again on the drive; a damaged list on the HDD is said and not copied). The recovery menu's **App password list** finds them there when the HDD has none, and opens them with the backup password as before. A server whose monthly snapshots are not encrypted (no backup key) gets no copy: the list never goes to the USB drive in plain text.
- **A restore into a folder that fills up mid-copy says what is there.** The room is checked before the copy; when the drive fills up anyway (something else wrote to it), the restore stopped with the system's "No space left on device" and a path. It now says how many files were copied, that they stay as they are, and how to go on (free space or remove the folder, then run the restore again, or restore to another drive).
- **Nextcloud's health check no longer waits an hour on a files folder it cannot write.** When Nextcloud answered but the check's test file could not be written (the files folder not Nextcloud's own, as after a new HDD before setup gave it back), the monthly update took the failure for a database migration still running and waited the full hour before rolling back. An answer with a failed test file now ends the wait after two minutes and says what to look at (`sudo bash install.sh`, or Nextcloud's log).

### Removed
- **The recovery menu's review of full monthly snapshots on the USB drive from before v0.21.1**, and the weekly copy's listing of them. Nobody runs those releases; an old drive that still holds a `backups/monthly` folder is looked through and cleared by hand (MONTHLY-MAINTENANCE.md says so).
- **The host step's handling of 0.28.0's Tailscale repository definition** (unmarked, with its key under `/usr/share/keyrings`). Only a definition that carries setup's marker is NekoServer's now; an unmarked one is someone else's and left alone, as any other.

## v0.41.1 — 2026-11-01

The second QA round of 0.41.0 (the three families the first round had not installed end to end: Fedora, Arch, AlmaLinux; the upgrade from 0.40.3; uninstall and reinstall), fixes, QA again.

**Tried for real** (WSL rig, the shared set of small virtual drives): Fedora 44 that had run 0.39.7, given the replaced drives: the fresh driven install (27 of 29 steps; the two left wait for the Tailscale sign-in), the feature check (45 of 45), the RAID drill (8 of 8), the fault suite (30 of 30) and the home gateway after a replaced SSD (the drill that found this release's bug; 11 of 11), then the update 0.41.0 → 0.41.1 on top (27 of 29, 45 of 45). Arch: the uninstaller with every choice, a fresh 0.40.3 install on the emptied drives (27 of 29, 46 of 47: 0.40.3's own minimum-size drive refusal, fixed in 0.41.0), the upgrade to 0.41.1 over it (27 of 29, 47 of 47), the fault suite (30 of 30) and the gateway drill (11 of 11). AlmaLinux 10.2 that had run 0.39.7, given the replaced drives: the fresh install (27 of 29), the feature check (45 of 45), the RAID drill (8 of 8), the fault suite (30 of 30), the gateway drill (11 of 11). The Python suite (1146 tests), the Rust suite (387) and clippy pass on the laid-out code; the dashboard's JavaScript tests on Fedora (where node is). Three things Alma showed were the rig's, not the release's, and are written down for the rig: a second distro started beside the one installing holds the shared SSD's Docker store and leaves its firewall rules in the network namespace all WSL distros share (new container networks then had no route: the restore test and the gateway's container check failed until the foreign rules were dropped); a round resumed outside the night window makes the monthly job refuse, as designed; a fault test interrupted while the HDD was full leaves its fill file. Then a third round on openSUSE Tumbleweed, with its own 0.41.0 server given the drives from the AlmaLinux round (the OS-reinstall family of cases: another server's SSD and HDD, this PC's own settings): the update to 0.41.1 (27 of 29, 47 of 47 once the Nextcloud administrator was taken back), the recovery menu driven end to end for the first time (photos from the USB drive and from the HDD, Nextcloud files, the password list, the nightly app and Steam/Samba settings: every restore checked against the live files by checksum, 3 sample photos among them), the HDD replaced by an empty one (setup's refusal and way out, the new drive asked for, fstab rewritten, backups and the feature check on it, Nextcloud back with an empty files folder, the Nextcloud files restored from the USB drive's last list with files, the photos restored from the USB drive) and the fault suite on the new HDD. The four fixes above for a new HDD came out of that drill; with every fix in, the update over the new HDD once more (27 of 29, 47 of 47) and the fault suite on it (30 of 30).

### Fixed
- **A PC that ran an earlier release, with a new SSD, no longer stops at the home gateway with a `sudo rm` to type.** Setup keeps a record of the gateway files it wrote so it never overwrites a file of someone else's; before 0.41.0 that record lived only on the SSD, so after the SSD was replaced (or Linux reinstalled) setup found its own earlier unit `nekoserver-gateway-lan.service` "not as it would write it, with no record" and refused. A file under NekoServer's own name (its unit, anything in a `nekoserver-*` folder) is nobody else's: it is now set aside (a copy beside it, `name.before-<time>`, said in the log) and written anew. A file outside NekoServer's name is still refused. The uninstaller removes the kept copies with the units. Seen on the rig: Fedora 44 that had run 0.39.7, given the shared SSD.
- **The gateway's certificates on an SSD moved to a reinstalled PC are adopted even when their old owner number now belongs to some other account.** The record on the SSD says NekoServer gave them that number (the old PC's caddy); whether the number is in use here (Fedora: "unbound") says nothing about the files. Before, setup asked for `chown -R caddy:caddy` by hand.
- **Settings from an earlier server, given this SSD, no longer stop the Nextcloud step.** Since 0.41.0 settings made anew after a Linux reinstall (naming the default administrator, "admin") take the installed Nextcloud's administrator back from the account store on the SSD. Settings that named another administrator setup had made up itself (a `neko_…` name from an earlier server of yours, say a new SSD set up before the old one was put back) were refused as "changed since it was installed (its image, trusted hosts or public address)", none of which had changed. A made-up name is never a person's choice, so it is taken back the same way; a name a person chose is still refused, and the refusal now names what differs (the administrator's name among them) and says that name cannot change after the install. Seen on the rig: openSUSE Tumbleweed with its own earlier settings, given the drives from the AlmaLinux round.
- **A new HDD no longer leaves Nextcloud answering 503 and setup waiting a quarter of an hour.** After the HDD was replaced (RECOVERY.md's way), Nextcloud's files folder on the new drive was made by setup as root's: Nextcloud (www-data) could not write there, answered "Service Unavailable", setup took that for an install still running and gave up after 15 minutes, and the gateway, accounts and maintenance steps waited behind it. A Nextcloud already set up whose files folder is empty now gets the folder back as its own, with the marker files it checks for, and setup says how to get the files back (the recovery menu, Nextcloud files, from the USB drive, then upload them again). RECOVERY.md says so in the "SSD or HDD broke" row.
- **A restore from the USB drive that is cut off (a terminal closed, a `kill`) unmounts the drive again, like Ctrl+C.** Before, the recovery menu left the USB drive mounted read-only, and every weekly copy after that failed with "mount exited with 32" (or, on another Linux, could not write). The weekly copy now also says in words when it finds the drive mounted read-only, and how to unmount it.
- **After a new HDD, the weekly USB copy no longer refuses week after week.** The HDD archive made anew holds a file Immich rewrites (its `.immich` marker files, say) under its plain name with the current content, while the USB drive keeps the first content it ever saw under that name and the later ones as dated versions beside it. The weekly copy compared the two, found "files differ between the HDD and the USB drive" and "verified files missing", and stopped with "Backup copies failed their checksum check" (the monthly job with it) until "Repair damaged backup copies" was run, which had nothing to repair. Two intact histories are now told apart from damage: a USB record from before this archive began is the USB drive's own (counted as "on the USB drive only", never as missing), and the archive's current content is added to the USB drive as a dated version, noted as the newest, so a restore has it too.
- **The recovery menu points past a backup that lists no files.** After a new HDD the weekly copy's newest list names no Nextcloud files (the folder was empty then), so "As of the last backup" restored nothing. When the newest list is empty and an earlier one is not, the menu says so and recommends the last backup that lists files.
- **A service whose drive record is older than a replaced drive says what to do.** The backups, the monthly job and the gateway each keep their own record of the drives; after a drive is replaced, a step that stops before theirs leaves them refusing with "should be the HDD (the old UUID); found …". The refusal now adds: if the drive was replaced, run setup so every service learns the new one.
- **The phone notification for a Btrfs pool reads correctly** ("The Btrfs pool (…) counts drive errors", not "The Btrfs drives (…) counts").

### Changed
- **The code is laid out legibly.** Much of the installer was written as one-line chains (`if x:raise …`, several statements per line, 200-column expressions), and the dashboard's and setup window's JavaScript, CSS and HTML and the shell scripts the same way (lines of up to 760 columns). Every `.py` file is now formatted by `ruff format` (`nekoserver/ruff.toml`: 120 columns, quotes kept), the web files by Prettier (`.prettierrc.json`) and `install.sh`/`uninstall.sh` by shfmt, with no change in what the code does (the formatters keep the program the same; the whole suite, the installs and the fault suite on the rig say so). Long message strings and regular expressions are left on one line: splitting those by hand could change them. The CI checks the layout, so a file put back the old way fails there; the step says the command that puts it right. Eight unit tests pinned to the old spacing, the file list or this PC's own maintenance journal were corrected.
- **SonarCloud's reading of the laid-out code, acted on** (the layout touched most lines, so its 70 remarks on code from earlier releases came up as new). Security: the certificate export (`gateway_cert.py`) checks its destination (a new `.crt`/`.pem`/`.cer` file in an ordinary folder, never under the system's own) and reads the SSD path as a checked mount path; the source import reads its two mounts the same way; the account setup's Docker calls refuse an argument with a control character; the CI installs Prettier without running package scripts, fetches shfmt over HTTPS only and keeps it in the runner's own temporary folder; the private network ranges are named once (`network_access.PRIVATE_NETWORKS`, `TAILNET`). Legibility: twenty nested `a if x else b if y else c` expressions became `if`/`elif` or a lookup, the guided setup's `checkpoint` is four short functions, repeated literals are constants (`.incomplete`, `--config `, `seerr:provider:`, `/data`, GNOME's settings program, `.config`), `[0-9]` is `\d`, the dashboard's service worker uses `async` functions instead of nested promise chains, and the `[hidden]` rule outranks the display rules by specificity instead of `!important`. The hardcoded RFC 1918 and Tailscale ranges remain (they are what the checks are for).
- **Code nothing used is gone, and one wrapper written twice is written once.** A hard-link publish helper (`neko_common.publish_no_replace`, superseded by the runtime's own) and the uninstaller's `read_settings` had no caller; the media and photos steps' identical "apply with the drives checked and the lock held" wrapper is `recovery_tools.guarded`; the Immich container bindings the acceptance and the snapshot checks both name are defined once.

## v0.41.0 — 2026-10-31

A QA round run as a company would: beta testers and IT on the WSL rig (fresh installs, the RAID drill, the fault suite), fixes, then QA again. Plus RAID and Btrfs pool health.

**Tried for real** (WSL rig, one shared set of small virtual drives: 40 GB SSD, 56 GB HDD, 19 GB USB): openSUSE Tumbleweed and Ubuntu 24.04, each a fresh driven install on the shared drives (27 of 29 steps; the two left wait for the Tailscale sign-in), the feature check (47 of 47 and 46 of 46), the RAID drill (a loop-device RAID1: healthy, a drive failed, rebuilt, read errors counted, stopped; 8 of 8), the fault suite (power cuts in every phase of the monthly job and the backups, app crash, Docker restart, USB, HDD and SSD unplugged, HDD full; 30 of 30 on both) and the home gateway after a replaced SSD (3 of 3). The Python suite passes on all five rig distros (Tumbleweed, Ubuntu, Fedora 44, Arch, AlmaLinux 10); the Rust suite and clippy on Tumbleweed. Not tried on real hardware: Btrfs pool device counters (unit-tested only), a real mdadm array on physical disks.

### Fixed
- **A replaced SSD or HDD no longer stops setup at /etc/fstab.** After a drive broke and a new one was put in (RECOVERY.md's way), setup refused the fstab line it had itself written for the old drive as "a line NekoServer did not write". A line of NekoServer's own for the same folder whose drive is not connected any more is now rewritten for the new drive, and setup says so; a line for a drive that is still connected, or one someone else wrote, stops setup as before. Found by the test rig's first-time installation after its drives were replaced.

- **A drive of exactly the minimum size is refused by the drive question, not a step later.** The drive space step wants the minimum free (20 GB on the SSD, 50 GB on the HDD), and a drive of that size in all never has it once formatted (ext4 keeps about 5 % for itself). The question now allows for that, so it never accepts a drive that the space check then refuses. Found by the rig's shared test drives, made exactly as big as the minimums.

- **A replaced SSD no longer makes setup refuse the gateway's own files.** The home gateway step keeps a record of the files it wrote (so it never overwrites a file someone else put there); that record lived only on the SSD, and after the SSD was replaced the step refused its own files on the system disk ("Unmanaged or modified gateway file preserved"). A copy of the record now stays on the system disk beside the files. Found on the test rig's fresh install on new drives.

- **The monthly update runs on a small SSD or HDD.** Setup accepts an SSD with 20 GB free (and an HDD with 50 GB), but the monthly job kept a 20 GB reserve (`maintenance.minimum_free_gb`) on top of the room its snapshot and the new app images need, so on such a drive it stopped every month with "Not enough space". The reserve is now at most a fifth of the drive; the message says when that is what it used. Found on the test rig's 33 GB SSD.

- **A monthly job cut off a few times no longer blocks every later one.** Each cut-off run (a power cut, a restart) leaves an unfinished snapshot of nearly full size on the HDD, which retention kept until newer complete runs replaced it; on a full HDD no run could complete, so they stayed for good and the job stopped every month with "Not enough space" (seen on the test rig after the fault suite's three cut-offs). When the HDD lacks room, unfinished snapshots are now removed first, oldest first, until the run fits; a complete snapshot is never removed for room.

- **Linux reinstalled with the SSD kept: Nextcloud is recognised again.** When the settings are made anew (the wizard asked for the drives again) Nextcloud's step compared the default administrator name with the one it was installed with and refused ("settings changed since it was installed"). The name is now taken back from the account store on the SSD, and setup says so. Found on the test rig's Ubuntu with the shared SSD of another install.
- **The gateway's refusal of a file it did not write says which files and what to do.** Instead of "Unmanaged or modified gateway file preserved", the message names the files, and the way out when they are an earlier install's (Linux reinstalled, or the SSD replaced by a release before 0.41.0, which kept no record on the system disk).

### Added
- **RAID arrays and Btrfs pools are watched, with the error counters that show a drive going bad early.** The dashboard's Drives page shows every Linux software RAID array (mdadm): each of its drives, a lost drive, a rebuild with its progress, and each drive's corrected-read-error count; each member's own SMART health is read too, and a member whose SMART fails marks the array as failing so the drive is replaced before it takes the array down. For a Btrfs SSD or HDD (a pool or a single drive) the per-drive read, write and corruption counters (`btrfs device stats`) and missing devices are shown. A lost drive, a rebuild, corruption, mismatching copies or an erring drive raise alerts on the page and the phone, with a "RECOVERED" message once the array is whole again. Setup refuses to put a new server on an array that has already lost a drive and says how to rebuild it first; a server already set up keeps running and warns instead (STORAGE.md, RECOVERY.md).

## v0.40.3 — 2026-10-31

Fixes for the project owner's review of 0.40.2.

### Fixed
- **"Repair damaged backup copies" never writes underneath a missing HDD.** With the HDD unplugged (or another drive in its folder), the repair copied the good USB copy into the empty folder on the system disk, said "repaired the HDD copy" and cleared the warning. Each drive the repair uses is now checked first (UUID, filesystem and Btrfs subvolume); a missing or wrong drive is left out, said as "NOT repaired" with the reason, and its warning stays. Every copy is written through the drive's own open folder, so a drive unplugged or swapped during the repair gets nothing more, and nothing lands on the system disk.
- **A copy found missing during a restore can be repaired right away.** The restore listed it in NOT-RESTORED.txt but did not pass it to the repair option, which then said there was nothing to repair. It is now recorded for repair, with the saved copy and its checksum, also when you stop the restore; copies you accepted as lost stay apart.
- **The uninstaller removes no container before its final checks.** It removed the apps' containers before taking the jobs' lock; now every destructive step comes after the checks under the lock, so a running job stops the uninstall with nothing removed.
- **The uninstaller holds the setup locks even when their files were missing**, so a setup started meanwhile waits instead of running alongside it.
- **A Btrfs pool's size is the pool's, not its first drive's.** A pool of a 32 GB and a 4 TB drive was accepted or refused depending on the order of its drives. A mounted pool now counts what the pool reports; one not yet mounted counts by its data profile (a RAID1 holds what its smallest mirror holds), or only its smallest drive when the profile can't be read.
- A test that gives files to other user IDs is skipped where the system refuses those IDs.

### Not in this release
- RAID and pool health monitoring with error counters is part-way built and comes in a later release.

## v0.40.2 — 2026-10-26

A company-style release test of 0.40.1: an IT team installed it for real on a clean Ubuntu 24.04 machine with systemd and test drives, QA tested every 0.40.1 change, and three customers used it (a first-time installer, a teenager on the phone dashboard, a photographer restoring after a lost HDD). IT fixed everything they found; QA then verified every fix (three rounds) before release.

### Fixed: backups and restores
- **Immich starts again after its weekly pause even when the HDD dropped out**, and when a missing drive comes back. It checks the SSD's Btrfs subvolume too, so it never starts Immich on another tree of the same drive.
- **Backups wait for setup on a Btrfs SSD too** (postponed, not failed).
- **The monthly restore test can't reach this PC's own services** (its network blocks the host as well as the internet).
- **Restored photos and Nextcloud files belong to you**, so you can open and upload them; restores get a folder of their own each time.
- **Damage a restore finds goes to "Repair damaged backup copies"**, the damaged copy is kept beside the restore, and copies you accepted as lost are named instead of asked about every time. The way to fix a damaged copy comes first in the message, so it's never cut off; cut messages end at a whole sentence or word.
- Drive return gives the real reason when something fails, and no longer reports services that aren't installed; each settings backup writes a line to the journal; the recovery menu shows local times, "0. Quit" and "1 file".

### Fixed: dashboard
- **Wrong-PIN messages appear inside the PIN box**, with the tries left and, when locked out, how long to wait and until when.
- **With a drive missing, backups show "Can't run now" / "Drive missing"**, never "All OK" or "Checksums match", and there is one alert naming the apps it stopped.
- **An undone monthly update says your apps are back on their previous version and work**, as a warning.
- Times in your own time zone; long messages cut at a sentence with "Show more"; "1 file"; apps setup never got to show "Not set up yet" (no Open link or restart menu); the Steam card only once the TV is set up; the filter chips fit a phone; the Fans page states the real heat rule.

### Fixed: setup
- **A download from Docker's or Tailscale's site is tried three times**; then setup names the site, the likely causes (a proxy first) and what to do, and the setup window opens to show it ("Stopped").
- **The setup window can be opened again** from the same account.
- **Drive sizes are stated in the question and checked before anything is mounted or saved**; a too-small drive is refused and you pick again. The drive list shows each drive's size, name and model.
- **The finishing screen and the log say in plain words what needs you and what waits for it**; waiting steps are no longer marked failed, and the step counts agree; the finishing screen stays in the window.
- Plain messages for a loop device, no home-network address and the app-login question; "Desktop: no" without a desktop; a 10-second pause when setup runs as root without your account; the READMEs give drive sizes, how to get git, the real step order and when the window appears.

### Fixed: docs and passwords
- **`account-setup.py show APP`** shows one app's login on the PC's screen with a QR code and clears it; the phone guide uses it instead of reading a root-only file.
- GUIDED-SETUP, RECOVERY ("Which situation are you in?") and DASHBOARD put the user first, with the technical detail under "For administrators". The recovery tools say what went wrong and what to do next.

### Fixed: drives and the uninstaller
- **An LVM snapshot is not taken for a copied drive** (a real copy still is). A hard drive that refuses the standby timer is left to its own settings instead of failing at every start.
- **The uninstaller:** never tells you to delete a swapped drive's folders by hand; says "size unknown without sudo"; removes setup's records on the system disk and says your fstab backup stays; removes nested Btrfs subvolumes with their folders.
- Nextcloud two-factor enforced only for other groups no longer lets a wrong saved admin password pass.
- The drive-return, HDD power and host snapshot services run with systemd sandboxing.

### Not in this release
- RAID and pool health monitoring (a degraded mdadm array or Btrfs pool is not yet shown on the dashboard) — planned next.
- Not tried on real hardware: LVM snapshots, LUKS, mdadm, and anything that needs the apps running.

## v0.40.1 — 2026-10-26

A full independent review of 0.40.0 (installing and the host, drives and recovery, the apps, backups and the monthly update, the dashboard), plus the findings of the project owner's own review, each checked against the code and fixed with a regression test.

### Fixed: backups and restores
- **The weekly Immich backup is one consistent point in time.** It copied the photos first (leaving out the ones changed in the last 10 minutes) and then copied the database while Immich kept running, so the database copy could name photos the backup never got. Now the photos are copied, then Immich's server and machine-learning containers are paused (its database keeps running), the last photos are copied whatever their age, the database is copied, and Immich starts again. Before anything stops, a note of what to start again is written, so a failure, a cut-off job or a power cut always starts Immich again (when the job ends, at the next start, or at the next run). The job counts as done only when both copies are verified. Immich is paused for a minute or two each week; one that was not running is left stopped.
- **One damaged copy that can't be repaired no longer stops every monthly update.** When no copy matches its checksum any more (an old version gone bad, a file deleted since), the repair menu can now accept that this saved copy is lost: it is moved to `neko-damaged`, its checksum is retired with the reason, and the monthly snapshot and updates go on. Until you decide, the monthly check says exactly how, and no longer claims "the other copy is kept" where there is none.
- **Editing a file whose first saved copy is damaged still backs up the edit**, and damage in the photo copy no longer skips the week's Immich database copy.
- **"As of the last backup" never quietly means an older backup.** Each copy notes when it starts and marks the run complete only once its files, checksums and list of files are saved. If a run was cut off or its list could not be written, the run shows as incomplete and the recovery menu says the newest backup did not finish, recommends "Everything ever kept", and labels the older list with its date.
- **Restores never land on the system disk.** With the HDD missing, the recovery menu wrote a restore into the empty `restored` folder on the disk Linux runs from. It now restores there only when the right HDD is mounted, and otherwise asks for a folder on another drive.
- **A file that became a folder (or back) in the Immich library no longer stops the photo archive.** It is kept aside in `neko-replaced`, as the Nextcloud copy does.
- **The weekly USB copy and the monthly update can no longer overlap**, also when the monthly run starts while the weekly copy waits for the USB drive.
- **The monthly update's restore test is isolated**: restored apps run on an internal Docker network with no port published, so they can't reach the home network or the internet. The network also gets no address on this PC, so they can't reach this PC either (an internal network alone still let them reach its services that listen on every address, such as Samba or SSH).
- **Backups and the monthly update wait for setup instead of failing.** A setup left at a question overnight made them fail after their 3-hour wait; now they are postponed ("setup is running"), and the monthly update tries again the next night.
- **Long database updates are not rolled back.** The monthly update now waits up to an hour while Immich, Nextcloud, Jellyfin or an Arr app is still updating its database.
- Restore lists are no longer written twice by the monthly check (the history covers what RECOVERY.md says), and half-written copies left by a cut-off run are cleaned up.

### Fixed: drives
- **Btrfs pools over several drives can be set up.** Setup compared the mounted drive with the one Linux names for the pool's UUID, which can be another drive of the same pool, and then refused it as "Wrong filesystem" on every run. It now compares the filesystem's UUID, type and subvolume.
- **An encrypted drive already in your settings** (after reinstalling Linux, say) must also be unlocked at every start (listed in `/etc/crypttab` without `noauto`), as a newly picked one must; setup says which one to add.
- **The HDD power service works on encrypted (LUKS) and LVM hard drives**, which keep their own power settings. It failed at every start before.
- **A copy of the SSD or HDD left connected is caught**: Docker and setup stop with "Two drives look the same (one is probably a copy); unplug the copy", instead of perhaps using the copy.
- Every drive of a Btrfs system pool counts as the system disk (never offered as the SSD, HDD or USB drive), every drive of a Btrfs HDD pool gets the standby timer, and setup without sudo no longer calls a pool "cloned drives".

### Fixed: installing and updating
- **Debian 12 can be updated again.** Every run after the first replaced Debian's newer Rust (rustc-web) with the old rustc 1.63, and the runtime build then failed every time.
- **The runtime build downloads first, with its retries, then works offline**, and says so when crates.io doesn't answer (it blamed "cleaning" before).
- **Setting up again after an older uninstaller no longer runs every backup at once.**
- **openSUSE: one package source that doesn't answer (a Packman mirror, say) no longer stops setup**; setup names it and goes on.
- **Debian without sudo:** install.sh says how to install sudo or run as root with your account named, and setup no longer assumes user ID 1000.
- The READMEs explain how to update a clone made before 0.40.0.

### Fixed: uninstalling
- **The uninstaller checks each drive again right before deleting anything on it**, and deletes only through the drive it checked: a different drive mounted in its folder, before or during the uninstall, loses nothing. The SSD and HDD are checked with their Btrfs subvolume too.
- **It never runs alongside NekoServer's jobs.** It stops if a monthly update or an interrupted backup is unfinished or starts while you confirm, never stops a running update, and holds the jobs' lock while files are deleted. Lock files in `/run` are no longer deleted.
- **Samba must really have stopped** before Samba accounts, shares or files are removed; otherwise it stops and turns everything back on as it was.
- NekoServer's socket units are removed too.

### Fixed: dashboard
- **"Restart app" works again.** Since 0.39.6 it always reported a failure after 4 minutes (and held the backups' lock meanwhile), because the control service could not see the app answer. OpenRGB now runs alone in a private network, reached through a socket only root can open (systemd's socket proxy); the control service is back on the computer's own network. Upgrades take effect without a restart, and setup and the feature check confirm OpenRGB is out of other programs' reach.
- **Wrong PINs lock out only the device that typed them**, also when several devices share one Tailscale account, and programs on the server can't use up the tries the devices share.
- **The setup window can only be opened by the account running setup (or root)**, even when another account sees its address first.

### Fixed: apps
- **A graphics chip that decodes but can't encode video** no longer leaves setup "needs setup" for ever (or suggests switching Immich to hardware conversion).
- **Nextcloud with two-factor sign-in** no longer fails the account check on every run (or holds up hardware video).
- **Imports:** a file whose name isn't UTF-8 no longer stops a whole import; `smb3` shares and shares that mount on first use are recognised; copying from FAT32/exFAT drives and some SMB shares no longer fails part-way with "changed".
- Existing app logins with a dot, a space or a leading digit (or Immich's `admin@localhost`) can be entered; Seerr kept without Sonarr and Radarr no longer leaves connecting the apps unfinished; removing Prowlarr or Jellyfin says which connections to remove by hand.

### Upgrade notes
- `git pull` in `nekoserver` (a clone made before 0.40.0 must be cloned again), then `sudo bash install.sh`. With lighting on, setup sets up the new OpenRGB socket and checks it; no restart of the computer is needed.

## v0.40.0 — 2026-10-26

Bug fixes only. A review of the code that changed since 0.39.2, and every supported Linux family installed for real.

### Tried for real
- **Every family, on this release:** an update from 0.39.1 on openSUSE Tumbleweed and Ubuntu 24.04, an upgrade from 0.35.1 on Fedora 44 (four releases in one step), and the update or a fresh installation on AlmaLinux 10, Arch and openSUSE Leap 16. On each: setup finished every step it can do there (26 of 28: the Tailscale sign-in waits for a person, and the TV and desktop steps need a screen), the feature check passed every check (45 to 47), with the monthly update run for real (checksum check, snapshot, restore test of every app, update), and the fault suite passed 30 of 30 (the monthly update cut off at three points, a backup cut off, the HDD and the USB drive unplugged, an app crash, a full HDD, a Docker restart).
- The unit tests passed on all of them (939 Python tests on Python 3.12 to 3.14, 332 Rust tests).

### Fixed
- **"As of the last backup" restores the newest list, also after the clock was set back.** Each weekly copy's list of the files you had is named by the time it was made, and the restore takes the newest name. A clock that went back (a flat clock battery corrected later, a virtual machine catching up) gave the new list an older name than last week's, so the restore would have used last week's files. A new list now always takes a name after the newest one there. Found by the unit tests on the test rig, where WSL's clock stepped back two seconds.
- **A copy of a Btrfs drive is no longer taken for a pool.** Since 0.39.5 setup accepts a Btrfs pool over several drives (one filesystem UUID, a device on each drive). A drive copied with `dd` or a disk cloner has the same UUID too, and was offered as such a pool; mounted by UUID, the kernel could then write to either drive. Each device of a real pool has its own device UUID (`UUID_SUB`), so setup now checks that, and two drives with the same one stop setup as cloned drives, as before 0.39.5. The system's own drives are never checked (they are never offered).
- **Only the setup window itself can claim it.** The window's address works once: the first page load gets the session, and everything after needs it. A request of another kind (a POST) sent to the address first claimed it too; now only loading the page does.
- **The unit tests no longer depend on this PC's own NekoServer.** One test read the real `/etc/docker/daemon.json`, and failed on a PC running NekoServer whose SSD was mounted; a runtime test of the dashboard's app restart read the PC's own unfinished-job records, and failed on one with a backup left to finish.
- **The unit tests no longer stop at a question when run from a terminal.** One test let setup ask about the backup counts of releases before 0.30.0, and waited for an answer for ever; it now runs as without a keyboard, as in CI.
- **Setting up again after the uninstaller no longer runs every backup at once.** The uninstaller left systemd's record of when each NekoServer timer last ran. A later setup on that PC switched the timers on again, systemd counted every night since as a missed run, and the nightly app snapshot, the settings snapshot and the weekly USB copy all started right away: the apps stopped for the snapshot just as setup said it was done. The uninstaller now removes those records with the timers. Found by a fresh installation on AlmaLinux 10 after the uninstaller.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).
- **The repository's history was cleaned**: old built runtimes and large recordings were taken out, so a clone is about a fifth of the size. A clone made before this cannot simply `git pull`. Clone again, then copy your `my-config.json` into the new `nekoserver` folder.

## v0.39.7 — 2026-10-25

### Fixed: backups
- **Restoring an earlier date can no longer return newer content.** When a Nextcloud file became a folder, the weekly copy moved the file's saved copy into `neko-replaced`, but the list of files from that earlier date still pointed at the old place. That date's restore then called the file missing, and if a new file later took the same name, it restored the new file's content and said its checksum matched. Each list now keeps the checksum of every file's content, and a restore goes by that checksum: it finds the saved copy wherever it was moved, and names a file as not restored rather than hand out other content. Lists made by 0.39.6 have no checksums; from those, a file that changed between file and folder later is left out (with a note), and "Everything ever kept" still has every saved copy.

## v0.39.6 — 2026-10-25

The improvements left out of 0.39.5, plus a fresh review of the drive checks, each with a regression test.

### New: restore only the files you had
- **Restoring photos or Nextcloud files can leave out what you deleted or renamed.** Each weekly copy now writes down which files you had at that moment. The recovery menu asks **Which files?**: as of the last backup (recommended), as of an earlier backup you pick, or everything ever kept. A backup made before 0.39.6 has no list yet; then it restores everything, as before (RECOVERY.md).
- **Nextcloud's database is copied every week.** The weekly USB backup now also saves Nextcloud's database (users, shares, calendars, contacts) on the HDD and the USB drive, the newest 4 in each place. Without a USB backup, only the monthly snapshot has it (STORAGE.md).

### Fixed: drives
- **A working Btrfs drive is no longer unmounted at every start.** The start-up check took a mounted Btrfs SSD or HDD for an unplugged one, unmounted it, mounted it again and restarted Docker.
- **Encrypted drives Linux won't unlock by itself are named.** Setup now refuses a drive that `/etc/crypttab` marks `noauto`, or an LVM volume or RAID array on an encrypted drive that is not unlocked at start, and says which one to add. Otherwise the apps would stay stopped after a restart.

### Fixed: apps
- **Taking an app out cleans up after it.** Removing Sonarr, Radarr, Lidarr or qBittorrent from `media.services` also removes the connections setup made to it in Prowlarr, the other apps and Seerr. Connections you made or changed yourself stay.
- **A new qBittorrent password reaches Sonarr, Radarr and Lidarr.** A password changed with `account-setup.py` is saved in their download clients too, and each setup run checks that they can still sign in.
- **qBittorrent's manual mode is kept when Lidarr is not used.** Automatic mode is now set only when Lidarr is selected (music needs it), and setup says when it switches it back on.
- **Imports can't be redirected while copying.** A file or folder swapped for a link during an import can no longer make it read outside the source or write outside the library. An imported copy is also never more open than its original: other accounts on the server can read it only when they could read the original.

### Fixed: dashboard
- **Wrong PINs lock out only the device that typed them.** One phone typing wrong PINs no longer locks out other devices or the server itself. Many devices guessing together are still slowed down. If you still use a 4-digit PIN from before 0.28.7, set a 6-digit one (`sudo bash install.sh`).
- **Only the control service can reach the lighting service (OpenRGB).** Before, any program on the server could change the lighting without the PIN. Network lighting devices (smart bulbs, light strips on the network) are not reachable from it any more.
- **The fixed fan speed slider starts at "Never slower than".** It showed speeds the fan would never really run at.
- The README says that apps can be restarted or closed from the dashboard (with the PIN), and the "busy" answer (E47) uses the standard reason "Conflict".

### Fixed: monthly update
- **Old app images are always cleaned up.** Also with `keep_snapshots` 1, after a rolled-back update, and after a run that stopped once it had downloaded updates. Each run's `retention.json` lists the images it removed.

### Fixed: uninstalling
- **Samba is switched off again when NekoServer switched it on.** It stays on when it was on before NekoServer, or when your own `smb.conf` uses it.
- **Nothing left behind goes unsaid.** The uninstaller names every file and step it could not remove or switch off, lists them at the end, and no longer says "Done." when something is left.
- **Run from a fresh download, it finds your drives** from setup's records in `/etc`, and says which settings it used.
- **Your own journal size limits are kept.** The server OS step keeps stricter limits you set yourself and says when it replaces a looser one.

### Upgrade notes
- Run `sudo bash install.sh` once after `git pull`, as usual. The "as of" restore works from the first weekly copy after the upgrade.

## v0.39.5 — 2026-10-25

A review round of the parts everyone meets first (installing and setting up, setting up the apps, the monthly update), then a strict review for edge cases, races and system failures, and a full independent review of the whole project, each finding checked against the code and fixed with a regression test.

### Fixed: setting up the apps
- **Lidarr can import music downloads.** qBittorrent puts a download in its category's folder only in its automatic mode, and starts new torrents in manual mode unless told otherwise, so an album landed on the HDD, where Lidarr (which sees only the SSD) could never find it. Setup now switches new torrents to automatic mode. Torrents you already have keep their mode.
- **Choosing an indexer in the terminal works.** Prowlarr refused every indexer chosen there (its catalogue leaves the indexer without a profile), so each one ended at "do it in the Prowlarr page". It now goes into Prowlarr's own profile, switched on.
- **Hardware video you switched off stays off.** Switching it off in Jellyfin or Immich was undone by the next `sudo bash install.sh`. **Intel graphics older than about 2014** (before Broadwell) use VA-API instead of Quick Sync, which they can't do on Linux: a one-second Quick Sync test encode decides, and a Quick Sync setting an earlier NekoServer made on such a chip is replaced.
- **Imported TV shows show up as shows.** Each imported folder went into a folder of its own inside the library, which Jellyfin read as one show (with the real shows as its seasons), and Sonarr, Radarr and Lidarr as one item. Imports now land in the library folder itself. What an earlier release imported is moved up there (a rename on the same drive, nothing copied again); a show already there under the same name is named for you to merge. A file already in the library that differs is never replaced.
- **Importing again is quick, and a source that is gone after its import no longer nags.** A rerun read every file twice (source and copy) to compare them; copies now keep their original's date, so they are recognised by size and date. A drive unplugged after its import finished is reported as "copied before", not as a step left to do.
- **New Jellyfin libraries are scanned at once,** so films and shows imported before them appear without waiting for Jellyfin's 12-hourly scan.
- **A failed onboarding says why,** not only the error's type.
- **NEXTCLOUD.md and MUSIC.md** say where your logins really are (the credentials file) and that a password you change in the app must also be told to setup (`account-setup.py`, **c**); following the old text made every later run stop at the account check.

### Fixed: installing and setup
- **Commands in the docs use your settings.** `sudo bash install.sh --module … --apply` without `--config` ran on the template `config.json` (no drives in it) and saved your answers into it, which git tracks, so the next `git pull` failed. Your `my-config.json` is now the default once the wizard has made it, and changes are never saved into the template.
- **What setup can't run without is said first.** SELinux enforcing (Fedora's default), an older systemd, a pending restart: said before minutes of package installs and the runtime build, and checked once more just before setup starts (an automatic update may have installed a kernel meanwhile).
- **Start in the setup window no longer says yes to everything.** It sent every switch as drawn, all on, so the TV (Steam), phone alerts and outage alerts were saved as wanted without being asked. Only the switches you press are saved; the TV and the alerts you leave alone are asked about when setup gets to them.
- **The setup window's last screen** shows setup's own verdict ("ready" or "almost ready"), the same as the terminal, the first checkup included.
- **A desktop whose Desktop folder is the home folder** (as xdg-user-dirs sets it once `~/Desktop` was deleted) gets its app shortcuts in the menu, instead of a failed step.
- **Ctrl+C at "Use them for this release?"** stops setup. It used to count as "no", make new settings, and never offer the earlier ones again.
- **A FAT32 or NTFS USB stick** is named, with how to make it usable (exFAT or ext4), instead of "connect it first".
- The welcome text and README no longer claim more than setup does (a full system update happens on Arch only; the timezone is the PC's own).

### Fixed: the monthly update
- **Updates are downloaded before any app stops.** The release lookups and image downloads ran after the apps had been stopped for the snapshot and its restore test, often for hours, and one GitHub or registry hiccup then cancelled the month. They now run first, with the apps still up, and a download refused for now is tried again (after 30 s, 1½ and 4 minutes); one that can never work (no such image, no access) is not. A new Immich release that can't be checked waits for next month without holding back the other apps.
- **The apps are never stopped in the morning, and big libraries still get their monthly snapshot.** On a big library, checking every copy could run past the night window. The check now reads each copy once instead of three times; the window is checked right before anything stops (past it, nothing is stopped or changed); and after a month whose night ran out, the next month takes its snapshot first and checks the copies the month after. If even that does not fit, the run fails and says how to give it a longer night.
- **A full or unmountable USB drive no longer cancels the monthly update.** Only damage found by the checksum check does; anything else is "done in part", with the reason.
- **Old app images are cleaned up.** Each update left the replaced images on the SSD for good (a few GB a month). NekoServer's own images from two updates ago that nothing uses are removed; your own images are never touched.
- **A rollback can start apps whose old image Docker lost** (`docker image prune -a`, say): it is loaded from the snapshot's `images.tar` first.
- **Nightly backups wait for an unfinished monthly update** (after a power cut, say) instead of saving its half-updated apps as the newest backup. **The weekly USB copy** no longer runs during the monthly update.
- **A run cut off while it tidied old snapshots** (a restart, the time limit) is finished as the installed update it is, not reported as a failure.
- **A later NekoServer release can finish an unfinished run** an earlier one started. A restore test cut off part-way has its unpacked copy removed (it took a second copy's space on the HDD for months), and a test container Docker can't remove never keeps the real apps from starting. The resume step at boot no longer waits for the backups' lock when there is nothing to finish.

### Fixed: backups and checksums
- **Damage found by the monthly checksum check stays on the dashboard and in the repair tool until it is repaired.** The next weekly or hourly copy (which compares only size and date) wiped it: the dashboard said "Checksums match", the phone got a false "RECOVERED", and the repair tool found nothing to repair. "Checksums match" now means a full check in the last 35 days.
- **A backup drive unplugged or failing during a backup** stops the backup with a clear message, instead of writing onto the system disk.
- **A full HDD no longer stops the nightly app backup for good:** what failed runs left is cleaned up, then older app backups if needed, always keeping the newest. A power cut while old backups are deleted no longer leaves a broken one that looks complete.
- **Files deleted or renamed during a backup** are skipped and counted instead of failing it; Nextcloud names that are not valid text are left out and listed; photos dated in the future (a camera with a wrong clock) are backed up; a copy dated in the future is checked by content. Music added in the last 10 minutes waits for the next run instead of failing it.
- **Errors say the system's reason** ("No space left on device"). If the USB drive fails to unmount, the real reason the copy failed is still shown. After the clock is put back, "last success" and alerts are no longer lost or held back.
- **The weekly USB copy no longer blocks the nightly backups for hours**, and never overlaps the monthly update. **Nightly backups wait only while a monthly update is cut off half-way** through installing or rolling back (the settings backup never waits), and an unfinished update is tried again every hour.
- **The dashboard says why a monthly run was "done in part"** (USB drive full, Immich release not checked).
- **A monthly run that stops before its update** no longer leaves a downloaded version for the next `sudo bash install.sh` to start untested: apps are pinned to the images they run before anything is downloaded.
- **The old-image clean-up** never removes an image that carries a name of your own, and also removes images from updates that were rolled back. Monthly runs that archived nothing no longer leave folders behind.
- **`sudo bash install.sh --finish-update`** lets a newer release finish a monthly update an earlier one left unfinished.
- **A restore that fails says why** (a full drive, a folder that already exists, a damaged file) and names the half-filled folder it left. A restored file is read back from the drive, not from memory, so damage on the drive is seen.
- The docs say which data is backed up nightly, weekly and monthly (the Nextcloud database only monthly), how to start a restored app at its exact saved versions, and that restoring an add-only copy brings back deleted and renamed files.

### Fixed: drives, recovery after reinstalling Linux
- **Reinstalling Linux keeps your earlier settings on the SSD,** also when the runtime is built again: they move with the kept copy, as `config.previous-<date-time>.json` (the oldest is always kept).
- **Reattaching checks first that the NekoServer program kept on the SSD runs on the new Linux** (one built on Arch, Ubuntu 24.04 or Fedora may not run on Debian 12); if not, it says to run `sudo bash install.sh` first and changes nothing. New `recover.py install-docker --apply` installs Docker the way setup does and leaves it stopped for reattaching (RECOVERY.md step 4).
- **More drive layouts work:** a Btrfs pool over several drives (it stopped setup with "Duplicate filesystem UUID"), LVM volumes, software RAID and encrypted (LUKS) drives that Linux unlocks at start. The disk Linux runs from is recognised from every place it is mounted (a Btrfs system partition, an EFI partition at `/efi`).
- **Setup's and the recovery tools' drive checks also check the Btrfs subvolume**, and refuse a mounted folder of an ext4 or XFS drive instead of saving it as a "subvolume" that stopped every later run.
- **Docker's settings are checked before they are written,** and never point Docker at the SSD before its drive guard exists: a setup cut off in between could let Docker fill the folder on the system disk. Settings Docker's service already sets as flags are named instead of leaving Docker unable to start.
- **An fstab line for the same drive or folder written another way** (`/dev/disk/by-uuid/`, `LABEL=`, `PARTUUID=`, a trailing slash) is found, instead of a second line being added. A slow mount (a big drive replaying its journal) is waited for up to 10 minutes. A drive choice is saved only once it passed the checks. The first fstab copy is never left cut off.
- **The shared lock never makes folders on the system disk** when the SSD is not mounted. When the HDD comes back, its power settings are applied again and the dashboard sees the drives again.
- RECOVERY.md: reinstall with a normal `sudo bash install.sh` first; the read-only mount commands for copying the settings off the SSD are really read-only; rebuilding Immich from the USB drive uses the recovery menu's newest-version restore.

### Fixed: setup, packages and interruptions
- **Upgraded servers get the steps newer releases added** (desktop shortcuts, hardware video): they are added to your `my-config.json` profiles (a step you took out stays out), and "ready" counts every step of the release.
- **The tools in the NekoServer folder use your `my-config.json`** without `--config` (`acceptance.py --save` from the docs failed on the template); the copy kept on the SSD uses its own settings. Settings you once put into the template `config.json` are recognised, with how to keep them.
- **Ctrl+C stops setup** at any question (it skipped that step); Ctrl+D leaves a question for later. **Closing the terminal or losing an SSH connection** no longer leaves services blocked from starting.
- **A slow package download** makes setup ask the package manager to stop and wait for it, instead of killing it; a pacman lock left by a cut-off pacman is removed. If automatic updates or a software centre are installing, setup says so and waits (up to 10 minutes). A package source that only gives a warning is tried again. A package that can't finish installing is named, with how to fix it. On Debian and Ubuntu, Steam's licence is accepted for the TV step.
- **A damaged Docker or Tailscale repository key** is downloaded again. **A wrong clock** is named, with `sudo timedatectl set-ntp true`.
- **The runtime build** retries its downloads, says what failed in one line, and is rebuilt when a build was cut off.
- **The setup window's address works once:** the browser that opened it gets a private session, so another account on the PC that reads the address can't answer setup's questions. A window closed mid-reply no longer prints errors into the terminal.
- **Folders setup makes are never left owned by root** after an interruption (TV, desktop shortcuts, app settings); earlier leftovers are repaired. A leftover file from a cut-off first save no longer blocks the account store. An old record on the system disk can no longer undo newer results. Connecting the media apps holds the backups' lock. Onboarding problems end as "needs setup" with the reason.
- **Samba share paths** may use only letters, digits, spaces and `. _ - /` (a line break could have added settings to Samba's configuration). Shared folders over Tailscale work after a restart: Samba waits up to a minute for the Tailscale address.
- **The TV step** brings its own YouTube and Steam shortcuts up to date when the browser or Steam moved, instead of stopping. Debian's Firefox ESR gets the Web Browser shortcut.

### Fixed: apps
- **The setup checkup no longer reports Sonarr, Radarr and Lidarr as "still to set up"** when their qBittorrent connection is correct, and **no longer fails Nextcloud when you switched it off**.
- **Imports:** a finished import is not copied again (Sonarr, Radarr or Lidarr may have renamed those files since; to copy what is new: `source_import.py --again`); a file the library already has differently is kept and named instead of refusing the whole source; earlier imports move up even with their drive unplugged; a network share no longer mounted after its import is "copied before". The source wizard asks for the folder that contains your shows, films or artists.
- **Hardware video you switched off stays off**, even after a run that couldn't check the app. Intel chips that encode only in low-power mode get hardware encoding in Jellyfin. Immich still starts when the graphics chip is gone (also in the monthly update).
- **Nextcloud keeps working over the home and Tailscale addresses** after its Docker network is recreated, and its database gets a minute to shut down cleanly. Seerr request setup accepts Sonarr 5.

### Fixed: dashboard, alerts and fans
- **Restore tests you witnessed stay recorded** through later checks (including the one setup runs at the end), and a complete server check no longer turns into "Finish checking your server" the next day; a recheck is suggested after a month.
- **Phone alerts now include** Tailscale signed out or about to sign out, monthly backups without a backup password, and the outside heartbeat not being reached. A 5-minute collection interval is no longer called "monitoring unavailable".
- **A hung drive no longer freezes the dashboard.** The heartbeat isn't called stale right after boot. The Sensors page no longer shows unconnected 127 °C probes.
- **Fans** keep following their curves while the control service waits for an app restart, survive a crashed request, and a fan that stops at its speed (with Detect fans run) is put to full speed with a note. A processor at its normal 95–100 °C under full load no longer triggers full speed or an urgent alert straight away: that happens at the chip's own limit, at 105 °C, or after 5 minutes above 95 °C. The fan speed slider starts at 20 %.
- The docs no longer call the phone dashboard read-only (changes need the PIN).

### Fixed: uninstalling
- **The uninstaller waits** while a monthly update or setup is running. **If it stops part-way**, NekoServer's services are switched back on. It replaces `/etc/fstab` and Docker's `daemon.json` in one step and keeps the old ones beside them, keeps Docker's start guard while Docker's data stays on the SSD, finishes or safely undoes a package install setup left half-done, and lists drives it couldn't unmount and anything it couldn't remove.
- **A preview of the uninstall without sudo** still says which folders it couldn't look into. On Python 3.14 those folders were quietly skipped instead.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).
- If `git pull` stops because `config.json` has local changes (0.39.4 could save your answers into it): run `git checkout -- config.json` (your settings are in `my-config.json` and on the SSD), then `git pull` again.
- If you imported TV shows with an earlier release, this run moves them up into the TV library; Jellyfin then shows them as shows. If it names a show already in the library, compare and merge the two folders by hand.

## v0.39.4 — 2026-10-25

A review round of the parts no review had covered yet: recovery, the gateways and remote access, and the dashboard.

### Fixed: recovery
- **One damaged or missing file no longer stops a whole restore.** A photo damaged on the HDD (and so never copied to the USB drive), or a file a backup lost, made the recovery menu refuse every photo or Nextcloud restore from that backup, whichever backup was tried. Now the good files are restored. The damaged or missing ones are left out and named (in the restore folder's `NOT-RESTORED.txt`) so they can be taken from the other backup. A missing file is asked about first. A checksum list a drive lost mid-write no longer blocks the restore either: the files come back unchecked, and the restore says so.
- **A Nextcloud folder of your own named `neko-damaged`** (or a file named like NekoServer's checksum list) at the top of your files is restored like any other. Only the backup's own, at the top of the whole backup, is left out, the same line the backup program draws.
- **App and settings restores keep each file's owner and permissions,** so a file copied back works for its app (copy it with `sudo cp -a`). A night when Steam was open (no Steam files in it) is marked in the list of nights and named after the restore.
- **Names that end in a dot** (`notes.`) get their newest version back; **a file without an extension** (`README`) no longer takes another file's versions (`README.md`'s).
- **The monthly restore** says where the password files are when the snapshot was made without a backup key (in it, unencrypted). **Restore tests of a photo snapshot** accept copies on a desktop drive whose path has spaces (`/run/media/you/My Passport`). **A password-list restore that cannot unpack** (a full disk) says why and leaves nothing half-done, instead of a Python traceback.
- **Nextcloud files ending in `.part`** that are your own are backed up; only Nextcloud's own unfinished uploads are left out.
- **Restores and media imports never read the system's own folders** (`/etc`, `/root`, `/usr` and the like), even if a settings file or a backup's own record were changed to point there. Your drives, wherever they are mounted (`/srv`, `/mnt`, `/data`, a USB drive under `/media` or `/run/media`), and folders in `/home` work as before. A drive name may also have brackets and a few other signs (`My Passport (2)`).
- **The weekly USB copy waits** for a restore from the USB drive (up to 8 hours) and for a photo archive run still writing (up to 6 hours), instead of failing.

### Fixed: gateways and remote access
- **Tailscale routes are recognised by their port and where they lead,** not by the server's name or the dashboard's port. A renamed server (or one added to the tailnet again after a reinstall), a dashboard moved to another port, or a Serve route of your own on another port no longer stops remote-access setup with "Malformed gateway ownership journal". A route of your own is never touched. A route of yours on a port NekoServer needs is named, with what to do. A dashboard moved later keeps its phone address: the route follows after every setup run.
- **Nextcloud on this PC itself (`http://127.0.0.1:8088`) works with a gateway set up.** Nextcloud was told to build every link as https, so this PC's own browser was sent to https on that port, which fails. Both gateways already tell Nextcloud the client used https, so the forced setting is taken out (and removed where an earlier release set it). Its link address no longer flips between the two gateways' ports either.
- **Tailscale's 180-day sign-in.** Setup now says to switch off key expiry for the server. The dashboard warns two weeks before a sign-in runs out, and says so at once when the server is signed out.
- **Firewall advice for nftables** gets an `nft` command instead of a `ufw` one, and every firewall hint says to test the home addresses from a phone (the check from the PC itself passes through any firewall).
- **An old home address never opens another app.** A port given to an app stays with it while the app is not selected, and the app gets it back. A port the gateway closed a moment ago no longer looks taken.
- **Immich, Navidrome and Nextcloud are published only once their first account exists,** as Jellyfin and Seerr already were: an app reset or restored empty would otherwise let whoever opened it first become its administrator.

### Fixed: dashboard
- **An unplugged temperature probe no longer holds every fan at full speed.** Many boards report a probe input with nothing connected as 127 °C. Fan control and fan detection took it for overheating. Readings of 127 °C and above, and sensors reporting a fault, are not taken for temperatures.
- **Failing phone alerts and a failing outage monitor** now show as "needs attention", not only as a badge on one card.
- **A backup that keeps failing stays "Failed"** once it is also overdue, instead of dropping to "Overdue".
- **An app that runs but does not answer** raises an urgent alert, as the page shows it, and is not reported as recovered by phone.
- **A switched-off backup** no longer shows "schedule is off" among the issues. An alert sent about a switched-off backup or an app taken out of the list is dropped quietly, never followed by "RECOVERED".

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release). Afterwards, in the Tailscale admin console, choose **Disable key expiry** for this server if it isn't already.

## v0.39.3 — 2026-10-25

The monthly run now starts by checking every backup copy, a guide for both ways of following the installation and for uninstalling, and a round of fixes to the uninstaller, the setup window, the backups and the dashboard.

### Changed
- **The full checksum check is the monthly run's first step: check, then back up, then update.** Every copy in the HDD photo archive, and every copy on the USB drive when it is plugged in, is read and compared with its recorded checksum before the monthly snapshot is taken and before any app is updated. Damage stops the run there, with nothing backed up or updated, and the dashboard says which files. The hourly photo archive and the weekly USB copy no longer do this every 4 weeks: they compare size and dates, so they stay quick. A USB drive that is not plugged in is checked in the next monthly run it is plugged in for. The setup space check no longer counts the 4-weekly check either.
- **A guide for the two ways of following the installation** (README, "Two ways to follow the installation"): how to open a terminal and start setup, how the setup window opens, how to open it again (the terminal now always shows its address), and what "Answer this step in the terminal" means. **Uninstalling** is now step by step: the dry run, the checklist (numbers switch lines on and off), the plan and the two confirmations.

### Fixed
- **The uninstaller removes only NekoServer's own folders.** It removed whole top-level folders of the drives, so a folder of your own there went too, and it trusted the settings' mount folders whatever was mounted there. Now only the folders NekoServer makes are removed (the plan lists the rest under "Not NekoServer's, so these stay"), and nothing at all on a drive whose UUID is not the one the settings name, or that is not mounted.
- **The uninstaller stops before deleting anything when something does not stop.** A service or timer that would not stop (or timed out), the fan control not handing the fans back to the motherboard, a container that stayed, or Docker that kept running: each now stops the uninstaller with "Nothing has been deleted yet" and what to do, instead of deleting files under a running program.
- **Samba accounts that were there before NekoServer stay.** The uninstaller removed every account in the share's "valid users". Setup now records the accounts it creates, and only those go; an account you had before is named as staying.
- **Only NekoServer's own Tailscale Serve routes are switched off.** `tailscale serve reset` removed your own routes too. The uninstaller now switches off, one by one, only the ports in NekoServer's record that are still exactly as it set them.
- **The PC can sleep again after uninstalling.** The sleep, logind and journald settings NekoServer added are removed, and the five sleep targets it masked are unmasked. Ones you had masked before NekoServer stay masked (setup now records them before masking).
- **Debian 12 builds the runtime with Debian's newer Rust.** Debian 12's own Rust (1.63) is older than the runtime needs (1.85), and the `rustc-1.85` names setup looked for exist only on Ubuntu. Setup now installs `rustc-web` and `cargo-web` (1.85 in bookworm, newer in bookworm-security), which provide the same commands. When an older `rustc` is already installed it does not replace it on its own: it says to run `sudo apt install rustc-web cargo-web`.
- **A late click in the setup window can no longer overwrite the settings.** A switch pressed, or Start pressed again, after setup had started wrote the window's old copy of the whole settings file over the one setup was updating (the drives it had found, say). Once setup starts, from the window or the terminal, the choices are fixed and later requests are refused; a switch before then changes only its own setting, on the file as it is.
- **Steps that ask in the terminal only say so in the window.** Connecting the media apps, music setup and the shared-folder password run programs of their own, whose questions never reached the window. The window now shows "Answer this step in the terminal" while they run.
- **Nextcloud added later is on the USB drive too.** The weekly USB copy kept copying only what was set up when it was installed. It is now brought up to date after every setup run: Nextcloud added later is copied from the next week on, and an Immich switched off is no longer asked for.
- **A Nextcloud-only USB copy no longer needs the old Immich database.** With Immich switched off its Compose file stays, and the weekly copy took that as a reason to copy the Immich database, reporting itself incomplete once the last copy was 8 days old. The database is now copied only with the photos.
- **A file that became a folder (or a folder that became a file) no longer blocks the Nextcloud backup.** The add-only copy kept the old `Projects` file, which stood in the way of the new `Projects/` folder, run after run, and files after it were not copied. The old one is now moved, with its dated copies and checksums, to `neko-replaced/<date>/` beside the user's files, and restores bring it back as "replaced earlier".
- **A file of yours named `.neko-…` is restored.** Every name starting with `.neko-` was taken for NekoServer's bookkeeping, so a Nextcloud file `.neko-notes.txt` was reported missing and stopped the restore. Only NekoServer's exact file names and patterns are left out now, in the backup program and the recovery wizard alike.
- **An approved Nextcloud major upgrade happens with an exact image tag too.** With `nextcloud:33.0.10-apache` (a tag setup accepts) the monthly update recognised version 33 and the approval for 34, but replaced only `nextcloud:33-apache`, so Nextcloud and its cron worker stayed on 33 and the run reported success. Every accepted tag form is moved now; the update fails (and is rolled back) when either service would stay behind, and it is committed only once Nextcloud itself reports the approved version.
- **OS-reinstall recovery accepts Docker's empty default store.** `recover.py reattach` refused `/var/lib/docker` when it held anything, even the empty folders Docker makes on its first start. It now uses the same test as setup: only a container, an image or a volume counts.
- **A Btrfs drive whose default subvolume is not the top level is set up.** With the default set to `/@data` (say) and no subvolume in the settings, setup mounted it and then refused what it found; on a retry it refused its own fstab line. The default tree it mounts is now saved as the drive's subvolume and its fstab line pinned to it, so a restart mounts the same files.
- **The dashboard's acceptance view goes "Needs recheck" when the USB drive or the photo archive changes.** Its fingerprint now includes the USB drive's UUID and filesystem, the archive folder and the weekly copy's schedule (the same in the setup code and the collector).
- **Switching Immich off no longer brings overdue-backup warnings.** Its archive and database jobs now show as "Switched off" (and the music backup when it is off), instead of eventually "Overdue". A schedule that stops while its app is still set up (switched off, or gone after it had run) is still flagged.
- **A server without a desktop can pass the final checkup.** The TV step is skipped on a PC with no desktop, but the acceptance checklist still asked for its launchers and the TV and controller test. Both now use the same decision (TV setup needs Steam on this Linux and a desktop), and any step setup recorded as not available on this PC is left out of the checks.
- **A media app taken out of `media.services` is stopped.** Setup deployed the apps still listed and left the removed one running, without the dashboard, the nightly backup or the monthly update looking after it. It is now stopped (`docker compose down`, without its volumes); its settings, data and Compose file stay, so adding it back starts it as it was.
- **Phone alerts and the outage monitor switched off are switched off.** Turned off in the setup window (or `"wanted": false`) after they were set up, both reused their saved address and switched their timers on again. Now the choice is read first and the timer stopped and disabled; the saved address stays for switching it back on. For the outage monitor setup also says to pause the check at the outside monitor, or it reports the outage it expects. A timer switched off by choice no longer shows as overdue on the dashboard.
- **With Immich and Nextcloud both off, the weekly USB copy is switched off too.** It stopped saying anything to copy but left its old timer running with the old selection. Its timer is now disabled (the copies on the USB drive stay).
- **A drive that comes back finishes the job it cut off.** When a drive disappeared while a backup or the monthly update had the apps stopped, the drive-return service mounted it and started Docker, but the apps the job had stopped stayed stopped (Docker leaves explicitly stopped containers alone). It now runs the jobs' own recovery after Docker: the monthly update's first (it may roll back), then the nightly app and host snapshots' and a photo snapshot's.
- **Hardware video encoding is switched on only on a chip that encodes.** The check accepted any chip that decodes, then switched on hardware encoding and a fixed list of formats. Some chips decode without an encoder (AMD's Radeon RX 6400 and 6500). Setup now reads which formats the chip decodes and whether it encodes, and a one-second test encode on the chip must pass: Jellyfin gets decoding of exactly those formats and encoding only when it passed; Immich, whose setting needs an encoder, stays in software on a decode-only chip.
- **The optional media monitor follows the app selection.** Its saved settings were written once, so it went on checking removed apps and missed new ones. They are brought up to date after every setup run.
- **A file with a backslash in its name is restored from a nightly snapshot.** Linux allows `\` in a name and the snapshots record it, but the restore refused any path with one, so the whole snapshot could not be restored. Only `..`, `.`, absolute paths and NUL are refused now.
- **Backup counts you chose are never lowered without asking.** Setup moved 14 nightly app and host backups to 7, and 8 Immich database copies to 4, on every run, taking any 14 or 8 for the default before v0.30.0, even one you had chosen. Now it asks once, when someone is at the keyboard, and keeps the answer (and without anyone there it keeps the number). A number seen after that is never touched again.
- **A home network on a bridge of your own (`br0`, `bridge0`) can be chosen.** Every interface starting with `br` was taken for a container's, so a server whose address is on its own bridge could not set up home access, and the gateway would not start. Only Docker's bridges (`docker0`, `br-` and 12 hex digits), libvirt's, Podman's, container links and tunnels are left out now, in setup and in the gateway's start check alike.
- **A Nextcloud user named `appdata_…` is backed up.** Every folder starting with `appdata_` was taken for Nextcloud's own (previews, caches), so such a user's files were silently left out of the USB copy and its space check. Only Nextcloud's own folder is left out now: the one its `config.php` names (`instanceid`), or, when that cannot be read, a name of the form Nextcloud gives it.
- **Recovery opens without the internet or Rust.** `sudo bash install.sh --recover` built the runtime first when it was missing (as on a reinstalled PC), which needs the internet. Recovery needs no runtime, so it no longer builds it; the next setup run does.
- **A database copy dated in the future no longer hides the fresh ones.** A copy made while the clock was wrong (named 2030, say) was taken as the newest for ever, so the fresh copies never reached the USB drive, and retention kept it while removing real ones. The newest copy with a believable date is used now; a future-dated one is kept, named in the job's result and never counted by retention.
- **A snapshot's recovery is no longer cut off while it waits.** The app and host snapshots' recovery may wait up to 3 hours for another job (the monthly update, say), but systemd gave it 20 and 5 minutes, and the same after every backup, so a backup that ended well could be reported failed. Recovery now returns at once when there is nothing to finish, and is given 3½ hours when there is.
- **"Restore tried" asks for the right restore tests.** A Nextcloud-only server was asked for a photo test, the music backup was not covered, and a USB copy of photos and Nextcloud files showed "Yes" after the photo test alone. Each backup now names the restore test of each kind of data it holds (photos, Nextcloud files, music, app settings, host and user files), "Yes" needs all of them, and the checklist has the two new ones (`cloud_restore`, `music_restore`).

### Tests
- A test that changed file owners for real failed as root in a container that may not do that; it now only records the change. A test ran setup on the template `config.json` itself and wrote to it; it uses a copy now. A test that read the real apt lists lock waited for minutes as a normal user. Regression tests for each fix above; the cognitive complexity of the changed functions stays within limits.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release). Setup records the Samba accounts it creates, and the sleep targets masked before it, from this release on. For an install made earlier, running `sudo bash install.sh` once does not add the account to the record (it already existed), so the uninstaller keeps it and says so; remove it yourself with `sudo smbpasswd -x <name>` if you want. The sleep targets are unmasked as long as NekoServer's sleep settings are still in place.

## v0.39.2 — 2026-10-25

Two things the uninstall-then-install cycle turned up, and four more real tries on the rig.

### Fixed
- **Setup after the uninstaller, or on a PC whose Docker holds nothing.** Found by reinstalling on Ubuntu after the uninstaller had run: Docker, back on its default store, had started once and made its empty folder tree, and the Docker step refused with "Docker is already set up on this PC and keeps its data in /var/lib/docker". A Docker store with no container, image or volume in it (just installed, or NekoServer uninstalled) is now taken over: a running one is stopped, and Docker runs from the SSD from then on. One with something in it still stops setup, as before.
- **Samba's untouched example `smb.conf` no longer stops the Samba step.** The same reinstall showed the other half: with the package's example back in place (the uninstaller puts it back; Samba installed before NekoServer leaves it there too), the step refused with "the Samba package's unchanged example, not your settings", and the nightly settings backup refused the unmanaged Samba. The step now sets that example aside as `smb.conf.packaged` itself, as the bootstrap already did for a package it installed, and says so. A `smb.conf` someone wrote still stops the step as before. With both fixes, the whole cycle on Ubuntu 24.04 (the uninstaller with every choice, then a fresh installation that took the settings left in the `nekoserver-v0.36.1` folder): 26 of 28 steps, 46 of 46 checks.

### Tried for real
- **The OS-reinstall recovery path, tried for real on openSUSE Tumbleweed** (RECOVERY.md, "Replace the OS and reattach the existing store"; until now only on AlmaLinux): the uninstaller stood in for the reinstall (NekoServer itself, the containers and the mounts gone, every drive's data kept), the storage module mounted the drives from the saved settings, the Docker step refused the existing store and pointed at `recover.py`, `recover.py check` / `reattach` / `reattach --apply --confirm <SSD UUID>` reattached it from the copy kept on the SSD, and a normal setup run brought everything back: 26 of 28 steps, 47 of 47 checks, passwords and photos as they were, all 16 containers up. No code change was needed.
- **The setup window's rough edges, tried for real:** setup started from the window's Start button; a question (Tailscale) stayed on the page after a refresh and was answered there, which the terminal showed as `no  (from the setup window)`; the window was then closed and the run went on from the terminal to the end. Nothing to fix.
- **The uninstaller on an older install of the Debian family, tried for real** (Ubuntu 24.04 with NekoServer 0.36.1, the 0.39.1 uninstaller, every choice): 22 seconds; no units, containers, networks, settings, Samba pieces, group, fstab lines or mounts left; Docker back on its default data-root; the apt repositories NekoServer added for Docker and Tailscale kept with their packages. One thing it showed: Samba's own example `smb.conf`, which setup sets aside as `smb.conf.packaged` when it writes its own, was left aside. The uninstaller now puts it back when NekoServer's `smb.conf` goes, and says so in the plan. Then on AlmaLinux 10 with NekoServer 0.31.0 (the RHEL family: `smb.service`, dnf repositories): the same, 22 seconds, the example `smb.conf` back in place, the repositories kept.

### Tests
- **A first mutation-testing pass** (`tests/MUTATION-NOTES.md`): cargo-mutants over the runtime's maintenance, backup, control and serve code (1,459 mutants) and mutmut over the deploy and kept-copy rules, the settings migration, the package maps and the maintenance guard (868 mutants, every one killed by the existing tests). Of the 559 Rust survivors, 25 new tests close the real gaps: above all the monthly update's Compose sandbox checks (`valid_model`, 54 survivors: a model with `privileged`, another network or PID namespace, a bind mount outside the app's folders or a port off loopback must be refused; one test covered one bind mount), the weekly copy's kept-names bookkeeping (the existing test never reached it), the dashboard sign-in's second device, the nvidia readings' freshness window. The rest are boundary swaps no input distinguishes, or defence in depth, each noted. No product bug turned up.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).

## v0.39.1 — 2026-10-25

SonarCloud's review of the uninstaller, and what a dry run without sudo turned up.

### Fixed
- **A settings file can only name NekoServer's drives.** The uninstaller read the drives' mount folders from `my-config.json` and used them as they were. Now a value is used only when it is a plain absolute folder of its own (`/srv/nekoserver`, `/mnt/...`): the root, a system folder or a path with `..` is refused, named in the plan (`!!`) and the default used instead, and the mount check answers only for those folders. Nothing in a settings file can point the removal anywhere else.
- **A dry run without sudo** died on `/root`. A folder the account may not read is noted and the dry run says what it leaves out (`sudo bash uninstall.sh --dry-run` shows the whole plan); the "no app containers found" note also names the missing permission.
- `runtime/build.py` reads the executable from where cargo put it when `CARGO_TARGET_DIR` is set in the environment (a shared build folder), instead of looking in `runtime/target` and failing.
- Code shape: the plan builder is one function per choice, the removal one per step, the menu's toggle its own function; the unused parameter and name are gone; a repeated path is a constant; the tests use `return_value` and `side_effect` instead of lambdas.

## v0.39.0 — 2026-10-25

An uninstaller, and a README that shows the thing.

### Added
- **`sudo bash uninstall.sh`: NekoServer off this PC, what you choose, after two confirmations.** NekoServer itself (its 21 services and timers, programs, settings, the Samba shares and account it made, the Docker and Samba storage guards, the tailnet pages, its group, the desktop shortcuts) always goes. Everything else is a checklist: the apps' containers and networks; the apps' data on the SSD (with Docker's data-root setting put back); your photos; your movies and music; your Nextcloud files; the backups on the HDD and USB drive; the drive mounts (the fstab lines and the empty folders). First the whole plan, with every path and its size, then `yes`, then a phrase that names what goes (`remove NekoServer and delete my photos, backups`). Nothing is ever formatted, and packages NekoServer installed stay for you to keep or remove. `bash uninstall.sh --dry-run` only shows the plan. Tried for real on openSUSE Tumbleweed: every choice, 27 seconds, nothing left behind.
- **The README shows the thing:** the overview picture, and recordings of the real installation, the setup window, the dashboard and the uninstaller from the test rig.

### Fixed
- **The setup window showed the wrong text above a question.** Seen while recording: the drive question ("Choose number") came with the Tailscale step's instructions above it and no drive list, because the window dropped every line the installer had time-stamped (the drive list among them) and kept everything since the step's last heading (the earlier question's text among it). A question now shows what the terminal said since the last answer, or since its step started, with the time stamps taken off and the installer's step markers left out.

## v0.38.1 — 2026-10-24

openSUSE Tumbleweed tried for real, with the one fix it needed; the update flow in the fixed folder tried for real, and the dashboard's runtime copy brought under 0.38.0's replacement rule.

### Fixed
- **openSUSE: lighting is installed under its openSUSE name.** On Tumbleweed the dashboard step failed with `Installing openrgb failed: Did you mean OpenRGB?`. openSUSE spells the lighting package `OpenRGB`; zypper's search found it for `openrgb` anyway (it ignores case) and the install then did not. The openSUSE package map names it, and the availability check asks zypper for the exact spelling (`--case-sensitive`). Leap 16, which has no OpenRGB package, keeps skipping lighting as before.
- **The dashboard's runtime copy follows the same rule as every other copy.** 0.38.0 made a runtime copy NekoServer deployed itself replaceable when the runtime was built again from other sources, with a build record beside each copy; the dashboard step kept its own copy under the older rule and would have stopped with `The installed dashboard file ... differs from the one in this release`. It now installs the record beside its copy and replaces the copy under the same condition. Other dashboard files that differ (the page's files) still stop the step, as a change of yours would.
- **A copy is replaced only when its record names exactly that build.** The rule from 0.38.0 took any record beside a copy as proof that NekoServer put it there. Now the copy's checksum must match the record's: a copy someone put in place of NekoServer's (even with the record left beside it), a record that cannot be read, or a stale one all protect the copy, and setup stops with the same clear message as for a copy without a record. A stale record beside this release's own runtime is simply refreshed.
- **The copy of the release kept on the SSD follows the same rule.** Within one version, the recovery step refused to go on when the copy kept on the SSD (`installer/releases/v<VERSION>`) differed from the folder running setup, which stopped every step after it: a `git pull` into the same folder within a version, the case 0.38.0 set out to handle, got no further than this. Now a kept copy that is NekoServer's own and untouched (every file as its manifest recorded it) is set aside as `v<VERSION>.replaced-<time>` and this folder's files take its place, with a line in the log; one set aside earlier for the same version goes, and the set-aside copy is retired with its version. A kept copy someone changed is still never overwritten.
- **The build checks for room first.** `runtime/build.py` refuses to start when less than 1 GB is free where NekoServer's folder is, naming the folder and what is free, instead of Cargo failing halfway through with a full disk. (A build takes about 150 MB; the margin is for Cargo's cache and other things on the disk.)
- **An answer typed in the terminal that lost the race to the setup window is dropped.** When both were answering the same question and the window's answer arrived first, the line typed in the terminal stayed in its buffer and would have answered the next question. The terminal's input is cleared when the window wins.
- **An earlier settings file that cannot be read is named, never passed over silently.** Updating from a `nekoserver-v0.37.x` folder, setup offers the newest earlier `my-config.json` it finds (then the copies it kept on the SSD). A damaged one is now reported by path and the next readable one offered, instead of setup quietly taking an older file, or starting from scratch, without a word.
- A test of the "monthly update still running" guard asked the real system about its maintenance service (a default argument bound the real check at import time), so it failed on a PC where that job was running at that moment. The check is looked up when it runs.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there: it offers your settings (answer `yes`) and builds the runtime for this release (a minute or so).

## v0.38.0 — 2026-10-24

The release folder is simply `nekoserver` from now on, so `git pull` updates it in place and a release's changes show as changes; the runtime is built again when it would not run here; and the on-PC build was tried on the other families.

### Changed
- **One folder, `nekoserver`, for every release.** Until now each release lived in a folder named for its version (`nekoserver-v0.37.3`), which made every update move every file and left your `my-config.json` behind in the old folder. The version now lives in `neko_common.VERSION` (and `runtime/Cargo.toml`); the copies setup keeps on the SSD and under `/usr/local/lib` are still named by version, so nothing changes on an installed server. Updating is `git pull` then `sudo bash install.sh` in the same folder; coming from a versioned folder, setup still finds the settings left there and offers them.
- **The runtime is built again when it would not run here.** `install.sh` also checks that the runtime starts and names this release, not only that it was built from these sources: a copy built on another Linux (kept on the SSD through a reinstall and taken back by the recovery path) is rebuilt instead of failing later.
- **A runtime NekoServer deployed itself is replaced when it was built again from other sources** (a `git pull` into the same folder within one version). Each deployed copy now has its build record beside it (`nekoserver-runtime.build.json`); a copy without one (someone's own, or from a release before this one) is still never overwritten.
- The on-PC build was tried on Arch, openSUSE Leap and AlmaLinux as well (see below), after Ubuntu, Fedora and Debian in 0.37.1.
- **openSUSE Tumbleweed, tried for real:** the dashboard step failed there with `Installing openrgb failed: Did you mean OpenRGB?`. openSUSE spells the lighting package `OpenRGB`; zypper's search found it for `openrgb` anyway (it ignores case) and the install then did not. The openSUSE package map names it, and the availability check asks zypper for the exact spelling. Leap 16, which has no OpenRGB package, keeps skipping lighting as before.
- Small things around the build and the bootstrap: when the runtime is built as the person who ran sudo, anything an earlier build left as root (`runtime/target`, `runtime/bin`) is handed to them first, instead of failing on permissions; pacman runs without its progress bars in `install.sh` (its output goes through the retry's log, where the bars came out garbled); the setup window opened by a Chromium-family browser is placed from the screen's real size (`xrandr`), low on the screen and always wholly on it, and left to the desktop where the size is unknown (Wayland). The tests no longer print the home-network and qBittorrent lines into the test log.

### Upgrade notes
- `git pull` renames the folder to `nekoserver`; run `sudo bash install.sh` there. Your `my-config.json` is still in the old `nekoserver-v0.37.x` folder: setup finds it and asks `Use them for this release (yes when updating)?`; answer `yes`. The old folder can be deleted afterwards. From now on updates stay in the same folder.
- The runtime is built for this release (a few minutes).

## v0.37.3 — 2026-10-24

Code-quality findings from SonarCloud's review of 0.37.1, and the CI fixed for a checkout without the built runtime. Nothing changes in how NekoServer works. (0.37.2 was the same work before the CI fix; it was never merged.)

### Changed
- The wizard's `run` has one return point; a build test keeps one call inside its failure check; an unused name in `runtime/build.py` is gone.
- Two tests that need a built runtime (the preserved installer bundle, the generated runtime commands) skip themselves where none is built instead of failing on a fresh checkout; the CI's Python job builds the runtime first, as a server has after `install.sh`. The recovery step says plainly when the runtime is not built yet.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.3`: it offers the settings left in the earlier folder, answers `yes`, and builds the runtime for this release (a few minutes).

## v0.37.1 — 2026-10-24

The runtime is now built on the PC that installs NekoServer instead of being kept in git, plus code-quality findings from SonarCloud's review of 0.37.0 and a fuller `.gitignore`. The setup window is unchanged.

### Changed
- **The Rust runtime is built on your PC, not kept in git.** The repository now holds the runtime's sources alone; the 4 MB executable and its build record (`runtime/bin/`, `runtime/build.json`) are no longer tracked. `sudo bash install.sh` builds the runtime when the release folder has none yet (the first run, and after each update): it installs Rust and a C compiler from your Linux's own packages, then runs `runtime/build.py`, which takes a few minutes. The build is for your PC itself (its own C library); `build.py` still makes the static release build when the toolchain has the musl target and rust-lld, as the CI does. The CI builds the runtime both ways on every change (with the oldest Rust the sources allow, 1.85, and with the pinned release toolchain) but commits nothing.
- The setup window's longer functions are split into smaller ones (the wizard's "Ready to start?" and its finish, the window's state and request handling, the answer wait); its page marks hidden parts without `!important`, uses `<output>` for the notice and a labelled answer box, and never leaves a promise unhandled.
- `.gitignore` covers Python and Rust build output, private settings and keys, what setup and the test rig leave behind (logs, state, moved-aside copies) and editor files.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (version number only).

## v0.37.0 — 2026-10-24

A setup window: `sudo bash install.sh` now opens a small page in the PC's browser, in the dashboard's look, that explains what is being set up and why, lets you leave out the optional parts, and asks every question setup asks so you can answer there or in the terminal. Tried end to end on Ubuntu 24.04 under WSL (the page followed a real run, showed its questions with their context, and an answer given in the page reached setup).

### Added
- **A setup window beside the terminal.** `sudo bash install.sh` now opens a small window in the PC's own browser (the dashboard's look; Chromium-family browsers open it as a window of its own sized for the bottom of the screen, Firefox as a normal window). Before setup starts it explains the optional parts (a TV, phone alerts, outage alerts, Nextcloud) in plain words, with what each installs, and lets you leave any of them out; **Start setup** there answers the terminal's "Ready to start?". While setup runs it shows the step under way with a one-line explanation, the packages it installs with what each one is for, the step's own messages and the state of every step. Every question setup asks is asked in the window too, with the terminal's last lines as context (the numbered drives, the instructions for a phone step): a text box, Yes/No buttons or a password box, and the answer can be given there or typed in the terminal, whichever comes first. At the end it shows the finishing screen. The page is served by setup itself on this PC only, behind an address nobody can guess, only while setup runs, and loads nothing from the internet. Without a desktop or a browser, setup prints the window's address and goes on in the terminal alone, as before.

### Changed
- SonarCloud's findings on 0.36.1: the retry's queries bind the package name per iteration, and two tests use one call inside their failure check.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.0`: it offers the settings left in the earlier folder; answer `yes`. The setup window opens on its own on a PC with a desktop.
- The bundled runtime was rebuilt for this release (version number only).

## v0.36.1 — 2026-10-24

The TV extras on every family that packages Steam, a retry when a package source does not answer, the fix for the test that failed on GitHub's runner in 0.36.0's CI, and SonarCloud's findings on 0.36.0. The TV step was tried for real on Fedora 44, Ubuntu 24.04 and Debian 13 under WSL (Steam installed from each system's own repositories after doing what the hint says), and the two systems without a Steam package were seen to say so.

### Added
- **The TV extras on every family with a Steam package.** The TV step (Steam Big Picture, a browser shortcut for YouTube, Bluetooth) was Arch/Nyarch-only; it now installs from the system's own repositories on Debian, Ubuntu, Fedora and openSUSE Tumbleweed/Slowroll too, with each family's names (`steam-installer` and Mesa's one Vulkan package with its 32-bit twin on Debian and Ubuntu, `mesa-vulkan-drivers.i686` on Fedora, `libvulkan_*-32bit` and `MozillaFirefox` on openSUSE; `bluetoothctl` comes inside `bluez` there). Steam lives in a part of the repositories that is off by default; NekoServer never switches it on itself: when the packages are not offered, the step stops and the log gives the exact command for this system (Arch's multilib; `dpkg --add-architecture i386` with contrib/non-free or multiverse; Fedora's `rpmfusion-nonfree-steam` repository from `fedora-workstation-repositories`; openSUSE's non-OSS repository), then setup is run again. RHEL and its rebuilds and openSUSE Leap 16 have no Steam package: the step reports itself unavailable there and names Flatpak as the way. Steam's shortcut and the guided handoff open it from `/usr/games` on Debian and Ubuntu; the YouTube shortcut uses the browser the PC already has, and Firefox is installed only where there is none.
- **A package source that does not answer is asked again.** A mirror hiccup, a download cut off or the network still coming up used to fail setup at once, on the first `apt-get update`, `dnf makecache`, `zypper refresh` or `pacman -Syu` as much as on an install later. Both `install.sh` and the installer now run the same command again after 20 seconds, three times in all, and say so; any other failure (a missing package, a conflict, a broken repository file) is still reported at once.

### Fixed
- **The runtime's fan-detection tests no longer depend on a fast machine.** Four of them slept 300 ms and then expected detection to have taken the first fan; on a slow runner (GitHub's) one failed. They wait for it now.

### Changed
- Code-quality findings from SonarCloud on 0.36.0: Samba's unit name is written once, `install.sh` lists the tools every family installs once, and two tests use unittest's own forms.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.36.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (its tests and the version number; nothing changes in what it does).
- A TV set up on Arch before keeps its shortcuts as they are. To add the TV extras on Debian, Ubuntu, Fedora or openSUSE Tumbleweed, set `"wanted": true` under `"tv"` in `my-config.json` (or answer yes when setup asks) and follow the hint about Steam's repository if setup gives one.

## v0.36.0 — 2026-10-24

openSUSE support, installed for real on Leap 16 under WSL with its own three virtual drives: setup reached 26 of 28 steps (the other two need the owner's Tailscale account and phone), every feature check passed (47) and the fault suite ran on it. Also a standing dashboard warning when the monthly backups are unencrypted, and a fix for a Docker version openSUSE's package reports.

### Added
- **openSUSE Leap 16 and newer, Tumbleweed and Slowroll.** `install.sh` and setup recognise them (anything naming opensuse in its `/etc/os-release`; SLES is not covered) and install with zypper. Everything comes from openSUSE's own repositories, Docker, Compose, Caddy and Tailscale included, so no maker's repository is written. zypper is told to wait for another zypper or YaST instead of failing at once. Leap 15 is refused before anything changes: its Python (3.6) is older than NekoServer needs. When the freshly installed Python cannot load parts of itself because the system's libraries are older than it (openSUSE's packages name a library, not its version; seen on a Leap 16 not updated for a while, where `xml.etree` failed on an old libexpat), setup brings the installed packages up to date (`zypper update`; `zypper dist-upgrade` on the rolling Tumbleweed and Slowroll) and goes on. Tried on Leap 16.0 under WSL.

### Changed
- **The dashboard says when the monthly backups are unencrypted.** When no backup password was chosen at setup, each monthly snapshot holds the app password files in plain text; until now only the monthly job's log said so. The dashboard now shows a standing warning, with the command that fixes it (`sudo python3 backup_secrets.py --setup`), as long as the monthly schedule exists without a backup key.

### Fixed
- **The recovery checkpoint takes a Docker version with a package suffix.** openSUSE's Docker calls itself `29.7.2_ce`; the checkpoint setup records for a later reattach refused that as not a version, so setup listed the checkpoint under "Still needs you" for good. It now reads the version by its leading numbers, as the reattach itself does, and when it refuses a Docker it says what it found (driver, store, version).

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.36.0`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (the checkpoint's version check and the version number).
- If your monthly backups were set up without a backup password, the dashboard now shows a warning until you choose one (`sudo python3 backup_secrets.py --setup` in the release folder). Nothing else changes.

## v0.35.1 — 2026-10-24

Code-quality findings from SonarCloud's review of 0.35.0. Nothing changes in how NekoServer works or what it says.

### Changed
- Names used in several places (Tailscale's service, the recovery tool, the settings template) are each written once.
- The home gateway's readiness check is split into smaller parts: checking that Jellyfin and Seerr finished their first-run setup, and checking the apps' containers.
- Tests: each test that expects a failure now runs only the call that should fail inside its check, and a set comparison and a mock use unittest's own forms.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.35.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (version number only).

## v0.35.0 — 2026-10-24

Fedora and RHEL support, and every supported Linux installed and broken on purpose for real: a fresh install on Arch, Ubuntu 24.04, Fedora 44 and AlmaLinux 10, Debian 13 updated from 0.30.0 the way `git pull` leaves it, and the fault suite on them (all under WSL, each with its own three virtual drives). WSL has no SELinux, so SELinux is the part still to be tried on a real Fedora or RHEL: see below. Versions 0.31.0 to 0.34.0 were not released.

### Added
- **Fedora 42 and newer.** `install.sh` and setup recognise Fedora and install with dnf. Docker CE and Tailscale come from their makers' repositories (NekoServer writes the repository definitions itself; dnf checks the packages with the makers' keys), Caddy, Samba and the tools from Fedora's own. Fedora's own container packages (`moby-engine`, `podman-docker`, `containerd`) are never mixed in: setup stops and says how to remove them. New services are kept from starting while they install and stay switched off until their own steps start them on the right drive, as on Debian. Samba's untouched example `smb.conf` is recognised by rpm's record of it and set aside. Only the package lists are refreshed (`dnf makecache`); upgrading Fedora stays your own routine. Reattaching the apps after reinstalling Linux ([RECOVERY.md](nekoserver-v0.35.0/RECOVERY.md)) is allowed on Fedora too, but has not been tried there yet.
- **RHEL 10 and its rebuilds: Rocky Linux, AlmaLinux, CentOS Stream.** `install.sh` and setup recognise them (anything naming rhel in its `/etc/os-release`) and install with dnf, as on Fedora:
  - Docker's packages come from Docker's repository for RHEL (RHEL itself) or for CentOS (the rebuilds); Tailscale's from Tailscale's repository for RHEL.
  - Caddy (the home gateway) and qrencode come from **EPEL**, Fedora's extra packages for these systems. Setup adds EPEL when it isn't there yet: from the system's own repositories (Rocky, AlmaLinux, CentOS Stream), or from EPEL's site on RHEL itself.
  - `runc` and `podman-docker`, which clash with Docker's packages, must be removed first: setup says so. Podman itself can stay.
  - SELinux and firewalld are handled as on Fedora.
  - Version 9 is refused before anything changes: its Python (3.9) is older than NekoServer needs. Oracle Linux is not covered.
  - Tried on AlmaLinux 10.2 under WSL: recognised by itself, it installed what it lacked (openssl, pciutils, EPEL), reached 26 of 28 steps (the other two need the owner's Tailscale account and phone), passed every feature check (45) and the whole fault suite (30: the monthly update and the nightly backup cut off at each stage, a crash, Docker restarting, the USB drive and the HDD unplugged, the HDD full). Moving the server to a reinstalled Linux was tried there too: a fresh AlmaLinux took the drives over as RECOVERY.md describes, the apps' Docker store was reattached, and every feature check passed (45). RHEL itself and Rocky Linux have not been tried yet.
- **SELinux.** NekoServer is not tested with SELinux yet, and Fedora and RHEL have it enforcing by default: setup stops at its system check while SELinux is enforcing, says what may be refused (the shared folders, the home gateway, the backup jobs), how to switch to permissive mode to try it, and which command's output to send (`sudo ausearch -m AVC -ts recent`) so the right rules can be added.
- **Memory is part of the system check.** It shows how much the PC has, and below 8 GB notes that the apps use about 2 GB when idle (measured on a full install), and more while Immich looks at new photos or Jellyfin converts a video, so with less those can be stopped for lack of memory. A note only: setup goes on. The README lists 8 GB under what you need.
- **Image-based systems are turned away before anything changes.** Fedora Silverblue and Kinoite, and SteamOS, call themselves Fedora or Arch, but keep their system files read-only, so installing NekoServer's packages would fail halfway. `install.sh` now says so and stops before the package manager runs.

### Changed
- The firewall note gives a `firewall-cmd` example where firewalld is on (Fedora), and the `ufw` one elsewhere.

### Fixed (every Linux)
- **Updating with `git pull` keeps your settings.** Each release has a folder named for its version, and `my-config.json` (git does not keep it) stayed in the earlier one: setup run from the new folder made a new settings file and started over, asking for the drives and passwords of a server already set up. Setup now finds the earlier settings, in the earlier release's folder or in the copy it keeps on the SSD, and asks `Use them for this release (yes when updating)?`; recovery (`--recover`) reads them too. The README now says how to update.
- **Nextcloud's first install is finished when the image gave up.** Nextcloud's image installs it on its first start only, retrying for about two minutes; with its database out of reach that long, every later start skipped the install, Nextcloud said "not installed" for good and the accounts step reported a login that could not match. After starting Nextcloud, setup now checks it and, once the image's own attempt is over, finishes the install with the same settings (taken from the container, so no password is on a command line).
- **The nightly settings backup no longer fails while OpenRGB writes its log.** It kept OpenRGB's whole folder, logs included, and OpenRGB writes a log while it runs: a backup that copied it as it changed (right after setup restarted OpenRGB, say) was refused as a whole with "Host inventory changed during snapshot", and nothing said which file. It now keeps OpenRGB's settings only (`OpenRGB.json` and `sizes.ors`), and the message names up to three of the files that changed.
- **A new drive with nothing on it is named, not called missing.** When no drive could be used for the SSD or HDD, setup explained drives formatted for Windows and drives in use, but about a brand-new, unformatted one it said "No other drive was found: check that it is connected". It now names that drive and says to format it as ext4 first.
- **An app being stopped when the monthly update or the nightly backup was cut off starts again.** When the job was stopped or crashed just as it stopped the apps (seen on Ubuntu: Sonarr), Docker was still finishing that app's stop when the tidy-up service started the apps again. The stop ended afterwards, and the app stayed down until the next restart, failing every later monthly run with "Managed application is not in a healthy running baseline". The tidy-up now lets each stop finish, then starts the apps. A second tidy-up started meanwhile (the one at the next start, say) waits for the first instead of failing with "Job already running or unsafe lock".
- **A nightly backup missed while the PC was off no longer fails when it starts.** The PC catches the missed backup up as it starts, at the same moment as the service that tidies up after a cut-off backup. The two share a lock, so the app backup failed ("Job already running or unsafe lock") and the dashboard showed it failed until the next night. The nightly app and settings backups now wait for that service. Tried on Fedora: the PC was switched off with both backups due, then on; both ran after it and succeeded.
- **Setup run during the monthly update says the update is running.** It said the update "did not finish" and told you to start the service that finishes a cut-off one, while the update was still working. It now says the update is running right now, and to wait for it to finish.
- **A drive check that fails names the drive.** The backups, the home gateway and the app jobs check the SSD and HDD before they start; when one was wrong or missing they said only "Backup storage identity mismatch". They now say which folder should hold which drive (its UUID and filesystem) and what was found there instead, or that nothing is mounted.
- **Docker installed as a snap is noticed.** Ubuntu offers Docker as a snap too. Tried on Ubuntu: installed next to a running server, its Docker took over the socket every docker command uses, so `docker ps` showed none of the apps (they kept running) and the backups, the monthly update and setup would have reached the wrong Docker. Setup now stops before installing Docker's packages beside it, and at the Docker step and the home gateway. It says what it found and how to undo it: remove the snap, restart Docker (the socket is gone until then), run setup again.
- **The recovery commands work as written.** RECOVERY.md and setup's messages said to `cd /srv/nekoserver/installer/releases/v0.30.0` and then run `sudo python3 recover.py …`. That folder is root's alone, so the `cd` failed for a normal user, and the command then ran whatever `recover.py` was in the current folder: in your own release folder, one that reads the blank template settings ("data_ssd: no configured UUID"). Every command now names the tool on the SSD by its full path. The settings copy taken from the SSD with `sudo cp` was root's alone; the steps now make it yours to edit (`sudo chown`), as setup leaves it. Found by reinstalling Linux (AlmaLinux) and taking the apps back as RECOVERY.md describes.
- **Another program on an app's port is named.** With something else already on an app's port (a Jellyfin installed beside NekoServer's, say), setup said only "Media projects failed: ['jellyfin']", and the home gateway "Application onboarding could not be verified". Setup now checks each app's ports before starting it and says which program holds which one (or which Docker container); the apps step lists each app's reason, and the gateway names the app that did not answer. Tried on AlmaLinux.
- **An app left without its network is put right.** That failed start (a port in use) left Docker starting Jellyfin with no network at all, so it ran, but nothing could reach it, even after the port was free again and setup ran. Setup now notices an app's container running without its network and recreates it. Tried on AlmaLinux: Jellyfin answered again.
- **The home gateway names the containers it could not verify** instead of saying only that the selected containers must use the verified mounts, networks and ports.
- Enabling the weekly photo timers no longer prints systemd's "Created symlink" notes on the screen.

### Fixed (Debian / Ubuntu)
- **Setup waits while the system refreshes its package lists.** Debian and Ubuntu refresh their lists themselves now and then (often just after the PC starts). Setup started then stopped at once with "Could not get lock /var/lib/apt/lists/lock", because apt waits for dpkg's lock but not for that one. It now waits for the refresh to finish, up to 10 minutes. Tried on Ubuntu with the lock held as setup started: it waited, then finished as usual.

### Fixed (Arch)
- **Setup waits for another pacman, and finishes an update cut off by a restart.** With pacman's lock in place (an updater at work, or an update cut off by a power cut), setup stopped at once with pacman's own "unable to lock database". It now waits up to 10 minutes for another pacman to finish. A lock older than the last restart, which nothing can still hold, is removed, and the system update that follows finishes the one that was cut off. A lock that stays is explained, with the command to remove it. Tried on Arch both ways: a lock dated before the last restart was removed and the update ran; a lock held while setup started was waited for until it went.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.35.0`: it offers the settings left in `nekoserver-v0.30.0`; answer `yes`.
- The bundled runtime was rebuilt for this release.
- Nothing else to do on Arch, Debian and Ubuntu.

## v0.30.0 — 2026-10-23

Deliberate failures on the installed Debian and Arch servers: the monthly update and the backups cut off as by a power cut (also while the new app versions start), a drive unplugged and plugged back, the HDD filled up, and Linux reinstalled with the drives put back. What they found, and fewer backups kept:

### Added
- **The server comes back by itself when a drive returns.** When the SSD or HDD disappeared while the server ran (unplugged, a loose cable, a USB case that reset), Docker and every app stopped, as they must, so nothing is written in the drive's place. But when the drive came back, a dead mount stayed behind and everything stayed down until someone restarted the PC. A new service (*nekoserver-drive-return*, installed by the server-OS step) now runs whenever either drive appears: it releases the dead mount, mounts the drive again, checks that both drives are the right ones, and starts Docker, the shared folders and the home gateway again. The dashboard's "drive unavailable" alert says so.

### Changed
- **Fewer backups are kept: 7 nightly, 4 weekly, 3 monthly.** The nightly app and settings backups keep the newest 7 (was 14) and the weekly Immich database copies the newest 4 on the SSD and the HDD (was 8; the USB drive already kept 4); the monthly snapshots stay at 3. A settings file made from an earlier template still holds 14 and 8 without anyone having chosen them: setup moves those to 7 and 4 (and says so in its log), so older backups beyond the new counts are deleted after the next successful runs. A number you set yourself is kept. To keep more, set `keep_snapshots` under `app_backup` or `host_backup`, or `database_keep` under `photos`, in `my-config.json`.
- **Unfinished monthly snapshots are deleted too, once newer ones replace them.** A monthly run cut off by a power cut or a restart (or one whose restore test failed) leaves a snapshot of nearly full size on the HDD, and retention never deleted those, so each such night took its share of the HDD for good. Now one is deleted once `keep_snapshots` (3) complete snapshots newer than it exist, just as a complete snapshot of its age would be. Newer unfinished ones, the run in progress and folders the worker did not create are kept, as before. When the HDD is too full for a monthly run, its message lists the unfinished ones still kept, with their size.

### Fixed (every Linux)
- **A media drive in a USB case, or an SSD, no longer leaves setup waiting on you.** The HDD power-saving service sets only internal spinning SATA disks, and failed on every other drive at every start: setup listed "No-sleep and log limits" under *Still needs you* for good, and the dashboard showed "Drive power settings has stopped with an error", with nothing anyone could do about it. A drive in a USB case, an SSD, a virtual disk or an encrypted, LVM or RAID device now keeps its own power settings: the service ends normally and says why, and setup shows that line and goes on. A drive that refuses the setting is still reported.
- **Docker keeps its start guard when an earlier release is tidied away.** After an upgrade, setup removes the runtime copies of earlier releases that no service uses any more. It never looked inside drop-in folders, where Docker's start guard is named (`docker.service.d`), so a setup run without the Docker step (only the shared folders, say) right after downloading a new release could remove the guard Docker starts with: after the next restart Docker, and every app with it, would not start. Drop-ins are read now. The tidy-up also ran only when every step had passed, so a server with a step still pending (Tailscale not signed in, say) kept every earlier release's files for good; it runs after every setup run now, still removing only what nothing names.
- **A second restore test names the first one.** A restore test is left running for you to check; running another on the same port said only "Restore-test port is already in use". It now names the earlier test, gives the exact command that stops it, and mentions `--port`.
- **A run cut off by a power cut says so.** After the resume service had put things right, the dashboard showed the monthly job, the nightly app backup or the settings backup as failed with no reason. Each now says it was cut off and that what it had stopped was started again; the backups add that the earlier backups are kept, and the monthly job says at which point it was cut off (taking the snapshot, testing it, installing the updates), that the apps run their earlier versions, and that the next run tries again.
- **Setup run again after a monthly update starts the apps.** The monthly update pins each app to the exact image it tested (by ID), and since 0.28.11 setup downloads an app's images before starting it; an image cannot be downloaded by its ID, so every later setup run failed the media, photos and Nextcloud steps with "pull access denied for sha256, repository does not exist". Setup now downloads only images that have a name, checks that a pinned image is still on the PC, and says how to get it back if not.
- **Setup on a reinstalled Linux says how to take the apps back.** With the SSD of an earlier install put back after reinstalling Linux, setup stopped at the Docker step with "...is not empty; refusing to adopt existing Docker data without migration verification" and nothing about what to do. It now says the SSD holds an earlier install's apps and names the reattach steps in RECOVERY.md and their command. RECOVERY.md named a `recovery-config.json` that nothing creates; it now names the settings file and the copy setup keeps on the SSD. Reattaching no longer says it supports Arch only: Debian and Ubuntu were already allowed. And the Docker version that reattaching requires is now kept current by the nightly app backup: it was recorded only when setup ran, while the OS's own updates move Docker on, so after a reinstall the steps could ask for a Docker version the server had stopped using months before. A newer Docker than the recorded one is now accepted too, as a freshly installed Linux usually brings one (the OS's own updates move Docker on over the same store all the time); an older one is still refused, and after the takeover the record names the Docker now running.
- **The home gateway keeps its certificates when Linux is reinstalled.** Tried end to end: the installed server was moved to a freshly reset Debian with the same drives. The gateway refused its own certificates on the SSD ("Persistent TLS-state owner differs; preserve files and plan recovery manually"), because the new Linux's caddy account had another number. Setup now hands them to it when NekoServer itself gave them the old owner and no account on this PC has that number, so phones that trusted the server keep doing so; any other owner is still left alone, and the message names it and the command to use. RECOVERY.md also says how to read the settings copy on the SSD without writing to it, and what a reinstall asks for again (dashboard PIN, Samba and backup passwords, Tailscale).
- **The app-accounts step waits for Docker.** With Docker not running (on a reinstalled Linux, before the reattach), it asked to finish each of the 11 apps by hand, which could not succeed, and the next run asked again for each one. It now waits for the Docker step, like the app steps.
- **Backups made while the clock was wrong no longer crowd out the real ones.** Backups are named after the time they were made, and retention keeps the newest by name. One made while the server's clock ran a day or more ahead looked newest until the clock caught up, possibly years later: the nightly backups kept only the one just made besides it, and the monthly snapshots lost their history the same way. Such a backup now counts as the oldest (it was made before the clock was put right), and the log names it and says to check the clock.
- **A full drive says so on the dashboard.** From 97% the alert read "Media HDD is filling up: Review free space before uploads or backups", even with the drive completely full. It now says the drive is almost full, what stops (on the HDD downloads, the nightly backups and the monthly snapshot; on the SSD the apps themselves) and how to free space.
- **The nightly settings backup names the drive and the space it lacks.** A full HDD failed it with "Insufficient snapshot space"; it now names the HDD, the free and the needed gigabytes, and the reserve setting (`host_backup.minimum_free_gb`).
- **The shared-folders step shows why Samba did not start.** When `systemctl enable --now` failed, the step's message was systemd's "Created symlink …" notice; it now shows the failure itself ("Job for smb.service failed …").

### Fixed (Debian / Ubuntu)
- **The nightly settings backup runs on Debian and Ubuntu: winbind is switched off.** Their `samba` package also installs and starts `winbind`, which NekoServer's standalone shares never use, and the settings backup refused to run beside it ("Writer active or process probe failed"). Setup now keeps winbind from starting when it installs Samba, and the shared-folders step switches it (and nmbd) off on a server already set up, once Samba's settings are NekoServer's own. The backup's refusal names the program it found and how to stop it.
- **The nightly settings backup works on Debian and Ubuntu.** Two of the system files it keeps, `/etc/vconsole.conf` and `/etc/default/locale`, are links there, and a linked file was refused ("Symlink path refused"), which failed the whole backup every night. A linked file is now kept as a link, as links inside a folder already were; and the refusal, where it still applies, names the path.
- **The dashboard lists Samba once on Debian and Ubuntu.** There `smb.service` is another name for `smbd.service`, and the dashboard listed (and alerted about) the shared folders twice.

### Fixed (from code review)
- The Docker version check reads version numbers with a bounded pattern, so a long run of digits can no longer make it backtrack for a long time; the Samba service-state parsing uses a dict comprehension.

### Upgrade notes
- The bundled runtime was rebuilt for this release.
- Run `sudo bash install.sh` once after upgrading: the server-OS step installs the new drive-return service, the shared-folders step switches winbind off on Debian and Ubuntu, and the backup counts still at the earlier defaults (14 nightly, 8 weekly database copies) move to 7 and 4.

## v0.29.0 — 2026-10-23

First run of the installer on real Debian 13 and Arch systems (under WSL, with three loop-device drives). What it found:

### Fixed (every Linux)
- **The Tailscale sign-in link is shown.** The link comes from Tailscale's control server, which takes a few seconds on the first contact; setup waited 3 seconds and then said "No authorization link available. Run sudo tailscale up in another terminal window". It now waits up to 15 seconds, so the sign-in page opens as the step says.
- **A busy image registry no longer fails the app steps.** Setup downloads the images of a dozen apps in a row, and the registry answered some with "toomanyrequests" (it limits requests from one address), which failed the media step (Prowlarr) and the photos step (Immich) on the first run. A download refused for that reason, or cut off by the connection, is now tried again after a wait (30 s, then 1, 2, 4 and 8 minutes), and the log says so, with the registry's own reason. Other download failures still stop the step as before.
- **Sonarr, Radarr, Lidarr and Prowlarr get their account on the first run.** Setup gave each its sign-in settings and login, then checked the login at once; the app applies new sign-in settings only after a restart, so the check found no sign-in page and reported "not verified (identity/administrator check did not match)", and the next step asked you to finish every one of them by hand. Setup now restarts the app after giving it the login and waits for it to answer before checking.
- **Sonarr, Radarr, Lidarr and Prowlarr logins are verified.** After signing in with the saved login (which worked), setup proved the session with an API call; since Sonarr v4 the API wants its key even with a signed-in session, so every check ended "not verified (HTTP 401)" and the login step asked you to finish each app by hand. The session is now proved on the page the app itself loads after sign-in.
- **qBittorrent gets its account.** qBittorrent 5 answers a good login with an empty reply instead of "Ok.", so setup took every login for a failure, including the one with the temporary password from the app's log, and gave up ("identity/administrator check did not match"). Setup now goes by the session cookie the app sets.
- **Seerr gets its account, also when setup is run again.** Two things stopped it: once an earlier run had linked Seerr to Jellyfin, every later run sent the Jellyfin address again and Seerr refused it ("Jellyfin hostname already configured", HTTP 500); and Seerr 3.5 no longer gives the email in its sign-in answers, so setup took its own login for someone else's ("identity/administrator check did not match"). A linked Seerr is now signed into with the Jellyfin login alone, and the signed-in user is checked on Seerr's own account page.
- **Connecting the apps no longer waits three minutes for apps that are up.** The onboarding probe follows no redirects (since 0.28.8), but took an app's redirect to its sign-in page for "not answering", so it waited the full `onboarding.wait_seconds`, then reported every app but qBittorrent as "Startup wait ended; still unavailable" and `http:` needs_setup. A redirect, or a request to sign in, now counts as the app being up.
- **Prowlarr's indexer list is read whole.** The connector read at most 3 MB of an app's answer; Prowlarr's indexer catalogue is larger, so choosing indexers ended with "indexers: incomplete (Expecting value: line 108710 ...)". Up to 64 MB is read now, and a larger answer is named as such.
- **The connector signs into qBittorrent 5** (the same empty-reply and cookie-name change as above), so Sonarr, Radarr and Lidarr get their download client instead of "download clients: incomplete (qBittorrent authentication failed)".
- **Sonarr, Radarr and Lidarr get qBittorrent as their download client.** The connector set a field named `category`; the apps call it `tvCategory`, `movieCategory` and `musicCategory`, so every app ended with "unsupported schema field category; needs manual configuration".
- **Setup run again keeps the Prowlarr links it made.** Prowlarr masks the stored API key in its answers, and the connector compared it with the real key, so a second run reported every link as "existing connection differs; review in Prowlarr". The addresses are compared now; the key was checked when the link was made.
- **Jellyfin 12 accepts NekoServer's key again.** Jellyfin 12 no longer reads an API key from the `X-Emby-Token` header alone, so the library check ended "Jellyfin no longer accepts NekoServer's key (it was revoked or replaced)" and Seerr's library check failed with it, although the key was fine. The key is now sent the way Jellyfin 12 wants it (in the MediaBrowser Authorization header).
- **A rebuilt runtime of the same version says what to do.** When a release's runtime or dashboard files were rebuilt after that release was installed (as happened in this repository's history), every step that deploys them stopped with "Existing native runtime differs; custom file preserved". The message now names the file and says how to move the old copy away.
- **The monthly snapshot and update run on a server that is set up.** The runtime's own check of the media apps (the dashboard's "media" readings and the gate before the monthly update) had the same two mistakes as the connector, qBittorrent's category field name and Prowlarr's masked key, so Sonarr, Radarr, Lidarr and Prowlarr always read "needs setup" and the monthly job refused with "Live automated media connections are not ready". Both are read as the connector now reads them.
- **No indexer no longer stops the monthly update.** Choosing an indexer is optional (it needs an account with a provider); without one the apps cannot find requests, but they, their backups and the monthly update work. A missing indexer is now noted in the media report instead of counting as "needs setup" for the app.
- **The recovery restore test can be opened.** The restored Immich ran on a Docker "internal" network, for which Docker publishes no port, so it never answered at its test address (http://127.0.0.1:2284/) and the test failed although the import had worked. The test network is now an ordinary one without IP masquerading: the address works from this PC, and the restored app still cannot reach the internet or the other apps.
- **The recovery restore test gives the restored Immich 10 minutes to answer.** It allowed about 2 to 5 minutes; a freshly imported Immich starting next to every other app took longer, and the test ended "Isolated application did not become HTTP-ready" although the import had completed. The message now names the address and the commands that show what the restored app is doing.
- **Docker is started again after earlier failed starts.** When Docker had failed to start a few times before setup ran (its start guard missing after a drive problem, say), systemd refused the setup's own start with "start of the service was attempted too often" and the Docker step failed although everything was in place. Setup now clears that count before starting Docker.
- **A new release can change how Nextcloud's containers run.** The Nextcloud step compared the whole container definition with the installed one, so any change a release made to it (such as the cron fix below) stopped setup on an existing server with "Nextcloud's settings in my-config.json changed since it was installed". It now compares the settings that feed the definition (image, administrator, trusted hosts, public address, drives); with those unchanged, the new definition is written and applied.
- **The monthly snapshot and update go through with Nextcloud.** Nextcloud's cron container is built from the Apache image, whose stop signal is Apache's graceful SIGWINCH; crond ignores it, so stopping the apps for the monthly snapshot killed the container after 60 seconds (exit code 137) and the update was refused ("Unclean application shutdown"). It is now stopped with SIGTERM, through an init process, and stops at once. Setup run again rewrites the container; the nightly app backup is not affected.
- **The monthly snapshot's restore test can reach the restored apps.** Like the recovery restore test, it ran the restored apps on a Docker "internal" network, for which Docker publishes no port, so the health check ended "Private health port required". The test network is now an ordinary one without IP masquerading: the ports work on this PC, and the restored apps still cannot reach the outside.
- **A monthly run that runs out of night is "done in part", not failed.** When the snapshot and its restore tests took until the end of the night window, the job deferred the app updates (by design) but reported a failure, so the dashboard showed a failed monthly job and an alert went out although every backup was complete. It now records the run as done in part, with the updates named as what was left for the next run.
- **The monthly snapshot's restore test accepts Seerr.** Its probe asked Seerr's status, which makes Seerr ask GitHub whether an update exists; on the test network (no way out) that hangs, and the probe timed out ("curl exited with 28"). The probe now reads Seerr's public settings, which need nothing from outside.
- **The monthly snapshot's restore test accepts qBittorrent.** The restored qBittorrent answers on a port chosen for the test, and qBittorrent refuses a request whose Host header carries another port than its own (401), so the test failed on it ("curl exited with 22"). The probe now names qBittorrent's own port in the Host header.
- **The monthly snapshot's restore test accepts Immich.** The test copy of Immich's definition gained a `volumes: null` entry for its Redis (which has none), and Docker Compose refused the file ("services.redis.volumes must be a array"), so the monthly job failed after taking the snapshot and the apps stayed stopped until the resume service ran. A service without volumes is left as it is.
- **The monthly update names the app that did not stop cleanly.** "Unclean application shutdown; update refused" now says which app and project, whether it was still running, killed after 60 seconds or out of memory, and the command for its log.
- **The monthly update says which drive is short of space and by how much.** "Not enough space for backup and verified recovery" now names the drive, the free and the needed gigabytes, and the reserve setting (`maintenance.minimum_free_gb`).
- **Shared folders and the USB copy wait for the steps that make their folders.** When Docker could not start (so the media and photos steps did not run), the shared-folders step failed with "Share directory missing or unsafe: …/library" and the USB copy with "Archive folder missing", hiding the real cause. Both now say they are waiting for the media or photos step. A share folder you named yourself must still exist, and the message says how to take it out.
- **A chosen drive that is missing says what to do.** "Configured data_ssd UUID not present or not supported" now says which drive is not connected and, for a drive that was replaced, which line to take out of my-config.json so setup asks for the drive anew.
- **A failed command's message is cut at a line, not mid-word** ("rror toomanyrequests").
- **The HDD power-saving service says why it did not start.** "HDD policy rerun failed; inspect the managed service" now carries the service's own reason (for example that only an internal rotating SATA HDD is supported, as on a virtual disk), says that setup goes on without it, and names the log command.
- **The finishing screen never stops on the dashboard PIN file.** Its folder is root-only; shown without root, the screen crashed with "Permission denied". It now says the PIN state is unknown without root.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (its Jellyfin check sends the key the way Jellyfin 12 wants it).

## v0.28.11 — 2026-10-22

### Fixed (every Linux)
- **Downloads during setup say why they failed.** Finding the current Immich or Nextcloud release, and fetching Immich's compose file, ended with a bare "<urlopen error …>" when offline, "HTTP Error 403" when GitHub was limiting requests from the address, or "HTTP Error 404" for a release that is gone. Each now says which it is and what to do (connect to the internet; wait an hour; try later).
- **A slow internet connection no longer fails the app steps.** The apps' images (several GB for Immich) had to download within the 15 to 30 minutes allowed for starting an app, so on a slow connection the step failed with a timeout. The images are now downloaded first, with up to four hours, and the apps started afterwards; a download cut off with Ctrl+C goes on where it stopped next time. The log says when a download is running.
- **The phone setup page could not be made: the steps say so.** When the QR-code page failed to write (no qrencode, a read-only folder), the first phone step still said "A page with QR codes opened on this PC". It now says the addresses are shown app by app instead, and the log says why the page was not written.
- **The apps get three minutes to answer on their first start, not one.** Sonarr, Radarr and Lidarr set up their databases the first time they start, which can take longer than the 60 seconds the account and onboarding steps waited; both then reported "needs setup" for something that would have solved itself. The default wait is now 180 seconds (settable up to 600 in `onboarding.wait_seconds` and `accounts.wait_seconds`); the wait ends as soon as the apps answer.
- **A home-address port already used by another program: setup takes a free one.** The home HTTPS addresses use ports 8442 to 8447; when another program on the server had one, setup stopped with "LAN gateway address/port unavailable". It now takes the next free port from 8450-8499 for that app, says so, and keeps the choice in `"lan_ports"` under `"gateway"` so the address never changes later. The app menu page, the gateway, Nextcloud's trusted address, the finishing screen and the acceptance check all use the chosen port. The Tailscale addresses are not affected (they listen on the Tailscale address only).
- **The dashboard's port already used by another program: setup takes a free one and checks the page answers.** With port 8787 taken, the dashboard service restarted in a loop while setup reported the step as done. Setup now takes the next free port from 8788-8799, writes it to `"port"` under `"dashboard"` and says so; everything that shows the address (the app menu entry, the finishing screen, the Tailscale proxy) follows. After starting, setup waits up to 30 seconds for the page to answer and otherwise fails the step with the command that shows the service's log.
- **No second, manual sign-in check when the logins were just verified.** Before giving the apps their private addresses, setup opened every app in the browser and asked you to sign into each and confirm, although the accounts step had verified every saved login through the apps' own APIs a moment earlier. When every selected app's login is verified, that confirmation is recorded by itself; the manual check stays for logins that are not.
- **Two mounts stacked on one folder no longer confuse setup or the backups.** When a drive folder had been mounted twice (by hand, or a desktop automount on top of NekoServer's), `findmnt` lists both, and setup read the two lines as one answer: the Docker step said "Data root must itself be a mountpoint", the storage step "Wrong filesystem already mounted", and the backup guards refused with "identity mismatch". Every check now looks at the mount in effect (the top one), in the installer and in the backup program alike.
- **A failure while setting up the Tailscale addresses says what failed**, instead of "inspect the pending journal and current Serve routes locally".
- **A server without a desktop skips the TV and desktop-shortcut steps.** On a PC with no graphical session installed, the TV step asked whether a TV is connected and the shortcut step wrote shortcuts nobody could see. Both now report "not available on this system" instead.
- **An app login that could not be verified says why.** The setup log said only "pending; saved credentials preserved, no success claimed". It now gives the reason (the app did not answer, a wrong saved password, and so on), never the password. A wrong saved password for an app that already has an account now says to run `sudo python3 account-setup.py` and choose `c` to enter the real login.

## v0.28.10 — 2026-10-22

### Fixed (every Linux)
- **An app added later gets its login asked before it starts.** `sudo bash install.sh --module media --apply` (or `--module photos`) started a newly added app without choosing its login, so the next setup took the app for one already set up ("Existing app data: enter the current login") although nobody had ever made one. Both steps now choose the logins first, as Nextcloud already did.
- **A new home address from the router is asked again instead of failing.** When the router gave the server another address, LAN folder sharing and the home HTTPS addresses failed on every setup run with "Configured LAN address/subnet is not on an active physical LAN interface". Setup now says the address changed and asks for the home network again (without a terminal, it says to run setup in one).
- **Docker already used on this PC: setup says what to do.** It stopped with "use separate migration script" or "manual migration required", but no such script or guide existed. The message now names the folder Docker uses and points to a new section in STORAGE.md with the steps. Existing Docker data is still never moved or deleted automatically.
- **The Docker recovery checkpoint says why it wasn't saved**, instead of a fixed "Verified classic overlay2 Docker checkpoint still required".
- **Picking the drives: a typo is asked again, and "no drive" says why.** A wrong number, or a mistyped name at "Type sdb1 to confirm", ended the storage step (and with it the whole setup), and the USB backup picker did the same. Both now ask again (Enter still stops). When no drive can be used, setup lists each drive it found and why: formatted NTFS/exFAT/FAT (with how to format it as ext4), or already in use at another folder (with how to unmount it). The system disk is never listed.
- **Opening a setup page never installs a browser.** When Firefox wasn't installed (a Chrome or Chromium user), each sign-in page installed it first; on Ubuntu that is the Firefox snap, a large download in the middle of a step. Pages now open in the browser you already have.
- **The gateway and Tailscale steps say what failed.** "Gateway command failed; inspect local setup and HTTPS permissions" and "systemctl failed; inspect the local service/package status" now name the command and its own error line.
- **A drive you already mount from /etc/fstab: the message says what to do.** "Conflicting existing fstab entry" now shows the line, explains that NekoServer mounts the drive itself (with options that let the PC start when a drive is missing), and says to take the line out and unmount the drive.
- **Docker's or Tailscale's repository key can't be downloaded: the reason is named.** Without internet, setup ended with a bare "<urlopen error …>"; on a brand-new Ubuntu release the makers don't package for yet, with "HTTP Error 404". Setup now says which it is and what to do.
- **Your own smb.conf no longer blocks shared folders.** On a PC where Samba was set up by hand before, setup said only "refusing to alter it. Review manually." It now says how to move the file aside, or how to keep your own shares as well: add `include = /etc/samba/nekoserver.conf` to your `[global]` section, and setup accepts the file as it is.
- **Shared folders and the TV keep their account.** Both took "the person who ran sudo" on every run, so setup run later by another administrator (or with sudo from a second account) silently moved the shares to that person and set the TV up for them. Both now keep the account chosen the first time; `networking.samba_user` and `tv.user` name another on purpose (settings table in README.md).
- **A firewall that is on: the message names the ports.** It said "allow your home network to reach Samba (port 445) and the home address (gateway port)"; it now names ports 8442-8447 and gives the ufw command, and says Tailscale needs no rule.
- **A command that fails inside a step is reported with its own words.** The summary said "Command '[…]' returned non-zero exit status 1"; it now says which command failed and what it printed (or how long it ran before timing out).

### Changed
- **Files of earlier releases are tidied up.** Each release installs its own copy of the runtime in five places on the system disk and one on the SSD, and kept them for good (20 MB per release). When a setup run has finished, copies that no service uses any more are removed, and release bundles older than the two newest go too (Docker's start-guard folder included). The current release, and any folder a service still points at, are never touched.
- Sonar: two exception tests make a single call inside `assertRaises`.

## v0.28.9 — 2026-10-22

### Fixed (every Linux)
- **Setup runs again after the PC's timezone changes.** The media apps' settings files carry the timezone, and setup replaced one only when it matched what the current settings would write. With `"timezone": "auto"` (the default), setting the PC's timezone after setup made every later run fail the media step with "has local edits or different settings". Setup now keeps a checksum of each file it writes (`.neko-written` next to it), so it recognises its own unedited file whatever changed (timezone, a drive, the graphics chip) and replaces it after keeping a copy. Files from earlier releases are recognised when only the timezone differs. A file changed by hand is still left alone, and the message says how to get a fresh one. Changing `media.uid`/`gid` is still refused, now with a clear message: Navidrome and Seerr couldn't read their own settings and music as another user.
- **The drive space check no longer reads every photo when setup runs again.** On a re-run the result is only shown, yet the 4-weekly checksum comparison of the USB copies could read the whole photo archive on both drives (an hour or more on a big library). Re-runs now compare by size and date; a new install still checks in full.
- **Phone alerts: a topic set in my-config.json gets its access token, and can be changed.** With `notifications.endpoint` set in the settings, setup never asked for the token, so a protected topic refused every alert. Changing the topic later failed the step on every run. Setup now asks for the new topic's token and, when alerts already go elsewhere, asks you to confirm the switch. Without a terminal, alerts keep going to the earlier topic and setup says how to switch.
- **A guided setup without Navidrome goes ahead.** Taking Navidrome out of `media.services` made the whole setup refuse to start ("Music setup requires navidrome"). The music step now says there is no music player to set up; the music backup still keeps Lidarr's music.
- **"yes" means yes when adding media sources.** The source questions accepted only `y`: typing `yes` added nothing, and for "Copy this folder…?" it meant no, without a word. Both now take yes/no (Enter = no) and ask again otherwise; Ctrl+D keeps the sources added so far.
- **A typo in the media connector's lists is asked again.** A number outside the list (choosing an indexer or Lidarr's profiles) failed the step, and onboarding then stopped setup. It is now asked again (Enter or Ctrl+D skips). Ctrl+D elsewhere in the connector stops it cleanly instead of with a Python error.
- **A mistyped Jellyfin password during library setup asks you to try again**, instead of failing onboarding with "rerun after checking the service". (Only when saved app accounts are turned off.)
- **Too old a Python says what to do.** On a system based on an older release (Linux Mint 21 is Ubuntu 22.04), install.sh said "bring the host up to date", which doesn't bring Python 3.11. It now names the version found and the releases that have 3.11.

## v0.28.8 — 2026-10-22

### Fixed (every Linux)
- **Nights with Steam open can't delete the last backup of your Steam saves.** Snapshots made while Steam was open (without its files) counted towards `keep_snapshots` like any other, so a few such nights retired the last snapshot that had them. The newest snapshot with Steam's files is now kept until a newer one has them.
- **A restart cut off part-way is finished only when no backup or update needs the apps stopped.** At start, the control service started what an interrupted restart had stopped right away, without the shared lock or the check for an unfinished backup or update. It now waits for both, as a restart from the dashboard does, retrying every 30 seconds. Its record also moved from `/run` to `/etc/nekoserver-control`, so it survives a restart of the computer.
- **Setup no longer says "NekoServer is ready!" when the first checkup found problems.** The wizard ignored the checkup's result. A backup that finished but left something out (Steam open, say) counted as done, and checks still waiting for setup weren't listed. The checkup now reads each backup's own report (done, done in part, or did not finish) and lists every check that didn't pass. The finishing screen and the exit code include it.
- **"Restore newest" never takes a file of its own for an older version.** With a recorded version of `report.txt`, an older-looking `report.neko-20250101T120000Z.txt` saved in its own right was still treated as a version of it, and only one file came back. A dated copy now counts as a version only when the backup recorded it, or its note names it as the current copy. Older unrecorded versions come back as files of their own.
- **The unencrypted password list is checked before it is restored.** Taking it out of a monthly snapshot made without a backup key skipped the snapshot's checksum, so a changed archive gave changed passwords. `data.tar` is now checked against its recorded checksum first.
- **The monthly Nextcloud check only removes its own test files.** A test file left by a cut-off check was cleaned up by name prefix (`neko-maintenance-…`), which could match a file of yours. Each test file's exact name is now written down before it is made. Only those are removed, and only while they still hold what NekoServer wrote.
- **Btrfs drives mount the same tree after a restart.** A Btrfs drive already mounted with a subvolume (`subvol=@data`, say) got an fstab line without it, so a restart mounted the filesystem's default tree instead and the apps' files looked missing. The subvolume is now remembered in the settings (`storage.<drive>.subvolume`), written into fstab, checked after mounting, and part of the drive identity every backup checks.
- **TV setup finishes on KDE and XFCE.** The step asked for a GNOME power setting that other desktops don't have, and stayed "needs setup" although sleep was already blocked system-wide by the server-OS step. The system-wide block now counts on every desktop; GNOME's own setting is still switched off where it exists.
- **Nextcloud on its own gets its password step.** `--module nextcloud` failed with "Complete the account_credentials prompts before Nextcloud setup", because that step was only reached through the full and cloud profiles. Nextcloud now depends on it.
- **Repairing accounts leaves switched-off apps alone.** Running only the accounts step tried every login ever saved, including apps you had since switched off (whose containers are gone), and then asked you to finish setting them up. It now works on the apps set up in the settings; the other logins stay saved for when those apps come back.
- **A drive under /home is refused before anything is installed.** Such a mount passed the settings check, but NekoServer's services (dashboard, gateway, alerts) run with systemd's ProtectHome and can't see `/home`, `/root` or `/run/user`. The message now says to mount it under `/srv` or `/mnt`.
- **The HDD power setting works on a Btrfs drive.** For a Btrfs subvolume, the mount source reads `/dev/sdb1[/media]`; the drive-policy service took the whole text for a device name and refused it ("Simple physical device path required"), leaving the server-OS step unfinished. The device is now taken from before the bracket; the identity checks stay.
- **A PIN typed in another script's digits asks again instead of failing the step.** Setup accepted digits such as `١٢٣٤٥٦` (Python counts them as digits), the control service refused them, and the dashboard step failed. Setup now takes 0-9 only, and a PIN the service refuses is asked again.
- **Setup on a server that is already set up:**
  - **Shared folders follow your settings.** A share added, Immich switched on, write access, or another account made the Samba step fail for good with "Existing Samba config differs". NekoServer's own share file is now rewritten; only a file someone else wrote, or widening Tailscale-only sharing to the home network (confirm with `"allow_tailnet_to_lan": true`), stops setup.
  - **`my-config.json` stays yours to edit.** The first setup run saved it as root's (readable by root only), though the guide says to edit it and run setup again. It keeps its owner now.
  - **Changing `data_mount` works.** The shipped settings also carried `"persistent_root"` under `"docker"`, which had to match, so the edit the guide invites made setup refuse to start. The line is gone from the template (Docker always uses the app SSD), and an old settings file with it gets a message that says what to do.
  - **The gateway list follows the apps set up.** Left as shipped, `"services"` under `"gateway"` names every app, so a server without Immich or Nextcloud failed both gateway steps. The shipped list now follows your apps; a list you edited is used as it is, and an app in it that isn't set up is named.
  - **A Tailscale address failure says why.** With MagicDNS and HTTPS already on, a failure to set up the addresses (an app not running, say) sent you to the DNS settings page over and over without the reason. The reason is reported, and the step waits for you. Checks in every guided step now show the error they hit.
- **Setup messages and prompts:**
  - **Docker Compose failures say why.** Immich, Nextcloud and the server-OS step reported only "exit status 1"; the command's own message is in the log now.
  - **A mistyped Samba password is asked again** (up to 3 times) instead of failing the shared-folders step.
  - **A wrong number at the home-network question is asked again**; three wrong ones (or Ctrl+D) leave the choice for next time.
  - **Enter at the USB-drive question skips it, as offered**, instead of failing the step; Ctrl+D does the same.
  - **Choosing no backup key (three mismatches) still installs the monthly update**; its snapshots keep the password files unencrypted until a key exists, as the message says.
  - **Ctrl+D at any question** is recorded with a reason instead of a blank one; Ctrl+C while packages install ends with a short message, not a traceback.
  - The timezone message says when the PC's timezone couldn't be read and UTC is used; the dry-run messages show the configured backup times; "Docker: not installed yet" is no longer shown when Docker is installed but not started; an NVIDIA-only PC without `lspci` gets the NVIDIA explanation; a bundled copy that differs, and a changed Nextcloud setting, say what to do.
- **Timezone from `/etc/timezone` too.** On Debian and Ubuntu images where `/etc/localtime` is a copied file rather than a link, the PC's timezone couldn't be read and every backup silently ran at UTC times. `/etc/timezone` is read now, and one-word zones such as `Japan` or `CET` are accepted.
- **install.sh:** too old a Debian or Ubuntu (Python 3.11 needs Debian 12 or Ubuntu 24.04) is refused before apt runs, with the release named; free space is measured in MB (df rounded 4.1 GB up to 5); dpkg never asks about an edited settings file; the message after a kernel update no longer says "nothing else was changed"; a network timeout or HTTP error while repairing repositories is reported, not a traceback; the install time limit in the message matches the real one.
- **Health-check probes during onboarding** go to the apps directly (no proxy, no redirects), so a proxy kept by sudo can't make every app look down.
- **The Docker storage guard check** accepts a `data_mount` with a trailing slash; the recovery checkpoint no longer fails on a settings file without `media.uid`.
- **Switching Immich off removes its photo schedules.** Its weekly photo and database backups stayed scheduled and ran against the stopped Immich. They are now stopped and removed (one you wrote yourself is kept). The USB copy of Nextcloud's files keeps its own schedule.

### Changed
- Code tidied after the 0.28.7 review (no change in behaviour):
  - named constants for the photo archive's default folder and the encrypted password list's file name;
  - simpler parsing of Debian dependency entries;
  - plainer code for the graphics-device decision;
  - single-call exception tests.
- Version 0.28.8; the release folder is now `nekoserver-v0.28.8`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.7 — 2026-10-22

### Added
- **First checkup and backup at the end of setup.** Once every step has finished, the wizard runs the app health check and each backup you set up straight away, instead of waiting for the first night (or the first Sunday for the USB copy), then checks the whole server and saves the report for the dashboard. It names any backup that didn't finish (with the command to see its log) and any check that needs a look. Ctrl+C stops it; the backups then run at their usual times.
- **Desktop shortcuts on every Linux** (new step *Desktop shortcuts*): the dashboard, the web browser, Steam (if installed) and each app that was set up, on the desktop and in the app menu. Shortcuts NekoServer made earlier are brought up to date; ones you made or changed are kept. Before, only the TV step (Arch only) made a few.
- **The finishing screen says whether a dashboard PIN is set**, and the welcome screen asks you to have one ready.

### Changed
- **Steam Big Picture no longer opens by itself at login.** The `autostart_big_picture` setting is gone, and the autostart an older version added is removed (one you made yourself is kept). Big Picture is still a shortcut.
- **A new dashboard PIN has 6 to 8 digits** (a 4- or 5-digit one chosen before still works). After five wrong PINs changes lock for 5 minutes, and **each further five lock them twice as long** (up to a day). Before, the 5-minute lock started over each time, so a 4-digit PIN could be guessed in a few days.
- **Close app asks first**, like Force close. It sends Linux's ordinary "please quit" signal: many apps save or ask, but some quit straight away, so unsaved work can be lost. The menu no longer says it is "like closing its window".
- Messages and guides say "terminal" instead of "Konsole": on Ubuntu and Debian it is called Terminal.

### Fixed (every Linux)
- **Drives get 90 seconds to appear at startup** instead of 15. A big hard drive, or one in a USB case, could take longer; the HDD then wasn't mounted, and Docker and every app stayed stopped until someone stepped in. Existing installs get their drive lines updated; your original `/etc/fstab` backup is kept.
- **The dashboard keeps updating when the app SSD can't be written.** A full or read-only SSD made every reading fail, and the page kept showing the last, healthier one. Readings are published anyway, with an urgent "App SSD can't be written" alert.
- **A full drive reads 100%, not 95%.** The space Linux keeps for itself on ext4 (5%) was counted as free, so a drive full for the apps showed 95% and a "filling up" warning, never the urgent alert.
- **A phone whose clock is off no longer sees "old readings".** The page judged how old readings are by the phone's clock; a minute off dimmed live readings, a few minutes off showed every reading as old and stopped browser alerts. It now uses the server's time.
- **A service that has stopped is no longer green.** Docker waiting for a missing drive (or any service that runs all the time) showed "Starts by itself" in green when it wasn't running. It now shows *Not running* and raises an urgent alert; "recovered" is sent only once it runs again.
- **A backup cut off by a power cut no longer shows "Running" for days.** Its report still said running; now the collector sees its service has stopped and shows it as stopped part-way.
- **A health check made after an app started counts.** Docker's "Up 5 minutes" is rounded down, so a failed check from 5½ minutes ago could be discarded as older than the app's start. The exact start time is used now.
- **An app check report dated in the future** (the clock was wrong when it was written) is no longer counted as current.
- **The dashboard's web server restarts itself after a crash** in one of its workers, instead of slowly running out of workers.
- **Setup no longer gives up on the control service while it finishes an app restart.** Setup waited 60 seconds; the service may take up to 10 minutes to stop (its unit now allows 600 s), because it first lets an app restart finish: up to two parts, each up to 2 minutes to stop and 2 to start. If the service is ever stopped by force in the middle of one, what it had stopped is started again when it next starts.
- **exFAT USB drives: names kept from earlier weeks count too.** A new `documents/report.pdf` could still merge with an old `Documents/report.pdf` that stays on the backup (it is add-only). The check now includes the names already on the drive and in its ledger.
- **Repair asks when the HDD and USB ledgers disagree.** It trusted the HDD's checksum, replaced the USB copy and called both verified, though the USB copy then failed its own list. Now nothing changes until you choose which copy is right (or keep both); your choice is recorded in both lists.
- **"Restore newest" never merges two separate files.** A file named like a dated version (`report.neko-20261022T120000Z.txt`) but saved in its own right was taken for a version of `report.txt`, and only one came back. The backup now records which file each dated copy belongs to; older backups count a dated name as a version only beside the note naming the current copy, and not when the record says otherwise.
- **Nextcloud's USB backup no longer needs Immich.** With Nextcloud on and Immich off, setting up the weekly USB copy failed ("Archive folder missing"), and it waited for the photo steps. Nextcloud's files then had no copy off the server, since monthly snapshots leave them out. The USB copy now takes whichever of the two is set up, the photo schedule isn't installed without Immich, and the dashboard calls the job *USB copy (photos and files)*.
- **Monthly snapshots made without a backup key keep the app password list.** The job said the password files stayed (unencrypted) in `data.tar`, but `secrets/accounts` (APP-CREDENTIALS.txt and credentials.json) wasn't in it, and no encrypted archive was written either, so the snapshot had no password list at all. It is in `data.tar` now (root-only, and the manifest says it isn't encrypted), a rollback puts it back, and the recovery wizard's *Restore the password list* also offers these snapshots. With a backup key, nothing changes: the list is kept only in the encrypted archive.
- **Adding an app reaches the backups already installed.** After adding an app (setup run again with `--module media`), the nightly app backup and the monthly update kept working from the settings saved when they were installed, so the new app was left out until they were set up again. Both are now brought up to date after every setup run, like the dashboard's and the host backup's.
- **A damaged backup key is no longer reported as a wrong password.** The key copy and certificate stored in each monthly snapshot had no checksum, so a damaged key passed every check and the restore said "Wrong password". Both now have checksums: the snapshot check and retention catch the damage, and a restore says the key copy is damaged and uses this server's own copy of the same key when it has one.
- **Switching Immich off stops it.** With `"deploy_immich": false` after Immich was set up, it kept running while monitoring, backups and monthly updates left it out. Setup now removes its containers (under the lock backups and updates hold, and not during an unfinished monthly update); its photos, database and settings stay on the SSD.
- **A restore can't go inside a backup.** A destination inside the photo archive, the snapshots or the USB drive (or one around them) was accepted, and the restored files became part of that backup and of every later restore from it. Such a folder is now refused.
- **Jellyfin Quick Sync uses the chip that passed the check.** The device was written to Jellyfin's VA-API setting, which Quick Sync doesn't use. Quick Sync now gets *QSV Device*, VA-API *VA-API Device*.
- **Settings an older NekoServer made get their device.** Immich and Jellyfin set up by an earlier release kept choosing a device by themselves, because setup then reported "kept your setting". A setting NekoServer made (its last report says so) gets the device filled in. One someone else made is kept, but on a server with several chips the step says which device to choose instead of counting as done.
- **A second graphics chip of the same make is tried.** Only the first render device of each vendor was kept, so with two Intel chips the second was never tried when the first failed the video check.
- **Debian / Ubuntu: finishing an interrupted package installation follows Debian's rules for choices.** A dependency like `A (>= 2.0) | B` counted as met by any installed A, even 1.0, so an unfinished B that was needed stayed unfinished. A held package (`hi`) didn't count as installed, so an alternative was set up for nothing. Versions are now compared as dpkg does, an unversioned *Provides* never meets a versioned dependency, and held packages count.
- **Leaving Steam open no longer stops the whole nightly host backup.** With Steam (or its web helper, or Wine) running, the backup refused to run at all, so system and Samba settings weren't saved either, on a PC left on for Steam Remote Play. Now everything else is saved, Steam's own files are left out that night, and the run shows as partial with "Steam was open". Steam is never closed for you.
- **Shared folders work on a server without Immich.** The default shares include *Photos* (the photo archive), which only exists with Immich, so Samba setup failed on a media-only server ("Share directory missing"). The default Photos share is now left out without Immich; a share you added or changed yourself must still exist, as before.
- **Monthly update:**
  - The space check no longer turns down a drive with enough room. It counted every Docker container on the server, also ones that aren't NekoServer's, and the same image again for each container using it (20 containers sharing a 1 GB image counted as 20 GB), then asked the SSD for room for images that are saved on the HDD. Now it counts the selected apps' images once each, and each drive for what goes on it.
  - A finished update is no longer reported as failed when reopening Immich or Nextcloud needed a second try, or a power cut came after it was installed.
  - A rollback no longer unpacks the app data twice (once to the HDD for nothing), and it is never cut off part-way by a time limit: before, a large one could be stopped after 6 hours, leaving the apps stopped.
  - The unpacked copy used by the restore test is removed when the test fails, the rollback stages after a rollback, and the failed update's data that a rollback set aside is removed together with that month's snapshot. Before, each of these stayed for good and slowly filled the drives.
  - Nextcloud's check uses a new test file each try and removes one a cut-off try left behind, so one interrupted check can't make every later one fail.
- **Setup:**
  - **Ctrl+C ends setup cleanly** with a short message, and the step it stopped is recorded as stopped (not left "running"), so the next run starts it again. During recovery it tells you to run recovery again.
  - **A settings file missing a drive setting says which one** (and how to fix it), instead of a bare error.
  - **The first setup log isn't kept twice**: it moves to the SSD once the SSD is mounted.
  - **A typo in the outage-monitor or phone-alert address is asked again** (up to 3 times, with an example of the right form) instead of failing the step; a "/" copied at the end is dropped.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run `sudo bash install.sh` once: it updates the drive lines in `/etc/fstab`, the control service's stop time, the monthly update's time limits and the desktop shortcuts.

## v0.28.6 — 2026-10-22

### Fixed (every Linux)
- **Closing a desktop app reaches all of its processes.** Some apps put their own processes in a group below the app's own (a browser's helpers, a launcher's children). Those were neither asked to quit nor force-closed, and the app could count as closed while they kept running. Every group below the app's is included now.
- **A program started from another app says so.** Firefox opened from a terminal belongs to the terminal's group, so closing it closes the terminal too. The menu now names the group ("Close Konsole", "Konsole and everything started from it") instead of looking like it closes one program.
- **Two restart requests at once can't both start.** The second was refused with a message about a backup running. Only one app restarts at a time, and the message says so.
- **Immich without machine learning, or Nextcloud without its cron container, can be restarted.** Restart app refused with "one of its parts is missing" when an optional part was left out on purpose. Only the app's main container is required now.
- **A restart that can't start every part says so, and tries them all.** If Docker refused to start one part, the parts after it were left stopped and the message blamed stopping. Every part is started regardless, and the result names what to do.
- **A restart is never cut in half by setup.** When setup restarted the control service during an app restart, the service waited 80 seconds, which two parts stopping (up to 60 s each) could exceed. It now waits until the parts are started again (up to 200 s; the service's stop limit is 240 s), and no longer waits while it is merely checking that the app answers.
- **"Not responding" no longer outlives a restart.** The hourly app check from before a restart could label the app as not responding for up to an hour afterwards, even though Docker reported it running and the restart had seen it answer. A check older than the app's current start no longer counts.
- **Setup no longer complains about fans a moment too early.** With fan control switched off, setup looked for fans still held right after restarting the control service, before the service had handed them back. It now gives the service up to 15 seconds.
- **A restore that can't set a file's date still succeeds.** Restoring the saved modification times (new in 0.28.5) failed the whole restore if the filesystem refused a timestamp, after every file was already copied and verified. The restore now finishes and its report says `times_restored: false`.

### Changed
- Version 0.28.6; the release folder is now `nekoserver-v0.28.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.5 — 2026-10-22

### Added
- **Restart or close apps from the dashboard**: right-click on a computer, like a task manager, or tap the ⋯ button on a phone or tablet (it stands out there, since phones have no right-click):
  - **Server apps** (on *Your apps*, or any of their programs on *Processes*): **Restart app** stops the whole app and starts it again in order, then waits until it runs and answers. Its database and cache keep running: Immich restarts its server and machine learning, Nextcloud its web part and background jobs.
  - **Desktop apps** (opened on the server's own desktop): **Close app** asks it to quit. If it is still running 10 seconds later, the page says so and offers **Force close**, which warns first that unsaved work is lost.
  - The page shows *Stopping…* or *Restarting…*, then the result. A failure says why (it stopped again, its health check fails, it doesn't answer yet) and what to do.
  - **Protected**: databases and caches inside apps, backups, the monthly update, system services, the desktop itself and NekoServer are never stopped from here. Their menu says so.
  - **Every app action needs the PIN, on every device, the server too.** Without a PIN, app controls stay off.
  - **Backups and updates come first.** A restart waits while a backup or the monthly update runs, or while a cut-off one still has to be finished (new error E47). Only one app restarts at a time.
  - Restarts and closes are done by the control service (root), which checks each request itself. The web server still can't stop anything, and closing goes only to processes in a desktop session's own app group.
- **The Apps page shows each app's health.** It comes from the app's own health check: Docker's where the app has one, otherwise the hourly app check that asks it for a page. *Not responding* never comes from processor use alone, because converting a video can rightly use every core.

### Fixed (every Linux)
- **Long file names no longer stop a backup.** Copies were written as `name.neko-part` first, so a file whose name was 246 bytes or longer (255 is allowed) failed. A version of an edit (`name.neko-<date>.ext`) failed from about 224 bytes. The copy in progress now has a short name of its own. Dated versions and current-version notes that would be too long keep the start of the name plus a short code, and the full name is kept in the folder's `.neko-names.json`. The recovery wizard uses it to restore the file under its own name. This affects the photo archive, the USB copies and Nextcloud.
- **The USB drive keeps its checksum records when the HDD's are lost.** The weekly mirror replaced the USB's ledger with the HDD's. With the HDD's ledger gone, that left the USB with an empty one, which no longer said which copy of a damaged file is the good one. Both ledgers are now read and combined; neither loses a record, and a run that fails part way leaves the USB's ledger as it was. Two ledgers that record different checksums decide nothing: both copies are kept and reported. An unreadable USB ledger is set aside, never overwritten.
- **Nextcloud files whose names differ only in upper/lower case** (`Documents/report.pdf` and `documents/report.pdf`) are no longer merged on an exFAT USB drive. exFAT takes them as one name, so the two folders became one and the files looked like versions of each other. The first keeps its place; the others are left out and listed, and the backup shows as incomplete. An ext4 drive keeps them apart.
- **Restores give files their original modification times back.** Host and app snapshot restores set every file to today's date, which confuses synchronisation and tools that compare ages. Saved times are now set again on files and folders. Nightly app snapshots now record them too; host snapshots already did.
- **Immich uses the graphics chip that passed the check.** Setup set the method (`qsv` for Intel, say) but not the device, so on a computer with two chips Immich could pick the other one (an AMD chip with Intel's method) and fail. The tested device is now set as Immich's preferred hardware device.
- **The boot hook that restarts Immich after a cut-off snapshot works from a folder with spaces.** Its command wasn't quoted, so `Neko Server/recover.py` became two arguments and Immich stayed stopped. The paths are now quoted for systemd. The hook uses the installer copy that setup keeps on the SSD, which isn't moved or deleted like a download folder can be.
- **LAN folder sharing finishes on the first setup run.** With `share_mode: "lan"` and automatic LAN settings, Samba said "Needs setup" because only the LAN gateway step chose the home-network address, and that step ran after Samba. Samba and the gateway now share one choice, made by whichever runs first, so sharing folders no longer needs the HTTPS gateway.
- **Nextcloud with server-side encryption is no longer reported as backed up.** With Nextcloud's *Encryption* app on, the weekly USB copy took the encrypted files but not the keys needed to read them (`files_encryption`). The checksums passed, and the copy still could not be read after losing the server. The weekly backup now detects encryption and reports itself as incomplete, saying why. The recovery wizard warns that such files can't be read instead of suggesting an upload. Encryption is off by default.
- **Onboarding finishes with only Navidrome, or only qBittorrent.** Setup always asks onboarding to connect apps, and onboarding stopped with "No supported connectors selected" when none of the chosen apps had anything to connect. Running setup again didn't help. Onboarding now checks those apps' accounts and readiness and carries on.
- **A media import stops as soon as its drive is gone.** The import checked the drives before it started and again only after copying everything. If the HDD was unmounted midway, the next files went into the empty folder underneath, on the system disk, and could fill it. The drive is now checked before each file and again before each one is published, and the whole import stops at once.
- **The backup password is chosen and typed twice, never taken from sudo.** Setup checked the password by running `sudo -v` with it. Under a passwordless sudo rule that succeeds whatever was typed, so a typo could become the backup password unnoticed. Setup now asks for a backup password (at least 8 characters, your login password is fine), typed twice. Keys made before keep opening with the sudo password you had then.
- **Monthly snapshots without a backup key say what they really hold.** Setup said the app password files would be left out, but the monthly job kept them in `data.tar`, unencrypted (readable by root only), so a restore still has them. That behaviour is kept, and now setup, every monthly run's log, the snapshot's manifest (`"password_files": "unencrypted_in_data_tar"`) and MONTHLY-MAINTENANCE.md all say so.
- **The Rust test that failed in CI now and then is fixed.** Its stand-in for OpenRGB answered after 4 seconds while the client waits 3, so it passed only when the system happened to deliver early. It now answers within the client's limit and still proves the fans never wait for the lights.

### Fixed (Debian / Ubuntu)
- **Finishing half-installed packages no longer sets up unneeded alternatives.** For a dependency with a choice (`A | B`), both counted as needed, so an unfinished B was set up even when A was installed. Now nothing is added when one choice is already set up (or provided by an installed package); otherwise only the first unfinished one is.

### Changed
- NEXTCLOUD.md says what is really backed up. The monthly snapshot holds Nextcloud's application, settings and database, not your files; those get the weekly USB copy. The monthly restore test proves the restored database, not the whole app with your files.
- The control service (`nekoserver-control`) runs whenever the dashboard is installed, because app controls need it. With fan control switched off (`"fan_control": false`), it never takes over a fan, and any fan it held goes back to the motherboard. Setup checks that before carrying on.
- Version 0.28.5; the release folder is now `nekoserver-v0.28.5`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- No PIN yet? Run setup again in a terminal to choose one; app controls stay off without it.

## v0.28.4 — 2026-10-21

### Fixed (every Linux)
- **Every Immich original is now archived, whatever its format.** The photo archive copied only files with extensions on its own list. Files Immich accepts but the list lacked (`.jfif`, `.jpe`, `.jp2`, `.psd`, `.svg`, `.mxf`, several camera RAW formats) were left out without a word, and the job still reported success. The USB photo backup copies that archive, so they were missing there too. Now every regular file in Immich's upload and library folders is archived, whatever its name or extension, including Immich's own `.immich` folder markers, so formats Immich adds in later versions are covered too. Every copy is still checked by checksum, a file that changes while it is copied is not kept, and symlinks or special files still stop the run. The next archive run copies the files that were left out before.
- **Immich comes back after a manual snapshot that was cut off.** The manual Immich snapshot (`recover.py snapshot`) stops Immich's services and restarted them only in a `finally` block. That never runs after a power cut, an out-of-memory kill or `kill -9`, so Immich stayed offline until restarted by hand. Before stopping anything, the snapshot now records which services it stopped and installs a one-time boot hook. At the next boot, the hook (or `sudo python3 recover.py resume`, or the next snapshot) starts them again. The record and the hook are removed only once they are confirmed running.
- **A second graphics chip is tried when the first fails.** On a machine with Intel and AMD graphics, only Intel was checked. If it failed, both apps went to software even though the AMD chip might have worked. Each app now gets the first chip, Intel first, that passes the check inside that app.
- **The finishing screen shows the dashboard at its real address.** With `dashboard.port` changed (to 8899, say), it still said `http://127.0.0.1:8787`. It now uses the configured port, and the default port is written in one place instead of seven.
- **Sonarr, Radarr and Lidarr could change the media library while an update was still being checked.** They started normally, with a writable library and their background jobs running. An import, rename or delete in that window stayed after a rollback, while their databases went back, so the two could disagree. While checked they now see the library read-only, keeping their ports for the media connection audit. After the commit they start as usual.
- **Hardware video works with only one of Jellyfin and Immich.** With Immich but not Jellyfin, the chip was checked inside a Jellyfin that doesn't exist, so Immich was never switched over. With Jellyfin but not Immich, setup said "needs setup: Immich" for an app you turned off. Now only the apps this server runs are checked, each inside its own container, and only those count towards finishing the step. A chip that fails one app's check leaves that app in software; that is a result, not a step left to do.
- **A backup that systemd can't even start now raises an alert.** Its worker writes no report then, so the dashboard showed "Unknown — No recorded successful run" for ever, and nothing reached the phone. The collector now reads the backup service's own result: a run systemd couldn't start or that crashed counts as failed, with an alert. A backup that has never succeeded becomes overdue one interval plus a day after its schedule was set up.
- **A monthly update that failed before its snapshot no longer leaves passwords behind.** The saved app definitions (`*-old.json`, with database passwords) are written before the recovery journal exists. When an early check failed there was nothing to roll back, so they stayed in plain text in the snapshot folder on the HDD (root-only). They are now blanked after any failed run; a run a journal still needs is left alone.
- **Immich set up with its own worker split** (`IMMICH_WORKERS_EXCLUDE` in its settings) was left with no worker at all while the monthly update checked it, so every update failed its checks and rolled back. The exclusion is set aside for the check.

### Fixed (Debian / Ubuntu)
- **A package you hold** (`apt-mark hold docker-ce`, say) counted as not installed. Setup then tried to install it again, and apt refused because held packages may not change. Held packages now count as installed and are left alone.
- **Ubuntu's automatic security updates no longer make setup fail at random.** When they were running, apt stopped at once because dpkg was busy. apt now waits for them, up to 10 minutes.
- **Ubuntu's needrestart can no longer restart your running services during setup.** After an install it may restart services that use updated libraries, a Docker you already run included. It is switched off for NekoServer's package installs.
- **A new setup run waits for a cut-off installation that is still working.** After an interruption, setup already waited for apt and dpkg before tidying up. But a fresh setup run went straight into the tidy-up, so it could remove the record, the service masks and the start policy while a surviving dpkg was still installing. Every tidy-up now waits until apt and dpkg have stopped. If they are still busy, it keeps everything protected and says so. A `dpkg --audit` that fails also stops the tidy-up rather than counting as "nothing unfinished".
- **Finishing half-installed packages touches only NekoServer's.** It used `dpkg --configure -a`, which also sets up any unrelated package that is waiting (a half-installed Postfix, say), along with its problems. Now only this installation's packages and the unfinished packages they depend on are set up; others are left as they were.
- **Recovery works offline.** `install.sh --recover` repaired the Docker and Tailscale repositories first. Without a network, a missing signing key stopped it before the recovery wizard opened. Recovery now skips that repair; a recovery step that installs packages does what it needs then.
- **Ubuntu-based systems that name only Ubuntu** in their os-release (elementary OS, for one) passed `install.sh` and then failed at "not supported yet". They are recognised as Debian/Ubuntu everywhere.

### Changed
- The README treats Arch Linux, Nyarch, Debian and Ubuntu alike, and no longer points to the Arch-only edition, which is retired.
- Version 0.28.4; the release folder is now `nekoserver-v0.28.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.3 — 2026-10-21

### Fixed (every Linux)
- **Passwords no longer stay in monthly snapshots.** The definitions of Immich and Nextcloud as they run during the checks (`*-quarantine.json`, new in 0.28.2) hold the same database passwords as the other saved definitions. When the update was over they were not blanked like the others, so the passwords stayed in plain text in the snapshot on the HDD. They are blanked now; snapshots from 0.28.2 are cleaned at the next monthly run.
- **Nextcloud's file check leaves nothing behind.** Before and after each update, the check writes a small test file in the administrator's files and deletes it. The deleted file went to the trash bin, so one was left there every month. After a rollback, it was also a file the restored database knew nothing about. The trash bin is now paused for that one deletion, where Nextcloud allows it.
- **A failure after the update is committed says so.** If Immich or Nextcloud could not be opened up again, the message read like a failed update. It now says the update is installed and that the resume service retries opening them up, without rolling back.

### Fixed (Debian / Ubuntu)
- **Setup waits for dpkg as well as apt.** When an installation is cut off, apt-get stops and lets go of its lock, but the dpkg it started can keep working. Setup only checked apt's lock, so it could tidy up while dpkg was still installing. It now waits until dpkg's own lock is free too.
- **Packages left half set up are finished under protection.** A cut-off installation can leave packages unpacked but not set up. Setting them up later by hand (`dpkg --configure -a`) would start their services with nothing holding them back. Now NekoServer finishes setting them up itself while their services are still masked and kept from starting, then switches them off as usual. If that fails, the services stay protected and setup says what went wrong. Half-set-up packages that have nothing to do with NekoServer's installation are left to whoever installed them.
- **LMDE (Linux Mint Debian Edition) gets the right Docker and Tailscale repositories.** Its release name (`faye`) is not a Debian one; the Debian release it is built on (`DEBIAN_CODENAME`, `bookworm`) is used, as Ubuntu-based systems already use theirs.
- **A `policy-rc.d` that hangs or can't run** made setup stop with a Python error. It now counts as not refusing, so setup stops with the usual explanation of how to move it aside.
- **Samba's unchanged example installed before NekoServer** (for example with `smbclient`) stopped setup with "Existing unmanaged smb.conf; review manually". When Debian's record proves the file is the untouched example, the message now says so and how to move it aside. NekoServer still doesn't move it by itself, because Samba may be in use for something else.

### Changed
- Version 0.28.3; the release folder is now `nekoserver-v0.28.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.2 — 2026-10-21

### Fixed (every Linux)
- **A failed monthly update can no longer leave photos or files out of step with their database.** The updated apps used to start on their usual ports before every check had finished, so phones could reconnect and upload, edit or delete in that window. If a later check then failed, the rollback restored the old Immich and Nextcloud databases but not the photo library or user files, which aren't in the monthly snapshot. That could leave uploads with no database entry, or database entries pointing at deleted files.
  - While Immich and Nextcloud are checked, they run on a random port on this computer only, with no background jobs (no Nextcloud cron, Immich API only).
  - The update is committed (journal phase `committed`) only after every check and the media audit have passed. Only then do the two apps open up again.
  - A failure before the commit rolls back while nothing could have changed those files. A failure after it keeps the update and only retries opening the apps up.
  - Tests run the whole sequence with stand-ins for Docker and induce failures at each step. A real Docker run with an induced failure still needs to be done on a server.

### Fixed (Debian / Ubuntu)
- **An installation that times out is tidied up too.** The tidy-up ran only when apt finished on its own. A timeout skipped it, while the "don't start services" policy was already gone. Newly installed Samba services stayed enabled, so a restart before the next setup run could start them before their drive guards existed. Now:
  - The new services are masked before installing, so they cannot start, not even after a restart.
  - After any interruption, setup waits until apt and dpkg have really stopped, then tidies up.
  - The masks and the policy are removed only once the services are confirmed switched off. If that fails, they stay for the next run.
  - A mask the package set itself (`samba-ad-dc`) is left alone.
- **Two setups can't step on each other.**
  - `install.sh` takes a setup lock before doing anything, and the installer keeps it until setup ends; a second `sudo bash install.sh` stops right away.
  - Every package job takes a separate package lock: installing, tidying up and repairing repositories.
  - Tidying up a half-done installation now happens inside the installer's lock. A second process can no longer mistake a running installation for an abandoned one and remove its record and start protection.
- **Repository entries written over several lines are recognised.** In deb822 `.sources` files, a value can continue on the next lines (`URIs:` with the address underneath), and field names are not case-sensitive. NekoServer missed such entries and added a second, conflicting definition.
- **Repository problems are repaired before apt first runs.** A duplicate definition left by 0.28.0 made the first `apt-get update` fail before the repair could happen. `install.sh` now runs `python3 host.py repair` first.
- **Upgrades move the Tailscale key even when nothing needs installing.** The repair now updates NekoServer's own repository definitions on every run:
  - 0.28.0's Tailscale list moves to the key under `/etc/apt/keyrings`.
  - A missing key is downloaded again.
  - Offline, a definition that still works is kept as it is.
- **Samba's `smb.conf` is moved aside only when Debian's record proves it is the untouched example.** That record is the checksum `ucf` keeps for it. Without the record, the file stays, and the Samba step asks you to review it.

### Changed
- Code cleanup from SonarCloud's review of 0.28.1, with no change in behaviour:
  - The base package adapter's `prepare` no longer returns an unused default.
  - Two `dict(...)` calls became dictionary comprehensions.
  - Tests that expect an error now make only the call that should fail inside the check.
- Version 0.28.2; the release folder is now `nekoserver-v0.28.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.1 — 2026-10-21

### Fixed (Debian / Ubuntu)
- **A package installation that fails part-way is tidied up.** apt can install some packages and then fail. Before, the error came before the tidy-up, and a retry skipped the packages that were already in. Their services stayed switched on for the next restart, before NekoServer's drive guards existed. Samba's example `smb.conf` also stayed, so the Samba step refused it. Now each installation is recorded first (`/var/lib/nekoserver-host/package-install.json`), with what the system was like before. Whatever got installed is tidied up even when apt fails, and an installation cut off earlier is finished at the start of the next setup run.
- **Samba's example settings are told apart from yours.** The packaged `smb.conf` is set aside only when none existed before the installation and the file is still exactly what the package wrote (`ucf`'s record of it). An `smb.conf` you edited is never moved.
- **Existing repository definitions are respected.** Docker's and Tailscale's repositories are recognised by their address in every source file: `.list` or deb822 `.sources`, whatever the file is called, switched-off and commented entries excepted. A second definition with a different key made apt stop working ("Conflicting values set for option Signed-By"). NekoServer now adds its own only when there is none. If it finds its own next to someone else's, it removes its own. A file that has NekoServer's name but isn't NekoServer's is never overwritten.
- **Package checks work in every language.** The package tools and `systemctl` now run with `LC_ALL=C`, because their output is read: French apt prints `Candidat :`, so Caddy, video drivers, smartmontools and OpenRGB looked unavailable.
- **The Tailscale repository key is in host backups.** It now lives in `/etc/apt/keyrings` with the other keys. Restored repository settings whose key is missing get the key downloaded again. 0.28.0's Tailscale list is moved to the new key place.
- **Someone else's `policy-rc.d` can't let services start early.** If one exists, it must already refuse to start the services being installed. If it doesn't, setup stops before installing and explains how to move it aside for the installation. One left behind by an interrupted installation is removed.
- **Switching services off is checked.** After installing, setup checks that each new service is really disabled and stopped. If one isn't, setup stops and tells you the command. Services that aren't installed or are masked are left alone.
- **A Docker you already run is left running.** Installing only a missing part (Buildx, say) no longer reinstalls or upgrades the rest. It also no longer stops and disables Docker's service. Only services that are new with this installation are switched off, and packages already there are never reinstalled.
- **TV setup is "Not available on this system"** on Debian/Ubuntu, instead of passing. It isn't counted as done or as waiting. The finishing screen lists it on its own, and acceptance no longer asks for TV evidence there.

### Fixed
- **The installer copy kept on the SSD can set the dashboard up again.** It kept the dashboard's page, styles, scripts and logo, but not its app manifest and icons (`manifest.webmanifest`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`). So after an OS rebuild, running dashboard setup from that copy stopped with "file not found"; the dashboard already installed kept working. The whole dashboard folder is now kept. A test checks that the kept copy has every file of the release, except tests and the template settings, and that dashboard setup can load its files from it.

### Changed
- The acceptance report's host check names both supported families.
- Version 0.28.1; the release folder is now `nekoserver-v0.28.1`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.0 — 2026-10-21

The first release of **NekoServer All**, the edition for all major Linux distributions. NekoServer for Arch continues separately.

### Added
- **Debian 12+ and Ubuntu 24.04+** (and systems built on them, such as Linux Mint 22). This is new and not yet proven on a real install, so try it on a spare PC or a virtual machine first.
  - `install.sh` only refreshes the package lists (`apt-get update`) and installs just what NekoServer needs. It never upgrades the rest of the system. If an earlier update is waiting for a restart (`/run/reboot-required`), it stops and asks for the restart before changing anything.
  - **Docker comes from Docker's official repository** (`docker-ce` with the Compose plugin) and Tailscale from Tailscale's. Their signing keys and repository lists are added only when no list for them exists yet. If Debian's own Docker packages (`docker.io`, `podman-docker` and so on) are installed, setup stops and asks you to remove them, rather than mixing the two.
  - Debian's packages start their services as they install. NekoServer prevents that while installing and leaves Docker, Caddy, Samba (with NetBIOS) and Tailscale switched off afterwards. Each one's own setup step switches it on once its drive and settings are ready, so Docker still never starts on the system disk.
  - Samba's example `smb.conf` from the package is moved aside to `smb.conf.packaged`. NekoServer then writes its own, as on Arch. A `smb.conf` that existed before is never touched.
  - Samba runs as `smbd.service`. The dashboard, notifications and the host backup find whichever Samba service is installed.
  - Host backups keep apt's sources, keys and preferences, plus `/etc/default/locale` and `/etc/default/keyboard`, instead of pacman's settings.
  - Package names are mapped; for example, the video drivers are `intel-media-va-driver`, `mesa-va-drivers` and `vainfo`. OpenRGB lighting is skipped when the system's repositories don't offer it.
  - The TV step (Steam Big Picture) is skipped on Debian and Ubuntu for now.
- **Setup checks the computer first.** Before changing anything, every setup run lists what it found and stops when a requirement is missing. The requirements are an x86-64 processor, Python 3.11 or newer, systemd 247 or newer, a supported Linux family and no restart pending from an earlier update. Docker and Compose versions, control groups v2, SELinux, AppArmor, a switched-on firewall (ufw, firewalld, nftables) and whether a desktop runs are reported as well (`capabilities.py`).
- **One place knows the Linux family** (`host.py`): its package manager, package names, service names, vendor repositories and the settings files that belong in host backups. [PORTING.md](nekoserver-v0.28.0/PORTING.md) explains how another family is added.

### Changed
- Every module that installed packages called `pacman` itself (Docker, Compose, Samba, Tailscale, Caddy, OpenRGB, smartmontools, hdparm, video drivers, the TV packages, Firefox/xdg-utils and qrencode). They now all ask `host.py`, and a test fails if `pacman`, `apt-get` or `dpkg` appears anywhere else. On Arch the same packages are installed in the same way.
- `install.sh` installs the apps' packages through `python3 host.py bootstrap` on both families. Arch still runs its full `pacman -Syu` first.
- `neko_common.arch_host()` was replaced by `host.supported()`.
- The guides say which steps differ between Arch and Debian/Ubuntu.
- CI runs on pushes to `main`.
- Version 0.28.0; the release folder is now `nekoserver-v0.28.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.8 — 2026-10-21

### Changed
- With `"rgb": false` the fan and light service no longer even opens the saved lighting file (it already ignored it).
- Version 0.27.8; the release folder is now `nekoserver-v0.27.8`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.7 — 2026-10-21

### Fixed
- **Switching off only RGB lighting no longer stops setup.** With fan control still on, setup stopped OpenRGB and then took the records of the fans still under control as a sign that fans had not gone back to the motherboard, and stopped with that error (reproduced). Those records are now only checked when fan control itself is switched off.

- **`"rgb": false` now really leaves lighting alone.** Setup removed NekoServer's OpenRGB service, but the fan and light service still read the saved lighting, asked OpenRGB every few seconds and set its saved colours again, so an OpenRGB you run yourself (its app with the SDK server on) could be overwritten. The service is now told lighting is off: it never reads, asks or changes lighting, and the page says lighting is switched off.
- **Setup tells the truth about old app snapshots.** It said "No automatic backup deletion", although by default the newest 14 complete snapshots are kept and older ones deleted. It now says what the setting does (and "none are deleted" only with `0`).
- **The app backup guide no longer contradicts itself.** One paragraph said symlinks, hard links, special files and nested filesystems make the backup fail; they are skipped and listed (hard links copied as ordinary files), as the guide already said earlier. Only custom writable mounts and folders shared with another container make it fail.

### Changed
- Version 0.27.7; the release folder is now `nekoserver-v0.27.7`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.6 — 2026-10-21

### Changed (code cleanup; no change in what the server does)
- **Removed unused code.** The old Python dashboard collector (replaced by the Rust one in 0.26.0) is gone; the small checks setup, the gateway and the acceptance checker still use moved to `status_checks.py`. A full scan for unused functions, variables, imports, page code, styles and page elements finds nothing left.
- **One definition instead of copies:**
  - one list of apps with their names and ports (`neko_common.APPS`), used by setup, checks, accounts, the TV shortcuts and the phone page (it was typed out in nine places);
  - one way to talk to the apps without proxies or redirects (`local_opener`, was three copies plus eight hand-built openers);
  - one place for systemd units and their "managed by NekoServer" check (`check_units` / `write_units`, was eight copies);
  - one Seerr sign-in, one check for source addresses;
  - in the Rust runtime: one HTTPS delivery (heartbeat and notifications), one way to open a backup job, stage and publish a snapshot (app and host snapshots), one "move without replacing", one fan-speed conversion, one table of error sentences;
  - on the page: one way to show fan speeds and temperatures.
- Duplicated code measured with jscpd: 14 copies (145 lines) before, 2 short ones (27 lines) left.
- **Bug-risk fixes found by a full Python lint pass (ruff):** helper functions inside loops now keep the value of their own loop pass; errors re-raised inside error handlers keep (or deliberately drop) their cause; loop variables are no longer overwritten; default arguments no longer call functions when a file is loaded; and every `subprocess.run` that checks the result itself now says so (`check=False`).
- **The shared command and report helpers are stricter:** only known programs with plain arguments are run, and only plain absolute paths are read (found by SonarCloud after the move).
- New tests check that names the installer and the runtime share (fan recovery files, settings folder, phone address port) stay the same in both.
- Version 0.27.6; the release folder is now `nekoserver-v0.27.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.5 — 2026-10-21

### Fixed (review of v0.27.4)
- **Undo can't bring back an old fan speed.** After saving 80 %, Undo read the page's older reading and put 40 % back, which a later name-only save then sent (reproduced). Each fan now remembers the setting the server last confirmed, and Undo returns to that.
- **A passing Tailscale problem no longer removes the phone address.** When Tailscale couldn't be asked during setup (not connected yet, restarting), the dashboard's Tailscale name was removed as if remote access had been switched off. The name already in use is now kept; it is only removed when remote access is switched off.
- **The acceptance check expects the same backup account as setup.** Setup keeps backing up the first account, but the check worked out the account again from whoever ran it and reported "needs setup". Both now use the same answer.

### Changed
- Version 0.27.5; the release folder is now `nekoserver-v0.27.5`.
- Code check: the host snapshot list is built in small steps, and a dashboard constant moved to the top of its file.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.4 — 2026-10-21

### Fixed (review of v0.27.3)
- **A damaged fan record is never "restored".** A recovery record with missing or wrong fields (valid JSON otherwise) switched the fan to mode 2, reported success and deleted the record. Records are now checked field by field; one that isn't a real setting is kept, moved aside and reported, and nothing is changed.
- **A save can't be undone by an older reading.** A reading asked for just before saving 80 % could arrive afterwards and put 40 % back in the editor, and renaming the fan then saved 40 % over the good change (reproduced). Readings asked for before the last save are no longer taken into the editor.
- **"All off" (and "Apply to all") win over edits still on their way.** A colour change sent a moment later could turn a light back on (reproduced).
- **A reset device is noticed whatever it shows.** The check accepted the right effect with the wrong colour or brightness, and an animation as a steady colour. It now checks the effect and its colour, or a steady mode with every light at the chosen colour.
- **Remote access set up after the dashboard now reaches it.** With the dashboard installed before Tailscale was connected, setting up remote access created the phone address, but the dashboard refused it (E41, reproduced). Every setup run now gives the dashboard its current Tailscale name (and restarts it) when that changed.
- **Lighting saved by 0.27.2 isn't lost.** Two different devices sharing a serial string were named by their i2c place in 0.27.2 and by serial in 0.27.3, and the 0.27.2 names weren't moved over (reproduced: both lights lost their choice). 0.27.2 names are now moved too.
- **Host snapshots keep backing up the same person.** With `"user": "auto"`, setup run by another administrator replaced the first user's Steam backup with their own. The account chosen the first time now stays chosen until it is named differently.

### Changed
- Version 0.27.4; the release folder is now `nekoserver-v0.27.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.3 — 2026-10-21

### Fixed (review of v0.27.2)
- **Fixed speeds keep their safety net.** A fan at a fixed speed kept running slowly when every temperature reading disappeared (only curves were covered). Losing every temperature now sends every controlled fan to full speed.
- **Failing fan speed sensors can't delay the safety checks.** Each unreadable speed sensor added up to 0.4 s of retries without checking temperatures or Stop, so ten failing sensors delayed the next check by 4 s (reproduced). All fans are now read together, and retries wait through the same safety checks as the rest of detection, at most twice per measurement.
- **Detect fans respects a sensor's own critical limit.** It only stopped at 85 °C, so a sensor with a 70 °C limit could run hot while fans were slowed. Detection now stops on the same limits as normal fan control, with its 85 °C ceiling on top.
- **A fan output that moved is never confused with another chip.** Linux reuses hwmon numbers; an old path could point at a different chip before the next look (reproduced: an unrelated chip's fan changed). Outputs now keep their resolved device folder and are checked to be the same chip on the same device before every change; a moved output is looked for again at once.
- **"Full speed" is claimed only when it was set.** When a fan refused to go back to the motherboard and full speed couldn't be set either, the page still said full speed. It now says the speed couldn't be set.
- **A fan is only taken over when its own setting can be read.** Otherwise its original setting would be unknown and couldn't be put back.
- **Damaged recovery records are kept and reported.** A record that couldn't be read was treated as empty and deleted. It is now kept (moved aside when the service starts), never written over, and the page and the restore command say so.
- **Conflicting recovery records can't undo a good restore.** Both records are now merged first, the main one winning, and each fan is restored once.
- **Lighting names moved at start are saved at once,** so lighting survives a restart that renumbers a device even if the dashboard was never opened.
- **Another device with the same serial no longer renames yours.** Serial clashes now only count between devices with the same name.
- **Lighting comes back after a device or OpenRGB reset.** NekoServer now checks every 10 seconds that each device still shows its choice, and sends it again when not.
- **Switching fan control off in setup is checked.** Failures stopping the services were ignored. Setup now stops with a clear message if the services keep running or a fan didn't go back to the motherboard (the record is kept when the service stops, so it can be checked).
- **Turn off wins over a colour still on its way.** A colour change sent a moment later turned the lights back on.
- **Undo shows what will be saved.** The speed labels and the name kept the discarded values.
- **Host snapshots include the new settings.** Fan curves, lighting choices, the PIN hash, the dashboard settings, OpenRGB's settings and the i2c driver setting were not backed up. They are now, and every setup run brings an existing list of places up to date.

### Changed
- Version 0.27.3; the release folder is now `nekoserver-v0.27.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run setup once so host snapshots pick up the new settings.

## v0.27.2 — 2026-10-20

### Fixed (review of v0.27.1)
- **Fan recovery records are never deleted early.** When a fan couldn't be handed back and the combined record couldn't be saved, both records were deleted; detection also deleted its record without checking that moving it had worked. Now a record is removed only after its replacement is safely on disk, and if saving fails both stay exactly as they were. When both records name the same fan, the one from before NekoServer touched it wins.
- **Detect fans won't start while a fan isn't back with the motherboard.** Otherwise it could record NekoServer's own manual setting as the "original" and later put that back. It now refuses with a short explanation.
- **An unreadable fan speed is no longer read as a slowdown.** A failed reading counted as 0 RPM, so detection could link a fan to the wrong output. Readings are retried, and a fan whose speed stays unreadable is left out of that measurement.
- **Lighting keeps its device after a restart.** A device was named by its connection path first, even `/dev/hidrawN`, which Linux may renumber. Now a usable unique serial number comes first, then a stable place (HID paths left out, i2c bus numbers dropped, slot addresses kept). Choices saved by 0.27.0 and 0.27.1 move to the new names.

### Changed
- Version 0.27.2; the release folder is now `nekoserver-v0.27.2`.
- Code check: the installer's file checks are split into three small steps, and the page copies a fan setting with `structuredClone`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.1 — 2026-10-20

### Fixed (review of v0.27.0)
- **Fans are handed back after a crash during Detect fans.** Detection kept the motherboard's own fan settings only in memory, so a crash during its 35 % test could leave fans in manual mode. Every fan's original setting is now written to disk *before* it is changed (for curves and for detection alike), the recovery command reads both records, and the service also puts back anything left over when it starts. Stopping the service now waits for a running detection to hand its fans back first.
- **Lighting can't delay cooling.** Talking to OpenRGB held the same lock as the fan loop, so a slow or hung OpenRGB could delay temperature checks and the full-speed safety response by seconds. Fans, lighting and the PIN now have separate locks, each request runs on its own, retrying lights has its own thread, and the page shows the last lighting answer while OpenRGB is busy.
- **Detect fans needs temperatures it can read.** A sensor that failed was simply ignored while fans were slowed down. Detection now won't start when no temperature can be read, and stops (handing the fans back) as soon as a watched temperature can no longer be read.
- **A fan that won't go back to the motherboard is never shown as handed back.** Errors putting back the motherboard's setting were ignored and the record of it dropped, so the page could say "Motherboard" while the fan stayed in manual mode. The result is now checked: until it works, the record is kept (in memory and on disk), the fan runs at full speed, the page says *Not handed back*, and NekoServer keeps trying, also after a restart.
- **Changes that can't be saved are refused.** Failures writing the fan or lighting settings were ignored, so a change could work until the next restart and then vanish. Such a change is now refused with the new code E46, and nothing changes.
- **The fan page no longer shows old readings as current.** After losing the connection it kept showing the last temperatures and speeds without a warning. They are now dimmed, the badge and banner show the error, and nothing can be changed until fresh readings arrive.
- **Edits made while saving are kept.** Changing a fan while its Save was on the way marked everything as saved although only the earlier values were sent. The page now tracks which edit was saved and asks you to save again.
- **Lighting controls follow changes.** After *Apply to all* or a change on another device, only the colour dot changed; the colour, brightness and effect controls now follow too, except while you're using them.
- **Brightness is honest about effects with their own colours.** Brightness only scales the chosen colour, so it did nothing for effects such as a rainbow. For those effects, colour and brightness are now switched off with a short explanation.
- **Lights are retried until every device got its setting.** A device missing when the lights were restored (still starting, plugged in later) was skipped for good. The service now keeps retrying until every saved device has received its lighting.
- **Identical lighting devices keep their own settings.** Two identical RAM sticks or controllers shared one name (name and description only), so they shared settings and appeared as one card. Each device is now named by where it is connected (or its serial number, or its position as a last resort), and the card shows where it is. Choices saved by 0.27.0 move to each matching device.

### Changed
- Version 0.27.1; the release folder is now `nekoserver-v0.27.1`.
- Smaller fixes from the code check: no `!important` in the stylesheet, and the dashboard installer is split into short steps.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.0 — 2026-10-20

### Added
- **Fan control that really controls the fans.** The new *Fans & lights* page finds every fan output Linux can drive (motherboard chip, graphics card, cooler) and runs each one by a temperature curve, at a fixed speed, or leaves it to the motherboard. Curves are edited by dragging their points (or with the arrow keys), with *Silent*, *Balanced* and *Performance* presets, a minimum speed, and a live marker for the temperature now. Written in Rust like the rest of the runtime.
- **Detect fans.** A guided test finds which fan is on which output: all fans to full speed, then each output slowed in turn while the speeds are watched. It can be stopped at any time, stops by itself at 85 °C, and always hands the fans back to the motherboard.
- **RGB lighting that changes.** OpenRGB is installed from Arch's own repository and runs as a service on this computer only (with the i2c driver for memory and motherboard lights). Each device gets a colour, brightness, an effect and on/off, plus *Apply to all*; choices come back after a restart.
- **A PIN for phones and tablets.** On the server itself changes need nothing more. Other devices (through Tailscale) can always look, but need the PIN chosen during setup to change fans or lights; five wrong tries lock changes for 5 minutes. Only a slow, salted hash is kept.
- New error codes: E21 (fan and light control unavailable), E43 (PIN needed), E44 (wrong PIN), E45 (too many tries). Help has two new guides, *Fans don't change* and *Lights don't change*.

### Safety
- Hardware is changed only by a new root service, `nekoserver-control`, reached over a private socket that only the dashboard's (still unprivileged) web server can use. Every request is checked: known outputs, sensors and lights, sensible ranges, and changes only from the dashboard page itself.
- Fans speed up at once and slow down gently; a controlled fan never goes below 20%; every controlled fan goes to full speed at 95 °C, near a sensor's own limit, or when its temperature can't be read. Whenever the service stops (a crash too) every output is handed back to the motherboard.
- `"fan_control": false` (or `"rgb": false`) under `"dashboard"` turns it off; setup then gives the fans back to the motherboard.

### Changed
- Version 0.27.0; the release folder is now `nekoserver-v0.27.0`.
- RGB devices moved from *Sensors* to *Fans & lights*; live readings no longer ask OpenRGB every minute.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run setup in a terminal to choose the PIN; without one, fans and lights can be changed on the server only.

## v0.26.2 — 2026-10-20

### Fixed (review of v0.26.1)
- **A missing Nextcloud folder no longer gives a successful weekly backup.** When the Nextcloud files folder was missing, the USB job said "nothing to copy" and could still pass without Nextcloud. It is now recorded as incomplete, with the reason on the dashboard and in notifications.
- **Service alerts reach your phone.** The new "stopped with an error" and "won't start after a restart" alerts had no notification label, so they were silently dropped (reproduced). They are now sent, and "RECOVERED" is sent only when the service list shows the service enabled or no longer failed (never when the list is missing).
- **"Restore tried" asks only about backups you have.** It counted placeholder rows for backups that aren't installed, so a server with only app backups (restore tested) showed "1 of 3" (reproduced). Only backups whose schedule is installed (even if switched off) or that have ever run count now.
- **A failed ownership restore no longer reports success.** The app and host restore wrote "original owners restored" into its report before changing any owner, so a failed `chown` left a report claiming success (reproduced). The report now says "incomplete" until every owner and permission has been applied.
- **NVIDIA figures are never shown as live when they are old.** They come from the minute-by-minute collector, and were merged into the live readings without checking its age. Each card now shows when its figures were measured, and once the collector is late they are left out with a note instead.

### Changed
- Version 0.26.2; the release folder is now `nekoserver-v0.26.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.26.1 — 2026-10-20

### Fixed (review of v0.26.0)
- **An open dashboard no longer keeps the media hard drive awake.** Live readings asked SATA drives for their temperature every few seconds, which resets the spin-down timer on some hard drives. Those temperatures now come from the drive health check (every 30 minutes, never waking a sleeping drive).
- **Readings are never shown as current when they aren't.** When live readings failed, Home kept showing the old ones under "Connected"; when the snapshot failed, "Everything looks good" could stay beside the error. Each feed is now tracked on its own: errors show on every page with their code, old live readings are dimmed and marked with their time, and the overall status includes failed requests.
- **"Restore tried" means the backups you have were restored.** It turned green for any completed checklist, including one without a single restore test (reproduced with the dashboard profile). It now counts only the witnessed restore tests for the backups this server makes (photos, apps, host and user files) and names the ones still to try.
- **Checksum results need a date.** A checksum report without a valid date, or older than two weeks, showed "Checksums match" (reproduced). It now shows "Not dated" or "Overdue", and the time of the last recorded check.
- **Keyboard and controller focus stays put.** Starring an app or a refresh rebuilt the cards and dropped focus to the top of the page (reproduced). Focus now returns to the same control.
- **A failed service is never green.** "Starts by itself" only looked at whether a service starts at boot, so an enabled Samba that had failed was green with no warning (reproduced). Working now and starting at boot are shown separately, and a failed service raises an alert (urgent for Docker, Tailscale, the dashboard and Samba).
- **No false "out of date" with a five-minute schedule.** Readings counted as late after a fixed 150 seconds, although a 300-second collection interval is allowed (reproduced: E12 after 151 s). The collector now publishes its interval, and the page and the acceptance check both allow two intervals plus 30 seconds.
- **A manual photo snapshot can't silently leave out an album.** Its file listing skipped folders it couldn't read, so a snapshot missing an album passed, and so did its verification (reproduced). Planning, copying and verifying now share one strict listing: anything unreadable stops the snapshot, which stays marked incomplete.
- **A manual snapshot checks that Immich stopped cleanly.** It now refuses to copy when a container is still running or was killed (exit 137 or out of memory), like the nightly app snapshot.
- **A corrected clock no longer postpones full checksum checks for years.** A check recorded while the clock was wrong (in the future) counted as recent. Future or invalid records now make the full check due, in the backup program and in the space estimate.
- **USB backups on exFAT stay private.** exFAT has no owners or permissions, so the backup's chmod was silently ignored and other local accounts could read the Nextcloud copies and database dumps. exFAT drives are now mounted for root only, and a drive already mounted readable by others is refused before anything private is copied. The restore helper also releases the drive (and unmounts it) when opening it fails.

### Changed
- Version 0.26.1; the release folder is now `nekoserver-v0.26.1`. v0.26.0 was merged before these fixes, so this release carries its own version (an installed release folder is never overwritten).
- The dashboard script was restructured for readability (SonarCloud maintainability: no nested conditions, small functions), with no change in behaviour. Folder walks check their path first (absolute, no `..`, no symlinks; recovery folders use the same plain characters as mount paths).

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.26.0 — 2026-10-20

### New dashboard
- **A new dashboard, in Rust, replacing the old one.** Same cat and colours, rebuilt for everyday use: Home (what needs attention, apps, storage and live readings), Your apps (with each app's processor and memory use), Backups, **Hardware**, **Sensors**, **Processes**, Server health, Needs attention and Help.
- **Hardware and sensors are found automatically** by the dashboard's own web server, with no setup and without root: processor (total, each core, clock, waiting work), memory and swap, every graphics card and AI chip (named from the system's PCI list; AMD load and video memory, Intel clock, NVIDIA through the collector), every hardware-monitoring chip (temperatures, fans, fan outputs, power, voltages; thermal zones when no chip reports a temperature), network traffic per card and Tailscale, drive activity, batteries and UPS, and RGB devices of a running OpenRGB server. Readings are taken only while a page asks for them. Nothing is ever written to fans, lighting or processes. `nekoserver-runtime hardware` prints what it finds.
- **Processes**, with the app each one belongs to (ffmpeg → Jellyfin). Names only: never command lines, environments or users.
- **Phone, tablet and TV:** a bottom tab bar on phones, an icon rail on tablets, larger touch targets and text (Settings → Large or TV).
- **Use it like an app instead of a browser tab:** it can be installed on phones, tablets and computers (Add to Home Screen / Install app) and opens in its own window. If the server can't be reached, the page still opens and says why. On the server, **NekoServer Dashboard** in the app menu opens it in an app window (Chromium, Chrome, Brave, Edge or Vivaldi when installed; otherwise the normal browser).
- **Short error codes** (E01 … E42) with one plain sentence each, shown at the top of the page and explained in Help and DASHBOARD.md.
- **Starts by itself:** the dashboard restarts if it ever stops (Restart=always). Server health lists what starts with the computer (Docker and the apps, Tailscale, the dashboard, Samba, the resume services, the schedules), and Needs attention warns when one of them is switched off.

### Fixed
- **Upgrading from v0.25.1 works.** The follow-up fixes below were merged while the release still called itself 0.25.1 everywhere inside (Python, the Rust runtime, its build manifest and install paths). Releases are installed into immutable per-version folders, so installing it over the real v0.25.1 was refused ("Installed release differs; refusing overwrite" and "Existing native runtime differs; custom file preserved", reproduced). This release is consistently 0.26.0 (0.25.2 to 0.25.31 were not used as internal versions), and a test now checks that the folder, Python, Cargo, config, README, CI and the install paths in the guides all name the same version. The immutable-file protection is unchanged.

### Fixed (follow-up review of v0.25.1)
- **Jellyfin-only setups pass the monthly check too.** The Python check used Jellyfin's own key, but the bundled runtime (used by the monthly update and the dashboard) still read the key only from Seerr's settings, so a Jellyfin-only server stayed "needs setup" and monthly maintenance was blocked (reproduced). The runtime now reads `secrets/jellyfin-readiness.key` first, exactly like Python, and both are tested against the same cases.
- **An update that starts while setup waits now stops setup.** Setup checked for an unfinished monthly update only before it began; a step waiting for the shared lock while an update started (and failed) then went on and started apps (reproduced). Every step that changes or starts apps now checks again once it holds the lock. Docker re-attachment and backup repair stay allowed, since they can be what lets a stuck update finish.
- **Repair finds the live original of a dated version.** A damaged `photo.neko-20261020T010000Z.jpg` was looked up in Immich under that dated name, so a live `photo.jpg` that still matched its recorded checksum was never used (reproduced). Repair now looks for `photo.jpg` and uses it only when its SHA-256 matches the ledger.
- **A revoked or replaced Jellyfin key is renewed by setup.** Setup kept NekoServer's Jellyfin key as long as its file was not empty, so a key revoked in Jellyfin stayed in use and the check kept failing even after rerunning setup (reproduced). Setup, while signed in as administrator, now compares the kept key with Jellyfin's active NekoServer keys and saves the current one (or creates a new one) when it is gone. Scheduled checks never change credentials: they report "Jellyfin no longer accepts NekoServer's key" and say to rerun setup.

### Changed
- Version 0.26.0; the release folder is now `nekoserver-v0.26.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.25.1 — 2026-10-20

### Fixed (from an outside review)
- **A verified file that disappears is now reported.** The checks only re-hashed the files that were still there, so a backed-up file deleted from the HDD archive (and from Immich) went unnoticed: the archive audit and the USB copy both passed (reproduced). Every run now compares the ledger's full list with the files actually present. A file missing from the HDD whose USB copy still matches is reported as *missing* and can be put back from the USB drive with **Repair damaged backup copies**; a file gone (or damaged) everywhere is CRITICAL. Missing files show on the dashboard and raise an alert.
- **A restore stops when a verified file is missing.** A restore whose ledger listed two files with only one left on the drive succeeded with "1 files matched" (reproduced). Before anything is chosen or copied, every file the ledger lists for the folder (or Nextcloud user) being restored must be present; otherwise the restore stops and names the missing files.
- **No setup while a monthly update is unfinished, even of the same version.** The check before setup only blocked an unfinished update started by another version; with the same version, setup went on and started Nextcloud before the pending rollback (reproduced in a simulation). Any unfinished monthly update now stops setup first, with the command that finishes or rolls it back.
- **Repair clears a warning only for copies it actually checked.** With the USB drive unplugged, repair checked only the HDD copy but also cleared the USB copy's warning (reproduced). Now a warning is cleared only when every copy it is about was checked: the USB warnings stay until the drive is connected, and the repair says so.
- **A clock jump never deletes the newest app or host snapshot.** Nightly app and host snapshots are pruned by the time in their names; with a snapshot dated in the future, a new one could be deleted right away (reproduced with retention 1). The snapshot just made is never removed, as for database copies since v0.24.6.

### Fixed (setup flow, from a wider review)
- **Setup can reach "ready" without looping on playback.** The media check always listed a real playback test as pending, so onboarding stayed unfinished however often it ran, and your confirmation was ignored (reproduced). Only automatic configuration checks now decide whether onboarding is finished. A real playback test is asked for once, afterwards: a yes is recorded; skipping it still finishes setup, and the finishing screen says "Playback verification pending".
- **Phone setup runs after everything it links to.** The Tailscale and phone step ran before the dashboard was installed but already asked you to test the dashboard on the phone. It now runs after all apps, the dashboard and the TV step, and the dashboard is only offered (and routed) once it is installed.
- **The dashboard's settings fingerprint uses the final setup choices.** Choices made after the dashboard step (TV, heartbeat, ...) changed the fingerprint an acceptance report must match, so a fresh report read as "outdated" (reproduced). The dashboard's collector settings are written again from the final configuration at the end of every setup run.
- **Jellyfin's check no longer needs Seerr.** The Jellyfin library check used the key stored in Seerr's settings, so a Jellyfin-only setup could never pass it (reproduced). Setup now creates Jellyfin's own NekoServer API key the one time it signs in as administrator, kept privately on the SSD (`secrets/jellyfin-readiness.key`, root only); Seerr's link is used only when Seerr is installed.
- **A media import no longer skips folders it cannot read.** The importer's folder walk ignored listing errors, so an unreadable album on a local drive or SMB/NFS share was left out and the import still said "copied" (reproduced). Imports now use the same strict listing as restores: if any folder or file of a source cannot be read, nothing is copied from that source, it is marked incomplete, and the report names the unreadable paths (relative to the source; the source's own location stays private). Files still being written (`.part`, `.tmp`, `.crdownload`, `.!qB`, `.neko-part`) are explicitly excluded.
- **Container logs are size-limited.** Docker keeps container logs forever by default, one way for the SSD to fill up. NekoServer's Docker settings now cap each container's log at 3 files of 10 MB, without overriding a log driver or limits you chose yourself. Containers made before keep unlimited logs until they are recreated after Docker restarts (a reboot, then the monthly update or rerunning setup); the acceptance check lists any that still do.

### Changed
- Version 0.25.1; the release folder is now `nekoserver-v0.25.1`. Two long functions were split for readability (no change in behaviour).
- **Steadier checksum ledger lock.** Opening a ledger now retries a busy lock for up to 2 seconds before reporting another writer: a program started at the same moment by another thread can briefly hold a copy of the lock (seen as an occasional test failure in CI). Paths read from the command line or settings are validated before use (installer state files, Jellyfin's key file).
- **Slimmer release.** Removed `INSTALLATION-GUIDE.pdf`, a v0.21.0 walk-through nothing linked to any more (the README is the current guide). Out-of-date guidance in the docs was corrected: old install paths and commands (for example NOTIFICATIONS.md's test command pointed at v0.15.1), DASHBOARD.md's claims that the dashboard is local-only and can still run in Python, and "use the v0.10.0 installer" in RECOVERY.md. DASHBOARD, NEXTCLOUD, HEARTBEAT and SERVER-OS guides are now linked from the README. Upgrade code for older releases is kept on purpose: it moves existing servers forward safely and protects old backups.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.25.0 — 2026-10-19

### Added: checksum ledger and integrity rules for every backup
Every backup tree (the HDD photo archive, its USB copy and the USB Nextcloud copy) now has a checksum ledger, `.neko-ledger.json`. See STORAGE.md, "How backups are verified".
- **Immutable checksums.** A file's SHA-256 is recorded when a copy of it is first verified and is never replaced, even when the file on a drive changes, so damage can never become the expected content.
- **Unverified files are flagged.** Files from before the ledger stay unverified (counted in the job log) until two independent copies agree (the archive and the Immich original, or the HDD and USB copies); then the agreed checksum is recorded.
- **HDD vs USB vs ledger.** The HDD matches and the USB doesn't: the USB copy is repaired from the HDD (the damaged one is kept in `neko-damaged/`). The USB matches and the HDD doesn't: the USB copy is kept, the HDD file is reported and never copied onto the USB drive. Neither matches: both are kept untouched and the job fails as CRITICAL. No checksum and the copies differ: both are kept and reported. The weekly job finishes every copy first, then fails with the file names.
- **A damaged archive file is no longer taken for an edit.** Before, a rotted archive file looked like a changed photo and got a new dated version. Now it is compared with its checksum. If the Immich original still matches, the archive file is repaired from it; otherwise it is reported.
- **Three-checksum copies.** Every copy is hashed while it streams, fsync'd and read back from disk, and the source is hashed again. It is published only when all three match and the source's size, modification and status-change times did not change. A source rewritten mid-copy (even with its size and mtime put back) is refused.
- **Durable, ordered ledgers.** Each ledger is written to `.neko-ledger.json.<uuid>.tmp`, fsync'd, renamed and its folder fsync'd, always after the files it lists. A lock keeps it to one writer. The USB drive gets its copy of the archive ledger after the files.
- **Fast checks vs deep audits.** Size and dates are used only to skip unchanged files. Every 4 weeks every copy on every drive is read in full against its checksum, which catches bit rot that leaves size and dates unchanged.
- **Strict enumeration and restore.** Backup trees with a symlink, a special file or an unreadable entry are reported, never silently skipped. The recovery wizard checks every restored file against the ledger and stops at a damaged, missing or unreadable file. Ledger paths must be plain relative paths (no leading `/`, no `..`); a ledger with any other path is not trusted at all.
- **Damage shows on the dashboard and can be repaired.** Each job saves what its checks found in `reports/integrity.json`. The dashboard's Backups page shows each tree as intact or needing repair, with the number of unverified files, and raises an alert (also sent to your phone) when anything is damaged. The alert clears only when a later check finds nothing. The new recovery menu option **Repair damaged backup copies** fixes each reported file from whichever copy still matches its SHA-256 checksum (HDD, USB drive or the Immich/Nextcloud original). Files with no checksum whose copies differ are settled by the original, or by you, and then recorded. Damaged copies are moved aside, never deleted. Without this, a damaged HDD file would have failed the weekly job every week.
- **SHA-256 everywhere.** Every backup checksum (ledgers, copies, database copies, snapshots) is SHA-256; tests check the published FIPS 180-2 test values in both the backup program and the Python tools, and the ledger accepts nothing but 64-character SHA-256 values.

### Changed
- Version 0.25.0; the release folder is now `nekoserver-v0.25.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- The first runs after upgrading record checksums as files are copied. Existing backups are reported as unverified until the next 4-weekly full check confirms them against a second copy.

## v0.24.6 — 2026-10-19

### Fixed (from an outside review)
- **An upgrade waits for an unfinished monthly update.** A monthly update can only be finished (or rolled back) by the program version that started it, and the installer replaced that program without checking, so the new version refused the old update's notes (`Maintenance recovery identity mismatch`) and the apps stayed stopped until someone ran the old program by hand (reproduced). `install.sh` now checks first, before the system update or any NekoServer file changes, and the installer checks again before running anything. If an update from another version is unfinished, they stop and give the exact command: `sudo systemctl start nekoserver-maintenance-resume.service` (still the old version, since nothing was replaced), then run `sudo bash install.sh` again. The version check inside the update itself stays.
- **The dashboard and qBittorrent's peer port cannot be the same.** A custom `dashboard.port` equal to `media.torrent_port` passed both checks, and one of them could not start. Both checks now refuse it.

### Hardened (from a review of failure modes)
- **A database copy cut off by a full disk is never kept.** If the disk fills up while the Immich database copy is written, a program in between can still report success. The nightly database copy and the manual Immich snapshot now require `pg_dump`'s own end-of-dump line; a cut-off copy is removed and the job fails. (Monthly snapshots and file copies already failed on a write error and are checksummed.)
- **A system clock jump never deletes the newest database copy.** Copies are named by their time and the oldest are removed by name; after a clock jump the copy just made could sort as "oldest". The copy just made is now never removed, and a newest copy dated in the future marks the USB backup incomplete with a hint to check the clock.
- Containers that do not stop cleanly were already covered: monthly maintenance and nightly app snapshots allow 60 seconds, then refuse to continue if any container was killed (exit code 137), ran out of memory or is still running.

### Changed
- Version 0.24.6; the release folder is now `nekoserver-v0.24.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.5 — 2026-10-19

### Fixed (from an outside review)
- **The USB space check counts damaged photos that the weekly job will replace.** Since v0.24.4 the weekly mirror keeps a damaged USB copy aside and writes the archive file again in full, but the estimate still counted a same-size copy as already done (reproduced: 0 bytes estimated while the backup kept the damaged file and wrote a replacement; with large videos this could run the drive out of space unexpectedly). Photos copied from the HDD archive are now estimated with the mirror's own rules: a file named by a current-version note is compared by checksum every week, every file is compared when the 4-weekly full check is due, and a copy that does not match counts its full size.
- **Restoring the password files checks the snapshot's checksum first.** `backup_secrets.py --restore` (and the recovery menu's password option) checked only that the encrypted archive existed. Encrypted data does not reveal damage on its own: a damaged archive decrypted "successfully" into wrong passwords (reproduced). The checksum recorded in the snapshot is now compared before anything is decrypted or created; a damaged archive, or one without a recorded checksum, is refused and nothing is restored. (`monthly-recovery.py` already did this.)
- **Restarting apps after an interrupted backup waits for the drives and the shared lock.** `app-resume` and `host-resume` (run at boot after an interrupted nightly backup) restarted containers and Samba and removed their notes before checking the drives, and without the lock monthly maintenance holds (reproduced with the bundled runtime: with the lock held and the wrong drive mounted, they still started everything). They, the nightly app and host backups, the photo archive and database jobs and the music backup now all check the drives, take the shared lock, check the drives again and only then resume anything. Tests cover the check and the order.
- **A nightly app snapshot is published only if it matches what was listed.** File sizes were recorded when the files were listed; a file rewritten before it was copied (installer and settings files can have other writers while the apps are stopped) was copied in its new form under the old size, the job passed, and the restore check later rejected the snapshot (reproduced: 1 byte listed, 18 copied). Everything is now listed again after copying, and the staged copy is checked file by file (size and checksum) against its own list before it is published. If anything changed, nothing is published and the job fails clearly; it runs again the next night.
- **The dashboard cannot use an app's port.** `dashboard.port` accepted 8088, which Nextcloud uses, so one of them could not start. The dashboard and media import checks now share one list of the ports the apps use (Nextcloud and the private Tailscale addresses included).

### Changed
- Version 0.24.5; the release folder is now `nekoserver-v0.24.5`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.4 — 2026-10-19

### Fixed (from an outside review)
- **A damaged USB copy is replaced, never left as the "current" version.** When a photo's copy on the USB drive was damaged (reproduced: `AAAA` → `BBBB`, then the USB copy of `BBBB` changed to `ZZZZ`), the weekly job saved the good content under another name while the current-version note still pointed at the damaged file, and "newest only" recovery restored `ZZZZ`. The HDD archive never changes a file it has saved, so a USB copy that differs from it is damaged: it is now moved to `neko-damaged/<time>/` on the USB drive (kept, never deleted, and outside the folders recovery reads) and the verified archive file is copied back under its own name. Before the run counts as done, every file a current-version note names is compared by checksum with the archive and replaced if it differs, so this is caught every week, not only in the 4-weekly full check.
- **The USB space check includes the whole photo archive.** It compared the Immich library on the SSD with the USB drive, but the weekly job copies the HDD archive, which also keeps photos deleted from Immich and every saved version (reproduced: 0 bytes estimated while a kept photo was still to be copied). It now compares the HDD archive with the USB drive, and adds the photos on the SSD that the archive does not hold yet (they reach the USB drive the same week). The room-to-grow margin uses the archive's size when it is larger than the library.
- **Hardware video setup waits for backups and monthly updates.** It wrote Immich's graphics settings and restarted Immich without the lock that backups and monthly maintenance hold, so a rerun of the installer could restart Immich while maintenance needed it stopped for a consistent snapshot or a rollback (reproduced: with the lock held it still wrote the settings and started Immich). Recreating Immich and switching Jellyfin and Immich over now happen while holding that lock, after checking the drives; when a backup is running, an unattended run stops with a clear message and an interactive one waits for it.
- **Phone alerts no longer announce a recovery without proof.** The list of things that need evidence before "RECOVERED" is sent left out Nextcloud, monthly maintenance and the music backup, so a failed job that was merely running again, or Nextcloud with an unreadable state, produced a recovery message (reproduced with the bundled runtime). The list now comes from the same app and job lists the dashboard uses, and recovery is only announced after a passed job or a running, not unhealthy app. A test checks every app and job.
- **A manual Immich snapshot records the version that made its database.** `recover.py snapshot --apply` read the Compose settings, the Immich version, the exact images and the running services before taking the lock that monthly maintenance holds. If an update finished in between, the snapshot paired the upgraded database with the old version and images, and still passed its checks (reproduced: database from v2.0.0, snapshot recorded v1.0.0). All of that is now read, and the drives checked again, only after the lock is taken.

### Changed
- Version 0.24.4; the release folder is now `nekoserver-v0.24.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.3 — 2026-10-19

### Fixed (from an outside review)
- **A failed monthly update can roll back with encryption on.** Since v0.24.2 each app's `.env` and Nextcloud's `config.php` go only into the encrypted part of the snapshot, but the automatic rollback restored from `data.tar` alone, so a failed update could leave Nextcloud without its configuration and the apps without their passwords. While an update runs, a root-only copy of those files is now kept on the data SSD (where the live files already are, never in the portable snapshot). The rollback checks it against its checksum and puts the files back before the old apps start, unattended. The copy is removed when the update or its rollback is over. If it is missing or damaged, the rollback stops and points to `monthly-recovery.py --secrets`. A test runs a failed update with encryption on.
- **"Restore the newest version" restores the content you last had.** After an edit was undone (`AAAA → BBBB → AAAA`), the backup correctly made no new copy, but recovery picked the dated `BBBB` because it had the latest date. Version dates also came from the file's own timestamp, which can be set back. When a file has saved versions, the backup now keeps a small hidden note (`.name.ext.neko-latest`) naming the copy that matches the file as the backup last saw it, and recovery restores that one. New version dates are the time the backup saw the change. The weekly USB job mirrors the HDD archive's notes as they are. Backups made before this release have no notes; recovery then uses the newest date, as before.
- **The USB drive's Immich database copy is checked.** A missing database copy, one more than 8 days old, or one made by an older release without a note now marks the weekly USB backup **incomplete** on the dashboard (it passed before). Each database copy now gets a `.sql.json` note when it is made, with the Immich version that made it (not the version installed when the USB copy runs), the time and its checksum. The checksum is checked before copying to the USB drive (a damaged copy is never copied) and a damaged USB copy is replaced. `IMMICH-VERSION.txt` names the version of the newest copy.
- **No database password left behind by an Immich update.** The update's new Immich settings, `.env` included, were written into the snapshot folder on the HDD and stayed there in plain text. They are now prepared in the private SSD work folder and removed after the update; copies left in older snapshots are deleted the next time maintenance runs.
- **A saved USB drive must be a separate disk too.** The drive picker refused another partition of the media HDD or the system disk, but a drive already named in the settings skipped the picker. Setup now refuses it when it is plugged in, and the weekly job checks before every run (through partitions, encryption and LVM) that the USB drive shares no disk with the system, the SSD or the HDD.
- **The USB space check counts same-size edits.** It treated a file as already copied whenever the sizes matched. It now decides like the backup itself: a copy of the same size written after the file last changed counts; otherwise the contents are compared (including saved versions), so an edit of the same size needs room again.
- **Turning Nextcloud off waits for backups and monthly updates.** Stopping a disabled Nextcloud skipped the lock that monthly maintenance and the backups hold, so it could stop Nextcloud during an update, fail its health check and trigger a rollback that started Nextcloud again. It now waits for that lock, and if an unfinished update is waiting to be resumed (which would start Nextcloud again) it says so instead of stopping it.

### Changed
- Version 0.24.3; the release folder is now `nekoserver-v0.24.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.2 — 2026-10-19

### Fixed (from an outside review)
- **`install.sh` exited silently before setup.** It called `df -P --output=avail`, which GNU df rejects; with `set -euo pipefail` and the error hidden, the installer stopped without a word. It now uses `df --output=avail`, and if the space cannot be measured it says so plainly and continues.
- **Weekly backups could miss a replaced file.** A copy was trusted when its size matched and it was written after the source's *modified* time, which tools that preserve timestamps can set back (reproduced: `AAAA` backed up, replaced by `BBBB` with the old timestamp, nothing copied). The check now uses the source's *status-change* time (ctime), which any rewrite or replacement moves forward and which cannot be set back. In addition, every 4 weeks the archive and USB jobs compare every existing copy by checksum.
- **Turning on the dashboard's Tailscale address broke the dashboard service, local access included.** The service passed `--public-host`, which the runtime's option table did not accept. The option is registered.
- **Incomplete USB backups were reported as successful.** When Nextcloud files were skipped (in use) or refused (names exFAT cannot store), the weekly job still recorded success. It now records **partial**, with counts and up to 50 file names; the dashboard shows *Incomplete* with the files, phone alerts warn, and it does not count as a full success.

- **Password files are no longer in plain text in monthly snapshots.** Encrypting `APP-CREDENTIALS.txt` left other copies readable: each app's Compose `.env` (database and Nextcloud admin passwords) and Nextcloud's `config.php` in `data.tar`, and the resolved app definitions (`*-old.json`, `*-new.json`, `*-rehearsal.json`, `result.json`). The `.env` files and `config.php` now go only into the encrypted `secrets.tar.cms` (left out of `data.tar`), and the saved definitions have their password values blanked once the update or its rollback is over (they are needed until then). Snapshots made before this release stay as they were until retention removes them. Without a backup key the files stay in `data.tar` so the snapshot remains restorable.
- **The USB drive can rebuild Immich on its own.** It held the photos but not the database, so albums, faces and users needed the SSD or HDD. The weekly USB job now also copies the newest Immich database copy (the one made that night; the newest 4 are kept) and `IMMICH-VERSION.txt` with the Immich version that made them (never the database password). RECOVERY.md has a "Rebuild Immich from the USB drive" section.
- **The recovery wizard described file versions backwards.** It called the dated `.neko-<date>` copies "older versions"; they are the later edits. It now offers to restore only the newest version of each file under its own name (the default), shows the date of each file that had several versions, and explains the naming correctly when every version is copied.
- **Every restore checks free space first.** Nightly snapshot restores, monthly snapshot unpacking and loading saved app versions now use the same check as photo and Nextcloud restores (refuse if less than 5 GB or 5% would be left free).
- **Nightly app and host backups use the configured timezone**, like every other schedule (they ran in the system's own timezone). A test checks every generated timer.

- **The USB backup must be a separate physical disk.** The picker excluded the SSD's and HDD's own filesystems but not other partitions on the same disks, so a second partition of the media HDD (or of the system disk) could be chosen, and one disk failure would take both copies. Only partitions on a disk that holds neither the system, the SSD nor the media HDD are offered now.
- **Phone alerts cover Nextcloud and monthly maintenance.** Notifications used a list of known alert names that left out `app:nextcloud`, `backup:maintenance`, `backup:music` and the new major-update notices, so those were shown on the dashboard but never sent. A test now checks every monitored app and job has a notification.
- **The Tailscale acceptance check accepts the installer's own routes.** It compared against the apps only, without the dashboard route the installer adds, and failed. Installation and the check now use one route calculation (`tailnet_routes`).
- **Failures say why and where.** The runtime printed one generic message and dropped every command's error output. It now keeps a short, sanitized reason (anything that looks like a password or key removed) and the step it was on ("copying photos and videos to the USB drive"), in the job report, on the dashboard ("Failed while …: …") and in its error output.

- **Old full snapshots on the USB drive are no longer removed automatically.** Releases before v0.21.1 wrote full monthly snapshots (photos and Nextcloud files included) to the USB drive, and the weekly job removed them once current backups existed, without checking that their files survive elsewhere. A file deleted from the server long ago could exist only there. The weekly job now keeps and lists them; the recovery menu (`sudo bash install.sh --recover`, "Old full snapshots on the USB drive") shows their dates and sizes, offers to copy them to the HDD first, and removes them only when you type DELETE. The automatic cleanup code is gone.
- **qBittorrent's music category must point to the music download folder.** Setup accepted an existing `music` category saving to `/data/downloads` (on the HDD, where Lidarr does not look) and reported success. Music must save to `/data/downloads/music` (on the SSD); a wrong but empty music category is corrected, one with torrents stops setup with instructions. TV and movies may still use the shared `/data/downloads`.
- **Turning Nextcloud off stops an installed Nextcloud.** `"enabled": false` only skipped installation, so an existing Nextcloud, its database, Redis and cron kept running and restarting. Its containers are now stopped (files and database kept; they stay stopped after a reboot), and turning it back on starts them again.

- **`--config` reaches the wizard and recovery.** `sudo bash install.sh --recover --config FILE` (and `--wizard --config FILE`) accepted the option but used the settings next to the installer, so recovery could look for backups on the wrong drives. Both now use the chosen file; without `--config` they use `my-config.json` as before.
- **Nextcloud acceptance results no longer turn the dashboard's acceptance panel to "unknown".** Python writes a manual check named `cloud_client` that the Rust dashboard did not know, so it treated every cloud/full report as invalid. It is known now, and a test checks that every manual check Python can write is known to the dashboard.
- **A pending app login can be corrected.** If creating an app's account did not finish and you completed the app's setup yourself with another login, the account tool kept retrying the saved login and never offered to change it. It now offers to enter the login you used; that switches the app to "existing account", so it is never created or reset.

- **The USB space check no longer counts old backup versions as free room.** It added everything already on the USB drive to its free space, as if those files could be reused. The backup only ever adds: earlier versions stay, and a changed file needs its full size again. With 100 GB of new files, 30 GB free and 150 GB of earlier versions, it gave no warning. It now adds up what the next weekly copy still has to write (files not on the drive yet, files whose copy differs, and this week's Immich database copy) and compares that, plus room to grow, with the free space.
- **An interrupted first setup of Immich or Nextcloud now finishes on the next run.** Each writes several settings files; stopping between them left a half-written set that every rerun refused ("Incomplete … deployment"). `compose.yaml` is now always written last, so it marks a finished step. A rerun that finds only the earlier files completes the step: Immich keeps the `.env` it wrote (its version and database password); Nextcloud writes its files again, since nothing has started yet. Half-written sets left by older releases are completed the same way as long as the app has no database yet; once it has one, the files are still kept for inspection.

- **`--config` paths are checked before they are read.** The installer, the setup wizard and recovery now accept only a `.json` file, with no `..` and no symlinks along the path (flagged by SonarCloud as a possible path traversal).

### Added: tests of how the pieces connect
- `tests/test_integration.py` runs the real `install.sh` preflight with the real `df` (only pacman, network tools and system identity stood in for), and checks every runtime command the installer writes into a systemd unit (22 commands) against the runtime's own option table, and against the bundled binary whenever it matches its sources. Both reported blockers fail these tests.

### Fixed
- **Dashboard app buttons work on the phone.** Opened over Tailscale, the dashboard's app buttons still pointed to `http://127.0.0.1:…`, which on a phone means the phone itself. They now open each everyday app's private Tailscale address (Jellyfin, Seerr, Immich, Navidrome, Nextcloud); the admin apps, which are only reachable on the server, say "Opens only on the server itself" instead of failing.

### Changed
- Version 0.24.2; the release folder is now `nekoserver-v0.24.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (version change only).

## v0.24.1 — 2026-10-19

### Added
- **Dashboard on your phone.** The read-only dashboard (drives, apps, backups, alerts) now also has a private Tailscale address, `https://<server>.ts.net:8448/`, on your own devices only (Tailscale Serve, never Funnel). It is on the phone setup page and the finishing screen. The dashboard server accepts only its loopback names and this server's own Tailscale name, and has no way to change anything. `"remote": false` under `"dashboard"` keeps it on the server only.
- **Hardware video in Jellyfin and Immich.** A new step, *Video using the graphics chip*, finds the graphics chip, installs its video drivers, checks from inside Jellyfin that the chip really decodes video, then turns on hardware transcoding: Quick Sync on Intel, VA-API on AMD, in both Jellyfin and Immich (Immich's server also gets the graphics device, kept across monthly updates in a `hardware.yaml` next to its Compose file). A setting you already chose in either app is kept; without a usable chip (or with NVIDIA) both apps keep working in software. See HARDWARE-VIDEO.md.
- Shared `gpu.py` for graphics detection (TV and hardware video).

### Fixed
- **Music no longer stays "unfinished" without Firefox.** Its optional desktop shortcut needed Firefox, which only the TV setup installed; with the TV extra turned down, music (and the finishing screen) stayed "needs setup" forever. A missing browser now just skips the shortcut; music works in any browser and on phones.

### Changed: less duplicated code (same behaviour)
- `app-recovery.py` and `host-recovery.py` were two near-identical 140-line copies. Both now share `snapshot_restore.py`; the commands and their output are unchanged, and the restore logic is covered by tests for the first time.
- One yes/no question (`guided_setup.yes_no`) replaces five copies (setup wizard, space check, recovery wizard, optional extras, guided steps).
- One Arch/Nyarch check (`neko_common.arch_host`) replaces three (Tailscale, TV, recovery); it also tolerates a missing or unusual `/etc/os-release`.
- One drive-list command for all drive pickers.
- Rust: one monthly-snapshot listing for maintenance, the weekly USB job and the dashboard, and one Immich database-copy name check.
- Version 0.24.1; the release folder is now `nekoserver-v0.24.1`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing servers: add `"hardware_video"` after `"accounts"` in the `full` profile of `my-config.json` (or start from the new template), then run `sudo bash install.sh`. The dashboard's Tailscale address is added on that run too.

## v0.23.3 — 2026-10-19

### Fixed
- **Old full snapshots on the USB drive were removed once an HDD snapshot merely existed.** The weekly job now also checks that snapshot's archives against their checksums before removing them; a missing or altered archive keeps the USB snapshots.
- **The Seerr permission check still assumed user 1000.** Since v0.23.2 Seerr's folder belongs to your user, but the readiness check still checked it as 1000:1000 and reported a false "needs setup" for other users. It now checks the configured user, like every other app.
- **The system disk is checked before the system update.** The update and packages go on the system disk, before the drives are picked; with less than 5 GB free there, `install.sh` now stops before changing anything and says how to free space. (The SSD and HDD are still checked after you pick them, before any app is installed.)
- **Monthly retention could delete the last good snapshot.** Snapshots beyond the newest 3 were deleted without checking that the 3 kept are intact. If any kept one fails its checksum check, nothing older is deleted that month, and the log says why.
- **Running the installer during a night-time backup no longer fails with "Another recovery operation is active".** It waits for the backup (up to 30 minutes, and says so), then explains what to do if it is still running.
- The home-network step and the README now say to reserve the server's address in the router, so the home addresses keep working after a restart.
- Finishing screen: a list join no longer uses `sum()` (SonarCloud).

### Changed
- Version 0.23.3; the release folder is now `nekoserver-v0.23.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

## v0.23.2 — 2026-10-19

### Fixed (from an outside review)
- **A monthly backup's SSD copy could be deleted while the HDD copy was missing or damaged.** Old SSD copies of monthly snapshots were removed when their manifest matched the HDD's, without checking the HDD archives themselves. Now the SSD copy is removed only when the HDD run is complete (restore-tested) and every HDD archive matches its checksum. Otherwise it stays, as possibly the only good copy.
- **A failed drive space check did not stop the installation.** Docker, the apps, Immich, Nextcloud, backups and maintenance still ran after the check failed (or you answered no). Every step that writes to the drives now waits for it; Tailscale, which does not, still runs.
- **Seerr could not use its settings folder with a user other than 1000.** It runs as your user (`media.uid`/`gid`), but its folder was created for 1000:1000 with mode 0750. The folder is now created for your user, and an existing one owned by 1000:1000 is handed to your user (that folder only; links are not followed).
- **A correct nightly app-backup setup failed its readiness check.** Setup writes the retention setting into the backup's private settings, but the check expected them without it and reported "needs setup". Both now use one shared definition.
- **The finishing screen could say "ready" with missing or unreadable state.** It now checks every step the installation is meant to run; a step with no record counts as unfinished.
- **The USB space check used the drive's size, not its free space**, so a large but nearly full drive passed. It now measures free space, plus what an earlier weekly copy already holds there (opening the drive read-only if needed).

### Fixed
- **No stale "newer Nextcloud" warning after an approved upgrade.** In the month an approved Nextcloud major was installed, maintenance first recorded "Nextcloud 32 is available (you have 31)" and the dashboard kept showing it for a month. The check is now skipped in the month a major is being installed.
- **Approving an Immich major survives a fix release.** The approval had to be the exact release (`"v3.0.1"`), so it stopped matching if Immich published v3.0.2 before the update night. You can now approve the major itself (`"immich_major_update": "v3"`); an exact release still works.
- **Acceptance checks leave out extras you turned down** (TV, phone alerts, outage alerts), so they no longer stay "not done" on the dashboard.
- **Answering "no" at the wizard's first question no longer says "Nothing was changed"**: by then the system update and packages are already installed, and it now says so.
- Docs: a big Nextcloud can take longer than the 5-minute health wait to upgrade; MONTHLY-MAINTENANCE.md explains raising `health_wait_seconds`.

### Changed
- Version 0.23.2; the release folder is now `nekoserver-v0.23.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

## v0.23.1 — 2026-10-19

### Changed: install order
- **The system is updated first.** `sudo bash install.sh` now runs a full `pacman -Syu` (with the Arch keyring first, as Arch recommends) before installing anything, so the apps and their dependencies install at their current versions. If the update installed a new Linux kernel, it stops and asks you to restart, then run the same command again: Docker networking and Tailscale need the running kernel's modules. The recovery wizard (`--recover`) skips the update.
- **Every system package the apps need is installed up front**, before the wizard starts: `docker`, `docker-compose`, `tailscale`, `caddy`, `samba`, `hdparm`, `smartmontools`, `qrencode`, `xdg-utils`. A missing package now stops the setup at the start instead of halfway. (Installing Docker does not start it; the Docker step still sets it up on the SSD.)
- **Tailscale comes right after the system check**, before the drives and apps. The Tailscale addresses for the apps are set up before the home-network addresses.
- **The USB backup drive is chosen with the SSD and HDD**, so the early space check covers it. Before, it was only chosen at the weekly-backup step near the end, and the USB check never ran on a first install.

### Fixed
- **Weekly photo and Nextcloud copies no longer re-read the whole library.** They only ever copied missing files, but checked every existing copy by reading the photo and the copy in full (SSD and HDD, then HDD and USB) each week. A copy with the same size, written after the photo last changed, is now skipped without reading it; new and changed files are still copied and checksum-verified.
- **New major versions of Immich and Nextcloud are no longer silent.** Monthly maintenance recorded a new Immich major only inside the snapshot folder, and never checked Nextcloud at all, which stays on its installed major. Both now show on the dashboard, and you can approve one in `my-config.json` (`immich_major_update`, `nextcloud_major_update`); the next monthly update installs it with the usual snapshot, restore test and automatic rollback. Nextcloud is moved one major at a time.
- **Drives the desktop already opened (`/run/media/...`) can be chosen as the SSD or HDD.** Before, a freshly plugged-in drive was missing from the list.
- **Drive prompts no longer mention "1TB" and "12TB"**, and you confirm a drive by typing its short name (such as `sdb1`) instead of its long UUID.
- **Missed weekly backups run at the next start-up** (the PC was off on Sunday night) instead of being skipped for that week.
- Samba explains the password it asks for.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing settings keep their old step order (it still works); the new order applies to new settings files.

## v0.23.0 — 2026-10-19

### Fixed
- **Running the installer again no longer repeats finished steps.** The Tailscale HTTPS step is skipped when MagicDNS and HTTPS are already on, the phone walkthrough is skipped when it was already confirmed for the same addresses (the QR page is still refreshed), and TV hardware tests already confirmed are not asked again.
- **Optional extras can be turned down.** TV setup, phone alerts (ntfy) and the outage monitor were part of the main installation with no way to say no, so servers without them stayed "almost ready" for good and were asked again on every run. Each is now asked once; `no` is saved as `"wanted": false` and counts as done. Change it to `true` to set one up later.
- **The drive space check no longer blocks a re-run.** Its minimums are for a new install; once Docker is on the SSD, low space is reported but nothing is stopped.
- **The phone steps and the QR page show the real password-list path** on your data SSD instead of always `/srv/nekoserver/...`.
- **The recovery wizard never shares the USB drive with the weekly backup** (it takes the same lock, and says to try later if the backup is running), so the backup cannot unmount the drive in the middle of a restore. It also checks free space first and refuses a restore that would leave less than 5 GB (or 5%) free.
- **TV setup installs the right Vulkan driver** for Intel or AMD graphics (it always installed the Intel one). With NVIDIA only, it points to the Arch wiki instead of guessing a driver.

### Changed
- Version 0.23.0; the release folder is now `nekoserver-v0.23.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (the version changed; the Rust sources did not).
- The first run after upgrading asks the three optional questions once (unless the extra is already set up).

## v0.22.0 — 2026-10-19

### Added
- **Setup wizard: `sudo bash install.sh`.** One command, no options. It explains what will happen, creates `my-config.json` by itself (your timezone and user filled in), runs the whole installation with every guided pause, and continues where it stopped when run again.
- **Early drive space check**, right after the drives are chosen and before anything is installed. It stops with a plain explanation if the SSD or HDD is too small (under 20 GB / 50 GB free) and warns, with a yes/no to continue, below the recommended sizes or when the USB drive is too small for the photos and Nextcloud files.
- **Tailscale in the main installation.** The wizard pauses to explain Tailscale, opens the sign-in page, and turns on secure addresses. Jellyfin, Seerr, Immich, Navidrome and Nextcloud get private addresses that work anywhere on your own devices: Tailscale Serve, never Funnel, no router changes. The admin apps stay on the server.
- **Phone setup, app by app, with a QR-code page.** For each app: which phone app to install (iPhone and Android), what to type, and which username to use. Passwords are never shown; the steps say where they are. The page is at `/usr/local/share/nekoserver-pages/phone-setup.html`.
- **Finishing screen**: what's done, what still needs you (in plain words), your home and Tailscale addresses, where the passwords and phone page are, and what to do next.
- **Recovery wizard: `sudo bash install.sh --recover`.** A menu for photos, Nextcloud files, the password list, app settings, Steam/Samba settings or a monthly snapshot. It lists backups by date, restores into a new folder only, and explains how to put things back.
- `"timezone": "auto"` (the new default) uses the PC's own timezone.

### Fixed
- **Old monthly snapshots on the USB drive are removed only when it is safe.** Before, they were removed after any weekly run, even one where Nextcloud files were skipped (in use, or names exFAT cannot store). Now the weekly job keeps them unless this week's copies were complete and a restore-tested monthly snapshot (plus, if they covered Immich, an Immich database copy) is on the HDD. It logs why it kept them.

### Changed
- Version 0.22.0; the release folder is now `nekoserver-v0.22.0`.
- The Tailscale and phone steps are rewritten in plain language.

### Upgrade notes
- **Existing servers:** add `"capacity"` after `"storage"`, and `"tailscale"`, `"gateway_tailnet"` after `"gateway_lan"`, in the `full` profile of your config. Or start from the new template; the wizard creates one if `my-config.json` does not exist.
- The bundled runtime must be rebuilt for this release (Rust sources and version changed).
- A monthly update interrupted *during* the upgrade from 0.21.1 cannot be resumed by 0.22.0 (it refuses a journal from another version); finish or resolve it with 0.21.1 first.

## v0.21.1 — 2026-10-19

### Changed: what is backed up where
- **USB drive: only add-only copies of Immich photos/videos and Nextcloud user files.** The weekly USB job now also copies each Nextcloud user's own files (not previews, trash or caches). Monthly maintenance no longer writes to the USB drive or needs it connected; the full monthly snapshots older releases put there are removed by the weekly job once its own copies succeed.
- **HDD: software, OS and photos.** Nightly app-state and host-state snapshots, the weekly add-only photo copy and Immich database dump, and the monthly pre-update snapshot of app settings, databases and container images. The monthly snapshot no longer contains the Immich photo library or Nextcloud files; its restore test checks the Immich and Nextcloud databases by SQL instead of starting those two apps.
- **Add-only copies never fail on a changed file.** A file that changed since it was copied (an edited document, an updated `.xmp` photo sidecar) used to fail the whole job ("Collision or changed original"). It is now kept beside the old copy as `name.neko-<date>.ext`; nothing is overwritten or deleted.

### Fixed
- **Lidarr imports no longer copy music.** Lidarr now mounts the SSD library once at `/data`, so `/data/downloads/music` and `/data/music` share one mount. Previously they were separate bind mounts, so imports failed to hardlink/move (EXDEV) and fell back to copying, doubling SSD use while seeding. Container paths and app settings are unchanged; mount checks in `acceptance.py`, `media-doctor.py`, `media-health.py` and the Rust runtime were updated to match.
- **Seerr honours `media.uid` / `media.gid`** instead of a hardcoded `1000:1000`.
- **Nextcloud works on both gateways.** Applying `remote_access` after the LAN gateway replaced Nextcloud's `overwritehost` with the Tailscale name, breaking Nextcloud on the home network for devices without Tailscale. Gateways now only add their address to `trusted_domains`; a gateway-set `overwritehost` from v0.21.0 is removed. The Docker hop is added to `trusted_proxies`, so Nextcloud sees real client addresses for login throttling and logs.
- **Monthly maintenance no longer fills the SSD and blocks itself.** Each month it copied the full snapshot (all photos, Nextcloud files, images) onto the SSD as well, and never deleted any, so the free-space check soon failed and container updates stopped. Snapshots now go to the HDD only, old SSD copies are reclaimed once the same run is on the HDD, and the newest `maintenance.keep_snapshots` complete snapshots (default 3) are kept.
- **Nightly app backups failed every night with the default apps.** qBittorrent's SSD music-downloads mount (and now Lidarr's SSD library) counted as "unmanaged writable state". The SSD library is media, not app state, and is now allowed.
- **Nightly app and host backups no longer abort on one odd file.** Symlinks, sockets, FIFOs, devices and nested filesystems are skipped and listed in the manifest; hard-linked files are copied normally.
- **Monthly updates no longer require a configured USB drive.** Maintenance still listed the USB photo backup as a required module, so without a USB drive set up the installer blocked it and monthly updates never ran.
- **Overlapping night jobs no longer fail each other.** The weekly photo backup, monthly maintenance and nightly app/host backups share one lock, which failed immediately when busy: the Sunday app backup failed whenever the photo job ran long, the nightly backups failed on maintenance nights, and maintenance was skipped for the month when the 1st was a Sunday. Scheduled jobs now wait up to 3 hours (app/host backup service limits raised from 2 h to 6 h to allow for it).
- **Weekly Immich database dumps no longer pile up forever.** Each week added another full `.sql` dump to the SSD and HDD (often hundreds of MB to GBs). The newest `photos.database_keep` dumps (default 8, about two months) are kept on each drive; `0` keeps all.
- **Jellyfin starts on machines without a GPU.** `/dev/dri` is only passed through when it exists.
- **Weekly Nextcloud copy to an exFAT USB drive** skips (and lists) files whose names exFAT cannot store, such as `Meeting 10:30.docx`, instead of failing the job.
- **Fresh Nextcloud installs avoid a brand-new major** (x.0.0/x.0.1) and install the previous major, since the Docker image can lag the release.
- **Password restore** accepts only absolute snapshot/destination paths without `..` or symlinks, and checks the snapshot is a real monthly snapshot (SonarCloud findings).
- CI: actions pinned by commit, Rust pinned to 1.99.1, PyYAML installed by hash from wheels only.
- **Timezone is configurable.** A new top-level `timezone` config key drives container `TZ`, Immich's `.env` and every systemd timer. It falls back to the old `media.timezone` / `maintenance.timezone` keys, then the host's `/etc/localtime`, then UTC. The default stays `Asia/Jerusalem`.

### Removed
- **Python fallback backend.** Every scheduled job and background service runs the bundled Rust program. Configs that say `runtime.backend: python`, or have no `runtime` setting, are treated as Rust; rerun the installer once to rewrite their units. Deleted: `photo-preserve.py`, `photo-preservation-job.py`, `photo-backup.py`, `photo-weekly.py`, `immich-db-backup.py`, `job_status.py`, `modules/_runtime.py`, `dashboard_server.py`, `gateway_guard.py`. `dashboard_collect.py`, `storage-guard.py` and `media-health.py` stay: they are libraries or manual tools.
- **Unused tools:** `media-doctor.py`, `migrate-docker.py` (referenced by nothing).
- **Historical docs and old test output:** `AUDIT.md`, `INSTALLATION-REVIEW.md`, `PLAN-REVIEW.md`, `RELEASE-CONTENTS.md`, `audit-evidence/`, and old version notes at the top of several docs. Tests now run in CI instead.
- Unused imports and one unused variable.

### Added
- **App password list encrypted in monthly HDD snapshots** (it is never written to the USB drive), opened with your sudo password (`backup_secrets.py`). The installer asks for the sudo password once and checks it; nightly runs need no password. Restore with `monthly-recovery.py --secrets` or `backup_secrets.py --restore`.
- **Retention** for monthly (`maintenance.keep_snapshots`, default 3) and nightly (`app_backup.keep_snapshots`, `host_backup.keep_snapshots`, default 14) snapshots. `0` keeps everything. Incomplete runs and anything the worker did not create are never deleted.
- **Optional qBittorrent peer port** (`media.torrent_port`, default closed). It is the only port allowed beyond localhost, and the installer, monthly worker and acceptance checks refuse any other.
- Docs: device certificate checks for phones/TVs on the LAN gateway.
- Upgrade path: unedited v0.21.0 Lidarr and Seerr manifests are recognised and migrated (`previous_templates()` in `modules/media.py`), alongside the existing Navidrome migration.
- Regression tests: `tests/test_review_fixes.py`, `tests/test_gateway_cloud_origin.py`, `tests/test_backup_secrets.py`, and Rust tests for retention, encryption, skipped files, the peer-port rule, versioned add-only copies, the Nextcloud copy and USB cleanup.
- GitHub Actions CI: Python compile/lint/tests, `docker compose config`, `cargo fmt`/`clippy`/`test`, a check that the bundled runtime matches its sources, and a static musl runtime build that can commit the rebuilt binary.
- `.gitignore`.

### Upgrade notes
- The bundled `runtime/bin/nekoserver-runtime` must be rebuilt for this release (Rust sources and version changed). Run the CI workflow with **commit_runtime**, or `python3 runtime/build.py` as a normal user.
- Rerunning the installer upgrades unedited v0.21.0 Lidarr and Seerr manifests in place, keeps the old one as `compose.before-v0.21.1.yaml`, and recreates those containers. A manifest you edited by hand is left alone and reported, as before.
- Rerun the installer interactively once so it can ask for your sudo password and create the backup key. Until then, monthly backups leave the password files out (they are never stored in plain text).
- **Old backups are deleted.** On the first successful runs after upgrading: HDD monthly snapshots beyond the newest 3, nightly snapshots beyond the newest 14, Immich database dumps beyond the newest 8, v0.21.0's SSD copies of monthly snapshots, and (after the weekly USB copy succeeds) every old full monthly snapshot on the USB drive. v0.21.0 monthly snapshots contain the password list in plain text; the HDD ones go once they fall outside the retention count. Set the `keep_snapshots` values before upgrading if you want to keep more.
- Rerun the `photo_backup` module (or the full profile) so the weekly USB job picks up the Nextcloud copy. The first run copies every Nextcloud file and can take a long time.
- `maintenance.require_external` is no longer used.
- Reapply the gateways (`full`, then `remote_access`) to apply the Nextcloud fix.
- The installation guide PDF still shows v0.21.0 paths; substitute `v0.21.1`.
