[app]
title = 网盘下载器
package.name = pandownloader
package.domain = com.pandl.app
source.dir = .
source.include_exts = py,png,jpg,kv,txt,json
version = 2.2.0
requirements = python3,pyjnius
orientation = portrait
p4a.bootstrap = webview
android.permissions = INTERNET,MANAGE_EXTERNAL_STORAGE
icon.filename = %(source.dir)s/icon.png
android.archs = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 0
