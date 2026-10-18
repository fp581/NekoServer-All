# NekoServer - Arch based media server

Overhall to fully automated server creation on Arch based systems.

Tested only in virtual emulation with docker and not on live. need further testing.

The script needs Arch based system to run.

The automation builds from the ground up the following server stack:

Immich (personal photos and video bakup and storage) : SSD for the software and data, weekly backup to the HDD and external drive (if connected). The stack also includes Sonarr, Radarr, Lidarr, Prowlarr, Seerr, and qBittorrent, plus the dashboard and access/monitoring components.
Jellyfin (Movies and shows): SSD for the software HDD for the data
Navidrome (Music app): SSD for the music as it will be accesed daily
Nextcloud: other files and data storage, HDD only with external drive backup monthly
Automated monthly backup and update, if the update fails the server will restore the old ver of the apps.
Status Dashboard for monitoring the server (need to check if works on a live server)
What makes sense to be in rust is in rust code, the rest shell and python for the instalation as it's one time thing. 

Stack & Storage Architecture Review
  +-------------------------------------------------------------------------+
  |                             SSD STORAGE                                 |
  |  - Containers / App Data / PostgreSQL Databases                        |
  |  - Immich App & Library Cache                                           |
  |  - Navidrome Music Collection (Daily Access)                             |
  +-------------------------------------------------------------------------+
                                      |
         +----------------------------+----------------------------+
         v                                                         v
  +-----------------------+                                 +-----------------------+
  |      HDD STORAGE      |                                 |    EXTERNAL BACKUP    |
  | - Jellyfin Media      | ===== Cron/Timer Backup =======>| - Immich Offsite      |
  | - Nextcloud Data      |                                 | - Nextcloud Offsite   |
  +-----------------------+                                 +-----------------------+
