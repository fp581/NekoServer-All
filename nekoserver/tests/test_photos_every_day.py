"""Deep check plans 711-730 (photos every day): what the guides say to do with Immich.

What breaks each test (one line each):
- RECOVERY.md's "The SSD or the HDD broke" row without the link to the Immich rebuild
  fails test_a_new_ssd_leads_to_putting_immichs_database_back;
- the rebuild's step 3 sending the reader to Immich's guide alone (its gunzip and its
  psql --dbname=postgres), or its commands drifting from the dump, the Compose project
  or the search-path line the restore rehearsal changes, fails
  test_the_rebuild_puts_the_dump_back_with_commands_that_match_it.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_tools  # noqa: E402

RECOVERY = (ROOT / 'RECOVERY.md').read_text()
BACKUP_RS = (ROOT / 'runtime/src/backup.rs').read_text()


def rebuild_section():
    return RECOVERY.split('### Rebuild Immich from the USB drive')[1].split('\n### ')[0]


class ImmichDatabaseBackTests(unittest.TestCase):
    def test_a_new_ssd_leads_to_putting_immichs_database_back(self):
        """Deep check 726: with the SSD broken, the row said to get the photos back with
        the recovery menu, whose end says to upload them in the browser: every photo in
        one account, without albums, faces or the family's accounts, while the weekly
        database copies sat on the HDD. The rebuild said it was for both drives lost."""
        row = next(line for line in RECOVERY.splitlines() if line.startswith('| **The SSD or the HDD broke**'))
        self.assertIn('(#rebuild-immich-from-the-usb-drive)', row)
        self.assertIn('/mnt/nekoserver-media/backups/immich-database/', row)
        # The keep_dump folder on the HDD.
        self.assertIn('media.join("backups/immich-database")', BACKUP_RS)
        self.assertIn('or the SSD alone', rebuild_section())

    def test_the_rebuild_puts_the_dump_back_with_commands_that_match_it(self):
        """Immich's guide, followed as it is with NekoServer's copy (checked with a real
        PostgreSQL): `gunzip` says "not in gzip format", and without it `psql
        --dbname=postgres` puts the tables in the postgres database, so Immich starts
        empty. The commands restore into the database the copy was made from."""
        section = rebuild_section()
        commands = re.search(r'```bash\n(.*?)```', section.split('\n3. ')[1], re.S)
        self.assertIsNotNone(commands, 'step 3 shows the commands')
        lines = [line.strip() for line in commands[1].splitlines() if line.strip()]
        self.assertNotIn('gunzip', commands[1])
        self.assertNotIn('--dbname=postgres', commands[1])
        # The copy is of the immich database (DUMP_SCRIPT: -d "$POSTGRES_DB", immich).
        self.assertIn('pg_dump -U \\"$POSTGRES_USER\\" -d \\"$POSTGRES_DB\\"', BACKUP_RS)
        restore = next(line for line in lines if line.startswith('sudo sed '))
        self.assertIn('psql -U postgres -d immich --single-transaction -v ON_ERROR_STOP=1', restore)
        # The one line the restore rehearsal changes, as a whole line only.
        empty = recovery_tools.SEARCH_PATH_EMPTY.decode()
        public = recovery_tools.SEARCH_PATH_PUBLIC.decode()
        self.assertIn(f'"s/^{empty}\\$/{public}/"', restore)
        # Immich's own project and files, as photos.py runs them.
        for line in lines[1:]:
            self.assertTrue(
                line.startswith(
                    ('sudo docker compose -p neko-immich -f $c/compose.yaml --env-file $c/.env ', 'sudo sed ')
                ),
                line,
            )
        self.assertEqual(lines[0], 'c=/srv/nekoserver/compose/immich')
        photos = (ROOT / 'modules/photos.py').read_text()
        self.assertIn("'neko-immich'", photos)
        self.assertIn("compose_dir / 'compose.yaml'", photos)
        # The new database is emptied first; Immich is stopped around it, never removed.
        self.assertIn("DROP DATABASE immich WITH (FORCE)' -c 'CREATE DATABASE immich'", commands[1])
        self.assertNotIn(' down', commands[1])


if __name__ == '__main__':
    unittest.main()
