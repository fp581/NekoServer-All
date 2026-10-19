"""Authenticated local qBittorrent setup; cookie and password stay in memory."""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class Client:
    def __init__(self,port=8080):
        self.base=f'http://127.0.0.1:{port}'
        self.jar=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.jar),NoRedirect())
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
        if reply!='Ok.' or not any(c.name=='SID' for c in self.jar):
            raise RuntimeError('qBittorrent authentication failed')
    def close(self):
        try:self.call('/auth/logout',{})
        except RuntimeError:pass
        self.jar.clear()


def configure(username,password,apply=False,categories=('tv','movies','music'),client=None):
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
        for category in categories:
            if category in existing and str(existing[category].get('savePath','')).rstrip('/') not in ('',desired,desired+'/'+category):
                raise RuntimeError(f'qBittorrent category {category} has a custom path; review manually')
        print('qBittorrent: login verified; download paths/categories', 'APPLY' if apply else 'PREVIEW')
        if apply:
            if changes:client.call('/app/setPreferences',{'json':json.dumps(changes)})
            for category in categories:
                if category not in existing:client.call('/torrents/createCategory',{'category':category,'savePath':desired+'/'+category})
            verified=client.call('/app/preferences',json_result=True)
            actual=client.call('/torrents/categories',json_result=True)
            if (str(verified.get('save_path','')).rstrip('/')!=desired
                or ('temp_path' in changes and str(verified.get('temp_path','')).rstrip('/')!=changes['temp_path'])
                or not all(c in actual and str(actual[c].get('savePath','')).rstrip('/') in ('',desired,desired+'/'+c) for c in categories)):
                raise RuntimeError('qBittorrent did not retain expected download configuration')
    finally:client.close()
