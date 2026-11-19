#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘直链提取下载器 v2.2 Android 版（UC + 夸克）
p4a webview bootstrap：本文件起 HTTP 服务，WebView 自动加载 127.0.0.1:5000
粘贴分享链接 -> 自动识别网盘 -> 解析文件列表 -> 提取直链 -> 多线程下载到手机
"""

import http.cookiejar
import json
import os
import platform
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------- Android SSL 证书修复（p4a 打包的 Python 找不到系统 CA） ----------------
_SSL_CTX = None


def _ssl_ctx():
    """全局 HTTPS 证书上下文：优先 certifi（Mozilla CA 包），回退系统默认。"""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys

HOST = "127.0.0.1"
PORT = 5000  # p4a webview bootstrap 默认加载的端口


def _android_files_dir():
    """Android app 私有文件目录（pyjnius 获取，失败回退本地）"""
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        return Path(act.getFilesDir().getAbsolutePath())
    except Exception:
        return Path(__file__).parent


BASE_DIR = _android_files_dir()
CONFIG_FILE = BASE_DIR / "config.json"
# 公共下载目录（配合 MANAGE_EXTERNAL_STORAGE 权限），不可用回退私有目录
_PUBLIC_DL = Path("/storage/emulated/0/Download/网盘下载")
DEFAULT_SAVE_DIR = _PUBLIC_DL if Path("/storage/emulated/0").exists() else BASE_DIR / "Download"


# ============================================================
# 网盘 API 封装（UC 与夸克同属阿里，API 结构同源，按类型切换域名与参数）
# ============================================================
PAN_PROFILES = {
    "uc": {
        "name": "UC网盘",
        "hosts": ["https://pc-api.uc.cn", "https://drive.uc.cn"],
        "client_params": {"pr": "UCBrowser", "fr": "pc"},
        "share_page": "https://drive.uc.cn/s/{pid}",
        "referer": "https://drive.uc.cn/",
        "need_cookie_download": False,
        # 模拟 UC 浏览器官方 UA
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36 UCBrowser/17.5.0.1736",
    },
    "quark": {
        "name": "夸克网盘",
        "hosts": ["https://drive.quark.cn", "https://drive-h.quark.cn"],
        "client_params": {"pr": "ucpro", "fr": "pc"},
        "share_page": "https://pan.quark.cn/s/{pid}",
        "referer": "https://pan.quark.cn/",
        "need_cookie_download": True,
        # 夸克 PC 客户端官方 UA（Electron 壳）——绕过 23018 大小限制的关键
        "ua": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) quark-cloud-drive/2.5.56 "
              "Chrome/100.0.4896.160 Electron/18.3.5.12 "
              "Safari/537.36 Channel/pckk_other_ch",
    },
}


# ============================================================
# Android 登录 Cookie 提取（pyjnius 调 WebView CookieManager 明文直读，
# 与 PC 版"弹窗登录自动提取"同思路：WebView 导航到登录页，轮询 Cookie）
# ============================================================
PAN_COOKIE_DOMAIN = {"quark": "quark.cn", "uc": "uc.cn"}
_COOKIE_BAD = re.compile(r"[^\x21-\x7e]")  # Cookie 值只允许可见 ASCII
# pan.quark.cn 对移动端 UA 只给 APP 推广页；quark.cn 网页版移动端有完整登录入口
# （手机验证码/账号密码/扫码/第三方），Cookie 写 .quark.cn 根域，pan.quark.cn 共享
LOGIN_URL = {"quark": "https://quark.cn/", "uc": "https://drive.uc.cn/"}
LOGIN_ACTIVE = {"pan": None}   # 当前登录提取任务（worker 用它判取消）


def _cm_cookies(url):
    """读 WebView CookieManager 里指定 URL 的整串 Cookie（含 httponly）。
    CookieManager 是系统级服务，不依赖任何 app 类名，任意线程可调。"""
    try:
        from jnius import autoclass
        cm = autoclass("android.webkit.CookieManager").getInstance()
        return cm.getCookie(url) or ""
    except Exception:
        return ""


def _app_context():
    """Application context（ActivityThread.currentApplication 是公开 SDK API）"""
    try:
        from jnius import autoclass
        return autoclass("android.app.ActivityThread").currentApplication()
    except Exception:
        return None


# ---------------- 夸克扫码登录（纯 API，社区逆向端点，零 WebView/UA 依赖） ----------------
QR_PC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _http_json(url, params=None, cj=None, timeout=30):
    """GET JSON（带 PC UA）；cj 传入时收集响应 Set-Cookie"""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full, headers=QR_PC_HEADERS)
    if cj is not None:
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=_ssl_ctx()))
        with op.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    with urllib.request.urlopen(req, timeout=timeout,
                                context=_ssl_ctx()) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def qr_get_token():
    """① 拿扫码 token（社区逆向端点）"""
    import uuid
    d = _http_json("https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin",
                   {"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())})
    if d.get("status") == 2000000:
        token = d.get("data", {}).get("members", {}).get("token")
        if token:
            return token
    raise RuntimeError(f"获取二维码token失败: {d.get('message', d)}")


def qr_build_url(token):
    """② 拼扫码登录短链（即二维码内容；夸克APP内打开会直接弹"确认登录"）"""
    q = urllib.parse.urlencode({
        "token": token, "client_id": "532", "ssb": "weblogin",
        "uc_param_str": "",
        "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"})
    return f"https://su.quark.cn/4_eMHBJ?{q}"


def qr_poll_worker(token):
    """③ 轮询扫码状态；用户确认后 ④ account/info 收 Set-Cookie 落全套登录态"""
    import uuid
    for _ in range(150):           # 2s × 150 = 5 分钟
        time.sleep(2)
        if LOGIN_ACTIVE["pan"] != "quark":    # 用户取消
            return
        try:
            d = _http_json(
                "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken",
                {"client_id": "532", "v": "1.2", "token": token,
                 "request_id": str(uuid.uuid4())})
            st = d.get("status")
            if st == 2000000 and d.get("data", {}).get("members", {}).get("service_ticket"):
                ticket = d["data"]["members"]["service_ticket"]
                cj = http.cookiejar.CookieJar()
                _http_json("https://pan.quark.cn/account/info",
                           {"st": ticket, "lw": "scan"}, cj=cj)
                pairs = {}
                for c in cj:
                    if "quark.cn" in (c.domain or ""):
                        pairs[c.name] = c.value
                if not any(k in pairs for k in ("__pus", "__uid", "__puus")):
                    STATE.log("✗ 登录态字段缺失，请重新扫码")
                    LOGIN_ACTIVE["pan"] = None
                    return
                cookie = "; ".join(f"{k}={v}" for k, v in pairs.items()
                                   if not _COOKIE_BAD.search(str(v)))
                cfg = load_config()
                cfg["cookie"] = cookie
                save_config(cfg)
                with STATE.lock:
                    STATE.cookie_ready = cookie
                STATE.log(f"✓ 扫码登录成功！Cookie 已保存（{len(pairs)} 条），自动继续解析")
                LOGIN_ACTIVE["pan"] = None
                return
            if st in (50004002, 50004003, 50004004):   # 明确失败/过期
                STATE.log("✗ 二维码已过期或被拒绝，请重新获取")
                LOGIN_ACTIVE["pan"] = None
                return
            # 50004001 = 等待扫码，继续轮询
        except Exception:
            pass
    STATE.log("扫码等待超时，请重新获取二维码")
    LOGIN_ACTIVE["pan"] = None


def qr_login_start():
    """启动扫码登录：拿 token + 生成二维码链接 + 后台轮询。返回二维码内容 URL"""
    token = qr_get_token()
    qr_url = qr_build_url(token)
    LOGIN_ACTIVE["pan"] = "quark"
    threading.Thread(target=qr_poll_worker, args=(token,), daemon=True).start()
    STATE.log("二维码已生成：夸克APP扫码确认，或APP内打开链接直接登录")
    return qr_url


def qr_login_cancel():
    LOGIN_ACTIVE["pan"] = None


def _ensure_storage_permission():
    """Android 11+ 写公共 Download 需"所有文件访问"权，没有则跳系统设置引导"""
    try:
        from jnius import autoclass
        env = autoclass("android.os.Environment")
        if env.isExternalStorageManager():
            return
        app = _app_context()
        if app is None:
            return
        intent_cls = autoclass("android.content.Intent")
        settings = autoclass("android.provider.Settings")
        uri = autoclass("android.net.Uri")
        it = intent_cls(settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        uri.parse("package:" + app.getPackageName()))
        it.addFlags(intent_cls.FLAG_ACTIVITY_NEW_TASK)  # 非 Activity context 启动必须
        app.startActivity(it)
    except Exception:
        pass


class PanAPI:
    def __init__(self, pan="uc", cookie=""):
        self.pan = pan
        self.prof = PAN_PROFILES[pan]
        # 构造即清洗：只允许 latin-1 字符进 Cookie（防乱码炸请求头）
        self.cookie = cookie.strip().encode("latin-1", "ignore").decode("latin-1")
        self.stoken = ""
        self.pwd_id = ""
        self.base = self.prof["hosts"][0]
        # API 要求先访问分享页拿会话 cookie（否则 403）
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.add_handler(urllib.request.HTTPSHandler(
            context=_ssl_ctx()))

    def warmup(self):
        url = self.prof["share_page"].format(pid=self.pwd_id or "")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            self.opener.open(req, timeout=30).read()
        except Exception:
            pass

    def _headers(self):
        h = {
            "User-Agent": self.prof["ua"],  # 按网盘模拟其官方浏览器 UA
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.prof["referer"],
            "Accept": "application/json, text/plain, */*",
            "Origin": self.prof["referer"].rstrip("/"),
        }
        if self.cookie:
            # HTTP 头必须 latin-1 编码：清洗掉任何混入的非 ASCII 字符，防请求整体炸掉
            h["Cookie"] = self.cookie.encode("latin-1", "ignore").decode("latin-1")
        return h

    def _http(self, method, url, payload=None, timeout=30):
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            # 把服务端真实错误原因（如 23018 size limit）透出来
            detail = ""
            try:
                ej = json.loads(e.read().decode("utf-8", "ignore"))
                detail = ej.get("message") or ""
                code = ej.get("code", "")
                if code:
                    detail = f"[{code}] {detail}"
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} {detail}".strip())

    def _api(self, method, path, payload=None, params=None):
        q = dict(self.prof["client_params"])
        if params:
            q.update(params)
        qs = "?" + urllib.parse.urlencode(q)
        last_exc = None
        for host in self.prof["hosts"]:
            try:
                data = self._http(method, host + path + qs, payload)
                self.base = host
                return data
            except Exception as e:  # noqa
                last_exc = e
        raise RuntimeError(f"API 请求失败: {last_exc}")

    # ---------- 链接解析（自动识别 UC / 夸克） ----------
    @staticmethod
    def parse_share(text):
        """返回 (pan_type, pwd_id, passcode)"""
        text = (text or "").strip()
        pan, pwd_id = "", ""
        m = re.search(r"https?://[^\s\"']*pan\.quark\.cn/s/([A-Za-z0-9_-]+)", text)
        if m:
            pan, pwd_id = "quark", m.group(1)
        else:
            m = re.search(r"https?://[^\s\"']*drive\.uc\.cn/s/([A-Za-z0-9_-]+)", text)
            if m:
                pan, pwd_id = "uc", m.group(1)
            else:
                m = re.search(r"/s/([A-Za-z0-9_-]{8,})", text)
                if m:
                    pan, pwd_id = "uc", m.group(1)  # 无域名兜底按 UC
        passcode = ""
        mp = re.search(r"(?:pwd|password|passcode)=([A-Za-z0-9]{4})", text)
        if mp:
            passcode = mp.group(1)
        else:
            # 先剔除 URL，避免"口令"关键词误抓链接里的字符
            text_no_url = re.sub(r"https?://\S+", "", text)
            mp = re.search(r"(?:提取码|密码|口令)[:：\s]*([A-Za-z0-9]{4})\b", text_no_url)
            if mp:
                passcode = mp.group(1)
        return pan, pwd_id, passcode

    # ---------- 分享 token ----------
    def get_stoken(self, pwd_id, passcode=""):
        self.pwd_id = pwd_id
        self.warmup()  # 先访问分享页拿会话 cookie（否则 API 403）
        data = self._api("POST", "/1/clouddrive/share/sharepage/token",
                         {"pwd_id": pwd_id, "passcode": passcode})
        st = (data.get("data") or {}).get("stoken", "")
        if not st:
            raise RuntimeError(
                "获取分享凭证失败: " + json.dumps(data, ensure_ascii=False)[:200])
        self.stoken = st
        return st

    # ---------- 文件列表（递归） ----------
    def list_files(self, pdir_fid="0", page=1, size=200):
        data = self._api("GET", "/1/clouddrive/share/sharepage/detail", params={
            "pwd_id": self.pwd_id, "stoken": self.stoken, "pdir_fid": pdir_fid,
            "force": 0, "_page": page, "_size": size,
            "_fetch_banner": 0, "_fetch_share": 0, "_fetch_total": 1,
            "_sort": "file_type:asc,file_name:asc",
        })
        d = data.get("data") or {}
        return (d.get("list") or []), ((d.get("metadata") or {}).get("_total", 0))

    def walk_all(self, pdir_fid="0", prefix=""):
        result, page = [], 1
        while True:
            items, total = self.list_files(pdir_fid, page)
            if not items:
                break
            for it in items:
                name = it.get("file_name", "未知")
                fid = it.get("fid", "")
                size = it.get("size", 0)
                is_dir = bool(it.get("dir"))
                rel = prefix + name
                result.append({"fid": fid, "name": name, "size": size,
                               "rel": rel, "is_dir": is_dir,
                               "fid_token": it.get("share_fid_token", "")})
                if is_dir:
                    result.extend(self.walk_all(fid, rel + "/"))
            if not items or (total and page * 200 >= total):
                break
            page += 1
        return result

    # ---------- 转存到自己网盘（免费账号可用，大文件绕 23018 的出路） ----------
    def save_to_drive(self, fid, fid_token=""):
        """转存分享文件到自己的夸克网盘根目录，返回 (True/False, 说明)"""
        if not (self.pwd_id and self.stoken):
            return False, "无分享上下文"
        try:
            save = self._api("POST", "/1/clouddrive/share/sharepage/save", {
                "pwd_id": self.pwd_id, "stoken": self.stoken,
                "fid_list": [fid], "fid_token_list": [fid_token],
                "to_pdir_fid": "0",
            })
            task_id = (save.get("data") or {}).get("task_id")
            if not task_id:
                return False, json.dumps(save, ensure_ascii=False)[:100]
            for _ in range(15):
                time.sleep(1)
                t = self._api("GET", "/1/clouddrive/task",
                              params={"task_id": task_id, "retry_index": 0})
                td = t.get("data") or {}
                # 夸克任务状态：2/3 都算完成，或以 save_as 字段出现为准
                if td.get("status") in (2, 3) or td.get("save_as"):
                    return True, "已转存到你的夸克网盘根目录"
            return False, "转存任务超时"
        except Exception as e:  # noqa
            return False, str(e)[:100]

    # ---------- 下载直链（fids + fids_token + stoken） ----------
    def get_download_url(self, fid, fid_token=""):
        # 夸克下载必须登录 Cookie；UC 免登录可直下
        if self.prof["need_cookie_download"]:
            if not self.cookie:
                return "", "夸克网盘下载必须填登录 Cookie（展开上面 Cookie 栏粘贴）"
            # 夸克登录态标志字段：__pus / __uid；只有 __qk_bx_ck_v1 是匿名 Cookie
            if "__pus" not in self.cookie and "__uid" not in self.cookie:
                return "", ("Cookie 里没有登录态（缺 __pus/__uid 字段）——"
                            "请先在浏览器【登录夸克账号】后再复制整段 Cookie")
        payload = {"fids": [fid], "download_type": 1}
        if self.pwd_id and self.stoken:
            payload["pwd_id"] = self.pwd_id
            payload["stoken"] = self.stoken
        if fid_token:
            payload["fids_token"] = [fid_token]
        try:
            data = self._api("POST", "/1/clouddrive/file/download", payload)
            dlist = data.get("data") or []
            if isinstance(dlist, dict):
                dlist = [dlist]
            for d in dlist:
                url = d.get("download_url") or d.get("durl") or d.get("url")
                if url:
                    return url, ""
            return "", "响应无下载地址（夸克请检查 Cookie 是否有效）: " + \
                json.dumps(data, ensure_ascii=False)[:150]
        except Exception as e:  # noqa
            msg = str(e)[:150]
            if "23018" in msg or "size limit" in msg:
                msg = ("夸克免费账号大文件限制：请改用夸克官方客户端下载，"
                       "或开会员后重试（UC 网盘无此限制）")
            elif "401" in msg or "403" in msg:
                msg = "鉴权失败 → Cookie 无效或过期，重新复制粘贴"
            return "", msg

    # ---------- 多线程分块下载（按连接限速 ~100KB/s，并发线性提速） ----------
    def download_file(self, url, save_path, progress_cb=None, cancel=None,
                      nthread=32):
        import queue
        headers = self._headers()
        # 安全软件（电脑管家/Defender）会隔离写入中的 APK/EXE，
        # 先用 .dat 中性扩展名落盘，下完再改名
        real_path = save_path
        if save_path.lower().endswith((".apk", ".exe", ".sh")):
            save_path = save_path + ".dat"

        # 1) 探测总大小（Range 0-0 拿 Content-Range）
        req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
        with self.opener.open(req, timeout=30) as resp:
            cr = resp.headers.get("Content-Range", "")
            total = int(cr.split("/")[-1]) if "/" in cr else 0
        if total <= 0:
            raise RuntimeError("无法获取文件大小")

        # 2) 分块：小块多并发（夸克按连接限速，连接数=速度）
        nthread = max(1, min(int(nthread), 128))
        CHUNK = max(256 * 1024, min(2 * 1024 * 1024,
                                    total // (nthread * 2) or total))
        CHUNK = (CHUNK + 65535) // 65536 * 65536  # 64KB 对齐
        if total <= CHUNK:
            nthread = 1
            CHUNK = total
        q = queue.Queue()
        for off in range(0, total, CHUNK):
            q.put((off, min(CHUNK, total - off)))

        # 3) 预分配文件（单句柄复用，避免每块 open/close 开销）
        out_f = open(save_path, "wb")
        out_f.truncate(total)

        state = {"done": 0, "errs": []}
        slock = threading.Lock()
        t0 = time.time()
        # 滑动窗口测速：最近 4 秒的真实吞吐（平均速度在限速场景太迟钝）
        win = []          # [(t, bytes)]

        def _tick_speed(n):
            now = time.time()
            win.append((now, n))
            cutoff = now - 4.0
            while win and win[0][0] < cutoff:
                win.pop(0)
            spd = sum(b for _, b in win) / max(now - win[0][0], 0.5) if len(win) > 1 else 0
            return spd

        def worker():
            # 每线程独立 opener：连接互不复用，规避服务端按会话限速
            op = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_ssl_ctx()))
            while True:
                if cancel and cancel.is_set():
                    return
                try:
                    off, size = q.get_nowait()
                except queue.Empty:
                    return
                pos = off
                remain = size
                for attempt in range(3):
                    if cancel and cancel.is_set():
                        return
                    try:
                        req = urllib.request.Request(
                            url, headers={**headers,
                                          "Range": f"bytes={pos}-{off+size-1}",
                                          "Connection": "close"})
                        with op.open(req, timeout=120) as resp:
                            while remain > 0:
                                if cancel and cancel.is_set():
                                    return
                                buf = resp.read(min(65536, remain))
                                if not buf:
                                    break
                                with slock:
                                    out_f.seek(pos)
                                    out_f.write(buf)
                                    state["done"] += len(buf)
                                    spd = _tick_speed(len(buf))
                                    if progress_cb:
                                        progress_cb(state["done"], total, spd)
                                pos += len(buf)
                                remain -= len(buf)
                        if remain > 0:
                            raise IOError(f"块提前结束，还差 {remain} 字节")
                        break
                    except Exception as e:  # noqa
                        # 断点续传式重试：pos/remain 保留已下部分，只补缺口
                        if getattr(e, "errno", None) == 2 and not os.path.exists(save_path):
                            try:
                                with open(save_path, "wb") as f:
                                    f.truncate(total)
                            except Exception:
                                pass
                        if attempt == 2:
                            with slock:
                                state["errs"].append(f"块@{off}: {str(e)[:60]}")
                        else:
                            time.sleep(1)
                if state["errs"]:
                    return

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(nthread)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            out_f.close()
        except Exception:
            pass

        if cancel and cancel.is_set():
            return False
        if state["errs"]:
            raise RuntimeError(state["errs"][0])
        if state["done"] == total:
            if save_path != real_path:  # 改名回正式扩展名
                try:
                    os.replace(save_path, real_path)
                except Exception:
                    pass
            return True
        return False


# ============================================================
# 全局任务状态
# ============================================================
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.api = None
        self.files = []
        self.status = "idle"     # idle/parsing/downloading
        self.queue = []
        self.current = None
        self.done = 0
        self.total = 0
        self.speed = 0.0
        self.finished = 0
        self.failed = []
        self.transferred = []    # 转存到网盘成功的文件
        self.cancel = threading.Event()
        self.logs = []
        self.pan = ""            # 当前网盘类型
        self.cookie_ready = ""   # 登录窗口提取成功的 Cookie（前端 tick 取走填入）
        self.need_login = ""     # 需要登录的网盘类型（前端 tick 看到自动弹登录窗）

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-200:]

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status, "current": self.current,
                "done": self.done, "total": self.total,
                "speed": self.speed, "finished": self.finished,
                "queue_len": len(self.queue), "failed": self.failed[-10:],
                "transferred": self.transferred[-10:],
                "logs": self.logs[-50:], "file_count": len(self.files),
                "files": self.files, "pan": self.pan,
                "need_login": self.need_login,
            }

    def pop_cookie_ready(self):
        with self.lock:
            c = self.cookie_ready
            self.cookie_ready = ""
            return c


STATE = State()
SRV = None


def fmt_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}PB"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# ============================================================
# 后台任务
# ============================================================
def _has_login_cookie(cookie):
    """Cookie 里有没有夸克登录态字段（__pus/__uid/__puus）"""
    return bool(cookie) and ("__pus=" in cookie or "__uid=" in cookie or "__puus=" in cookie)


def parse_worker(text, passcode, cookie):
    STATE.status = "parsing"
    STATE.cancel.clear()
    with STATE.lock:
        STATE.need_login = ""
    try:
        pan, pwd_id, pc = PanAPI.parse_share(text)
        if passcode:
            pc = passcode.strip()
        if not pwd_id:
            raise RuntimeError("没认出分享链接，支持 drive.uc.cn/s/xxx 或 pan.quark.cn/s/xxx")
        STATE.pan = pan
        pname = PAN_PROFILES[pan]["name"]
        STATE.log(f"识别为【{pname}】分享 ID: {pwd_id}  提取码: {pc or '(无)'}")
        # 夸克全程要登录态（解析+下载都走鉴权 API），无登录态直接弹登录窗
        if pan == "quark" and not _has_login_cookie(cookie):
            STATE.log("⚠ 夸克网盘需要登录，已弹出二维码（夸克APP扫码或打开链接确认）")
            with STATE.lock:
                STATE.need_login = "quark"
            return
        api = PanAPI(pan=pan, cookie=cookie)
        STATE.log("获取分享凭证...")
        api.get_stoken(pwd_id, pc)
        STATE.log("遍历文件列表（含文件夹递归）...")
        files = api.walk_all()
        with STATE.lock:
            STATE.api = api
            STATE.files = [f for f in files if not f["is_dir"]]
        dirs = sum(1 for f in files if f["is_dir"])
        STATE.log(f"解析完成: {len(STATE.files)} 个文件, {dirs} 个文件夹")
    except Exception as e:
        msg = str(e)
        STATE.log(f"解析失败: {msg}")
        # 鉴权类失败（400/401/403）且是夸克：Cookie 失效，重新弹登录
        if STATE.pan == "quark" and any(c in msg for c in ("400", "401", "403")):
            STATE.log("⚠ 登录态已失效，已弹出二维码，请重新扫码登录")
            with STATE.lock:
                STATE.need_login = "quark"
    finally:
        STATE.status = "idle"


def download_worker(fids, save_dir, cookie, nthread=32):
    STATE.status = "downloading"
    STATE.cancel.clear()
    STATE.finished = 0
    STATE.failed = []
    STATE.transferred = []
    root = Path(save_dir)
    root.mkdir(parents=True, exist_ok=True)
    # 夸克无登录态不白跑：弹登录窗，登录后用户再点下载
    if STATE.pan == "quark" and not _has_login_cookie(cookie):
        STATE.log("⚠ 夸克下载需要登录，已弹出二维码（确认后再点下载）")
        with STATE.lock:
            STATE.need_login = "quark"
        STATE.status = "idle"
        return
    api = STATE.api
    if api is None:
        api = PanAPI(pan=STATE.pan or "uc", cookie=cookie)
    elif cookie and cookie != api.cookie:
        api.cookie = cookie  # 下载前允许更新 Cookie
    total_files = len(fids)
    STATE.log(f"开始下载 {total_files} 个文件 -> {root}")
    for idx, fid in enumerate(fids, 1):
        if STATE.cancel.is_set():
            STATE.log("已取消")
            break
        item = next((f for f in STATE.files if f["fid"] == fid), None)
        if not item:
            continue
        name, rel = item["name"], item["rel"]
        STATE.current = f"({idx}/{total_files}) {name}"
        STATE.done = STATE.total = 0
        STATE.log(f"获取直链: {name}")
        url, err = api.get_download_url(fid, item.get("fid_token", ""))
        if not url:
            # 夸克免费账号大文件被限时：自动转存到用户网盘兜底
            if "23018" in err or "大文件限制" in err:
                ok, smsg = api.save_to_drive(fid, item.get("fid_token", ""))
                if ok:
                    STATE.log(f"  ➜ 已转存: {name} → 打开夸克客户端即可下载")
                    STATE.transferred.append(name)
                    continue
                err = err + " | 转存也失败: " + smsg
            STATE.log(f"  ✗ 无直链: {name} | {err[:120]}")
            STATE.failed.append(name)
            continue
        save = root / rel
        save.parent.mkdir(parents=True, exist_ok=True)

        def cb(d, t, s):
            STATE.done, STATE.total, STATE.speed = d, t, s

        try:
            ok = api.download_file(url, str(save), progress_cb=cb,
                                   cancel=STATE.cancel, nthread=nthread)
            if ok:
                STATE.finished += 1
                STATE.log(f"  ✓ 完成: {name} ({fmt_size(save.stat().st_size)})")
            else:
                STATE.log(f"  ✗ 中断: {name}")
                STATE.failed.append(name + " (取消)")
        except Exception as e:
            STATE.log(f"  ✗ 下载异常: {name} | {str(e)[:120]}")
            STATE.failed.append(name)
    STATE.current = None
    STATE.status = "idle"
    STATE.log(f"任务结束: 下载成功 {STATE.finished}, 转存 {len(STATE.transferred)}, "
              f"失败 {len(STATE.failed)}")


# ============================================================
# HTTP 服务
# ============================================================
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘直链下载器（UC + 夸克）</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8ef;--main:#2563eb;--txt:#1f2937;--sub:#6b7280}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font-family:"Microsoft YaHei",system-ui,sans-serif;padding:20px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:20px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:14px}
label{font-size:13px;color:var(--sub);display:block;margin:6px 0 3px}
input[type=text]{width:100%;padding:8px 10px;border:1px solid var(--line);
border-radius:6px;font-size:14px}
button{background:var(--main);color:#fff;border:0;border-radius:6px;
padding:8px 18px;font-size:14px;cursor:pointer}
button.ghost{background:#e5e7eb;color:#374151}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
.row>div{flex:1;min-width:180px}
.row>div.narrow{flex:0 0 120px;min-width:100px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px}
th{background:#f8fafc;position:sticky;top:0}
.listbox{max-height:300px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.bar{height:14px;background:#e5e7eb;border-radius:7px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--main);width:0;transition:width .2s}
.log{background:#0f172a;color:#a5f3fc;font-family:Consolas,monospace;font-size:12px;
padding:10px;border-radius:6px;height:170px;overflow:auto;white-space:pre-wrap}
.stat{font-size:13px;color:var(--sub);margin:6px 0}
details{margin-top:8px}summary{cursor:pointer;font-size:13px;color:var(--sub)}
.pan-tag{display:inline-block;font-size:12px;padding:2px 10px;border-radius:10px;
margin-left:8px;vertical-align:2px}
.tag-uc{background:#dbeafe;color:#1d4ed8}
.tag-quark{background:#fef3c7;color:#b45309}
.cookie-warn{display:none;color:#b45309;font-size:12px;margin-top:4px}
/* 完成提示弹窗 */
.mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;
align-items:center;justify-content:center;z-index:99}
.mask.show{display:flex}
.done-card{background:#fff;border-radius:14px;padding:28px 36px;text-align:center;
min-width:320px;box-shadow:0 20px 60px rgba(0,0,0,.25);animation:pop .25s ease}
@keyframes pop{from{transform:scale(.85);opacity:0}to{transform:scale(1);opacity:1}}
.done-icon{font-size:52px;line-height:1}
.done-title{font-size:20px;font-weight:700;margin:10px 0 6px;color:#16a34a}
.done-sub{font-size:14px;color:#6b7280;margin-bottom:14px;max-width:420px;word-break:break-all}
.done-fail{color:#dc2626;font-size:13px;margin-bottom:10px;text-align:left;
max-height:100px;overflow:auto}
</style></head><body><div class="wrap">
<h1>网盘直链下载器 <span style="font-size:12px;color:#6b7280">v2.2</span><span class="pan-tag" id=panTag style="display:none"></span></h1>

<div class="card">
<div class="row">
<div><label>分享链接 / 整段口令（UC 或夸克，自动识别）</label>
<input type=text id=url placeholder="https://drive.uc.cn/s/xxx 或 https://pan.quark.cn/s/xxx 或整段口令"></div>
<div class=narrow><label>提取码(可空)</label><input type=text id=pwd></div>
<div class=narrow><button onclick="doParse()">解 析</button></div>
</div>
<!-- Cookie 区已隐藏：识别到夸克自动弹登录窗提取，无需手动操作；DOM 保留供 JS 读写 -->
<details id=ckBox style="display:none"><summary>登录 Cookie</summary>
<label>Cookie <button class=ghost style="padding:2px 12px;font-size:12px;margin-left:8px" onclick="autoCk()">⚡ 弹窗登录自动提取</button></label>
<input type=text id=cookie placeholder="自动提取，无需手动填写" oninput="checkCk()">
<div class=cookie-warn id=ckWarn></div>
<div class=cookie-warn id=ckBad style="color:#dc2626"></div>
</details>
<div id=loginTip style="display:none;margin-top:6px;font-size:13px;color:#b45309">⚠ 夸克网盘需要登录：已弹出二维码，用夸克APP扫码或打开链接确认</div>
</div>

<div class="card">
<div class="row" style="align-items:center">
<div><label>保存目录</label><input type=text id=dir></div>
<div class=narrow><label>线程数(越多越快)</label>
<select id=threads style="width:100%;padding:7px;border:1px solid var(--line);border-radius:6px">
<option value=8>8 线程</option>
<option value=16>16 线程</option>
<option value=32 selected>32 线程(~3MB/s)</option>
<option value=64>64 线程(~6MB/s)</option>
<option value=128>128 线程(狂暴)</option>
</select></div>
<div class=narrow><button onclick="dl(false)">下载选中</button></div>
<div class=narrow><button onclick="dl(true)">下载全部</button></div>
<div class=narrow><button class=ghost onclick="cancel()">取 消</button></div>
</div>
<div class=stat id=stat>等待解析…</div>
<div class=listbox><table id=tbl>
<thead><tr><th style="width:30px"><input type=checkbox id=chkall onclick="toggleAll(this)"></th>
<th>文件名</th><th style="width:90px">大小</th><th>路径</th></tr></thead><tbody id=tb></tbody>
</table></div>
</div>

<div class="card">
<div class=stat id=prog>—</div>
<div class=bar><i id=b></i></div>
</div>

<div class="card"><div style="display:flex;justify-content:space-between;align-items:center">
<label style="margin:0">日志</label>
<div><button class=ghost style="padding:2px 12px;font-size:12px;margin-right:6px" onclick="dlLog()">下载日志</button><button class=ghost style="padding:2px 12px;font-size:12px" onclick="clearLog()">清空</button></div>
</div><div class=log id=log></div></div>
</div>

<div class=mask id=doneMask><div class=done-card>
<div class=done-icon id=doneIcon>✅</div>
<div class=done-title id=doneTitle>下载完成</div>
<div class=done-sub id=doneSub></div>
<div class=done-fail id=doneFail></div>
<button onclick="$('doneMask').classList.remove('show')">知道了</button>
</div></div>

<div class=mask id=qrMask><div class=done-card style="padding:20px 24px">
<div class=done-title style="font-size:17px">夸克扫码登录</div>
<img id=qrImg style="width:240px;height:240px;background:#fff;border-radius:8px;margin:12px 0" onerror="this.style.display='none'">
<div class=done-sub id=qrHint style="font-size:13px;line-height:1.8;text-align:left">
<b>方式一（推荐）</b>：截屏二维码 → 打开夸克APP → 我的 → 扫一扫 → 右上角相册选截图 → 确认登录<br>
<b>方式二</b>：点下方"复制链接"→ 粘贴到夸克APP搜索框打开 → 直接确认登录
</div>
<div style="display:flex;gap:6px;margin-top:10px;align-items:center">
<input type=text id=qrLink readonly style="flex:1;font-size:10px;padding:6px;border:1px solid var(--line);border-radius:6px" onclick="this.select()">
<button style="padding:6px 14px;font-size:13px" onclick="copyQr()">复制链接</button>
</div>
<button class=ghost style="margin-top:10px" onclick="closeQr()">取消登录</button>
</div></div>
<script>
const $=id=>document.getElementById(id);
function fmt(n){const u=["B","KB","MB","GB","TB"];let i=0;n=+n;
while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(1)+u[i]}
async function api(path,body){
const r=await fetch("/api/"+path,{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(body||{})});return r.json()}
async function doParse(){
if(!$("url").value.trim()){$("stat").textContent="先粘贴分享链接";return}
$("stat").textContent="解析中…";
const r=await api("parse",{url:$("url").value,pwd:$("pwd").value,cookie:$("cookie").value,dir:$("dir").value});
if(r.error)$("stat").textContent="失败: "+r.error;}
function render(fs){$("tb").innerHTML=fs.map(f=>
`<tr><td><input type=checkbox class=ck value="${f.fid}"></td><td title="${f.name}">${f.name}</td>
<td>${fmt(f.size)}</td><td title="${f.rel}">${f.rel}</td></tr>`).join("")}
function toggleAll(c){document.querySelectorAll(".ck").forEach(x=>x.checked=c.checked)}
async function dl(all){
let fids=[];
if(all){fids="__ALL__"}else{
fids=[...document.querySelectorAll(".ck:checked")].map(x=>x.value);
if(!fids.length){$("stat").textContent="先勾选文件再点下载";return}}
await api("download",{fids:fids,dir:$("dir").value,cookie:$("cookie").value,threads:+$("threads").value})}
async function cancel(){await api("cancel")}
function setPan(p){
const t=$("panTag");
if(!p){t.style.display="none";return}
t.style.display="inline-block";
if(p==="quark"){t.textContent="夸克网盘";t.className="pan-tag tag-quark";}
else{t.textContent="UC网盘";t.className="pan-tag tag-uc";$("loginTip").style.display="none";}}
function checkCk(){
const v=$("cookie").value;
const hasLogin=/__pus=|__uid=/.test(v);
$("ckBad").style.display=(v.trim()&&!hasLogin&&$("panTag").textContent==="夸克网盘")?"block":"none";}
let lastFp="";
let wasDownloading=false;
function beep(){
try{const ctx=new(window.AudioContext||window.webkitAudioContext)();
[880,1175,1568].forEach((f,i)=>{
const o=ctx.createOscillator(),g=ctx.createGain();
o.connect(g);g.connect(ctx.destination);
o.frequency.value=f;o.type="sine";
g.gain.setValueAtTime(0.18,ctx.currentTime+i*0.12);
g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+i*0.12+0.35);
o.start(ctx.currentTime+i*0.12);o.stop(ctx.currentTime+i*0.12+0.4);});}catch(e){}}
function showDone(finished,transferred,failed){
const total=finished+transferred.length;
$("doneIcon").textContent=failed.length?"⚠️":"✅";
$("doneTitle").textContent=failed.length?"处理完成（部分失败）":"处理完成";
let sub=[];
if(finished)sub.push(`下载到本地 ${finished} 个`);
if(transferred.length)sub.push(`已转存到网盘 ${transferred.length} 个（打开夸克客户端下载）`);
if(failed.length)sub.push(`失败 ${failed.length} 个`);
$("doneSub").textContent=sub.join("，")||"无文件处理";
let det="";
if(transferred.length)det+="已转存：\n"+transferred.join("\n")+"\n\n";
if(failed.length)det+="失败列表：\n"+failed.join("\n");
$("doneFail").textContent=det;
$("doneMask").classList.add("show");
beep();}
async function clearLog(){await api("clear_logs");$("log").textContent="";}
async function dlLog(){await api("save_log")}  // 结果直接打进日志区（alert在p4a壳里被系统吞掉）
function copyQr(){
const el=$("qrLink");el.select();
try{document.execCommand("copy")}catch(e){}
if(navigator.clipboard)navigator.clipboard.writeText(el.value).catch(()=>{});
el.style.background="#dcfce7";setTimeout(()=>el.style.background="",800);}
async function closeQr(){
await api("qr_cancel");
$("qrMask").classList.remove("show");
loginAsking=false;$("loginTip").style.display="none";
$("stat").textContent="已取消登录，重新点解析可再次获取二维码";}
async function autoCk(force){
// 隐藏区备用入口：与自动流程同款扫码登录
const r=await api("login_extract",{pan:"quark"});
if(r.ok&&r.qr_url){
$("qrLink").value=r.qr_url;
$("qrImg").style.display="";
$("qrImg").src="https://api.qrserver.com/v1/create-qr-code/?size=240x240&data="+encodeURIComponent(r.qr_url);
$("qrMask").classList.add("show");loginAsking=true;
$("loginTip").style.display="block";}
else $("stat").textContent="二维码获取失败: "+(r.error||"");}
let loginAsking=false;   // 登录窗已弹出标志（防重入）
async function tick(){
try{const s=await (await fetch("/api/progress")).json();
if(s.total>0)$("b").style.width=(s.done*100/s.total)+"%";
$("prog").textContent=s.current?`${s.current}  ${fmt(s.done)}/${fmt(s.total)}  ${fmt(s.speed)}/s  | 队列 ${s.queue_len} 下载 ${s.finished} 转存 ${(s.transferred||[]).length}`:"—";
if(s.status==="downloading")wasDownloading=true;
if(wasDownloading&&s.status==="idle"){wasDownloading=false;showDone(s.finished,s.transferred||[],s.failed||[]);}
if(s.pan)setPan(s.pan);
// 后端判定需要登录（夸克无登录态/Cookie失效）→ 获取二维码弹层（纯API不跳页）
if(s.need_login&&!loginAsking){
loginAsking=true;
$("loginTip").style.display="block";
api("login_extract",{pan:s.need_login}).then(r=>{
if(r.ok&&r.qr_url){
$("qrLink").value=r.qr_url;
$("qrImg").style.display="";
$("qrImg").src="https://api.qrserver.com/v1/create-qr-code/?size=240x240&data="+encodeURIComponent(r.qr_url);
$("qrMask").classList.add("show");
}else{
loginAsking=false;$("loginTip").style.display="none";
$("stat").textContent="二维码获取失败: "+(r.error||"");
}});}
if(s.cookie_ready){
$("cookie").value=s.cookie_ready;
loginAsking=false;
$("loginTip").style.display="none";
$("qrMask").classList.remove("show");
// 登录成功 → 自动重新解析当前链接
if($("url").value.trim())doParse();
}
const fs=s.files||[];
const fp=s.status+"|"+fs.length+"|"+(fs[0]?fs[0].fid:"")+"|"+(fs.length?fs[fs.length-1].fid:"");
if(s.status==="parsing"){$("stat").textContent="解析中…";lastFp="";}
else if(fp!==lastFp){lastFp=fp;render(fs);
$("stat").textContent=fs.length?`共 ${fs.length} 个文件（勾选后下载）`:"等待解析…";}
$("log").textContent=s.logs.join("\n");$("log").scrollTop=1e9;
}catch(e){}}
setInterval(tick,800);
(async()=>{const c=await(await fetch("/api/config")).json();
$("dir").value=c.dir||"";$("cookie").value=c.cookie||"";
// 登录跳回后自动恢复：有暂存链接且有 Cookie → 自动填入并重新解析
if(c.pending_url&&c.cookie){
$("url").value=c.pending_url;$("pwd").value=c.pending_pwd||"";
await api("clear_pending");
doParse();
}})();
</script></body></html>"""


