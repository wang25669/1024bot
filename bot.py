import os
import re
import sys
import json
import logging
import asyncio
import random
import traceback
import httpx
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ── 配置（从环境变量读取）─────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))
DOWNLOAD_BASE    = os.environ.get("DOWNLOAD_BASE", "/downloads")
DATA_DIR         = Path("/data")
SETTINGS_FILE    = DATA_DIR / "settings.json"
QUEUE_FILE       = DATA_DIR / "queue.json"
HISTORY_FILE     = DATA_DIR / "history.json"
LOG_HTML         = DATA_DIR / "tasklog.html"

DAILY_LIKE_COUNT = int(os.environ.get("DAILY_LIKE_COUNT", "10"))
LIKE_MIN, LIKE_MAX          = 3, 10        # 点赞间隔（秒）
COMMENT_MIN, COMMENT_MAX    = 1051, 1100   # 评论间隔（秒）

COMMENT_POOL = [
    "感谢分享",
    "谢谢楼主",
    "好帖，顶一个",
    "支持一下",
    "收藏了，感谢",
]

# 强制 stderr 行缓冲，确保 docker logs 能实时看到所有输出
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger(__name__)
# 只保留警告及以上：消除 httpx 每 10 秒一条的 getUpdates 心跳日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)


# ── 任务运行锁（防止并发重入）────────────────────────────────────────────
_daily_task_running = False


# ── JSON 存储（替代数据库）────────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}

def save_settings(d: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

def get_setting(key: str, default: str = "") -> str:
    return load_settings().get(key, default)

def set_setting(key: str, value: str):
    d = load_settings()
    d[key] = value
    save_settings(d)


def load_queue() -> list:
    """下载队列，每项：{url, title, status, added_at}"""
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text())
    return []

def save_queue(q: list):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2))

def extract_tid(url: str) -> str | None:
    """从 htm_mob / htm_data URL 中提取帖子 TID，用于去重"""
    m = re.search(r'/htm_(?:mob|data)/\d+/\d+/(\d+)\.html', url)
    if m:
        return m.group(1)
    from urllib.parse import parse_qs
    qs = parse_qs(urlparse(url).query)
    return qs.get("tid", [None])[0]


def queue_add(url: str) -> bool:
    q = load_queue()
    # 精确 URL 去重
    if any(item["url"] == url for item in q):
        return False
    # TID 去重：htm_mob 与 htm_data 指向同一帖子，只保留先到的那条
    tid = extract_tid(url)
    if tid and any(extract_tid(item["url"]) == tid for item in q):
        return False
    q.append({"url": url, "title": "", "status": "pending", "added_at": now_str()})
    save_queue(q)
    return True

def queue_update(url: str, status: str, title: str = ""):
    q = load_queue()
    for item in q:
        if item["url"] == url:
            item["status"] = status
            if title:
                item["title"] = title
            item["updated_at"] = now_str()
    save_queue(q)

def queue_stats() -> dict:
    q = load_queue()
    result = {}
    for item in q:
        result[item["status"]] = result.get(item["status"], 0) + 1
    return result

def queue_pending() -> list:
    return [i for i in load_queue() if i["status"] == "pending"]

def queue_retry_failed():
    q = load_queue()
    count = 0
    for item in q:
        if item["status"] == "failed":
            item["status"] = "pending"
            count += 1
    save_queue(q)
    return count


# ── 点赞 / 评论历史（去重用）────────────────────────────────────────────

def load_history() -> dict:
    """返回 {"liked": [tid, ...], "commented": [tid, ...]}"""
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    return {"liked": [], "commented": []}

def _history_add(action: str, tid: str):
    if not tid:
        return
    h = load_history()
    if tid not in h[action]:
        h[action].append(tid)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=2))

def history_add_liked(tid: str):    _history_add("liked", tid)
def history_add_commented(tid: str): _history_add("commented", tid)

def history_has_liked(tid: str) -> bool:
    return bool(tid) and tid in load_history()["liked"]

def history_has_commented(tid: str) -> bool:
    return bool(tid) and tid in load_history()["commented"]


