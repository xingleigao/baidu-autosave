"""影片搜索、资源择优和任务生命周期管理。"""

import hashlib
import json
import math
import posixpath
import re
import time
from datetime import datetime, timedelta
from threading import RLock
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from seedhub import DEFAULT_BASE_URL, SeedHubClient, failure_details, normalize_share
from utils import share_url_with_pwd
from season_rules import build_season_rule, season_identity


VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.ts', '.m2ts', '.rmvb', '.wmv', '.flv', '.m4v', '.webm'}
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_MEDIA_LINKS = 18
DEFAULT_SETTINGS = {'save_dir': '/追剧', 'cron': '0 */6 * * *', 'max_results': 5, 'max_links': MAX_MEDIA_LINKS}
BEIJING_TIMEZONE = ZoneInfo('Asia/Shanghai')


def format_size(size):
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return '未知大小'
    if value < 1024:
        return f'{int(value)} B'
    units = ('KB', 'MB', 'GB', 'TB')
    for unit in units:
        value /= 1024
        if value < 1024 or unit == units[-1]:
            return f'{value:.2f}'.rstrip('0').rstrip('.') + f' {unit}'


def default_media_schedule(latest):
    timestamp = latest.get('mtime') if latest else 0
    try:
        updated_at = (datetime.fromtimestamp(float(timestamp), BEIJING_TIMEZONE)
                      if timestamp else datetime.now(BEIJING_TIMEZONE))
    except (TypeError, ValueError, OSError, OverflowError):
        updated_at = datetime.now()
    start = updated_at - timedelta(hours=2)
    end = updated_at + timedelta(hours=2)
    if start.date() != end.date():
        hours = list(range(start.hour, 24)) + list(range(0, end.hour + 1))
        hour_field = ','.join(str(hour) for hour in hours)
    else:
        hour_field = f'{start.hour}-{end.hour}'
    cron = f'*/30 {hour_field} * * *'
    label = f'每天 {start.hour:02d}:00–{end.hour:02d}:59 每30分钟（按上次更新时间 {updated_at:%H:%M} 前后两小时）'
    return cron, label


def parse_update_schedule(value, default_cron):
    if value is None or value == '':
        cron = default_cron
        label = f'{cron}（Asia/Shanghai）'
    else:
        if not isinstance(value, str):
            raise ValueError('更新频率必须是文本，例如：每周六周日6-8点，或 cron 0 6-8 * * 6,0')
        value = value.strip()
        if value.startswith('cron '):
            cron = value[5:].strip()
            label = f'{cron}（Asia/Shanghai）'
        elif len(value.split()) == 5:
            cron = value
            label = f'{cron}（Asia/Shanghai）'
        else:
            weekly_match = re.fullmatch(r'每周([一二三四五六日天末周、,，]+?)(?:(白天|上午|下午|晚上))?(\d{1,2})(?:[-~至到](\d{1,2}))?点(?:每(\d{1,2})分钟)?', value)
            match = re.fullmatch(r'每天(?:(白天|上午|下午|晚上))?(\d{1,2})(?:[-~至到](\d{1,2}))?点(?:每(\d{1,2})分钟)?', value)
            interval_match = re.fullmatch(r'每(\d{1,2})分钟', value)
            if weekly_match:
                day_text, period, start, end, interval = weekly_match.groups()
                day_text = day_text.replace('周', '')
                if day_text == '末':
                    days = ['6', '0']
                else:
                    day_map = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '日': '0', '天': '0'}
                    days = [day_map[character] for character in day_text if character in day_map]
                if not days:
                    raise ValueError('星期格式应类似：每周六周日6-8点')
                start, end = int(start), int(end or start)
                if period in ('下午', '晚上'):
                    start = start + 12 if 1 <= start < 12 else start
                    end = end + 12 if 1 <= end < 12 else end
                if not 0 <= start <= end <= 23:
                    raise ValueError('时间范围必须在同一天的 0–23 点之间，例如：每周六周日6-8点')
                minutes = int(interval) if interval else 60
                if not 1 <= minutes <= 60:
                    raise ValueError('检测间隔必须是 1–60 分钟')
                minute_field = '0' if minutes == 60 else f'*/{minutes}'
                hour_field = str(start) if start == end else f'{start}-{end}'
                cron = f'{minute_field} {hour_field} * * {",".join(dict.fromkeys(days))}'
                day_label = '周末' if day_text == '末' else '、'.join(
                    '周日' if day == '0' else f'周{character}'
                    for character, day in zip((character for character in day_text if character in '一二三四五六日天'), days))
                label = f'{day_label} {start:02d}:00–{end:02d}:59，每{minutes}分钟（北京时间）'
            elif match:
                period, start, end, interval = match.groups()
                start, end = int(start), int(end or start)
                if period in ('下午', '晚上'):
                    start = start + 12 if 1 <= start < 12 else start
                    end = end + 12 if 1 <= end < 12 else end
                if not 0 <= start <= end <= 23:
                    raise ValueError('时间范围必须在同一天的 0–23 点之间，例如：每天6-8点')
                minutes = int(interval) if interval else 60
                if not 1 <= minutes <= 60:
                    raise ValueError('检测间隔必须是 1–60 分钟')
                minute_field = '0' if minutes == 60 else f'*/{minutes}'
                hour_field = str(start) if start == end else f'{start}-{end}'
                cron = f'{minute_field} {hour_field} * * *'
                hours = f'{start:02d}:00' if start == end else f'{start:02d}:00–{end:02d}:59'
                label = f'每天 {hours}，每{minutes}分钟（北京时间）'
            elif interval_match and 1 <= int(interval_match[1]) <= 59:
                minutes = int(interval_match[1])
                cron = f'*/{minutes} * * * *'
                label = f'每{minutes}分钟（北京时间，按钟表分钟）'
            else:
                raise ValueError('更新频率格式：每天6-8点、每周六周日6-8点、每30分钟，或 cron 0 6-8 * * *')
    CronTrigger.from_crontab(cron)
    return cron, label


