# 微信 ClawBot 转发程序（只走出站请求）

不开任何端口、不跑 web 服务。所有请求都是本机主动发出的 HTTPS：iLink（ilinkai.weixin.qq.com）和 Grok Bot routine webhook。

## 安装
`bash setup.sh`：需要 python3、requests、cryptography、qrcode[pil]（见 requirements.txt）。系统 python 缺依赖时，会在本目录建 `.venv` 并安装；start.sh 会自动使用 `.venv`（也可用环境变量 `WECHAT_PYTHON` 指定解释器）。安装目录就是这些脚本所在的目录，可放在任意位置；`WECHAT_BRIDGE_ROOT` 可改 state/logs/inbox 的位置。

## 第一次使用
1. `python3 login.py --background`：生成 `qr.png`。备用链接写在 `login_status.json` 的 `fallback_url` 里。
   - 二维码约 5 分钟有效，过期会自动刷新，最多刷新 3 次。
   - 如果手机上显示数字验证码，把数字写进 `verifycode.txt`。
2. 绑定成功后，`state/credentials.json`（权限 600）里会有 bot_token 和绑定人的 ilink_user_id。只有这一个人的消息会被处理。
3. 先在微信 ClawBot 对话里发一句话。回复要用到这条消息带的 context_token。
4. `bash start.sh` 启动转发程序。
   webhook 地址和 key 可以用 `WECHAT_ROUTINE_WEBHOOK_URL=... WECHAT_ROUTINE_WEBHOOK_KEY=... bash write_env.sh` 写入 `.env`。

## 云端电脑重启后
```
bash <INSTALL_DIR>/start.sh
```
- webhook 地址和 key 从环境变量 `WECHAT_ROUTINE_WEBHOOK_URL` / `WECHAT_ROUTINE_WEBHOOK_KEY` 读取。没有的话，也可以放在 `.env`（必须 chmod 600）。
- 脚本可以重复运行，不会启动第二份。如果另一个安装目录里已经有 bridge.py 在跑，start.sh 会拒绝启动（同一个微信 bot 通道同一时间只能有一个轮询者），先用那个目录的 stop.sh 停掉旧的。
- stop.sh 只会停属于本目录的 bridge.py；pid 文件指向别的进程时会拒绝并保持原样。
- 查看状态用 `bash status.sh`，停止用 `bash stop.sh`。

## 遇到 ERRCODE -14（凭证过期）
- 转发程序会写入 `state/expired`、清掉游标和 context_token，然后退出（退出码 14），不会反复请求。
- `reply.py` 会输出 `ERRCODE -14`，退出码 14。
- 处理办法：
  1. 运行 `python3 login.py`，重新扫码。
  2. 在微信里发一句话。
  3. 运行 `bash start.sh`。

## 回复
`python3 reply.py "纯文本"`，也可以从 stdin 传入。
- Markdown 会自动去掉。
- 超过 2000 字会分段，最多 5 段。
- 加 `--dry-run` 只打印请求，不发送。

## 图片、文件、语音、视频（收和发）
全部走出站 HTTPS：微信 CDN（novac2c.cdn.weixin.qq.com），媒体用 AES-128-ECB 加密，做法照搬腾讯官方插件。

**收（入站）**
- 绑定用户发来的图片、文件、语音、视频，会从 CDN 下载并解密，存到 `inbox/<YYYYMMDD>/<消息ID>_<序号>_<文件名>`。目录权限 700，文件权限 600。
- 转给 routine 的 webhook 文本里会带绝对路径，例如 `[图片] <INSTALL_DIR>/inbox/20261015/123_0_image.jpg (image/jpeg, 35.2KB)`；payload 里另有 `media` 数组（kind、path、size、filename、mime）。
- 语音带微信转写文字（有的话）；音频文件本身是 SILK 格式（.silk），没有转码。
- 单个文件上限 50MB（环境变量 `WECHAT_MEDIA_MAX_BYTES`）。下载或解密失败时，退回原来的占位文字（`[图片]`、`[文件] 名字`），payload 里加 `media_errors`，文字消息不受影响。
- 连续快速发来的多条消息（4 秒内没有新消息才算一批，最长等 20 秒）合并成一次 webhook 调用，避免重复触发 routine。可用 `WECHAT_BATCH_WINDOW_S` / `WECHAT_BATCH_MAX_S` 调整。合并后的 payload 有 `batched_messages` 和 `message_ids`。
- 只处理绑定用户的消息；陌生人的消息在下载前就被丢弃。
- inbox 里超过 14 天的日期文件夹会在程序启动时清理。