# ── HTML 任务日志 ─────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def append_log_html(action: str, tid: str, url: str, result: str, detail: str = ""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ok = result == "ok"
    icon = "✅" if ok else "❌"
    color = "#2ecc71" if ok else "#e74c3c"
    row = (
        f'<tr>'
        f'<td>{now_str()}</td>'
        f'<td style="color:{color}">{icon} {action}</td>'
        f'<td>{tid}</td>'
        f'<td style="font-size:12px;word-break:break-all">'
        f'<a href="{url}" target="_blank">{url[:60]}...</a></td>'
        f'<td>{detail or result}</td>'
        f'</tr>\n'
    )

    if not LOG_HTML.exists():
        LOG_HTML.write_text(
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<title>1024 Bot 任务日志</title>'
            '<style>'
            'body{font-family:sans-serif;padding:20px;background:#1a1a2e;color:#eee}'
            'h1{color:#e94560}'
            'table{width:100%;border-collapse:collapse;font-size:13px}'
            'th{background:#16213e;padding:8px;text-align:left;color:#0f3460}'
            'td{padding:6px 8px;border-bottom:1px solid #333}'
            'tr:hover{background:#16213e}'
            'a{color:#e94560}'
            '.summary{background:#16213e;padding:12px;border-radius:6px;margin-bottom:20px}'
            '</style></head><body>'
            '<h1>📋 1024 Bot 任务日志</h1>'
            '<table><thead><tr>'
            '<th>时间</th><th>操作</th><th>TID</th><th>URL</th><th>结果</th>'
            '</tr></thead><tbody id="logs">\n',
            encoding="utf-8"
        )

    content = LOG_HTML.read_text(encoding="utf-8")
    # 插入到 tbody 后面
    content = content.replace('<tbody id="logs">\n', f'<tbody id="logs">\n{row}')
    LOG_HTML.write_text(content, encoding="utf-8")


def write_log_summary(total_like_ok, total_like_fail, total_comment_ok, total_comment_fail):
    """在日志顶部更新统计摘要"""
    if not LOG_HTML.exists():
        return
    summary = (
        f'<div class="summary">最后执行：{now_str()} &nbsp;|&nbsp; '
        f'👍 点赞 ✅{total_like_ok} ❌{total_like_fail} &nbsp;|&nbsp; '
        f'💬 评论 ✅{total_comment_ok} ❌{total_comment_fail}</div>'
    )
    content = LOG_HTML.read_text(encoding="utf-8")
    # 替换或插入摘要
    if '<div class="summary">' in content:
        content = re.sub(r'<div class="summary">.*?</div>', summary, content, flags=re.DOTALL)
    else:
        content = content.replace('<table>', f'{summary}<table>')
    LOG_HTML.write_text(content, encoding="utf-8")


# ── URL 解析 ──────────────────────────────────────────────────────────────

def parse_post_url(url: str) -> tuple:
    """返回 (domain, tid, fid)"""
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    m = re.search(r'/htm_mob/(\d+)/(\d+)/(\d+)\.html', parsed.path)
    if m:
        return domain, m.group(3), m.group(2)
    from urllib.parse import parse_qs
    qs = parse_qs(parsed.query)
    tid = qs.get("tid", [None])[0]
    fid = qs.get("fid", [None])[0]
    return domain, tid, fid or "7"

