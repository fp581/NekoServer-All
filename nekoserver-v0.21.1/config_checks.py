"""Pure checks for selected modules, before any installer module can mutate OS state."""
import re
from neko_common import host_timezone
from modules import app_backup, dashboard, heartbeat, host_backup, notifications, server_os, nextcloud, maintenance, music


def validate(config, plan):
    validators = {
        'music': music.options, 'nextcloud': nextcloud.options, 'maintenance': maintenance.options, 'server_os': server_os.settings, 'dashboard': dashboard.options,
        'notifications': notifications.options, 'heartbeat': heartbeat.interval,
        'host_backup': host_backup.settings,
        'app_backup': lambda c: app_backup.definitions(c, '/runtime', '/config'),
    }
    for name in plan:
        if name in validators:
            validators[name](config)
    for group in ('tv', 'onboarding', 'docker', 'photos', 'networking', 'guided_setup'):
        values = config.get(group, {})
        if not isinstance(values, dict):
            raise RuntimeError(group+' must be an object')
    if type(config.get('guided_setup', {}).get('enabled', True)) is not bool:
        raise RuntimeError('guided_setup.enabled must be a boolean')
    switches = {
        'tv': ('desktop_shortcuts','autostart_big_picture','bluetooth','keep_awake'),
        'onboarding': ('enabled','collect_sources','configure_indexers','configure_libraries','configure_request_providers'),
        'docker': ('install_engine','enable_service'), 'photos': ('deploy_immich',),
        'networking': ('tailnet_only','samba_media_write'),
    }
    for name in plan:
        group = 'networking' if name == 'samba' else name
        for key in switches.get(group, ()):
            values = config.get(group, {})
            if key in values and type(values[key]) is not bool:
                raise RuntimeError(group+'.'+key+' must be a boolean')
    if 'media' in plan:
        values = config.get('media', {})
        for key in ('uid','gid'):
            value = values.get(key,1000)
            if type(value) is not int or not 1 <= value <= 2**32-2:
                raise RuntimeError('Media uid/gid must be positive numeric account IDs')
        from modules import media as media_module
        media_module.torrent_port(values)
        if values.get('bind_ip','127.0.0.1') != '127.0.0.1':
            raise RuntimeError('Media web interfaces must bind to localhost')
    if set(plan) & {'media', 'photos', 'photo_backup', 'photo_preservation', 'music_backup', 'maintenance'}:
        host_timezone(config)
    if 'media_monitor' in plan:
        value=config.get('monitoring',{}).get('interval_minutes',60)
        if type(value) is not int or not 15<=value<=1440:raise RuntimeError('Invalid media monitoring interval')
    if 'onboarding' in plan:
        value=config.get('onboarding',{}).get('wait_seconds',60)
        if type(value) is not int or not 0<=value<=300:raise RuntimeError('Invalid startup wait')
    if set(plan) & {'account_credentials', 'accounts'}:
        import account_store
        account_store.enabled(config)
        value = config.get('accounts', {}).get('wait_seconds', 60)
        if type(value) is not int or not 0 <= value <= 300:
            raise RuntimeError('Account startup wait must be 0..300 seconds')
    opts=config.get('photos',{})
    if 'photos' in plan:
        value=opts.get('immich_version','auto')
        if value != 'auto' and (not isinstance(value,str) or not re.fullmatch(r'v\d+\.\d+\.\d+',value)):
            raise RuntimeError('Invalid Immich release version')
    if 'photo_preservation' in plan:
        for key,default,minimum in (('archive_interval_minutes',60,15),('archive_min_age_seconds',600,60)):
            value=opts.get(key,default)
            maximum=10080 if key=='archive_interval_minutes' else 31536000
            if type(value) is not int or not minimum<=value<=maximum:raise RuntimeError('Invalid photo archive interval/age')
        _time(opts.get('database_backup_time','02:15'))
    if 'photo_backup' in plan:
        _time(opts.get('backup_time','03:00'))
        if opts.get('backup_weekday','Sun') not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'):
            raise RuntimeError('Invalid external backup weekday')
        if opts.get('external_backup_fstype','ext4') not in ('ext4','xfs','btrfs','exfat'):
            raise RuntimeError('Invalid external backup filesystem')
    if config.get('runtime',{}).get('backend','python') != 'rust' and set(plan)&{'app_backup','host_backup','notifications','heartbeat','maintenance'}:
        raise RuntimeError('Selected native backup/notification modules require runtime.backend=rust')


def _time(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
        raise RuntimeError('Invalid job time; use HH:MM')
