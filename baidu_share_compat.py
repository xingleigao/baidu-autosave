"""兼容百度新版 JSON 分享页面，不执行页面 JavaScript。"""
import json
import re
from html.parser import HTMLParser


class JsonScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('type', '').lower() == 'application/json'
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.documents.append(''.join(self.parts))
            self.active = False


def parse_shared_page(html):
    parser = JsonScripts()
    parser.feed(html)
    candidates = parser.documents + [m.group(1) for m in re.finditer(
        r'(?:yunData\.setData|locals\.mset)\((.+?)\);', html, re.S)]
    for text in candidates:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            continue
        if (isinstance(data, dict) and (data.get('share_uk') or data.get('uk'))
                and 'shareid' in data and 'bdstoken' in data and 'file_list' in data):
            return data
    raise ValueError('分享页未返回可识别的文件数据，请检查链接、提取码或百度验证状态')


def install_share_compat():
    from baidupcs_py.baidupcs.pcs import BaiduPCS, Method
    from baidupcs_py.baidupcs.errors import assert_ok
    if getattr(BaiduPCS.shared_paths, '_share_v2_compat', False):
        return

    @assert_ok
    def shared_paths(self, shared_url):
        if not self._stoken:
            raise ValueError('Cookie 缺少 STOKEN，无法读取分享')
        response = self._request(Method.Get, shared_url, params=None)
        response.raise_for_status()
        self._cookies_update(response.cookies.get_dict())
        return parse_shared_page(response.text)

    shared_paths._share_v2_compat = True
    BaiduPCS.shared_paths = shared_paths