def sanitize_dirname(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.strip('. ')
    return name[:80] if name else "unknown"


# ── 下载 ──────────────────────────────────────────────────────────────────

# 允许下载的文件后缀（小写）
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov"}
ALLOWED_EXT = ALLOWED_IMAGE_EXT | ALLOWED_VIDEO_EXT


def extract_media_urls(html: str, base_url: str) -> tuple[list, str]:
    """
    从帖子 HTML 中提取正文媒体链接：
    - 截取范围：id="conttpc" 到第一个 clickLike 之间
    - 只保留 ALLOWED_EXT 后缀的链接
    返回 (media_url_list, post_title)
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse

    soup = BeautifulSoup(html, "html.parser")

    # 获取帖子标题
    title = "未知标题"
    title_tag = soup.find("h4") or soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # 定位正文区域 id="conttpc"
    conttpc = soup.find(id="conttpc")
    if not conttpc:
        logger.warning("未找到 id=conttpc，尝试全页扫描")
        conttpc = soup.body or soup

    # 把 conttpc 转成字符串，截断到第一个 clickLike
    content_html = str(conttpc)
    cutoff = content_html.find("clickLike")
    if cutoff != -1:
        content_html = content_html[:cutoff]

    # 重新解析截断后的区域
    content_soup = BeautifulSoup(content_html, "html.parser")

    media_urls = []
    seen = set()

    def add_url(raw: str):
        if not raw:
            return
        # 补全相对路径
        full = urljoin(base_url, raw.strip())
        # 只保留 http/https
        if not full.startswith("http"):
            return
        # 取路径部分判断后缀（忽略查询参数）
        parsed_full = urlparse(full)
        path = parsed_full.path.lower()
        ext = ""
        if "." in path:
            ext = "." + path.rsplit(".", 1)[-1]
        # 后缀不在白名单时，也检查查询参数里是否包含视频格式关键字
        if ext not in ALLOWED_EXT:
            qs = parsed_full.query.lower()
            for candidate in ALLOWED_VIDEO_EXT:
                if candidate[1:] in qs:   # e.g. "mp4" in query
                    ext = candidate
                    break
        if ext not in ALLOWED_EXT:
            logger.debug(f"[跳过] ext={ext!r} url={full[:100]}")
            return
        if full not in seen:
            seen.add(full)
            media_urls.append(full)
            logger.debug(f"[收录] ext={ext} url={full[:100]}")

    # 提取 <img>：优先 ess-data（直链）> data-link > src > data-src
    for tag in content_soup.find_all("img"):
        raw = (
            tag.get("ess-data") or
            tag.get("data-link") or
            tag.get("src") or
            tag.get("data-src") or ""
        )
        # 过滤广告占位图（iyl-data / a.d 域名）
        if not raw or "a.d/" in raw or "adblo_ck" in raw:
            continue
        add_url(raw)

    # 提取 <video> 标签本身 及内部 <source>
    for tag in content_soup.find_all("video"):
        add_url(tag.get("src") or tag.get("data-src") or "")
        for src_tag in tag.find_all("source"):
            add_url(src_tag.get("src") or src_tag.get("data-src") or "")

    # 提取独立 <source>（不在 video 里的）
    for tag in content_soup.find_all("source"):
        add_url(tag.get("src") or tag.get("data-src") or "")

    # 提取 <a href>（视频直链 / 下载链接）
    for tag in content_soup.find_all("a", href=True):
        add_url(tag["href"])

    img_count  = len([u for u in media_urls if "." + urlparse(u).path.lower().rsplit(".", 1)[-1] in ALLOWED_IMAGE_EXT])
    vid_count  = len(media_urls) - img_count
    logger.info(f"[解析] 标题={title!r} | img标签={len(content_soup.find_all('img'))} "
                f"video标签={len(content_soup.find_all('video'))} "
                f"a标签={len(content_soup.find_all('a', href=True))} "
                f"→ 收录 图{img_count} 视频{vid_count}")

    # 分类：图片 vs 视频
    image_urls = [u for u in media_urls
                  if "." + urlparse(u).path.lower().rsplit(".", 1)[-1] in ALLOWED_IMAGE_EXT]
    video_urls = [u for u in media_urls
                  if u not in image_urls]

    return media_urls, title, image_urls, video_urls


async def download_file(session: httpx.AsyncClient, file_url: str, dest_dir: Path) -> bool:
    """下载单个文件到目标目录"""
    filename = urlparse(file_url).path.rsplit("/", 1)[-1]
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename) or "file"
    dest = dest_dir / filename

    # 同名文件跳过
    if dest.exists():
        logger.info(f"已存在跳过: {filename}")
        return True

    try:
        async with session.stream("GET", file_url, timeout=120) as resp:
            if resp.status_code != 200:
                logger.warning(f"下载失败 {resp.status_code}: {file_url}")
                return False
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
        logger.info(f"下载完成: {filename}")
        return True
    except Exception as e:
        logger.error(f"下载异常 {file_url}: {e}")
        # 清理不完整文件
        if dest.exists():
            dest.unlink()
        return False


async def download_url(url: str) -> tuple:
    """
    下载帖子正文（id=conttpc 到 clickLike 之间）的图片和视频
    只下载 ALLOWED_EXT 中的格式
    返回 (ok: bool, info: dict)
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # 1. 抓取帖子页面
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=build_headers(base_url))
            html = resp.text
    except Exception as e:
        return False, {"error": f"页面请求失败: {e}"}

    # 2. 解析正文区域，提取媒体链接
    media_urls, title, image_urls, video_urls = extract_media_urls(html, base_url)
    logger.info(f"[下载] {title} — 找到 {len(image_urls)} 图 / {len(video_urls)} 视频")

    if not media_urls:
        return False, {"error": "正文区域内未找到符合格式的图片/视频"}

    # 3. 创建下载目录
    safe_title = sanitize_dirname(title)
    dl_dir = Path(DOWNLOAD_BASE) / safe_title
    dl_dir.mkdir(parents=True, exist_ok=True)

    # 4. 逐个下载，分别统计图片/视频
    img_ok = vid_ok = fail = 0
    async with httpx.AsyncClient(
        headers=build_headers(base_url),
        follow_redirects=True
    ) as session:
        for file_url in media_urls:
            success = await download_file(session, file_url, dl_dir)
            ext = ""
            path = urlparse(file_url).path.lower()
            if "." in path:
                ext = "." + path.rsplit(".", 1)[-1]
            if success:
                if ext in ALLOWED_IMAGE_EXT:
                    img_ok += 1
                else:
                    vid_ok += 1
            else:
                fail += 1

    total_ok = img_ok + vid_ok
    if total_ok == 0:
        return False, {"error": f"找到 {len(media_urls)} 个链接但全部下载失败"}

    return True, {
        "title": title,
        "total": total_ok,
        "images": img_ok,
        "videos": vid_ok,
        "failed": fail,
        "total_found": len(media_urls),
    }


