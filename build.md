# Docker 构建与 ClawBot 接入说明

推荐的轻量接法：构建并重建容器后，在 **系统设置 → 微信网关** 中直接扫码，可添加多个账号供可信家人分别扫码、共用任务，无需另装 OpenClaw；详见 `integrations/wechat/README.md`。原微信公众号通知仍在“通知设置”中配置，网关推送任务通知默认关闭。下方第 5 节仅供已有 OpenClaw 的用户兼容接入使用。


## 1. 普通本机构建

在项目根目录执行：

```bash
docker build -t baidu-autosave:local .
```

现有 Dockerfile 会完成前端构建、Python 依赖安装和最终镜像打包，无需先手动构建前端。新增的 `seedhub.py`、`media.py`、`clawbot.py` 会通过 `COPY *.py ./` 打包进去。

SeedHub 已统一按 `test.py` 使用 `curl_cffi` 的 `safari17_0` 指纹和 iPhone Safari 请求头，无需 Chrome/Chromium 或其他浏览器依赖。更新后重新构建镜像即可安装依赖；宿主机运行前先执行 `python -m pip install -r requirements.txt`，再用 `python seedhub.py search 地球超新鲜` 或 `python seedhub.py links 138432` 测试。旧配置中的 `request_mode` 不再生效，`media.base_url` 保持不变（如使用已验证的域名，可设为 `https://sidhub.cc`）。

启动：

```bash
mkdir -p config log

docker run -d \
  --name baidu-autosave \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/config:/app/config" \
  -v "$PWD/log:/app/log" \
  baidu-autosave:local
```

查看启动日志：

```bash
docker logs -f baidu-autosave
```

现有 `docker-compose.yml` 使用远端镜像，不会自动构建当前修改的代码。若使用 Compose 本地构建，将服务配置改为：

```yaml
services:
  baidu-autosave:
    build: .
    image: baidu-autosave:local
    container_name: baidu-autosave
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config
      - ./log:/app/log
    environment:
      TZ: Asia/Shanghai
```

然后执行：

```bash
docker compose up -d --build
```

## 2. x86 机器通过 QEMU 构建 ARM64

本项目可以用 Docker Buildx + QEMU 构建 ARM64 镜像。ARM64 机器原生构建不需要 QEMU；模拟构建通常比原生构建慢。项目现有 GitHub Actions 也配置了 QEMU，并构建 `linux/amd64,linux/arm64`。

以下适用于 Linux Docker Engine。先注册 ARM64 模拟器，再创建 Buildx 构建器；注册模拟器需要特权容器权限。Docker Desktop 通常已内置模拟支持，可跳过注册步骤。

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64

docker buildx create \
  --name baidu-multiarch \
  --driver docker-container \
  --use

docker buildx inspect --bootstrap
```

如果同名构建器已经存在，使用：

```bash
docker buildx use baidu-multiarch
```

构建并加载单架构 ARM64 镜像到本机 Docker：

```bash
docker buildx build \
  --platform linux/arm64 \
  -t baidu-autosave:arm64 \
  --load \
  .
```

`--platform` 指定目标架构，`--load` 将构建结果加载到本机镜像库。这是真正构建 ARM64 镜像，不是把 amd64 镜像改个标签。

若需要搬到另一台 ARM64 机器运行：

```bash
# 构建机导出
docker save -o baidu-autosave-arm64.tar baidu-autosave:arm64

# 将 tar 文件复制到 ARM64 机器后执行
docker load -i baidu-autosave-arm64.tar
```

随后使用第一节的启动命令，将镜像名换为 `baidu-autosave:arm64`。

## 3. 同时构建 amd64、arm64 并推送

多架构镜像推送到仓库后，不同架构的客户端可以使用同一标签，选择匹配的架构。

```bash
docker login

# 将 YOUR_DOCKERHUB_USER 替换为你的 Docker Hub 用户名
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t YOUR_DOCKERHUB_USER/baidu-autosave:latest \
  --push \
  .
```

检查仓库中的架构：

```bash
docker buildx imagetools inspect \
  YOUR_DOCKERHUB_USER/baidu-autosave:latest
