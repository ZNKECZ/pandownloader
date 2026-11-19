[app]
title = 网盘下载器
package.name = pandownloader
package.domain = com.pandl.app
source.dir = .
source.include_exts = py,png,jpg,kv,txt,json
version = 2.2.0
# pyjnius = Java 桥（CookieManager 读 WebView 登录 Cookie）
# certifi = Mozilla CA 证书包（修 p4a 的 SSL CERTIFICATE_VERIFY_FAILED）
requirements = python3,pyjnius,certifi
orientation = portrait
p4a.bootstrap = webview
android.permissions = INTERNET,MANAGE_EXTERNAL_STORAGE
icon.filename = %(source.dir)s/icon.png
android.archs = arm64-v8a

# 用 GitHub 云机预装的 Android SDK（避免自己下载+license弹窗无人应答）
android.sdk_path = /usr/local/lib/android/sdk

[buildozer]
log_level = 2
warn_on_root = 0
