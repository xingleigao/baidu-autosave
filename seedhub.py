"""使用 curl_cffi Safari 请求搜索影片并逐个解析百度分享。"""

import argparse
import html
import json
import re
import traceback
import time
from urllib.parse import parse_qs, quote, urljoin, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException, Timeout
from loguru import logger
from selectolax.lexbor import LexborHTMLParser


DEFAULT_BASE_URL = 'https://sidhub.cc'
REQUEST_IMPERSONATE = 'safari17_0'
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
}


def normalize_share(url, text=''):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname != 'pan.baidu.com':
        raise ValueError('不是百度网盘分享链接')
    params = parse_qs(parsed.query)
    password = params.get('pwd', [''])[0]
    if not password:
        match = re.search(r'(?:提取码|密码)\s*[:：]?\s*([a-zA-Z0-9]{4})\b', text)
        password = match.group(1) if match else ''
    if parsed.path == '/share/init' and params.get('surl'):
        share_id = params['surl'][0]
        if not share_id.startswith('1'):
            share_id = '1' + share_id
    else:
        match = re.fullmatch(r'/s/([a-zA-Z0-9_-]+)', parsed.path)
        if not match:
            raise ValueError('无法识别百度分享地址')
        share_id = match.group(1)
    return {'url': f'https://pan.baidu.com/s/{share_id}', 'pwd': password}


class SeedHubError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def failure_details(error):
    codes = re.findall(r'(?:error_code|errno|errcode)[\s\'\"]*[:=][\s\'\"]*(-?\d+)', str(error))
    frames = traceback.extract_tb(error.__traceback__)
    location = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in frames)
    reasons = [reason for reason in ('提取码', '分享已取消', '分享已过期', '分享链接已失效',
                                    '没有可比较的影片文件', '验证码', '登录', '超时') if reason in str(error)]
    return f'{type(error).__name__}, codes={codes}, reasons={reasons}, location={location}'


class SeedHubClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, session=None, timeout=30):
        self.base_url = base_url.rstrip('/')
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def close(self):
        close = getattr(self.session, 'close', None)
        if close:
            close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _get(self, url, timeout=None, allow_redirects=True):
        for attempt in range(1, 4):
            try:
                return self._request_once(url, timeout, allow_redirects)
            except SeedHubError as error:
                if not error.retryable:
                    raise
                if attempt == 3:
                    raise SeedHubError(f'{error}（已尝试 3 次）') from error
                logger.warning(f'SeedHub 查询重试: host={urlparse(url).hostname}, attempt={attempt}/3, delay={attempt * 2}s')
                time.sleep(attempt * 2)

    def _request_once(self, url, timeout=None, allow_redirects=True):
        hostname = urlparse(url).hostname
        logger.info(f'SeedHub 请求开始: host={hostname}, path={urlparse(url).path}')
        try:
            response = self.session.get(url, headers=REQUEST_HEADERS, impersonate=REQUEST_IMPERSONATE,
                                        timeout=timeout or self.timeout, allow_redirects=allow_redirects)
        except Timeout as error:
            logger.warning(f'SeedHub 请求超时: host={hostname}, error={type(error).__name__}')
            raise SeedHubError(f'搜索源 {hostname} 访问超时；未创建任务。') from error
        except RequestException as error:
            logger.warning(f'SeedHub 请求异常: host={hostname}, {failure_details(error)}')
            raise SeedHubError(f'无法连接搜索源 {hostname}，请检查网络；未创建任务。') from error
        redirect_chain = '>'.join(
            f'{item.status_code}:{urlparse(item.url).hostname}' for item in response.history)
        logger.info(f'SeedHub 响应: host={hostname}, status={response.status_code}, final_host={urlparse(response.url).hostname}, redirects={redirect_chain or "none"}')
        if response.status_code in (301, 302, 303, 307, 308) and not allow_redirects:
            logger.info(f'SeedHub 收到重定向响应，不重试: host={hostname}, status={response.status_code}, location={response.headers.get("Location", "")}')
            return response
        if response.status_code != 200:
            ray_id = response.headers.get('CF-RAY') or response.headers.get('cf-ray')
            ray_details = f', cf_ray={ray_id}' if ray_id else ''
            logger.warning(f'SeedHub 返回非 200: host={hostname}, status={response.status_code}, server={response.headers.get("Server", "")}, content_type={response.headers.get("Content-Type", "")}{ray_details}')
            ray_message = f'，Cloudflare Ray ID: {ray_id}' if ray_id else ''
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            raise SeedHubError(f'搜索源 {hostname} 返回 HTTP {response.status_code}{ray_message}；未创建任务。',
                               retryable=retryable)
        if 'cf-chl-' in response.text or '<title>Just a moment' in response.text:
            raise SeedHubError(f'搜索源 {hostname} 要求网页验证；未创建任务。', retryable=False)
        return response

    def search(self, keyword, limit=20):
        response = self._get(f'{self.base_url}/s/{quote(keyword.strip(), safe="")}')
        parser = LexborHTMLParser(response.text)
        infos = re.findall(r'<li>(\d{4}\s*/\s*(?:电影|剧集|动漫)[^<]*)</li>', response.text)
        ratings = re.findall(r'豆瓣评分:\s*<a[^>]*>([^<]+)</a>', response.text)
        results, seen_ids = [], set()
        for index, node in enumerate(parser.css('a.image[target="_blank"]')):
            movie_url = urljoin(response.url, node.attributes.get('href', ''))
            match = re.fullmatch(r'/movies/(\d+)/?', urlparse(movie_url).path)
            if not match or match.group(1) in seen_ids:
                continue
            seen_ids.add(match.group(1))
            results.append({
                'title': node.attributes.get('title') or node.text().strip(),
                'info': html.unescape(infos[index].strip()) if index < len(infos) else '',
                'rating': html.unescape(ratings[index].strip()) if index < len(ratings) else '?',
                'id': match.group(1), 'url': movie_url,
            })
            if len(results) >= limit:
                break
        return results

    def iter_links(self, movie_id, limit=30, title_callback=None):
        if not isinstance(movie_id, str) or not re.fullmatch(r'[0-9]+', movie_id):
            raise ValueError('追剧需要搜索结果中的数字 ID，例如：追剧 118943；请先发送“搜索 影片名”')
        response = self._get(f'{self.base_url}/movies/{movie_id}/')
        parser = LexborHTMLParser(response.text)
        heading = parser.css_first('h1')
        title = heading.text().lstrip('#').strip() if heading else f'影片 {movie_id}'
        if title_callback:
            title_callback(title)
        logger.info(f'SeedHub 影片详情已读取: seedhub_id={movie_id}, title={title}')
        errors, seen_paths, seen_shares = [], set(), set()
        targets = []
        for node in parser.css('[data-link][rel="nofollow"]'):
            if node.attributes['data-link'] != 'pan.baidu.com':
                continue
            target = node.attributes.get('href')
            if not target:
                continue
            parsed = urlparse(target)
            if parsed.hostname != 'pan.baidu.com' and (
                    (parsed.scheme and parsed.scheme not in ('http', 'https')) or
                    (parsed.netloc and parsed.netloc not in (urlparse(self.base_url).netloc,
                                                           urlparse(response.url).netloc))):
                continue
            if target in seen_paths:
                continue
            seen_paths.add(target)
            targets.append((target, node.attributes.get('title') or node.text()))
        logger.info(f'SeedHub 百度资源入口: seedhub_id={movie_id}, found={len(targets)}, resolve_limit={limit}')
        resolved_count = 0
        for link_index, (target, description) in enumerate(targets[:limit], 1):
            if link_index > 1:
                time.sleep(0.2)
            logger.info(f'SeedHub 解析百度入口: seedhub_id={movie_id}, link_index={link_index}')
            try:
                if urlparse(target).hostname == 'pan.baidu.com':
                    share = normalize_share(target, description)
                else:
                    resolved = self._get(urljoin(response.url, target), timeout=10)
                    page = LexborHTMLParser(resolved.text)
                    direct = page.css_first('a.direct-pan')
                    share_text = description + ' ' + page.text()
                    if direct:
                        description += ' ' + direct.text()
                    possible_urls = [resolved.url]
                    if direct:
                        possible_urls.append(urljoin(resolved.url, direct.attributes.get('href', '')))
                    possible_urls.extend(re.findall(
                        r'https?://pan\.baidu\.com/(?:s/[a-zA-Z0-9_-]+|share/init)[^\s<>"\']*',
                        html.unescape(resolved.text).replace('\\/', '/')))
                    share = None
                    for possible_url in possible_urls:
                        try:
                            share = normalize_share(possible_url, share_text)
                            break
                        except ValueError:
                            continue
                    if share is None:
                        logger.warning(f'SeedHub 百度入口未找到有效分享: seedhub_id={movie_id}, link_index={link_index}, final_host={urlparse(resolved.url).hostname}, has_direct_anchor={direct is not None}')
                        continue
                identity = (share['url'], share['pwd'])
                if identity in seen_shares:
                    continue
                seen_shares.add(identity)
                resolved_count += 1
                yield dict(share, title=title, description=description, link_index=link_index)
            except SeedHubError as error:
                logger.warning(f'SeedHub 百度入口解析失败: seedhub_id={movie_id}, link_index={link_index}, reason={error}')
                errors.append(error)
        logger.info(f'SeedHub 百度资源解析结束: seedhub_id={movie_id}, resolved={resolved_count}, failed={len(errors)}')
        if not resolved_count and errors:
            raise errors[0]

    def get_links(self, movie_id, limit=30):
        links = []
        title = f'影片 {movie_id}'
        def save_title(value):
            nonlocal title
            title = value
        links.extend(self.iter_links(movie_id, limit, title_callback=save_title))
        return {'id': movie_id, 'title': title, 'links': links}


if __name__ == '__main__':
    argument_parser = argparse.ArgumentParser(description='搜索影片或按 ID 提取百度分享')
    subparsers = argument_parser.add_subparsers(dest='command', required=True)
    search_parser = subparsers.add_parser('search')
    search_parser.add_argument('keyword')
    search_parser.add_argument('--limit', type=int, default=20)
    links_parser = subparsers.add_parser('links')
    links_parser.add_argument('movie_id')
    arguments = argument_parser.parse_args()
    client = SeedHubClient()
    try:
        result = (client.search(arguments.keyword, arguments.limit) if arguments.command == 'search'
                  else client.get_links(arguments.movie_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()