def report_progress(callback, message):
    logger.info(f'追剧进度: {message}')
    if callback:
        try:
            callback(message)
        except Exception as error:
            logger.warning(f'追剧进度发送失败，不中断任务: {failure_details(error)}')


def subscription_message(task, existing=False, checked_links=None, valid_links=None):
    label = task.get('media', {}).get('update_frequency') or f'{task.get("cron", "默认定时")}（Asia/Shanghai）'
    share_url = share_url_with_pwd(task['url'], task.get('pwd'))
    parts = [f'{"任务已存在" if existing else "已创建追剧任务"} | 链接：{share_url}',
             f'更新频率：{label} | 保存到：{task.get("save_dir", "未设置")}']
    if checked_links is not None:
        parts.append(f'检查链接：{checked_links} 个 | 有效：{valid_links} 个')
    rule = task.get('media', {}).get('season_rule')
    if rule:
        parts.append(f'季数筛选：{rule["description"]}')
        if rule['regex_pattern']:
            parts.append(f'自动排除规则（匹配则保留，作用于原始路径）：{rule["regex_pattern"]}')
    latest = task.get('media', {}).get('latest') or {}
    if task.get('url'):
        parts.append(f'最高第{latest.get("episode", 0)}集，最新文件：{latest.get("path", "未知")}，大小：{format_size(latest.get("size"))}')
    return '；'.join(parts)


