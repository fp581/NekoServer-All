# Getting files back from a backup card, without NekoServer

A removable-media backup set is a numbered set of cards (SD cards, USB sticks or external
drives) that NekoServer fills with copies of your photos, videos and documents. Every card
can be read on its own, on any Linux computer, with nothing installed and no network. This
page says how. You do not need the server, its catalogue or the other cards.

What is on a card, and what is not:

- **The original files**, exactly as they were: Immich's photos and videos, and Nextcloud's
  documents. Each one can be opened directly.
- **Not Immich itself.** Albums, people, faces, sharing and accounts are in Immich's
  database, which the cards do not hold. This is original-media recovery, not full Immich
  recovery.
- **Not encrypted.** Anyone holding a card can read it. Keep the cards somewhere safe.
- **Only its own files.** Each card holds part of the set; the other cards hold the rest.
  No tool can get a file back from a card that is not there.

## What you need

- A computer with Linux (any distribution). An **ext4** card needs Linux; an **exFAT**
  card can also be read on Windows and macOS, but the tool below is tested on Linux.
- Python 3, which almost every Linux has: `python3 --version` shows it. Nothing else.
- A card reader, and the card.

## 1. Connect the card, read only

On a desktop, insert the card: it opens as a folder, for example `/media/you/CARD`.
In a terminal you can mount it yourself, read only, so nothing on it can change:

```bash
lsblk -f                                  # find the card, e.g. sdb1
sudo mkdir -p /mnt/card
sudo mount -o ro /dev/sdb1 /mnt/card      # the name lsblk showed
cd /mnt/card
```

The card's top folder holds `NekoBackup/`, with `README.txt`, `identity.json` and the
tool, `NekoBackup/tools/neko-restore.py`.

**Where the card must be:** the tool reads the card it is on, or, with `--card`, a card
mounted where Linux mounts one: under `/media`, `/run/media` or `/mnt`. A folder anywhere
else (or a link that leads elsewhere) is refused, so that a mistyped path never makes it
read some other folder. A desktop mounts a card under `/media` or `/run/media` by itself.

## 2. Look at the card

Run these from the card's top folder (or add `--card /mnt/card` from anywhere):

```bash
python3 NekoBackup/tools/neko-restore.py info            # which card this is, how many files
python3 NekoBackup/tools/neko-restore.py list            # every file: its ID, size, date, path
python3 NekoBackup/tools/neko-restore.py list --search 2021/beach
python3 NekoBackup/tools/neko-restore.py verify          # every file checked against its checksum
```

`verify` reads every file and compares it with the SHA-256 checksum recorded when it was
copied. It says `ok`, `damaged` or `missing` for each one and changes nothing. Files whose
names hold unusual characters are shown with those characters escaped (a line break as
`\n`), so nothing is hidden.

If a command says it cannot read a file, run it with `sudo` in front.

## 3. Restore

Make a new, empty folder for the restored files, then:

```bash
mkdir ~/restored
python3 NekoBackup/tools/neko-restore.py restore ~/restored
python3 NekoBackup/tools/neko-restore.py restore ~/restored --search 2021/beach
```

Each file goes back into its own folders below `~/restored`, with its modification time.
Before a file gets its name it is checked twice: the card's bytes while they are copied,
and the restored file read back afterwards. A file that does not match is **not** restored
(and no half-written copy is left); the tool says `failed` for it.

**Nothing is ever overwritten.** When a file's place is taken, by a file already there or
by a name that differs only in upper and lower case (two such files are one file on many
systems), the file is `skipped`. You can choose instead:

- `--rename`: put each such file under `_renamed/<its ID>.<ext>`; the tool says which
  original path went where;
- `--overwrite PATH` (the file's original path, as `list` shows it): replace that one file,
  and only that one. Name each file you want replaced.

Add `--report report.json` to keep the list of every file, what happened to it and where it
went. The restore does not bring back owners, permissions or extended attributes.

## 4. Several cards

A file is on one card of the set. To find which, ask the card you have:

```bash
python3 NekoBackup/tools/neko-restore.py where beach
```

`where` reads the card's inventory: what the whole set held when this card was last
written. A card written later may know about more cards and files, so the newest card you
have gives the best answer.

To restore from several cards, use one progress file for all of them. It remembers what
is restored, so nothing is copied twice, and a restore that was stopped goes on where it
was:

```bash
python3 /mnt/card/NekoBackup/tools/neko-restore.py --card /mnt/card restore ~/restored --progress ~/restore-progress.json
# eject this card, insert the next, and run the same line again (with that card's folder)
```

## Without the tool

Every file under `NekoBackup/objects/` is an ordinary copy of one original file and opens
in any program. Its file name is a random ID; the file of the same ID under
`NekoBackup/records/` (ending in `.json`, readable in any text editor) says what it was:
`original_relative_path` is its folder and name, `byte_size` its size, `checksum` its
SHA-256. `sha256sum` on the file shows its own checksum, to compare.

## If the tool is not on the card

The same tool is in every NekoServer release: `nekoserver/neko_restore.py`. Copy it
anywhere and run it with `--card`:

```bash
python3 neko_restore.py --card /mnt/card info
```

## Good to know

- Exit status: 0 everything was fine; 1 something was damaged, missing, skipped or not
  restored (the lines above say what); 2 the card or the command could not be used.
- The checksums find accidental damage. They cannot prove that nobody changed a card on
  purpose.
- Old cards of an earlier NekoServer stay readable with this tool: the format of the cards
  ("archive format 1") does not change with NekoServer's updates. A card of another format
  is named as such and not read.
- Keep your other backups too, and try a restore now and then. A drawer of old cards is not
  a guarantee: cards, readers and storage conditions can fail.