**发（出站）**
```
python3 reply.py --image /path/a.png [--caption "说明"]
python3 reply.py --file /path/报告.pdf --caption "报告在这里"
python3 reply.py --video /path/v.mp4
python3 reply.py --media /path/x   # 按扩展名自动判断图片/视频/文件
python3 reply.py --dry-run --file /path/x.pdf   # 只打印请求体（密钥已隐藏），不上传
```
- 流程：getuploadurl → AES-128-ECB 加密 → 上传 CDN → sendmessage（一条消息一个 item，说明文字单独先发一条）。
- 一次最多 5 个媒体；单个文件上限 50MB。
- 配额：每条用户消息之后最多约 10 条回复，`state/send_quota.json` 记录已发数量，超了会拒绝（退出码 4），`--force` 可强制。
- 必须有最近一条用户消息带来的 context_token（24 小时内）。
- 退出码：0 成功，1 用法错误，2 无凭证或无 context，3 接口或网络错误，4 配额，5 媒体错误，14 凭证过期。

## 测试
`python3 -m unittest discover -s tests -p "test_*.py"`（全部离线，不碰网络）。
`python3 bridge.py --dry-run --fake-updates tests/fake_updates.json --bound-user x@im.wechat` 是离线演示，不会改动真实的 state。

## 文件
| 文件 | 作用 |
|---|---|
| ilink_common.py | 公共代码：请求头、Markdown 清理、消息过滤、日志脱敏 |
| login.py | 扫码绑定，写 qr.png、login_status.json、state/credentials.json |
| bridge.py | getupdates 长轮询，把消息 POST 给 routine webhook。`--dry-run` 只打印，`--fake-updates` 完全离线 |
| media.py | 媒体收发：AES-128-ECB、CDN 下载解密、getuploadurl + CDN 上传 |
| reply.py | 用 sendmessage 回复绑定的那个人（文字、图片、文件、视频） |
| start.sh / stop.sh / status.sh | 后台启动（setsid+nohup，写 pid 文件）/ 停止 / 查看状态 |
| write_env.sh | 从环境变量 `WECHAT_ROUTINE_WEBHOOK_URL` / `WECHAT_ROUTINE_WEBHOOK_KEY` 校验并写出 `.env`（chmod 600，不回显；key 前面的 `Bearer ` 会去掉；校验失败什么都不写）。加 `--check` 会再发一条无害的测试请求，只报告 HTTP 状态码 |
| reply.sh | 自动选 `.venv` 或系统 python，再运行 reply.py，参数原样传入。routine 提示词里用它，不再需要 `<PY>` |
| render_routine_prompt.sh | 把 `routine-prompt.md` 去掉头部、换上本目录的绝对路径后打印，可直接贴进 UpdateRoutine；有残留的 `<INSTALL_DIR>` 或 `<PY>` 就报错。从安装目录运行时把技能里的模板路径作为参数传入 |
| state/ (700) | credentials.json、context.json、cursor、seen_ids.json、expired、bridge.pid、failed_webhooks.jsonl、send_quota.json |
| inbox/ (700) | 收到的图片、文件等，按日期分文件夹 |
| logs/ | bridge.log、login.log、reply.log。日志不记录 token 或 key |
| tests/ | 离线单元测试：`python3 tests/test_bridge.py` |

可选环境变量：
- `WECHAT_ROUTINE_WEBHOOK_HEADER`：默认 `Authorization`
- `WECHAT_ROUTINE_WEBHOOK_FORMAT`：默认 `Bearer {key}`
- `WECHAT_SKROUTETAG`