# ── 登录 & Cookie ─────────────────────────────────────────────────────────

def build_headers(domain: str, extra: dict = None) -> dict:
    h = {
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 8.0.0; MI 5s Build/OPR1.170623.032; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            "Chrome/71.0.3578.99 Mobile Safari/537.36"
        ),
        "accept-language": "zh-CN,en-US;q=0.9",
        "origin": domain,
        "x-requested-with": "com.cl.newt66y",
        "cookie": get_setting("cookie", "ismob=1"),
    }
    if extra:
        h.update(extra)
    return h


async def auto_login(domain: str) -> tuple:
    username = get_setting("login_user", "")
    password = get_setting("login_pass", "")
    if not username or not password:
        return False, "未设置账号密码，请用 /setlogin 设置"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            resp = await client.post(
                f"{domain}/login.php",
                headers={
                    "user-agent": build_headers(domain)["user-agent"],
                    "content-type": "application/x-www-form-urlencoded",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "origin": domain,
                    "x-requested-with": "com.cl.newt66y",
                    "cookie": "ismob=1",
                },
                data={"pwuser": username, "pwpwd": password, "step": "2", "cktime": "31536000"}
            )
            cookies = {"ismob": "1"}
            for hv in [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]:
                # 服务器有时把多个 cookie 合并在一行用逗号分隔
                # 例如: "227c9_lastvisit=xxx; expires=Thu, 03-Jun-2027 ..., PHPSESSID=yyy; path=/"
                # 用正则在 "; " 后面的逗号处拆分，避免误拆 expires 日期里的逗号
                segments = re.split(r',\s*(?=[A-Za-z0-9_]+=)', hv)
                for seg in segments:
                    part = seg.split(";")[0].strip()
                    if "=" in part:
                        ck, cv = part.split("=", 1)
                        cookies[ck.strip()] = cv.strip()

            if "PHPSESSID" not in cookies:
                return False, f"登录失败，未拿到 PHPSESSID（HTTP {resp.status_code}）"

            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            set_setting("cookie", cookie_str)
            logger.info("自动登录成功")
            return True, cookie_str
    except Exception as e:
        return False, str(e)


# ── 点赞 / 评论 ───────────────────────────────────────────────────────────

async def do_like(domain: str, tid: str) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(
                f"{domain}/api.php",
                headers={**build_headers(domain), "accept": "application/json, text/javascript, */*; q=0.01",
                         "content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                data={"action": "clickLike", "id": tid, "page": "h", "type": "t", "json": "1"}
            )
            body = resp.text
            if resp.status_code == 200:
                return True, body[:80]
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


async def do_comment(domain: str, tid: str, fid: str, post_title: str) -> tuple:
    comment = random.choice(COMMENT_POOL)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.post(
                f"{domain}/post.php",
                headers={**build_headers(domain),
                         "content-type": "application/x-www-form-urlencoded"},
                data={
                    "atc_title": f"Re: {post_title}" if post_title else "Re: 感谢分享",
                    "atc_content": comment,
                    "atc_usesign": "1", "atc_convert": "1", "atc_autourl": "1",
                    "step": "2", "action": "reply",
                    "fid": fid, "tid": tid, "page": "h",
                    "pid": "", "article": "", "touid": "",
                    "verify": "verify", "Submit": "正在提交回覆..",
                }
            )
            if resp.status_code in (200, 302):
                return True, comment
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)


# ── 论坛帖子列表抓取 ──────────────────────────────────────────────────────