def episode_number(path):
    name = posixpath.basename(path)
    patterns = [r'(?i)S\d{1,2}[ ._-]*E(\d{1,4})', r'(?i)(?<![a-z])(?:EP?|第)[ ._-]*(\d{1,4})(?:[ ._-]*集)?',
                r'第([一二三四五六七八九十百零两]+)集', r'(?<!\d)(\d{1,3})(?=\.[^.]+$)',
                r'^(\d{1,3})(?=[ ._-])']
    for pattern in patterns:
        match = re.search(pattern, name)
        if match:
            value = match.group(1)
            if value.isdigit():
                number = int(value)
                if number in (264, 265, 360, 480, 576, 720):
                    continue
                return number
            digits = dict(zip('零一二三四五六七八九两', [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
            total = current = 0
            for character in value:
                if character in digits:
                    current = digits[character]
                else:
                    total += (current or 1) * {'十': 10, '百': 100}[character]
                    current = 0
            return total + current
    return 0


def file_info(item):
    values = item if isinstance(item, dict) else item._asdict()
    path = values.get('path') or values.get('server_filename', '')
    modified = float(values.get('server_mtime') or values.get('local_mtime') or values.get('mtime') or 0)
    if not math.isfinite(modified) or modified < 0:
        modified = 0
    return {'path': path, 'size': int(values.get('size') or 0), 'mtime': modified,
            'episode': episode_number(path), 'md5': values.get('md5') or '',
            'is_dir': bool(values.get('is_dir') or values.get('isdir'))}


def summarize(files, video_only=True, allow_empty=False):
    entries = [file_info(item) for item in files]
    season_rule = build_season_rule(entries, VIDEO_EXTENSIONS) if video_only else None
    if season_rule:
        excluded = set(season_rule['excluded_paths'])
        entries = [item for item in entries if item['path'] not in excluded]
    entries = [item for item in entries if not item['is_dir'] and
               (not video_only or posixpath.splitext(item['path'])[1].lower() in VIDEO_EXTENSIONS)]
    if not entries and not allow_empty:
        raise ValueError('分享内没有可比较的影片文件' if video_only else '分享为空')
    latest = max(entries, key=lambda item: (
        (season_identity(item['path']) or ('', 0))[1], item['episode'], item['mtime'], item['size']), default=None)
    fingerprint = hashlib.sha256(json.dumps(sorted(entries, key=lambda item: item['path']),
                                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'latest': latest, 'file_count': len(entries), 'fingerprint': fingerprint,
            'newest_mtime': max((item['mtime'] for item in entries), default=0), 'season_rule': season_rule}


def candidate_key(candidate):
    latest = candidate['latest']
    return (-latest['episode'], latest['mtime'] if latest['mtime'] > 0 else float('inf'), -latest['size'])


def definitely_invalid(error):
    text = str(error)
    codes = re.findall(r'(?:error_code|errno)[\s\'\"]*[:=][\s\'\"]*(-?\d+)', text)
    return bool({'-7', '115', '145'}.intersection(codes)) or any(
        phrase in text for phrase in ('分享链接已失效', '分享已取消', '分享已过期', '分享的文件已经被取消'))


class MediaService:
    def __init__(self, storage):
        self.storage = storage
        self.lock = RLock()

    def settings(self):
        saved = self.storage.config.get('media', {}).get('settings', {})
        settings = {key: saved.get(key, value) for key, value in DEFAULT_SETTINGS.items()}
        settings['max_links'] = min(settings['max_links'], MAX_MEDIA_LINKS)
        return settings

    def set_settings(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULT_SETTINGS):
            raise ValueError('支持的参数：save_dir、cron、max_results、max_links')
        updated = dict(self.settings(), **changes)
        if not isinstance(updated['save_dir'], str) or not updated['save_dir'].startswith('/'):
            raise ValueError('save_dir 必须是网盘绝对路径')
        if '..' in updated['save_dir'].split('/'):
            raise ValueError('保存目录不能包含 ..')
        CronTrigger.from_crontab(updated['cron'])
        for field, limit in (('max_results', 20), ('max_links', MAX_MEDIA_LINKS)):
            if type(updated[field]) is not int or not 1 <= updated[field] <= limit:
                raise ValueError(f'{field} 必须是 1 到 {limit} 的整数')
        with self.lock:
            self.storage.config.setdefault('media', {})['settings'] = updated
            self.storage._save_config(update_scheduler=False)
        return updated

    def search(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('影片名称长度应为 1 到 100 个字符')
        settings = self.settings()
        base_url = self.storage.config.get('media', {}).get('base_url', DEFAULT_BASE_URL)
        logger.info(f'影片搜索开始: keyword={name.strip()}, base_url={base_url}, limit={settings["max_results"]}')
        client = SeedHubClient(base_url)
        results = client.search(name, settings['max_results'])
        logger.info(f'影片搜索完成: keyword={name.strip()}, results={len(results)}')
        return {'results': results, 'message': self.search_message(results)}

    @staticmethod
    def search_message(results):
        if not results:
            return '未找到匹配影片，请换一个关键词搜索；未创建任务。'
        entries = []
        for index, item in enumerate(results, 1):
            entry = f"{index}. {item['title']} {item['info'] or '暂无年份/类型'} | 豆瓣 {item['rating']} | ID: {item['id']}"
            entries.append(' '.join(entry.split()))
        return '\n'.join([
            f'找到 {len(results)} 个匹配项目：',
            *entries,
            '发送“追剧 ID”选择对应影片，例如：追剧 ' + results[0]['id'],
        ])

    def subscribe(self, movie_id, schedule=None, progress=None):
        if not isinstance(movie_id, str) or not re.fullmatch(r'[0-9]+', movie_id.strip()):
            raise ValueError('追剧需要搜索结果中的数字 ID，例如：追剧 118943；请先发送“搜索 影片名”')
        movie_id = movie_id.strip()
        if not self.storage.client:
            logger.warning(f'追剧失败：百度客户端未登录: seedhub_id={movie_id}')
            raise ValueError('请先配置并登录百度网盘账号')
        settings = self.settings()
        cron, frequency = parse_update_schedule(schedule, settings['cron']) if schedule else (None, None)
        base_url = self.storage.config.get('media', {}).get('base_url', DEFAULT_BASE_URL)
        logger.info(f'追剧开始: seedhub_id={movie_id}, base_url={base_url}, max_links={settings["max_links"]}')
        client = SeedHubClient(base_url)
        name_holder = [f'影片 {movie_id}']
        candidates, rejected, resource_details = [], [], []
        parsed_links = 0
        for link in client.iter_links(movie_id, settings['max_links'], title_callback=lambda title: name_holder.__setitem__(0, title)):
            name = name_holder[0]
            parsed_links += 1
            logger.info(f'百度分享解析完成: seedhub_id={movie_id}, link_index={link.get("link_index")}, share={link["url"]}')
            try:
                logger.info(f'开始检查百度分享: seedhub_id={movie_id}, share={link["url"]}')
                for attempt in range(1, 4):
                    try:
                        files = self.storage.list_shared_files(link['url'], link['pwd'])
                        break
                    except Exception as error:
                        if attempt == 3 or definitely_invalid(error) or isinstance(error, ValueError):
                            raise
                        logger.warning(f'百度分享读取重试: seedhub_id={movie_id}, attempt={attempt}/3, {failure_details(error)}')
                        time.sleep(attempt * 2)
                summary = summarize(files)
                candidates.append(dict(link, **summary))
                latest = summary.get('latest') or {}
                logger.info(f'百度分享检查通过: seedhub_id={movie_id}, share={link["url"]}, files={summary["file_count"]}, episode={latest.get("episode", 0)}')
                updated_at = (datetime.fromtimestamp(latest['mtime'], BEIJING_TIMEZONE).strftime('%Y-%m-%d %H:%M')
                              if latest.get('mtime') else '未知')
                detail = f'{share_url_with_pwd(link["url"], link.get("pwd"))} | 有效 | 集数：{latest.get("episode", 0)} | 更新时间：{updated_at} | 文件数：{summary["file_count"]} | 大小：{format_size(latest.get("size"))}'
                detail += f' | 季数筛选：{summary["season_rule"]["description"]}'
                resource_details.append(detail)
            except Exception as error:
                logger.warning(f"检查影片资源失败: seedhub_id={movie_id}, share={link['url']}, {failure_details(error)}")
                reason = '分享已删除/取消' if definitely_invalid(error) else '无法完整读取影片文件'
                rejected.append({'url': link['url'], 'reason': reason})
                detail = f'{share_url_with_pwd(link["url"], link.get("pwd"))} | 无效 | 原因：{reason}'
                resource_details.append(detail)
            if parsed_links % 2 == 0:
                report_progress(progress, '\n'.join(
                    f'{index}. {detail}'
                    for index, detail in enumerate(resource_details[-2:], parsed_links - 1)))
        if parsed_links % 2:
            report_progress(progress, f'{parsed_links}. {resource_details[-1]}')
        name = name_holder[0]
        candidates.sort(key=candidate_key)
        best = candidates[0] if candidates else None
        if not best:
            logger.error(f'追剧失败：没有可用百度分享: seedhub_id={movie_id}, parsed_links={parsed_links}, rejected={len(rejected)}')
            raise ValueError(f'影片 ID {movie_id} 未找到可用百度资源；可能无百度链接、提取码错误或网盘访问失败')
        latest = best['latest']
        if cron is None:
            cron, frequency = default_media_schedule(latest)
            logger.info(f'未指定更新频率，按最新更新时间生成每日检测计划: seedhub_id={movie_id}, cron={cron}, label={frequency}')
        selected_detail = f'最终选择：{best["url"]}（最高第{latest["episode"]}集，最新文件 {latest.get("path", "未知")}，大小 {format_size(latest.get("size"))}）；决策顺序：各分享先排除同名旧季，再按集数最多 → 同集修改时间最早（未知时间靠后）→ 文件最大。'
        logger.info(f'有效链接 {len(candidates)}/{parsed_links} 个；{selected_detail}正在创建任务。')
        logger.info(f'已选择百度分享: seedhub_id={movie_id}, share={best["url"]}, episode={best["latest"]["episode"]}, files={best["file_count"]}')
        with self.lock:
            for task in self.storage.list_tasks():
                try:
                    same_url = normalize_share(task['url'])['url'] == best['url']
                except ValueError:
                    same_url = False
                if same_url:
                    task.setdefault('media', {})['resources'] = resource_details
                    task['media']['season_rule'] = best['season_rule']
                    if schedule is not None:
                        task['media']['update_frequency'] = frequency
                        task['cron'] = cron
                        from scheduler import TaskScheduler
                        scheduler = TaskScheduler.instance
                        if scheduler:
                            scheduler.update_task(task['url'], cron)
                    self.storage._save_config()
                    logger.info(f'追剧任务已存在: seedhub_id={movie_id}, task_uid={task.get("task_uid")}, share={best["url"]}')
                    return {'task': self.public_task(task), 'existing': True, 'selection': best,
                            'message': subscription_message(task, existing=True, checked_links=parsed_links,
                                                            valid_links=len(candidates))}
            settings = self.settings()
            folder_name = re.sub(r'[/\\\x00-\x1f]', '_', name.strip())
            if folder_name in ('.', '..'):
                raise ValueError('无效影片名称')
            save_dir = posixpath.join(settings['save_dir'], folder_name)
            logger.info(f'准备创建追剧任务: seedhub_id={movie_id}, name={name.strip()}, save_dir={save_dir}, cron={cron}')
            task_added = self.storage.add_task(best['url'], save_dir, pwd=best['pwd'], name=name.strip(), cron=cron)
            logger.info(f'追剧任务创建结果: seedhub_id={movie_id}, added={task_added}')
            if not task_added:
                logger.error(f'追剧失败：storage.add_task 返回 False: seedhub_id={movie_id}, share={best["url"]}')
                raise RuntimeError('创建影片转存任务失败')
            task = next((task for task in self.storage.list_tasks()
                         if task.get('url') == best['url']), None)
            if task is None:
                try:
                    normalized_best = normalize_share(best['url'])['url']
                except ValueError:
                    normalized_best = best['url']
                task = next((item for item in self.storage.list_tasks()
                             if item.get('url') == normalized_best), None)
            if task is None:
                logger.error(f'追剧失败：任务已添加但无法读取任务记录: seedhub_id={movie_id}, share={best["url"]}')
                raise RuntimeError('任务已创建，但读取任务记录失败，请在任务管理中检查')
            task['media'] = {'keyword': name.strip(), 'seedhub_id': movie_id, 'latest': best['latest'],
                             'update_frequency': frequency, 'resources': resource_details}
            self._record(task, best, time.time())
            self.storage._save_config(update_scheduler=False)
            logger.success(f'追剧任务创建完成: seedhub_id={movie_id}, task_uid={task.get("task_uid")}, name={name.strip()}')
            return {'task': self.public_task(task), 'existing': False, 'selection': best,
                    'message': subscription_message(task, checked_links=parsed_links, valid_links=len(candidates))}

    @staticmethod
    def public_task(task):
        return {field: task.get(field) for field in ('task_uid', 'name', 'url', 'save_dir', 'cron', 'status', 'media', 'share_watch')}

    def _record(self, task, summary, now):
        watch = task.setdefault('share_watch', {})
        changed = watch.get('fingerprint') != summary['fingerprint']
        if changed:
            watch['last_changed_at'] = now
        watch.update(fingerprint=summary['fingerprint'], last_checked_at=now,
                     newest_mtime=summary['newest_mtime'])
        if 'media' in task:
            task['media']['latest'] = summary['latest']
            task['media']['season_rule'] = summary.get('season_rule')
        return changed

    def maintain(self):
        if not self.storage.config.get('media', {}).get('cleanup_enabled', True) or not self.storage.client:
            return []
        if not self.lock.acquire(blocking=False):
            return []
        removed = []
        try:
            for selected in list(self.storage.list_tasks()):
                task = self.storage.get_task_by_uid(selected.get('task_uid'))
                if task is None or task.get('status') == 'running':
                    continue
                try:
                    files = self.storage.list_shared_files(task['url'], task.get('pwd'))
                    now = time.time()
                    summary = summarize(files, video_only='media' in task, allow_empty=True)
                    self._record(task, summary, now)
                    watch = task['share_watch']
                    last_update = max(watch['last_changed_at'], min(summary['newest_mtime'], now))
                    reason = '连续一周无更新' if now - last_update >= WEEK_SECONDS else None
                    self.storage._save_config(update_scheduler=False)
                except Exception as error:
                    if not definitely_invalid(error):
                        logger.warning(f"任务保留，无法确认分享状态: {task.get('name')} ({type(error).__name__})")
                        continue
                    reason = '百度网盘分享已失效'
                if reason and self.storage.remove_task(task['url']):
                    event = {'task_uid': task.get('task_uid'), 'name': task.get('name'), 'reason': reason}
                    removed.append(event)
                    self.notify('影片任务已删除', json.dumps(event, ensure_ascii=False))
            return removed
        finally:
            self.lock.release()

    def notify(self, title, content):
        from clawbot import push_message
        from notify import send

        try:
            if self.storage.config.get('notify', {}).get('enabled'):
                send(title, content)
        except Exception as error:
            logger.warning(f'影片通知失败: {type(error).__name__}')
        try:
            push_message(self.storage.config, title, content)
        except Exception as error:
            logger.warning(f'ClawBot 推送未确认成功: {type(error).__name__}')
        try:
            gateway = getattr(self.storage, 'wechat_gateway', None)
            if gateway:
                gateway.notify(title, content)
        except Exception as error:
            logger.warning(f'轻量微信推送未确认成功，原通知渠道不受影响: {type(error).__name__}')

    @staticmethod
    def task_list_message(tasks):
        if not tasks:
            return '当前没有追剧任务。'
        lines = ['当前追剧任务：']
        for task in tasks:
            lines.append(f'名称：{task.get("name") or "未命名"} | ID：{task.get("task_uid") or "无"}')
        return '\n'.join(lines)

    def remove_task_by_uid(self, task_uid):
        task_uid = task_uid.strip()
        if not task_uid:
            raise ValueError('请提供任务 ID，例如：删除任务 任务ID')
        task = self.storage.get_task_by_uid(task_uid)
        if not task:
            raise ValueError(f'未找到任务 ID：{task_uid}；请先发送“任务”查看当前任务 ID')
        if not self.storage.remove_task(task['url']):
            raise RuntimeError('删除任务失败，请在 Web 页面检查任务状态')
        logger.info(f'微信删除追剧任务: task_uid={task_uid}, name={task.get("name")}')
        return {'message': f'已删除任务：{task.get("name") or "未命名"} | ID：{task_uid}'}

    def command(self, text, progress=None):
        if not isinstance(text, str) or len(text) > 1000:
            raise ValueError('请输入不超过 1000 字的命令')
        command, _, argument = text.strip().partition(' ')
        argument = argument.strip()
        if command in ('帮助', 'help'):
            return {'message': '\r\n'.join((
                '可用命令：',
                '搜索 影片名：搜索影片并返回匹配 ID',
                '追剧 ID [更新频率]：按 ID 创建追剧任务',
                '  例如：追剧 118943 每周六周日6-8点',
                '  不写频率：按最新更新时间前后两小时，每天每30分钟检测',
                '任务：查看任务名称和唯一 ID',
                '删除任务 ID：删除指定追剧任务',
                '参数：查看追剧参数',
                '设置 参数名 值：修改追剧参数',
                '检查更新：立即检查任务分享状态',
            ))}
        if command == '搜索':
            return self.search(argument)
        if command in ('追剧', '获取影片'):
            movie_id, _, schedule = argument.partition(' ')
            return self.subscribe(movie_id, schedule=schedule.strip() or None, progress=progress)
        if command == '任务':
            tasks = self.storage.list_tasks()
            return {'tasks': [self.public_task(task) for task in tasks], 'message': self.task_list_message(tasks)}
        if command in ('删除任务', '删除'):
            return self.remove_task_by_uid(argument)
        if command == '参数':
            return self.settings()
        if command == '设置':
            key, _, value = argument.partition(' ')
            if key in ('max_results', 'max_links'):
                value = int(value)
            return self.set_settings({key: value})
        if command == '检查更新':
            return {'removed': self.maintain(), 'message': '已检查分享状态；转存按任务定时规则执行'}
        raise ValueError('未知命令，发送“帮助”查看用法')
