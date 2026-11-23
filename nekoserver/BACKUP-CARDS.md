# Backup cards

Backup cards are SD cards and USB sticks that hold a copy of your photos, videos and
documents, kept somewhere else than the server (at work, with family, in a drawer away
from home). A fire, a flood or a burglary that takes the server and its drives leaves the
cards. They are an extra copy, besides the backups on the HDD and the USB drive.

What goes on the cards: **Immich's original photos and videos** and **each person's
Nextcloud files**. Nothing else: not Immich's albums, faces or accounts (they are in its
database), not films, shows or music, not app settings.

**The copies on the cards are not encrypted.** Anyone holding a card can read it. Keep
the cards somewhere safe.

## Switch them on

Run setup (`sudo bash install.sh`) and, on its first page, under **Storage**, switch on
**Backup cards**. Nothing changes on the server before that, also after an upgrade.

## Choose a card

1. Take an **empty** SD card or USB stick, formatted **ext4** or **exFAT** (exFAT can also
   be read on Windows and macOS). NekoServer never formats a card and never erases one:
   format it on a computer first if it is not empty (that erases it).
2. Plug it into the server. On the desktop it opens as a folder by itself. Without a
   desktop, mount it yourself, for example `sudo mkdir -p /mnt/card && sudo mount /dev/sdb1 /mnt/card`
   (`lsblk -f` shows its name).
3. On the dashboard, open **Backup cards** (it asks for your PIN, on the server too) and
   press **Look for cards**. Each card or stick plugged in is listed with what it is:
   - *Empty: it can be card N of the set.* Press **Use this as card N of the set**, tick
     that you understand the copies are not encrypted, and confirm. NekoServer adds a
     `NekoBackup` folder to it, with a README and its own restore tool. Write the number
     on the card.
   - *Card N of your set*: it is in use already.
   - Otherwise it says why it can't be one: a card of another set (this server's own
     cards from before it was set up again are one too: see below), a card that already
     holds files, a full card, a filesystem other than ext4 or exFAT, a drive built into
     the PC, the disk Linux runs from or one of NekoServer's own drives.

The cards are numbered: card 1, card 2 and so on. Together they are **the set**.

## How the cards are filled

Every night, an hour after the other nightly backups (06:15 with the usual times),
NekoServer adds to the card plugged in what is new or changed since. A file already on a
card is not copied again. Each file is copied whole, read back and checked against its
checksum before it counts. Nothing on a card is ever overwritten or deleted.

With no card of the set plugged in, nothing is written that night; the dashboard shows how
many files wait for a card. **Write to the card now** (on the Backups page and the Backup
cards page) starts the copy at once. Leave the card in until the Backups page says it is
done. A card taken out in the middle loses nothing: plug it in again and the next run goes
on where it stopped.

**A card is full.** The copy stops when a little room is left (at least 256 MB, so the card
always has room for its records). The dashboard and your phone (a warning, not an urgent
alert) say *Card N is full: put in a new card (it becomes card N+1)*. Choose a new empty card
as above: the files that waited go onto it. Keep the full card: it holds its files for good.

**No card written for 30 days.** While backup cards are on, a set that has had no card
plugged in for 30 days gets a quiet reminder on the dashboard and on your phone (without a
sound). Plug a card of the set in overnight.

Each card also holds the set's inventory: which card held which file when it was last
written. Only the newest three are kept on each card, and a new one is written only when
something changed.

**Check your cards now and then.** The server reads a file on a card only when it writes
it, and an SD card can go bad over the years. Once a year or so, plug each card in and
press **Check this card** on its row of the Backup cards page (PIN). Every copy on it is
read again and compared with its checksum; the page shows how far it got and then what it
found. A damaged copy is named and no longer counts: the next run copies that file again
onto the card plugged in then (tonight, or at once with **Write to the card now**). Keep
the card: its other copies are fine. A check changes nothing on the card, and a card taken
out during the check changes nothing at all (plug it in and press the button again).
Without the server, the card's own tool does the same check: `verify`
([CARD-RECOVERY.md](CARD-RECOVERY.md)).

## Get files back

- **With the server**: `sudo bash install.sh --recover`, then **Restore something from a
  backup** and **From backup cards**. Plug in a card; the menu shows what is on it, asks
  for a word to look for (or every file) and a new folder, and restores into it. Nothing on
  the card or in your apps is changed, and nothing is overwritten.
- **Without the server**, on any Linux computer: [CARD-RECOVERY.md](CARD-RECOVERY.md).

## For administrators

- The job is `nekoserver-cards.service` (`nekoserver-runtime cards`), started by
  `nekoserver-cards.timer` and by **Write to the card now**. It holds the backups' lock, so
  it never runs at the same time as another backup or the monthly update. Its report is
  `reports/jobs/cards.json` on the SSD; what the server knows of the set is in `cards/` on
  the SSD (a catalogue of which file is on which card, which the cards can rebuild with
  **Take these cards back**, and the status the dashboard shows). The two buttons' work
  runs in the same service: `cards/request.json` is what the page asked for,
  `cards/task.json` how far it got and how it ended.
- **The SSD replaced, or NekoServer set up again.** The new catalogue doesn't know the old
  cards: the Backup cards page shows each as *card N of a backup set made on* that date.
  Plug in the cards of your set (all of them, or as many as you can) and press **Take
  these cards back** (PIN): the server reads them and makes its catalogue again from what
  is on them and from the inventory each card holds. The cards are then the set's again:
  what is on them is not copied again, and the next run writes on. A card that was not
  plugged in is known from the others' inventories; its files count again once it has
  been plugged in for a run. The button is there only while the server has no card of its
  own set: a card is never taken in without that click, and once a new card 1 is set up,
  the old cards stay an old set (they still restore without the server,
  [CARD-RECOVERY.md](CARD-RECOVERY.md), or with the recovery menu's **From backup
  cards**).
- A card is written only when it is removable, mounted, enrolled by this server on that
  very filesystem (its UUID), names this set in its `NekoBackup/identity.json`, and is not
  the system disk or one of NekoServer's drives.
- The format of the cards ("archive format 1") does not change with NekoServer's updates.