async def fetch_forum_posts(domain: str, fid: str, count: int) -> list:
    from bs4 import BeautifulSoup
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                f"{domain}/thread0806.php?fid={fid}",
                headers={**build_headers(domain),
                         "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
            )
            html = resp.text
    except Exception as e:
        logger.error(f"抓取论坛列表失败: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    separator = soup.find("div", class_="tac", string=lambda s: s and "普通主題" in s)
    if not separator:
        logger.warning("未找到'普通主題'分隔符")
        return []

    posts = []
    for sib in separator.find_all_next("div", class_="list"):
        a = sib.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r'/htm_mob/(\d+)/(\d+)/(\d+)\.html', href)
        if not m:
            continue
        posts.append((
            f"{domain}{href}",
            domain,
            m.group(3),   # tid
            m.group(2),   # fid
            a.get_text(strip=True)
        ))

    random.shuffle(posts)
    return posts[:count]


# ── 每日任务 ──────────────────────────────────────────────────────────────

async def run_daily_tasks(app: Application):
    if get_setting("task_enabled", "0") != "1":
        return
    chat_id = get_setting("chat_id", "")
    if not chat_id:
        return

    domain = get_setting("task_domain", "https://www.t66y.com")
    fid    = get_setting("task_fid", "7")

    # 任务开始时立即写一条日志，确保 tasklog.html 存在（即使后面崩溃也有记录）
    append_log_html("task_start", "-", domain, "ok", f"每日任务启动 fid={fid}")
    logger.info(f"每日任务启动，domain={domain}, fid={fid}")

    try:
        await _run_daily_tasks_inner(app, chat_id, domain, fid)
    except Exception as e:
        err_msg = f"每日任务异常崩溃: {e}\n{traceback.format_exc()}"
        logger.error(err_msg)
        append_log_html("task_crash", "-", domain, "error", str(e))
        try:
            await app.bot.send_message(chat_id=chat_id,
                                       text=f"💥 每日任务异常崩溃\n{e}")
        except Exception:
            pass


async def _run_daily_tasks_inner(app: Application, chat_id: str, domain: str, fid: str):
    # 任务启动时立即写日志，确保 tasklog.html 存在（即使后续崩溃也有记录）
    append_log_html("task_start", "-", domain, "ok", f"每日任务启动 fid={fid}")
    logger.info(f"_run_daily_tasks_inner 开始，domain={domain}, fid={fid}")

    if not get_setting("cookie", ""):
        await app.bot.send_message(chat_id=chat_id, text="🔑 Cookie 不存在，尝试自动登录...")
        ok, msg = await auto_login(domain)
        if not ok:
            await app.bot.send_message(chat_id=chat_id, text=f"❌ 自动登录失败：{msg}")
            return
        await app.bot.send_message(chat_id=chat_id, text="✅ 自动登录成功")

    fetch_count = random.randint(10, 20)
    await app.bot.send_message(
        chat_id=chat_id,
        text=f"🤖 每日任务开始，抓取 {fetch_count} 条帖子..."
    )

    posts = await fetch_forum_posts(domain, fid, fetch_count)
    if not posts:
        await app.bot.send_message(chat_id=chat_id, text="❌ 抓取帖子失败，可能 Cookie 已失效，尝试重新登录后用 /runnow 重试")
        return

    # ── 点赞去重：过滤掉已点赞的帖子 ──
    like_posts = [(url, dom, tid, pfid, title) for url, dom, tid, pfid, title in posts
                  if not history_has_liked(tid)]
    skip_like = len(posts) - len(like_posts)

    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📋 抓到 {len(posts)} 条"
            + (f"，跳过已点赞 {skip_like} 条" if skip_like else "")
            + f"\n开始点赞 {len(like_posts)} 条（间隔 {LIKE_MIN}~{LIKE_MAX} 秒）..."
        )
    )

    # 阶段一：批量点赞
    like_ok = like_fail = 0
    for i, (url, dom, tid, pfid, title) in enumerate(like_posts):
        ok, msg = await do_like(dom, tid)
        append_log_html("like", tid, url, "ok" if ok else msg)
        if ok:
            like_ok += 1
            history_add_liked(tid)
        else:
            like_fail += 1
        if i < len(like_posts) - 1:
            await asyncio.sleep(random.randint(LIKE_MIN, LIKE_MAX))

    # 点赞阶段任务报告
    # ── 评论去重：过滤掉已评论的帖子 ──
    cmt_posts = [(url, dom, tid, pfid, title) for url, dom, tid, pfid, title in posts
                 if not history_has_commented(tid)]
    skip_cmt = len(posts) - len(cmt_posts)

    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            f"👍 点赞任务完成\n"
            f"✅ 成功 {like_ok} 个　❌ 失败 {like_fail} 个"
            + (f"\n⏭ 跳过已点赞 {skip_like} 条" if skip_like else "")
            + f"\n\n💬 开始评论阶段（{len(cmt_posts)} 条，间隔 {COMMENT_MIN}~{COMMENT_MAX} 秒）..."
            + (f"\n⏭ 跳过已评论 {skip_cmt} 条" if skip_cmt else "")
        )
    )

    # 阶段二：逐条评论
    cmt_ok = cmt_fail = 0
    for i, (url, dom, tid, pfid, title) in enumerate(cmt_posts):
        ok, msg = await do_comment(dom, tid, pfid or "7", title)
        append_log_html("comment", tid, url, "ok" if ok else msg, msg)
        if ok:
            cmt_ok += 1
            history_add_commented(tid)
        else:
            cmt_fail += 1
        if i < len(cmt_posts) - 1:
            wait = random.randint(COMMENT_MIN, COMMENT_MAX)
            logger.info(f"评论间隔 {wait} 秒（{i+1}/{len(cmt_posts)}）")
            await asyncio.sleep(wait)

    write_log_summary(like_ok, like_fail, cmt_ok, cmt_fail)

    # 评论阶段任务报告
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            f"💬 评论任务完成\n"
            f"✅ 成功 {cmt_ok} 条　❌ 失败 {cmt_fail} 条"
            + (f"\n⏭ 跳过已评论 {skip_cmt} 条" if skip_cmt else "")
        )
    )