```

这里使用 `--push`，而不是依赖传统本地镜像库加载多架构结果；不同 Docker 镜像存储后端的多架构加载能力存在差异。

## 4. 项目注意事项

- Dockerfile 使用 Python 3.10，建议先保持不变，避免百度客户端依赖兼容问题。
- QEMU 解决跨架构执行，但不保证依赖一定编译成功。若 `baidupcs-py`、`gevent` 或 `selectolax` 安装失败，需要根据日志处理。本文编写时未实际验证 ARM64 镜像构建。
- 已有 `config/config.json` 时，启动脚本不会自动合并新字段。需手动合并 `media`、`clawbot`，保留账号及任务。
- 当前镜像只打包后端，OpenClaw 技能需要单独安装到 OpenClaw 所在环境。
- 容器内的 `127.0.0.1` 指向容器自身，不是宿主机或其他容器。

建议先构建并验证单架构 ARM64 镜像，再做多架构发布。

## 5. 构建启动后连接微信 ClawBot

### 5.1 前提与连接方向

本项目不会自动安装 ClawBot、登录微信或提供微信扫码页面。以下接法要求你已有基于 OpenClaw 的机器人，已安装微信渠道插件，并能正常收发消息。若 ClawBot 不是基于 OpenClaw，需要自行适配其消息接口。

有两条独立链路，都应验证：

```text
微信指令 → OpenClaw → baidu-autosave 技能 → 后端 :5000/api/clawbot/message
转存/清理事件 → 后端 → OpenClaw :18789/hooks/agent → 微信接收人
```

### 5.2 配置后端

先通过 Web 页面配置可用的百度网盘账号。编辑 Docker 挂载的宿主机 `config/config.json`，合并以下字段，不要覆盖原有内容。修改前先备份，并尽量避免与 Web 配置修改同时进行。

```json
{
  "media": {
    "base_url": "https://www.seedhub.cc",
    "cleanup_enabled": true,
    "settings": {
      "save_dir": "/追剧",
      "cron": "0 */6 * * *",
      "max_results": 5,
      "max_links": 18
    }
  },
  "clawbot": {
    "enabled": true,
    "api_token": "替换为随机生成的后端命令令牌",
    "gateway_url": "http://OPENCLAW可达地址:18789",
    "hook_token": "替换为OpenClaw的hooks.token",
    "agent_id": "main",
    "channel": "替换为微信插件的实际渠道标识",
    "account_id": "",
    "to": "替换为微信插件认可的接收人ID"
  }
}
```

生成随机令牌，可执行两次分别用于 `api_token`、`hook_token`：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

- `api_token`：供 OpenClaw 技能访问本项目，与后文 `BAIDU_AUTOSAVE_TOKEN` 相同。
- `hook_token`：供本项目访问 OpenClaw，与 OpenClaw 的 `hooks.token` 相同；不要使用 Gateway 登录令牌，也不要与 `api_token` 共用。
- `channel`、`to`：从实际微信插件的配置、会话信息或文档获取，不能统一假设渠道叫 `wechat`，也不能默认普通微信号就是接收人 ID。
- `account_id`：可留空使用渠道默认账号；多账号场景按插件实际账号填写。
- `agent_id`：须为实际存在并允许通过 Webhook 调用的 agent；不是 `main` 时同步修改两端。

修改完成后重启后端：

```bash
docker restart baidu-autosave
```

### 5.3 配置 OpenClaw Webhook

将以下内容合并到 OpenClaw 实际使用的配置文件中，保留原有渠道等配置：

```json
{
  "hooks": {
    "enabled": true,
    "token": "与后端clawbot.hook_token相同",
    "path": "/hooks",
    "allowedAgentIds": ["main"],
    "allowRequestSessionKey": false
  }
}
```

按你的部署方式重启 OpenClaw Gateway。确保其监听地址和网络策略允许后端访问；若仅监听 OpenClaw 自身的回环地址，其他容器无法连接。不要直接将未加固的 Gateway 或后端暴露到公网；跨机器优先使用可信私网或经过鉴权的 HTTPS。

### 5.4 安装技能并设置环境变量

将本仓库 `integrations/openclaw/baidu-autosave` 文件夹完整复制到目标 agent 的 `workspace/skills/` 下。以下示例假定使用默认工作区，并且在能访问该工作区的机器上执行：

```bash
mkdir -p ~/.openclaw/workspace/skills
cp -R integrations/openclaw/baidu-autosave ~/.openclaw/workspace/skills/
```

若 OpenClaw 在另一机器或容器，需传到其实际挂载的工作区，不是只复制到本项目容器中。

为实际执行技能脚本的环境配置：

```bash
export BAIDU_AUTOSAVE_URL='http://后端可达地址:5000'
export BAIDU_AUTOSAVE_TOKEN='与后端clawbot.api_token相同'
```

脚本只依赖 Python 3 标准库。临时 `export` 只对当前终端及其后续子进程生效，不会修改已经运行的 Gateway。持久使用时，将变量配置到 OpenClaw 服务、容器环境或对应技能的环境配置，并重启使其生效。如果开启执行沙箱，必须将变量注入实际执行脚本的沙箱，并允许其访问后端。

只允许服务所有者通过微信插件的 allowlist/配对机制使用该技能，不要开放给不受信任的群聊或用户；此令牌具有全局任务和参数管理权限。

### 5.5 Docker 网络地址怎么填写

| 部署方式 | 填写原则 |
| --- | --- |
| 两个容器在同一个自定义 Docker 网络 | 使用对应服务名或容器网络别名，如后端访问 `http://openclaw:18789`，技能访问 `http://baidu-autosave:5000`；名称及端口须与实际部署一致 |
| OpenClaw 在宿主机，后端在容器 | 后端使用容器可达的宿主机地址；OpenClaw 还须监听该地址。Linux 可在创建后端容器时添加 `--add-host=host.docker.internal:host-gateway`，再使用该主机名 |
| 两者位于不同机器 | 使用双方实际可达的私网地址，并确认路由、防火墙与监听设置 |

