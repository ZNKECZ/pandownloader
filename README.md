# 网盘下载器 Android 版

夸克 / UC 网盘分享链接 → 解析 → 直链提取 → 多线程下载到手机，免客户端、免会员限速。

## 下载安装

**[点这里下载最新 APK](../../releases/latest)**（无需登录 GitHub）

安装时按提示允许「未知来源应用」即可。首次打开会跳系统设置请求「所有文件访问」权限（用于把文件保存到 `Download/网盘下载/`），允许后返回。

## 使用（3 步）

1. **粘贴分享链接** → 点【解析】（自动识别夸克 / UC）
2. 夸克首次使用会**弹出登录二维码**：夸克 APP「我的 → 扫一扫」扫码（或截屏后扫一扫选相册识别，也可以复制链接到夸克 APP 内打开）→ 确认登录，Cookie 自动保存，以后不用再扫
3. 文件列表出来后**勾选 → 下载**，文件在文件管理器 `Download/网盘下载/` 里

## 功能

- 粘贴分享链接自动识别网盘（夸克 pan.quark.cn / UC drive.uc.cn）
- 夸克扫码登录（纯 API，无需在 WebView 里登录），Cookie 持久保存
- 多线程分块下载（32–128 线程可调），滑动窗口实时测速
- 文件夹递归解析、批量勾选、下载日志导出
- UC 网盘免登录直下

## 源码与编译

- `main.py` —— 全部逻辑（纯标准库 + pyjnius），HTTP 服务 + WebView 壳
- `buildozer.spec` —— python-for-android 打包配置
- `.github/workflows/build.yml` —— GitHub Actions 云编译：push 后自动 Buildozer 编译并发布 Release

自行编译：fork 本仓库后 push 任意修改即触发，约 10 分钟出包（SDK/NDK 有缓存）。