# ── 定时调度 ──────────────────────────────────────────────────────────────

async def schedule_daily(app: Application):
    while True:
        from datetime import timedelta
        now = datetime.now()
        target = now.replace(hour=2, minute=random.randint(0, 30), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(f"下次任务：{target}，等待 {wait:.0f} 秒")
        await asyncio.sleep(wait)

        if get_setting("task_enabled", "0") == "1" and not _daily_task_running:
            chat_id = get_setting("chat_id", "")
            domain  = get_setting("task_domain", "https://www.t66y.com")
            fid     = get_setting("task_fid", "7")
            if chat_id:
                asyncio.create_task(_guarded_daily_task(app, chat_id, domain, fid))
            else:
                logger.warning("schedule_daily: chat_id 未设置，跳过本次任务")
        elif _daily_task_running:
            logger.warning("schedule_daily: 任务正在运行中，跳过本次触发")

        # 任务触发后睡到 03:00，确保整个凌晨 2 点窗口完全过去
        # 防止 loop 回到顶部后随机到同一档口内再次触发
        now = datetime.now()
        safe = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if safe <= now:
            safe += timedelta(days=1)
        gap = (safe - now).total_seconds()
        logger.info(f"任务已触发，休眠至 {safe} 后继续调度（{gap:.0f} 秒）")
        await asyncio.sleep(gap)


# ── Telegram handlers ─────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    return ALLOWED_USER_ID == 0 or user_id == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    set_setting("chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        "👋 *1024 下载 & 互动 Bot*\n\n"
        "📥 *下载*\n"
        "直接发帖子 URL 即可\n\n"
        "🤖 *每日任务*\n"
        "/taskon — 开启\n"
        "/taskoff — 关闭\n"
        "/runnow — 立即执行\n\n"
        "🔑 *账号设置*\n"
        "/setlogin <用户名> <密码>\n"
        "/settaskdomain <域名> [fid]\n"
        "/checkcookie — 检查登录状态\n\n"
        "📊 *统计*\n"
        "/status — 概览\n"
        "/list — 待下载队列\n"
        "/retry — 重试失败任务",
        parse_mode="Markdown"
    )


async def cmd_taskon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    set_setting("task_enabled", "1")
    set_setting("chat_id", str(update.effective_chat.id))
    await update.message.reply_text(
        f"✅ 每日任务已开启\n"
        f"每天凌晨 2 点自动执行\n"
        f"点赞间隔 {LIKE_MIN}~{LIKE_MAX} 秒，评论间隔 {COMMENT_MIN}~{COMMENT_MAX} 秒"
    )


async def cmd_taskoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    set_setting("task_enabled", "0")
    await update.message.reply_text("⏹ 每日任务已关闭")


async def cmd_runnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _daily_task_running
    if not is_authorized(update.effective_user.id): return
    if _daily_task_running:
        await update.message.reply_text("⚠️ 每日任务正在运行中，请等待完成后再试")
        return
    chat_id = str(update.effective_chat.id)
    set_setting("chat_id", chat_id)
    domain = get_setting("task_domain", "https://www.t66y.com")
    fid    = get_setting("task_fid", "7")
    await update.message.reply_text(
        f"🚀 任务已在后台启动（不阻塞其他操作）\n🌐 域名：{domain}  fid：{fid}"
    )
    asyncio.create_task(_guarded_daily_task(context.application, chat_id, domain, fid))