def build_log_file():
    """生成日志全文（含系统信息头），返回 (body_bytes, 文件名)"""
    lines = [
        "=" * 56,
        "  网盘直链下载器 v2.2 运行日志 (Android)",
        "  导出时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "  系统: " + platform.system() + " " + platform.release()
        + " (build " + platform.version() + ")",
        "  Python: " + platform.python_version(),
        "=" * 56,
        "",
    ]
    with STATE.lock:
        lines += list(STATE.logs)
    fname = "下载器日志_" + time.strftime("%Y%m%d_%H%M%S") + ".txt"
    return "\n".join(lines).encode("utf-8"), fname


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        ln = int(self.headers.get("Content-Length", 0))
        if ln <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/progress"):
            snap = STATE.snapshot()
            snap["cookie_ready"] = STATE.pop_cookie_ready()
            self._json(snap)
        elif self.path.startswith("/api/config"):
            cfg = load_config()
            cfg.setdefault("dir", str(DEFAULT_SAVE_DIR))
            self._json(cfg)
        elif self.path.startswith("/api/files"):
            self._json({"files": STATE.files})
        elif self.path.startswith("/api/log_file"):
            body, fname = build_log_file()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(fname))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            self._do_POST_inner()
        except Exception as e:
            import traceback
            STATE.log(f"接口异常: {e}")
            STATE.log(traceback.format_exc()[-400:])
            try:
                self._json({"error": f"服务内部错误: {str(e)[:100]}"}, 500)
            except Exception:
                pass

    def _do_POST_inner(self):
        d = self._body()
        if self.path == "/api/parse":
            if STATE.status != "idle":
                self._json({"error": "任务进行中，请稍后"})
                return
            save_config({"cookie": d.get("cookie", ""), "dir": d.get("dir", "")})
            STATE.files = []
            threading.Thread(target=parse_worker,
                             args=(d.get("url", ""), d.get("pwd", ""),
                                   d.get("cookie", "")), daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/files_refresh":
            self._json({"files": STATE.files})
        elif self.path == "/api/download":
            if STATE.status == "downloading":
                self._json({"error": "下载进行中"})
                return
            fids = d.get("fids") or []
            if fids == "__ALL__":
                fids = [f["fid"] for f in STATE.files]
            save_dir = d.get("dir") or str(DEFAULT_SAVE_DIR)
            save_config({"cookie": d.get("cookie", ""), "dir": save_dir})
            STATE.queue = list(fids)
            threading.Thread(target=download_worker,
                             args=(fids, save_dir, d.get("cookie", ""),
                                   int(d.get("threads", 32))),
                             daemon=True).start()
            self._json({"ok": True, "count": len(fids)})
        elif self.path == "/api/cancel":
            STATE.cancel.set()
            self._json({"ok": True})
        elif self.path == "/api/clear_logs":
            with STATE.lock:
                STATE.logs = []
            self._json({"ok": True})
        elif self.path == "/api/save_log":
            # Android WebView 不支持 a[download]，日志直接写公共下载目录；
            # 结果打进日志区（p4a 壳里 alert 被系统吞掉，用户看不到弹窗）
            try:
                body, fname = build_log_file()
                out = DEFAULT_SAVE_DIR / fname
                out.write_bytes(body)
                STATE.log("✓ 日志已导出: " + str(out))
                self._json({"ok": True, "path": str(out)})
            except Exception as e:
                STATE.log("✗ 日志导出失败: " + str(e)[:100])
                self._json({"ok": False, "error": str(e)[:120]})
        elif self.path == "/api/login_extract":
            # 夸克扫码登录：生成二维码短链 + 后台轮询（纯API，无需跳网页）
            try:
                qr_url = qr_login_start()
                self._json({"ok": True, "qr_url": qr_url})
            except Exception as e:
                self._json({"ok": False, "error": f"二维码获取失败: {str(e)[:80]}"})
        elif self.path == "/api/qr_cancel":
            qr_login_cancel()
            self._json({"ok": True})
        elif self.path == "/api/save_pending":
            # 跳登录页前暂存链接（页面重载后自动恢复解析）
            cfg = load_config()
            cfg["pending_url"] = d.get("url", "")
            cfg["pending_pwd"] = d.get("pwd", "")
            save_config(cfg)
            self._json({"ok": True})
        elif self.path == "/api/clear_pending":
            cfg = load_config()
            cfg.pop("pending_url", None)
            cfg.pop("pending_pwd", None)
            save_config(cfg)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main():
    global SRV
    DEFAULT_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    SRV = srv
    STATE.log("网盘直链下载器启动 (Android)")
    STATE.log(f"下载目录: {DEFAULT_SAVE_DIR}")
    # Android 11+ 公共目录写权限引导（跳一次系统设置）
    _ensure_storage_permission()
    # p4a webview bootstrap 自动加载 127.0.0.1:5000，主线程跑服务即可
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
