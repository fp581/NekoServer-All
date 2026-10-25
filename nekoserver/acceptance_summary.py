"""Restricted dashboard view of a private deployment acceptance report."""
import hashlib,json
from neko_common import VERSION
MANUAL={'cloud_client':'Personal cloud clients','outage_delivery':'Independent outage alert','host_restore':'Host and user restore rehearsal','server_idle':'Server idle and drive policy','notification_delivery':'Phone notification delivery','app_restore':'Media application restore rehearsal','missing_drive':'Missing-drive protection','media_workflow':'Media workflow','music_playback':'Music streaming','photo_preservation':'Photo deletion safety','independent_restore':'Independent full restore','external_backup':'External copy/reconnect','remote_permissions':'Remote permissions','tv_session':'TV and controller','os_recovery':'OS recovery','gateway_clients':'Application gateway clients','cloud_restore':'Nextcloud files restore','music_restore':'Music library restore'}
STATES=('pass','fail','needs_setup','warning','blocked')
PROFILES=('music','cloud','heartbeat','host_backup','server_os','notifications','app_backup','foundation','media','photos','full','network','networking','tv','dashboard','media_monitor','lan_access','remote_access')

# The photo and USB-copy settings whose change makes an earlier acceptance run outdated:
# a different USB drive or archive folder was never checked by it.
PHOTO_SCOPE=('external_backup_uuid','external_backup_fstype','archive_dir','backup_weekday','backup_time')

def photo_scope(config):
    photos=config.get('photos')
    photos=photos if isinstance(photos,dict) else {}
    return {'deploy_immich':photos.get('deploy_immich') is True,**{k:photos.get(k) for k in PHOTO_SCOPE}}

def scope_digest(config):
    supplied=config.get('acceptance_scope')
    if isinstance(supplied,str) and len(supplied)==64 and all(c in '0123456789abcdef' for c in supplied):return supplied
    values={k:config.get(k) for k in ('data_mount','media_mount','external_backup_mount','storage')}
    values['review_settings']={k:config.get(k) for k in ('server_os','heartbeat','host_backup','tv','networking','dashboard','monitoring','app_backup','nextcloud','maintenance')};values['runtime']={'backend':config.get('runtime',{}).get('backend')};values['gateway']={k:config.get('gateway',{}).get(k) for k in ('services','lan_ip','lan_cidr')};values['services']=config.get('media',{}).get('services',[]);values['photos']=photo_scope(config)
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def complete_status(stamp,now):
    """A complete check: 'complete', or 'stale' once it is over a day old (still complete)."""
    return 'stale' if now-stamp>86400 else 'complete'

def summarize(saved,config,now):
    result={'status':'unknown','profile':None,'generated_at':None,'automatic':{},'manual':{},'manual_checks':[]}
    stamp=saved.get('generated_at')
    if saved.get('format')!='neko-acceptance-v1' or saved.get('profile') not in PROFILES or type(stamp) not in (int,float) or not 0<=stamp<=now+60:return result
    result.update(profile=saved['profile'],generated_at=stamp)
    if saved.get('version')!=VERSION or saved.get('dashboard_scope')!=scope_digest(config):result['status']='outdated';return result
    valid=True
    for group in ('automatic','manual'):
        rows=saved.get(group,[])
        if not isinstance(rows,list) or not rows or len(rows)>200:valid=False;continue
        counts={s:0 for s in STATES}
        for row in rows:
            if not isinstance(row,dict) or row.get('status') not in STATES:valid=False;continue
            counts[row['status']]+=1
            if group=='manual':
                key=row.get('id')
                if key not in MANUAL:valid=False;continue
                if row['status']=='pass' and (row.get('evidence')!='user_reported' or type(row.get('confirmed_at')) not in (int,float) or not 0<=row['confirmed_at']<=now+60):valid=False
                result['manual_checks'].append({'name':MANUAL[key],'status':row['status']})
        result[group]=counts
    # "stale": complete, but over a day old (collect.rs acceptance says the same). A failed
    # or unfinished check stays failed or pending however old it is.
    if not valid:result['status']='unknown'
    elif any(result[g].get('fail',0) for g in ('automatic','manual')):result['status']='failed'
    elif saved.get('acceptance_complete') is True and all(sum(result[g].values())==result[g].get('pass',0) for g in ('automatic','manual')):result['status']=complete_status(stamp,now)
    else:result['status']='pending'
    return result