async def _guarded_daily_task(app: Application, chat_id: str, domain: str, fid: str):
    """带运行锁的每日任务包装，防止并发重入"""
    global _daily_task_running
    _daily_task_running = True
    try:
        await _run_daily_tasks_inner(app, chat_id, domain, fid)
    except Exception as e:
        logger.error(f"每日任务异常: {e}\n{traceback.format_exc()}")
        append_log_html("task_crash", "-", domain, "error", str(e))
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"💥 任务异常崩溃\n{e}")
        except Exception:
            pass
    finally:
        _daily_task_running = False


async def cmd_setlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("用法：/setlogin <用户名> <密码>")
        return
    set_setting("login_user", context.args[0])
    set_setting("login_pass", context.args[1])
    await update.message.reply_text(
        "✅ 账号密码已保存，Cookie 失效时自动重新登录\n\n"
        "⚠️ 请立即删除这条消息"
    )


async def cmd_settaskdomain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    if not context.args:
        d = get_setting("task_domain", "https://www.t66y.com")
        f = get_setting("task_fid", "7")
        await update.message.reply_text(
            f"当前域名：{d}\n当前 fid：{f}\n\n"
            "用法：/settaskdomain <域名> [fid]\n"
            "例如：/settaskdomain https://www.t66y.com 7"
        )
        return
    set_setting("task_domain", context.args[0].rstrip("/"))
    set_setting("task_fid", context.args[1] if len(context.args) > 1 else "7")
    await update.message.reply_text(f"✅ 域名已更新：{context.args[0]}")


