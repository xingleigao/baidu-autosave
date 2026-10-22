"""管理页面和其他应用使用的微信接口。"""

from functools import wraps

from flask import Blueprint, jsonify, request
from loguru import logger


def create_wechat_blueprint(get_storage, login_required):
    blueprint = Blueprint('wechat', __name__, url_prefix='/api/wechat')

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请求必须为 JSON 对象')
        return data

    def endpoint(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if request.method != 'GET':
                origin = request.headers.get('Origin')
                if origin and origin != request.host_url.rstrip('/'):
                    return jsonify(success=False, message='不允许跨站管理请求'), 403
                if not request.is_json:
                    return jsonify(success=False, message='请使用 JSON 请求'), 415
            storage = get_storage()
            gateway = getattr(storage, 'wechat_gateway', None)
            if gateway is None:
                return jsonify(success=False, message='微信网关未初始化，请检查依赖和日志'), 503
            try:
                return function(gateway, *args, **kwargs)
            except PermissionError:
                return jsonify(success=False, message='应用令牌无效'), 401
            except ValueError as error:
                return jsonify(success=False, message=str(error)), 400
            except Exception as error:
                logger.warning(f'微信接口失败: {type(error).__name__}')
                return jsonify(success=False, message='微信操作失败，请检查网络、登录状态和会话额度'), 502
        return wrapped

    @blueprint.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    @blueprint.get('/status')
    @login_required
    @endpoint
    def status(gateway):
        return jsonify(success=True, data=gateway.status())

    @blueprint.post('/settings')
    @login_required
    @endpoint
    def settings(gateway):
        return jsonify(success=True, data=gateway.settings(request.get_json(silent=True)))

    @blueprint.post('/login')
    @login_required
    @endpoint
    def login(gateway):
        return jsonify(success=True, data=gateway.start_login(body().get('account_id')))

    @blueprint.post('/logout')
    @login_required
    @endpoint
    def logout(gateway):
        return jsonify(success=True, data=gateway.logout(body().get('account_id')))

    @blueprint.post('/verify')
    @login_required
    @endpoint
    def verify(gateway):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请求必须为 JSON 对象')
        gateway.submit_verify_code(data.get('code'))
        return jsonify(success=True)

    @blueprint.post('/test')
    @login_required
    @endpoint
    def test(gateway):
        gateway.send_text('百度网盘微信网关连接测试。若收到此消息，文本发送链路正常。',
                          account_id=body().get('account_id'))
        return jsonify(success=True, message='微信接口已接受发送，请确认手机收到消息')

    @blueprint.post('/accounts')
    @login_required
    @endpoint
    def create_account(gateway):
        return jsonify(success=True, data=gateway.create_account(body().get('name')))

    @blueprint.post('/accounts/<identifier>/default')
    @login_required
    @endpoint
    def default_account(gateway, identifier):
        gateway.set_default_account(identifier)
        return jsonify(success=True)

    @blueprint.delete('/accounts/<identifier>')
    @login_required
    @endpoint
    def remove_account(gateway, identifier):
        gateway.remove_account(identifier)
        return jsonify(success=True)

    @blueprint.post('/apps')
    @login_required
    @endpoint
    def create_application(gateway):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请求必须为 JSON 对象')
        return jsonify(success=True, data=gateway.create_application(data.get('name'), data.get('account_id')))

    @blueprint.delete('/apps/<identifier>')
    @login_required
    @endpoint
    def revoke_application(gateway, identifier):
        gateway.revoke_application(identifier)
        return jsonify(success=True)

    @blueprint.post('/send')
    @endpoint
    def send(gateway):
        gateway.send_for_application(request.headers.get('Authorization', ''), request.get_json(silent=True))
        return jsonify(success=True, message='微信接口已接受发送，不代表手机已送达')

    return blueprint
