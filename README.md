# 1024 下载 & 互动 Bot

Telegram Bot，运行在 Docker 里，功能：
- 发帖子 URL → 自动下载图片/视频到本地（支持懒加载图片、多种视频格式）
- 每天凌晨 2 点自动从论坛抓帖子，批量点赞 + 随机评论
- Cookie 失效时自动重新登录，无需手动维护
- 点赞/评论/下载全部去重，已操作过的帖子自动跳过

---

## 部署（服务器只需三步）

```bash
# 1. 下载配置文件（只需这两个文件）
mkdir 1024bot && cd 1024bot
curl -O https://raw.githubusercontent.com/wang25669/1024bot/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/wang25669/1024bot/main/.env.example

# 2. 填写配置
cp .env.example .env
nano .env          # 填入 BOT_TOKEN 和 ALLOWED_USER_ID

# 3. 启动（自动从 ghcr.io 拉取镜像，支持 x86 / arm64）
docker compose up -d
```

> **镜像地址：** `ghcr.io/wang25669/1024bot:latest`
> 每次推送到 main 分支，GitHub Actions 自动构建并更新镜像。

---

## 更新镜像

```bash
docker compose pull && docker compose up -d
```

---

## 目录结构

```
1024bot/
├── docker-compose.yml      # 容器配置（生产用，拉取镜像）
├── .env                    # 环境变量（不要提交到 git）
└── data/                   # 运行时数据（自动创建）
    ├── settings.json       # Cookie、账号、域名等
    ├── queue.json          # 下载队列
    ├── history.json        # 点赞/评论历史（去重用）
    └── tasklog.html        # 任务日志（浏览器打开）
```

下载的文件存放在 `.env` 里 `DOWNLOAD_PATH` 指定的目录，默认 `./download`。

---

## .env 配置说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token，从 @BotFather 获取 |
| `ALLOWED_USER_ID` | ✅ | 你的 Telegram 用户 ID（@userinfobot 获取），填 0 不限制 |
| `DOWNLOAD_PATH` | | 下载目录，默认 `./download` |
| `DAILY_LIKE_COUNT` | | 每日任务帖子数量上限，默认 10 |

---

## Bot 初始化（部署后发给 Bot）

```
/start
/setlogin 你的论坛用户名 你的论坛密码
/settaskdomain https://www.t66y.com 7
/taskon
```

> ⚠️ 发完 `/setlogin` 后立即删除那条消息，防止密码留在聊天记录。

---

## 所有命令

### 下载

| 命令 | 说明 |
|------|------|
| 直接发 URL | 自动下载帖子图片和视频，同一帖子去重不重复下载 |
| `/status` | 队列统计 + 当前配置概览 |
| `/list` | 待下载队列（前 10 条） |
| `/retry` | 重置失败任务并立即重新下载 |

### 每日任务

| 命令 | 说明 |
|------|------|
| `/taskon` | 开启每日自动任务 |
| `/taskoff` | 关闭 |
| `/runnow` | 立即在后台执行一次（不阻塞其他操作） |

每天凌晨 2 点（随机偏移 0~30 分钟）自动执行：
1. 从论坛版块抓取普通帖子（过滤置顶），随机取 10~20 条
2. 过滤已点赞/评论的帖子（history.json 去重）
3. 批量点赞，间隔 **3~10 秒**
4. 逐条评论，间隔 **1051~1100 秒**（约 17~18 分钟）

### 账号与域名

| 命令 | 说明 |
|------|------|
| `/setlogin <用户名> <密码>` | 保存账号密码 |
| `/settaskdomain <域名> [fid]` | 更新论坛域名和版块 ID |
| `/checkcookie` | 手动触发重新登录验证 |
| `/debug <URL>` | 调试：查看帖子页面媒体结构 |

---

## 开发 / 本地构建

```bash
git clone https://github.com/wang25669/1024bot.git
cd 1024bot
cp .env.example .env && nano .env
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --build
```

或直接用 `build:` 临时覆盖：

```bash
docker compose up -d --build
```

---

## 查看日志

```bash
docker compose logs -f
```

任务详细记录在 `./data/tasklog.html`，下载到本地用浏览器打开。

---

## 常用运维

```bash
docker compose restart          # 重启
docker compose down             # 停止
docker compose pull && docker compose up -d   # 更新到最新镜像
cat ./data/settings.json        # 查看当前配置（含 Cookie）
```