async def cmd_checkcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    domain = get_setting("task_domain", "https://www.t66y.com")
    await update.message.reply_text("🔄 尝试自动登录验证...")
    ok, msg = await auto_login(domain)
    if ok:
        await update.message.reply_text("✅ 登录成功，Cookie 已刷新")
    else:
        await update.message.reply_text(f"❌ 登录失败：{msg}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    stats = queue_stats()
    task_on = get_setting("task_enabled", "0") == "1"
    domain  = get_setting("task_domain", "未设置")
    await update.message.reply_text(
        f"📊 下载队列：\n"
        f"✅ 已完成：{stats.get('done', 0)}\n"
        f"⏳ 待下载：{stats.get('pending', 0)}\n"
        f"❌ 失败：{stats.get('failed', 0)}\n\n"
        f"🤖 每日任务：{'开启 ✅' if task_on else '关闭 ⏹'}\n"
        f"🌐 域名：{domain}"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    items = queue_pending()
    if not items:
        await update.message.reply_text("✅ 没有待下载的任务")
        return
    text = f"⏳ 待下载（{len(items)} 条）：\n"
    for it in items[:10]:
        text += f"• {it['url']}\n"
    if len(items) > 10:
        text += f"...还有 {len(items)-10} 条"
    await update.message.reply_text(text)


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id): return
    reset_count = queue_retry_failed()
    pending = queue_pending()
    if not pending:
        await update.message.reply_text(
            f"🔄 已重置 {reset_count} 条失败任务\n✅ 当前没有待下载的任务"
        )
        return
    await update.message.reply_text(
        f"🔄 已重置 {reset_count} 条失败任务\n⏬ 开始下载 {len(pending)} 条..."
    )
    for item in pending:
        url = item["url"]
        msg = await update.message.reply_text(f"⏬ {url[:70]}...")
        await _do_download_and_reply(url, msg)


async def _do_download_and_reply(url: str, msg) -> bool:
    """下载一个 URL，编辑 msg 显示结果，写 tasklog，返回是否成功"""
    ok, info = await download_url(url)
    _, tid, _ = parse_post_url(url)
    if ok:
        parts = [
            f"✅ 下载完成",
            f"📌 {info['title']}",
            f"📦 共 {info['total']} 个文件（🖼 图片 {info['images']} / 🎬 视频 {info['videos']}）",
        ]
        if info["failed"] > 0:
            parts.append(f"⚠️ {info['failed']} 个下载失败")
        queue_update(url, "done", info["title"])
        append_log_html("download", tid or "-", url, "ok",
                        f"{info['title']} 图{info['images']} 视频{info['videos']}")
        await msg.edit_text("\n".join(parts))
    else:
        queue_update(url, "failed")
        append_log_html("download", tid or "-", url, info["error"])
        await msg.edit_text(f"❌ 下载失败\n{info['error']}\n🔗 {url}")
    return ok


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ 未授权")
        return
    urls = re.findall(r'https?://[^\s]+', update.message.text.strip())
    if not urls:
        await update.message.reply_text("⚠️ 未检测到 URL")
        return
    for url in urls:
        # ── 去重：已成功下载过的同 TID 帖子直接提示 ──
        tid = extract_tid(url)
        done = next(
            (i for i in load_queue()
             if i["status"] == "done" and (
                 i["url"] == url or (tid and extract_tid(i["url"]) == tid)
             )),
            None,
        )
        if done:
            await update.message.reply_text(
                f"✅ 此帖已下载过，无需重复\n"
                f"📌 {done.get('title') or url}\n"
                f"🕒 {done.get('updated_at', '未知时间')}"
            )
            continue
        if not queue_add(url):
            await update.message.reply_text(f"⚠️ 已在队列中（含同帖不同地址）：{url}")
            continue
        msg = await update.message.reply_text(f"⏬ 开始下载：{url}")
        await _do_download_and_reply(url, msg)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """调试：抓取帖子页面，把 conttpc 区域内所有标签结构发回来"""
    if not is_authorized(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("用法：/debug <URL>")
        return

    url = context.args[0]
    await update.message.reply_text(f"🔍 正在解析：{url}")

    from bs4 import BeautifulSoup
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=build_headers(base_url))
            html = resp.text
    except Exception as e:
        await update.message.reply_text(f"❌ 请求失败：{e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    conttpc = soup.find(id="conttpc")
    if not conttpc:
        await update.message.reply_text("❌ 页面里没有 id=conttpc")
        return

    content_html = str(conttpc)
    cutoff = content_html.find("clickLike")
    if cutoff != -1:
        content_html = content_html[:cutoff]
    csoup = BeautifulSoup(content_html, "html.parser")

    lines = ["📊 conttpc 区域标签统计（到 clickLike 截止）\n"]

    # img
    imgs = csoup.find_all("img")
    lines.append(f"🖼 img × {len(imgs)}")
    for tag in imgs[:4]:
        attrs = {k: (v[:60] if isinstance(v, str) else v)
                 for k, v in tag.attrs.items()
                 if k in ("src", "data-src", "ess-data", "data-link")}
        lines.append(f"  {attrs}")

    # video
    videos = csoup.find_all("video")
    lines.append(f"\n🎬 video × {len(videos)}")
    for tag in videos[:4]:
        lines.append(f"  src={tag.get('src','')[:80]}")

    # source
    sources = csoup.find_all("source")
    lines.append(f"\n📼 source × {len(sources)}")
    for tag in sources[:4]:
        lines.append(f"  {dict(tag.attrs)}")

    # a href
    anchors = csoup.find_all("a", href=True)
    lines.append(f"\n🔗 a[href] × {len(anchors)}")
    for tag in anchors[:6]:
        lines.append(f"  {tag['href'][:80]}")

    # iframe
    iframes = csoup.find_all("iframe")
    lines.append(f"\n🪟 iframe × {len(iframes)}")
    for tag in iframes[:3]:
        lines.append(f"  src={tag.get('src','')[:80]}")

    # 原始 HTML 片段
    lines.append(f"\n📝 HTML 前 600 字符：\n{content_html[:600]}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n…(已截断)"
    await update.message.reply_text(text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """统一错误处理：网络抖动降级为 WARNING，其他异常才打完整堆栈"""
    from telegram.error import NetworkError, TimedOut
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"Telegram 网络抖动（PTB 自动重试）: {context.error}")
    else:
        logger.error(f"未处理异常: {context.error}", exc_info=context.error)


# ── 启动 ──────────────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(DOWNLOAD_BASE).mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()
    for cmd, fn in [
        ("start", start), ("taskon", cmd_taskon), ("taskoff", cmd_taskoff),
        ("runnow", cmd_runnow), ("setlogin", cmd_setlogin),
        ("settaskdomain", cmd_settaskdomain), ("checkcookie", cmd_checkcookie),
        ("status", cmd_status), ("list", cmd_list), ("retry", cmd_retry),
        ("debug", cmd_debug),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_error_handler(error_handler)

    asyncio.get_event_loop().create_task(schedule_daily(app))
    logger.info("Bot 启动")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Bot 启动失败: {e}\n{traceback.format_exc()}")
        sys.exit(1)
