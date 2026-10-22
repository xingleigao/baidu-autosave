"""只依赖标准库的 ClawBot 后端命令客户端。"""

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def main():
    parser = argparse.ArgumentParser(description='调用百度网盘影片追更服务')
    parser.add_argument('text', help='例如：搜索 影片名，或追剧 118943（使用搜索结果的数字 ID）')
    arguments = parser.parse_args()
    base_url = os.environ.get('BAIDU_AUTOSAVE_URL', '').rstrip('/')
    token = os.environ.get('BAIDU_AUTOSAVE_TOKEN', '')
    if not token or urlparse(base_url).scheme not in ('http', 'https'):
        print('请配置 BAIDU_AUTOSAVE_URL 和 BAIDU_AUTOSAVE_TOKEN', file=sys.stderr)
        return 1
    request = Request(base_url + '/api/clawbot/message',
                      data=json.dumps({'text': arguments.text}).encode(),
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
    try:
        with build_opener(NoRedirect).open(request, timeout=600) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('success') else 1
    except HTTPError as error:
        print(f'后端返回 HTTP {error.code}', file=sys.stderr)
        try:
            print(json.dumps(json.load(error), ensure_ascii=False), file=sys.stderr)
        except (ValueError, UnicodeError):
            pass
    except (URLError, TimeoutError, ValueError) as error:
        print(f'调用后端失败：{type(error).__name__}；请先查询任务确认状态', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
