"""Current GET-only Jellyfin/Seerr readiness; no prompts or credentials in reports."""
import json
from pathlib import Path
from media_libraries import Client, SetupRequired, folders, matching, LIBRARIES, seerr_key, row
from neko_common import safe_path
import seerr_providers


class ReadClient(Client):
    def call(self, endpoint, method='GET', payload=None):
        if method!='GET' or payload is not None:raise RuntimeError('Readiness client only allows GET requests')
        return super().call(endpoint)
    def close(self):
        # Stored application keys are not newly created login sessions.
        self.headers.clear()


def check(config, factory=ReadClient):
    root=safe_path(config['data_mount']);apps=config.get('media',{}).get('services',[])
    results=[];ids=[];server_id=None
    settings={}
    if 'seerr' in apps:
        try:
            path=safe_path(root/'appdata/seerr/settings.json')
            if path.is_file():settings=json.loads(path.read_text())
        except (OSError,ValueError,RuntimeError):settings={}
    if 'jellyfin' in apps:
        client=factory('jellyfin')
        try:
            try:
                info=client.call('/System/Info/Public')
                if not isinstance(info,dict) or not info.get('Id'):raise RuntimeError('Invalid public system response')
                server_id=info['Id']
                if info.get('StartupWizardCompleted') is not True:
                    raise SetupRequired('Jellyfin first administrator wizard is incomplete')
                saved=settings.get('jellyfin',{}) if isinstance(settings,dict) else {}
                if not isinstance(saved,dict) or saved.get('serverId',saved.get('serverID'))!=server_id or not isinstance(saved.get('apiKey'),str) or not saved['apiKey']:
                    raise SetupRequired('No matching Jellyfin application key in Seerr settings; complete server linking or run authenticated --libraries')
                client.headers['X-Emby-Token']=saved['apiKey']
                values=folders(client)
                for category,name,kind in LIBRARIES:
                    value=matching(values,category,name,kind)
                    if not value:raise SetupRequired('Expected movie/TV libraries are missing')
                    ids.append(str(value['ItemId']))
                results.append(row('jellyfin:current_libraries','pass'))
            except SetupRequired as exc:results.append(row('jellyfin:current_libraries','needs_setup',str(exc)))
            except (OSError,RuntimeError,ValueError,TypeError,KeyError):results.append(row('jellyfin:current_libraries','fail','Jellyfin authenticated library check failed'))
        finally:client.close()
    if 'seerr' in apps:
        client=factory('seerr')
        try:
            try:
                client.headers['X-Api-Key']=seerr_key(root)
                public=client.call('/settings/public')
                if not isinstance(public,dict) or public.get('initialized') is not True:raise SetupRequired('Seerr first-run setup is incomplete')
                selected=client.call('/settings/jellyfin')
                if not server_id or not isinstance(selected,dict) or selected.get('serverId',selected.get('serverID'))!=server_id:raise SetupRequired('Seerr/Jellyfin server identity is not verified')
                values=client.call('/settings/jellyfin/library')
                if len(ids)!=2 or not isinstance(values,list) or not all(any(isinstance(x,dict) and str(x.get('id'))==i and x.get('enabled') is True for x in values) for i in ids):raise SetupRequired('Expected Jellyfin libraries are not currently enabled in Seerr')
                results.append(row('seerr:current_libraries','pass'))
            except SetupRequired as exc:results.append(row('seerr:current_libraries','needs_setup',str(exc)))
            except (OSError,RuntimeError,ValueError,TypeError,KeyError):results.append(row('seerr:current_libraries','fail','Seerr library/link API check failed'))
        finally:client.close()
        if set(apps)&set(seerr_providers.SPECS):
            results+=seerr_providers.configure(root,apps=apps,factory=factory,verify_only=True,prompt=lambda _: (_ for _ in ()).throw(RuntimeError('Readiness must not prompt')))
    if 'navidrome' in apps:
        import music_client
        music=music_client.check(config)
        results.append(row('navidrome:catalogue',music['status'],'Authenticated non-admin listener/library check; real audio playback remains witnessed'))
    for app in ('qbittorrent','navidrome'):
        if app in apps:results.append(row(app+':authenticated_playback_or_transfer','needs_setup','Current authenticated transfer/playback needs a server acceptance test; no passwords are requested by monitoring'))
    if apps:results.append(row('end_to_end_media','needs_setup','Complete a real request, acquisition, import and playback test on the deployed server'))
    return results