只有目标服务确实位于当前进程相同的网络命名空间时，才使用 `127.0.0.1`。`gateway_url` 填 Gateway 根地址，不要附加 `/hooks/agent`；`BAIDU_AUTOSAVE_URL` 填后端根地址，不要附加 `/api/clawbot/message`。

### 5.6 分步验证

先在 OpenClaw 实际执行技能的环境中配置上述变量，然后运行只读命令：

```bash
python3 ~/.openclaw/workspace/skills/baidu-autosave/scripts/command.py '参数'
python3 ~/.openclaw/workspace/skills/baidu-autosave/scripts/command.py '任务'
```

路径按实际工作区调整。成功后再在微信发送“参数”或“搜索 影片名”。搜索返回多个匹配项目和 ID，不创建任务；选择正确 ID 后发送“追剧 数字ID [更新频率]”（例如“追剧 118943 每天6-8点”）才会创建任务并尝试首次转存。不填写更新频率时，会以最新文件更新时间为基准，在前后两小时内每天每 30 分钟检测。发送“任务”查看名称和唯一 ID，发送“删除任务 ID”删除指定任务。临时查询和读取分享失败最多重试 3 次，创建任务和首次转存不自动重复。已有配置不会自动合并模板，若要使用与 seedhub-cli 相同的默认域名，请将 `media.base_url` 改为 `https://www.seedhub.cc`。

单独测试后端到微信的推送方向，可执行下面的命令。它会读取已挂载配置并向配置的接收人发送一条测试通知：

```bash
docker exec baidu-autosave python -c 'import json; from clawbot import push_message; config = json.load(open("/app/config/config.json", encoding="utf-8")); print(push_message(config, "连接测试", "百度网盘服务已连接 OpenClaw，请确认微信收到此消息。"))'
```

返回 `True` 仅表示 Gateway 接受事件，不保证微信已送达，仍需检查微信及 OpenClaw 日志。返回 `False` 表示后端未启用 ClawBot。

常见问题：

- 命令接口返回 403：检查 `clawbot.enabled`，修改文件后重启后端。
- 命令接口返回 401：检查技能令牌与 `clawbot.api_token` 是否一致。
- 连接被拒绝或超时：检查容器网络、监听地址、防火墙；优先排查误用 `127.0.0.1`。
- 技能脚本可用但微信不调用：检查技能是否在目标 agent 工作区、执行工具是否可用，以及运行进程或沙箱是否拿到变量。
- Gateway 接受但微信没收到：检查微信插件在线状态、`channel`、`to`、`account_id` 和 OpenClaw 日志。

完整命令表、资源比较与清理规则见 `integrations/openclaw/README.md`。

追剧时会从 SeedHub 逐个取得百度入口：每拿到一个就立即解析、读取和比较对应的百度分享，完成后才继续下一个，不会先集中拿完所有入口再请求百度网盘。每次最多检查 18 个入口，可通过 `设置 max_links 数量` 调低到 1–18；旧配置超过 18 会自动按 18 执行。每两个链接的最终结果合并发送，奇数个时最后一个单独发送，然后给出任务总结；最多 9 条结果消息加 1 条总结，不发送开场或选择中的额外进度消息。结果及转存通知中的链接包含已知提取码 `?pwd=...`。

## 6. 微信公众号多接收人通知

公众号通知与微信网关、OpenClaw 独立，无需开启网关的任务通知或部署 OpenClaw。

1. 首次升级此功能时，按第 1 节构建新镜像，并用新镜像重建容器；继续挂载原有 `config`、`log` 目录，保留账号和任务。仅执行 `docker restart` 不会更新旧容器中的代码。
2. 在 **系统设置 → 通知设置 → 启用通知 → 快速添加常用字段 → 微信公众号配置** 中填写或保留 `WxAppID`、`WxappSecret`、`Wxtemplate_id`。
3. 将所有接收人的 OpenID 填入同一个 `WxopenId` 字段，用英文分号 `;` 分隔，例如 `openid1;openid2;openid3`。OpenID 应属于已配置的同一个公众号，不是微信网关账号 ID。
4. 保存后点击“测试通知”，分别确认各接收人收到消息；此操作会向名单中的所有有效配置项发送。

客户端按顺序逐个发送，自动去掉首尾空格、忽略空项并去重，单个接收人报错后继续发送其他人。原来的单 OpenID 配置仍可用；升级后调整名单只需在 Web 页面保存，不用重新构建镜像。公众号权限、模板和发送限制仍然适用。

更完整的渠道区别及配置说明见 `integrations/wechat/README.md`。
