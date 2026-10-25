"""Authenticated local qBittorrent setup; cookie and password stay in memory."""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

from neko_common import app_port, local_opener


QBITTORRENT_PORT=app_port('qbittorrent')


class Client:
    def __init__(self,port=QBITTORRENT_PORT):
        self.base=f'http://127.0.0.1:{port}'
        self.jar=http.cookiejar.CookieJar()
        self.opener=local_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def call(self,endpoint,form=None,json_result=False):
        if not endpoint.startswith('/') or '://' in endpoint:raise ValueError('Invalid local API endpoint')
        body=None if form is None else urllib.parse.urlencode(form).encode()
        req=urllib.request.Request(self.base+'/api/v2'+endpoint,data=body,headers={'Referer':self.base,'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with self.opener.open(req,timeout=10) as response:payload=response.read(3_000_000).decode()
        except (OSError,urllib.error.URLError) as exc:
            if isinstance(exc,urllib.error.HTTPError):exc.close()
            raise RuntimeError('qBittorrent API request failed; inspect local WebUI') from None
        return json.loads(payload) if json_result else payload.strip()
    def login(self,username,password):
        reply=self.call('/auth/login',{'username':username,'password':password})
        # qBittorrent 4 answers a good login with "Ok." and a cookie named SID,
        # qBittorrent 5 with an empty reply and QBT_SID_<port>; the cookie is the proof.
        if reply.strip() not in ('Ok.','') or not any(c.name=='SID' or c.name.startswith('QBT_SID') for c in self.jar):
            raise RuntimeError('qBittorrent authentication failed')
    def close(self):
        try:self.call('/auth/logout',{})
        except RuntimeError:pass
        self.jar.clear()



def category_ok(category, path, desired='/data/downloads'):
    """An empty path means "default folder + category name". Music must end up in
    /data/downloads/music exactly: that folder is on the SSD, where Lidarr imports from.
    TV and movies may also use the shared /data/downloads on the HDD."""
    if category == 'music':
        return path in ('', desired + '/music')
    return path in ('', desired, desired + '/' + category)

def automatic_mode(preferences):
    """The preference change that makes new torrents start in automatic mode, where each
    category's own folder is used ({} when they already do)."""
    return {} if preferences.get('auto_tmm_enabled') is True else {'auto_tmm_enabled':True}

def retained(verified,actual,changes,categories,desired):
    """qBittorrent kept what setup asked for: the download folders, automatic mode and
    each category's folder."""
    path=lambda value:str(value).rstrip('/')
    return (path(verified.get('save_path',''))==desired
            and ('temp_path' not in changes or path(verified.get('temp_path',''))==changes['temp_path'])
            and verified.get('auto_tmm_enabled') is True
            and all(c in actual and category_ok(c,path(actual[c].get('savePath','')),desired) for c in categories))

def configure(username,password,apply=False,categories=('tv','movies','music'),client=None,output=print):
    client=client or Client()
    try:
        client.login(username,password)
        preferences=client.call('/app/preferences',json_result=True)
        existing=client.call('/torrents/categories',json_result=True)
        desired='/data/downloads'
        current=str(preferences.get('save_path','')).rstrip('/')
        changes={}
        if current!=desired:
            if current not in ('','/downloads','/downloads/complete'):
                raise RuntimeError('qBittorrent has a custom save path; preserve it and review manually')
            if client.call('/torrents/info',json_result=True):
                raise RuntimeError('Existing torrents prevent changing the default save path automatically')
            changes['save_path']=desired
        if preferences.get('temp_path_enabled'):
            temp=str(preferences.get('temp_path','')).rstrip('/')
            if temp!='/data/downloads/incomplete':
                if not temp.startswith('/downloads/'):
                    raise RuntimeError('qBittorrent has a custom incomplete path; review manually')
                if client.call('/torrents/info',json_result=True):raise RuntimeError('Existing torrents prevent changing the incomplete path')
                changes['temp_path']='/data/downloads/incomplete'
        # Each category's own folder is used only in Automatic Torrent Management mode,
        # and qBittorrent starts new torrents in Manual mode unless told otherwise: the
        # Arr apps send only the category, so in Manual mode an album would land in the
        # HDD's /data/downloads, where Lidarr (which sees only the SSD) cannot find it.
        # This changes only how new torrents start; existing torrents keep their mode.
        changes.update(automatic_mode(preferences))
        fix={}
        for category in categories:
            if category not in existing:continue
            path=str(existing[category].get('savePath','')).rstrip('/')
            if category_ok(category,path,desired):continue
            # Only music has a single right place (/data/downloads/music, on the SSD that
            # Lidarr sees). A wrong but empty music category is corrected; anything else
            # with torrents in it, or another category's custom path, needs a person.
            if category=='music' and path in (desired,'/downloads','/downloads/complete') and not client.call('/torrents/info?category=music',json_result=True):
                fix[category]=desired+'/'+category;continue
            raise RuntimeError(f'qBittorrent category {category} saves to {path or "the default folder"}, not {desired}/{category}; '
                               'change it in qBittorrent (right-click the category, Edit) and run setup again')
        output('qBittorrent: login verified; download paths/categories '+('APPLY' if apply else 'PREVIEW'))
        if apply:
            if changes:client.call('/app/setPreferences',{'json':json.dumps(changes)})
            for category in categories:
                if category not in existing:client.call('/torrents/createCategory',{'category':category,'savePath':desired+'/'+category})
            for category,path in fix.items():client.call('/torrents/editCategory',{'category':category,'savePath':path})
            verified=client.call('/app/preferences',json_result=True)
            actual=client.call('/torrents/categories',json_result=True)
            if not retained(verified,actual,changes,categories,desired):
                raise RuntimeError('qBittorrent did not retain expected download configuration')
    finally:client.close()
