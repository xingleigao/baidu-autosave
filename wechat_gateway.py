"""嵌入式微信通信网关，使用固定版本 WeiLink 管理会话与消息上下文。"""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid

import requests
from loguru import logger
from seedhub import SeedHubError, failure_details


LOGIN_BASE = 'https://ilinkai.weixin.qq.com'


def trusted_base_url(value):
    parsed = urlparse(value)
    hostname = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.port not in (None, 443) or parsed.query or parsed.fragment or
            parsed.path not in ('', '/') or
            not (hostname == 'weixin.qq.com' or hostname.endswith('.weixin.qq.com'))):
        raise ValueError('微信服务地址不在允许范围内')
    return value.rstrip('/')


class WeChatGateway:
    def __init__(self, storage, start_transfer, directory='config/wechat'):
        self.storage = storage
        self.start_transfer = start_transfer
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.state_path = self.directory / 'gateway.json'
        self.lock = threading.RLock()
        self.client_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.commands = queue.Queue(maxsize=20)
        self.login_thread = None
        self.login_generation = 0
        self.verify_code = ''
        self.login = {'status': 'idle', 'qr_image': '', 'message': ''}
        self.last_error = ''
        self.rate_limits = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
        else:
            self.state = {'enabled': False, 'notifications': False, 'apps': {}, 'seen': []}
        if 'accounts' not in self.state:
            self.state['accounts'] = {'default': {'name': '原微信账号'}}
        self.state.setdefault('default_account', next(iter(self.state['accounts']), ''))
        self.accounts = {}
        self.available = False
        try:
            for identifier in self.state['accounts']:
                self.accounts[identifier] = self._new_account(identifier)
            for application in self.state['apps'].values():
                application.setdefault('account_id', self.state['default_account'])
            self.available = True
            with self.lock:
                self._save()
        except ImportError:
            self.last_error = '缺少依赖，请安装 requirements.txt 后重启'
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name='wechat-poll')
        self.command_thread = threading.Thread(target=self._command_loop, daemon=True, name='wechat-command')

    def start(self):
        if self.available:
            self.poll_thread.start()
            self.command_thread.start()

    def close(self):
        self.stop_event.set()
        with self.lock:
            self.login_generation += 1
            self.verify_code = ''
            workers = [self.poll_thread] + [account.worker for account in self.accounts.values() if account.worker]
        deadline = time.monotonic() + 8
        for worker in workers:
            if worker.is_alive() and worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))

    def _save(self):
        temporary = self.state_path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(self.state, output, ensure_ascii=False, indent=2)
        temporary.replace(self.state_path)

    def _new_account(self, identifier):
        from weilink import WeiLink
        directory = self.directory / 'session' if identifier == 'default' else self.directory / 'accounts' / identifier
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        return SimpleNamespace(client=WeiLink(base_path=directory), expired=False, generation=0, error='',
                               retry_after=0, io_lock=threading.RLock(), worker=None)

    def _account(self, account_id=None):
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise ValueError('微信账号标识必须为非空字符串')
        identifier = account_id if account_id is not None else self.state['default_account']
        account = self.accounts.get(identifier)
        if account is None:
            raise ValueError('微信账号不存在，请在设置页添加账号')
        return identifier, account

    @property
    def client(self):
        account = self.accounts.get(self.state['default_account'])
        return account.client if account else None

    @property
    def expired(self):
        account = self.accounts.get(self.state['default_account'])
        return account.expired if account else False

    def _session(self, account_id=None):
        account = self.accounts.get(account_id if account_id is not None else self.state['default_account'])
        return account.client._default_session if account else None

    def _owner(self, account_id=None):
        session = self._session(account_id)
        return session.bot_info.user_id if session and session.bot_info else ''

    def status(self):
        with self.lock:
            session = self._session()
            return {
                'available': self.available,
                'enabled': self.state['enabled'],
                'notifications': self.state['notifications'],
                'connected': bool(session and session.bot_info and not self.expired),
                'owner': self._owner(),
                'bot_id': session.bot_info.bot_id if session and session.bot_info else '',
                'login': dict(self.login),
                'last_error': self.last_error,
                'default_account': self.state['default_account'],
                'accounts': [
                    {'id': identifier, 'name': metadata['name'], 'owner': self._owner(identifier),
                     'bot_id': self._session(identifier).bot_info.bot_id if self._owner(identifier) else '',
                     'connected': bool(self._owner(identifier) and not self.accounts[identifier].expired),
                     'last_error': self.accounts[identifier].error,
                     'is_default': identifier == self.state['default_account']}
                    for identifier, metadata in self.state['accounts'].items() if identifier in self.accounts
                ],
                'apps': [{'id': identifier, 'name': application['name'],
                          'account_id': application.get('account_id', '')}
                         for identifier, application in self.state['apps'].items()],
            }

    def settings(self, values):
        if not isinstance(values, dict):
            raise ValueError('请求必须为 JSON 对象')
        with self.lock:
            for key in ('enabled', 'notifications'):
                if key in values:
                    if not isinstance(values[key], bool):
                        raise ValueError('开关必须为布尔值')
                    self.state[key] = values[key]
            self._save()
        return self.status()

    def _require_client(self):
        if not self.available:
            raise ValueError('微信依赖未安装，请更新依赖并重启')

    def create_account(self, name):
        self._require_client()
        if not isinstance(name, str) or not name.strip() or len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError('账号名称需为 1–40 字且不含控制字符')
        with self.client_lock, self.lock:
            if len(self.accounts) >= 10:
                raise ValueError('最多添加 10 个微信账号')
            identifier = uuid.uuid4().hex
            self.accounts[identifier] = self._new_account(identifier)
            self.state['accounts'][identifier] = {'name': name.strip()}
            if not self.state['default_account']:
                self.state['default_account'] = identifier
            self._save()
        return {'id': identifier, 'name': name.strip()}

    def set_default_account(self, account_id):
        with self.lock:
            identifier, account = self._account(account_id)
            self.state['default_account'] = identifier
            self._save()

    def start_login(self, account_id=None):
        self._require_client()
        with self.lock:
            identifier, account = self._account(account_id)
            if (self.login_thread and self.login_thread.is_alive() and
                    self.login['status'] in ('loading', 'wait', 'scaned', 'scaned_but_redirect', 'need_verifycode')):
                raise ValueError('登录正在进行，请等待完成或先退出登录')
            if self._owner(identifier) and not account.expired:
                raise ValueError('已登录，请先退出再切换账号')
            self.login_generation += 1
            self.verify_code = ''
            generation = self.login_generation
            self.login = {'status': 'loading', 'qr_image': '', 'message': '正在获取二维码', 'account_id': identifier}
            self.login_thread = threading.Thread(target=self._login_loop, args=(generation, identifier), daemon=True)
            self.login_thread.start()
        return self.status()

    def _current_login(self, generation):
        return not self.stop_event.is_set() and generation == self.login_generation

    def submit_verify_code(self, code):
        if not isinstance(code, str) or not code.isascii() or not code.isdigit() or not 1 <= len(code) <= 16:
            raise ValueError('请输入微信手机端显示的数字，最多 16 位')
        self._rate_limit('verify', 5)
        with self.lock:
            if self.login['status'] != 'need_verifycode' or not self.login_thread or not self.login_thread.is_alive():
                raise ValueError('当前没有等待配对码的登录，请重新获取二维码')
            self.verify_code = code
            self.login['message'] = '配对码已提交，等待微信验证'

    def _login_loop(self, generation, account_id=None):
        try:
            identifier, account = self._account(account_id)
            from weilink.admin.handlers import _qr_to_svg
            from weilink.models import BotInfo
            headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str(0x02040A),
                       'AuthorizationType': 'ilink_bot_token',
                       'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()}
            response = requests.post(LOGIN_BASE + '/ilink/bot/get_bot_qrcode',
                                     params={'bot_type': '3'}, json={'local_token_list': []},
                                     headers=headers, timeout=15, allow_redirects=False)
            response.raise_for_status()
            result = response.json()
            code = result.get('qrcode')
            content = result.get('qrcode_img_content')
            if not isinstance(code, str) or not isinstance(content, str) or not code or not content:
                raise ValueError('微信未返回有效二维码')
            image = _qr_to_svg(content)
            with self.lock:
                if not self._current_login(generation):
                    return
                self.login = {'status': 'wait', 'qr_image': image, 'message': '请使用微信扫码并确认', 'account_id': identifier}
            base_url = LOGIN_BASE
            deadline = time.monotonic() + 300
            while self._current_login(generation) and time.monotonic() < deadline:
                try:
                    with self.lock:
                        if not self._current_login(generation) or self.accounts.get(identifier) is not account:
                            return
                        verify_code = self.verify_code
                        if self.login['status'] == 'need_verifycode' and not verify_code:
                            needs_code = True
                        else:
                            needs_code = False
                            self.verify_code = ''
                    if needs_code:
                        self.stop_event.wait(1)
                        continue
                    parameters = {'qrcode': code}
                    if verify_code:
                        parameters['verify_code'] = verify_code
                    response = requests.get(base_url + '/ilink/bot/get_qrcode_status',
                                            params=parameters, headers=headers,
                                            timeout=35, allow_redirects=False)
                    response.raise_for_status()
                    result = response.json()
                except requests.Timeout:
                    continue
                status = result.get('status', 'wait')
                if status == 'scaned_but_redirect':
                    base_url = trusted_base_url('https://' + result.get('redirect_host', ''))
                elif status == 'confirmed':
                    token = result.get('bot_token')
                    owner = result.get('ilink_user_id')
                    bot_id = result.get('ilink_bot_id')
                    if not all(isinstance(value, str) and value for value in (token, owner, bot_id)):
                        raise ValueError('微信未返回完整登录凭证和本人标识')
                    service_url = trusted_base_url(result.get('baseurl') or LOGIN_BASE)
                    with self.client_lock, account.io_lock, self.lock:
                        if not self._current_login(generation) or self.accounts.get(identifier) is not account:
                            return
                        if any(other_id != identifier and self._owner(other_id) == owner for other_id in self.accounts):
                            self.login = {'status': 'error', 'qr_image': '', 'account_id': identifier,
                                          'message': '此微信已在其他账号中登录，请使用其他微信扫码'}
                            return
                        session = self._session(identifier)
                        session.bot_info = BotInfo(bot_id=bot_id, base_url=service_url, token=token, user_id=owner)
                        session.cursor = ''
                        session.context_tokens.clear()
                        session.context_timestamps.clear()
                        session.send_counts.clear()
                        session.send_timestamps.clear()
                        session.user_first_seen.clear()
                        session.contexts_path.unlink(missing_ok=True)
                        with account.client._data_lock:
                            account.client._save_session_state(session)
                            account.client._save_session_contexts(session)
                        for filename in session.token_path.parent.glob('*.json'):
                            filename.chmod(0o600)
                        account.expired = False
                        account.generation += 1
                        account.error = ''
                        self.last_error = ''
                        self.state['enabled'] = True
                        self._save()
                        self.login = {'status': 'confirmed', 'qr_image': '', 'account_id': identifier,
                                      'message': '登录成功，请先在微信发送“帮助”以建立会话'}
                    return
                elif status == 'need_verifycode':
                    with self.lock:
                        if self._current_login(generation):
                            self.login['status'] = status
                            self.login['message'] = '请在此页面输入手机微信显示的数字配对码' if not verify_code else '配对码不匹配，请重新输入手机微信显示的数字'
                elif status in ('expired', 'verify_code_blocked', 'binded_redirect'):
                    messages = {'expired': '二维码已过期，请重新获取',
                                'verify_code_blocked': '微信验证受限，请稍后重试',
                                'binded_redirect': '已在其他客户端绑定，请先解除原绑定后重试'}
                    with self.lock:
                        if self._current_login(generation):
                            self.login = {'status': status, 'qr_image': '', 'message': messages[status]}
                    return
                else:
                    with self.lock:
                        if self._current_login(generation):
                            self.login['status'] = status
                            self.login['message'] = '已扫码，请在微信确认' if status == 'scaned' else '等待扫码确认'
                self.stop_event.wait(1)
            with self.lock:
                if self._current_login(generation):
                    self.login = {'status': 'expired', 'qr_image': '', 'message': '登录超时，请重新获取'}
        except Exception as error:
            logger.warning(f'微信登录失败: {type(error).__name__}')
            with self.lock:
                if self._current_login(generation):
                    self.login = {'status': 'error', 'qr_image': '', 'message': '登录失败，请检查网络或微信服务状态'}

    def logout(self, account_id=None):
        self._require_client()
        with self.client_lock:
            identifier, account = self._account(account_id)
            with account.io_lock, self.lock:
                if self.login.get('account_id') == identifier:
                    self.login_generation += 1
                    self.verify_code = ''
                    self.login = {'status': 'idle', 'qr_image': '', 'message': '已清除该账号本地登录凭证'}
                account.client.logout()
                replacement = self._new_account(identifier)
                replacement.generation = account.generation + 1
                self.accounts[identifier] = replacement
        return self.status()

    def remove_account(self, account_id):
        self._require_client()
        with self.client_lock:
            identifier, account = self._account(account_id)
            self.logout(identifier)
            with self.lock:
                del self.accounts[identifier]
                del self.state['accounts'][identifier]
                revoked = [key for key, application in self.state['apps'].items() if application['account_id'] == identifier]
                for key in revoked:
                    del self.state['apps'][key]
                if self.state['default_account'] == identifier:
                    self.state['default_account'] = next(iter(self.accounts), '')
                self._save()

    def _rate_limit(self, identifier, maximum):
        now = time.monotonic()
        with self.lock:
            recent = [timestamp for timestamp in self.rate_limits.get(identifier, []) if now - timestamp < 60]
            if len(recent) >= maximum:
                raise ValueError('操作过于频繁，请一分钟后重试')
            self.rate_limits[identifier] = recent + [now]

    def _poll_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                for identifier, account in self.accounts.items():
                    if account.worker is None or not account.worker.is_alive():
                        account.worker = threading.Thread(target=self._account_loop, args=(identifier, account),
                                                          daemon=True, name='wechat-' + identifier[:8])
                        account.worker.start()
            self.stop_event.wait(1)

    def _account_loop(self, identifier, account):
        while not self.stop_event.is_set() and self.accounts.get(identifier) is account:
            self._poll_account(identifier, account)
            self.stop_event.wait(0.5)

    def _poll_account(self, identifier, expected_account=None):
        from weilink import SessionExpiredError
        with self.lock:
            account = self.accounts.get(identifier)
            if expected_account is not None and account is not expected_account:
                return
            if (not self.state['enabled'] or not account or account.expired or not self._owner(identifier) or
                    time.monotonic() < account.retry_after):
                return
            generation = account.generation
        try:
            with account.io_lock:
                if self.accounts.get(identifier) is not account:
                    return
                session = self._session(identifier)
                session.longpoll_timeout = 1
                session.consecutive_failures = 0
                messages = account.client.recv(timeout=1)
            account.error = ''
            for message in messages:
                with self.lock:
                    if (self.stop_event.is_set() or self.accounts.get(identifier) is not account or generation != account.generation or
                            not self.state['enabled'] or message.from_user != self._owner(identifier) or
                            not message.text or int(message.msg_type) != 1):
                        continue
                    identity = str(message.message_id) if message.message_id is not None else hashlib.sha256(
                        f'{message.from_user}:{message.timestamp}:{message.context_token}:{message.text}'.encode()).hexdigest()
                    identity = identifier + ':' + identity
                    if identity in self.state['seen']:
                        continue
                    self.state['seen'] = (self.state['seen'] + [identity])[-2000:]
                    self._save()
                    try:
                        self._rate_limit('commands:' + identifier, 10)
                        self.commands.put_nowait((identifier, message.from_user, generation, message.text))
                    except (ValueError, queue.Full):
                        account.error = '微信指令过于频繁，已跳过；请稍后重试'
        except SessionExpiredError:
            if self.accounts.get(identifier) is account and generation == account.generation:
                account.expired = True
                account.error = '微信会话已失效，请在设置页为此账号重新扫码'
        except Exception as error:
            account.error = '微信收取失败，正在重连（' + type(error).__name__ + '）'
            account.retry_after = time.monotonic() + 5

    def _command_loop(self):
        while not self.stop_event.is_set():
            try:
                identifier, owner, generation, text = self.commands.get(timeout=1)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    account = self.accounts.get(identifier)
                    if (self.stop_event.is_set() or not self.state['enabled'] or account is None or generation != account.generation or
                            owner != self._owner(identifier)):
                        continue
                text = text.strip()
                if text.startswith('网盘 '):
                    text = text[3:].strip()
                command = text.partition(' ')[0]
                progress_sent = 0
                progress_failed = False

                def send_progress(message):
                    nonlocal progress_sent, progress_failed
                    if progress_failed or progress_sent >= 9:
                        return
                    try:
                        self.send_text(message, expected_owner=owner, expected_generation=generation, account_id=identifier)
                        progress_sent += 1
                    except Exception as error:
                        progress_failed = True
                        logger.warning(f'微信进度发送失败，继续执行任务并保留最终回复: {failure_details(error)}')

                result = (self.storage.media_service.command(text, progress=send_progress)
                          if command in ('追剧', '获取影片') else self.storage.media_service.command(text))
                if command in ('追剧', '获取影片'):
                    logger.info(f'微信追剧命令完成，准备首次转存: account_id={identifier}, task_uid={result.get("task", {}).get("task_uid")}, existing={result.get("existing")}')
                    self.start_transfer(result)
                reply = (result['message'] if command in ('帮助', 'help', '搜索', '追剧', '获取影片', '任务', '删除任务', '删除')
                         else json.dumps(result, ensure_ascii=False, indent=2))
            except SeedHubError as error:
                logger.warning(f'微信影片搜索失败: {error}')
                reply = str(error)
            except ValueError as error:
                logger.warning(f'微信命令无法执行: command={text.partition(" ")[0]}, {failure_details(error)}')
                reply = str(error)
            except (TypeError, KeyError) as error:
                logger.warning(f'微信命令无法执行: command={text.partition(" ")[0]}, {failure_details(error)}')
                reply = '命令无法执行。发送“帮助”查看用法，并检查参数和网盘账号。'
            except Exception as error:
                logger.warning(f'微信指令执行失败: command={text.partition(" ")[0]}, {failure_details(error)}')
                reply = ('搜索失败，未创建任务。请检查搜索源和网络，或查看服务日志。' if text.partition(' ')[0] == '搜索'
                         else '执行失败，请在 Web 页面检查任务状态，不要重复创建。')
            finally:
                self.commands.task_done()
            try:
                self.send_text(reply[:3500] + ('\n内容已截断，请在 Web 查看完整结果。' if len(reply) > 3500 else ''),
                               expected_owner=owner, expected_generation=generation, account_id=identifier)
            except Exception as error:
                account.error = '微信回复未确认成功（' + type(error).__name__ + '），请在 Web 查看执行结果'

    def send_text(self, text, expected_owner=None, expected_generation=None, account_id=None):
        self._require_client()
        from weilink import QuotaExhaustedError, SessionExpiredError
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 12000:
            raise ValueError('消息不能为空，且 UTF-8 长度不能超过 12000 字节')
        identifier, account = self._account(account_id)
        with account.io_lock:
            if self.accounts.get(identifier) is not account:
                raise ValueError('登录会话已切换，取消发送')
            if self.stop_event.is_set() or not self.state['enabled'] or account.expired or not self._owner(identifier):
                raise ValueError('微信未启用或未登录，请先在设置页扫码')
            if ((expected_owner is not None and expected_owner != self._owner(identifier)) or
                    (expected_generation is not None and expected_generation != account.generation)):
                raise ValueError('登录会话已切换，取消旧回复')
            try:
                if not account.client.send(self._owner(identifier), text, auto_recv=False):
                    raise ValueError('微信发送未确认成功，请先向机器人发送消息刷新会话后重试')
            except SessionExpiredError:
                account.expired = True
                account.error = '微信会话已失效，请在设置页为此账号重新扫码'
                raise ValueError(account.error) from None
            except QuotaExhaustedError:
                raise ValueError('本次微信会话回复额度已用完，请先向机器人再发一条消息') from None
        return True

    def notify(self, title, content):
        if not self.state['enabled'] or not self.state['notifications']:
            return False
        message = f'{title}\n{content}'
        if len(message) > 3500:
            message = message[:3500] + '\n内容已截断，请在 Web 查看详情。'
        with self.lock:
            identifiers = [identifier for identifier in self.accounts if self._owner(identifier)]
        accepted = False
        for identifier in identifiers:
            try:
                accepted = self.send_text(message, account_id=identifier) or accepted
            except Exception as error:
                with self.lock:
                    if identifier in self.accounts:
                        self.accounts[identifier].error = '微信通知未确认成功（' + type(error).__name__ + '）'
                logger.warning(f'微信账号通知失败: {identifier} ({type(error).__name__})')
        return accepted

    def create_application(self, name, account_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError('应用名称需为 1–40 字且不含控制字符')
        with self.lock:
            recipient, account = self._account(account_id)
            if len(self.state['apps']) >= 20:
                raise ValueError('最多创建 20 个应用，请先撤销不用的应用')
            identifier = uuid.uuid4().hex
            token = 'wxg_' + secrets.token_urlsafe(32)
            self.state['apps'][identifier] = {'name': name.strip(), 'hash': hashlib.sha256(token.encode()).hexdigest(),
                                             'account_id': recipient}
            self._save()
        return {'id': identifier, 'name': name.strip(), 'token': token, 'account_id': recipient}

    def revoke_application(self, identifier):
        with self.lock:
            self.state['apps'].pop(identifier, None)
            self._save()

    def send_for_application(self, authorization, data):
        supplied = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else ''
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        with self.lock:
            match = next(((identifier, dict(application)) for identifier, application in self.state['apps'].items()
                          if supplied and hmac.compare_digest(application['hash'], digest)), None)
        if match is None:
            raise PermissionError('应用令牌无效')
        identifier, application = match
        self._rate_limit(identifier, 10)
        if not isinstance(data, dict) or not isinstance(data.get('text'), str) or not data['text'].strip():
            raise ValueError('请求必须包含非空 text 字段')
        return self.send_text(f'[{application["name"]}]\n{data["text"]}', account_id=application['account_id'])
