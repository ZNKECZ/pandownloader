[app]

# 应用基本信息
title = 网盘下载器
package.name = pandownloader
package.domain = com.pandl.app

# 入口与版本
source.dir = .
source.include_exts = py,png,jpg,kv,txt,json
version = 2.2.0

# 依赖：纯标准库 + pyjnius（调 Android Java API）
requirements = python3,pyjnius

# 横竖屏
orientation = portrait

# WebView bootstrap：APK 启动后跑 main.py 起 HTTP 服务，WebView 加载 127.0.0.1:5000
p4a.bootstrap = webview

# 权限：联网 + 所有文件访问（写公共 Download 目录）
android.permissions = INTERNET,MANAGE_EXTERNAL_STORAGE

# 图标
icon.filename = %(source.dir)s/icon.png

# Android API 级别（p4a 默认工具链兼容值）
android.api = 33
android.minapi = 24
android.ndk = 25b

# 架构（arm64 为主力机型；要兼容老机器可加 armeabi-v7a，体积翻倍）
android.archs = arm64-v8a

[buildozer]

# 日志级别
log_level = 2

warn_on_root = 0
