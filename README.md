# Codex Launcher

一个原生 macOS 菜单栏工具，用于保存、切换多个 Codex / ChatGPT 桌面账号，并查看订阅等级与用量。界面为中文，SwiftUI + AppKit，后台使用 Python 标准库。

这是个人项目，**不是 OpenAI 官方应用**。当前实现主要针对采用 Codex 文件认证的桌面应用，不是通用的 ChatGPT 网页账号切换器。

![界面示意（虚构账号）](Assets/Menu%20Preview.png)

## 功能

- 添加账号、切换账号、打开桌面应用。
- 显示 Free、Go、Plus、Pro 5x、Pro 20x 等订阅标签。
- 账号右侧圆环显示剩余百分比和重置倒计时；彩色弧段表示剩余额度，红色短线表示周期已经过去的时间比例。
- 接口返回多个周期时分别显示；没有返回的周期不会虚构。
- 每 5 分钟自动刷新，支持手动刷新，并显示距上次刷新尝试的时间。
- 点击菜单外部自动收起；待切换账号和按钮有悬停背景。

## 编译前准备

目前构建脚本仅支持 **Apple Silicon（M 系列）Mac，macOS 14 或更新版本**。Intel Mac 尚未验证。

1. 安装 Xcode Command Line Tools：

   ```sh
   xcode-select --install
   ```

   安装窗口完成后再继续。已经安装时不用重复安装。

2. 安装 Python 3.9 或更新版本。推荐使用 [Homebrew](https://brew.sh/)：

   ```sh
   brew install python
   /opt/homebrew/bin/python3 --version
   ```

   Python 不需要任何 pip 依赖。运行中的应用会按顺序寻找 `/opt/homebrew/bin/python3`、`/usr/local/bin/python3`、已有的 Codex Python 运行时，以及 Command Line Tools 的 Python。Finder 启动的应用不依赖终端 PATH；如果 Python 在别的位置，需要修改 `Launcher.swift` 中的 `candidates` 再构建。

3. 安装支持 Codex 文件认证的桌面应用，并至少登录一次。

   **默认目标为 `/Applications/ChatGPT.app`**，且安装包内需要存在 `Contents/Resources/codex` 登录程序。源码是针对作者本机以此名称安装的 Codex 桌面应用编写的。

   如果你的应用是 `/Applications/Codex.app`，在构建前修改 `account_manager.py` 顶部：

   ```python
   APP_PATH = Path('/Applications/Codex.app')
   ```

   可先检查登录程序是否存在：

   ```sh
   test -x /Applications/Codex.app/Contents/Resources/codex && echo 'Found bundled Codex CLI'
   ```

   仅把普通 ChatGPT 客户端重命名并不能使它兼容。若没有这个登录程序，让 Codex 检查实际安装路径和认证方式后再使用。

## 编译和安装

```sh
git clone https://github.com/david-pro-preview/codex-launcher.git
cd codex-launcher
PYTHON_BIN=/opt/homebrew/bin/python3 bash build.sh
```

输出为 `dist/Codex Launcher.app`，中间文件在 `.build/`。脚本会进行本地 ad-hoc 签名，不需要付费 Apple 开发者会员；不会提交 Apple 公证或上传账号信息。

打开构建产物目录，把应用拖入 Finder 的「应用程序」：

```sh
open dist
```

或安装到当前用户的应用目录：

```sh
mkdir -p "$HOME/Applications"
ditto "dist/Codex Launcher.app" "$HOME/Applications/Codex Launcher.app"
open "$HOME/Applications/Codex Launcher.app"
```

请只保留一个正在使用的安装副本。更新时先从启动器底部电源按钮退出，再重新构建并覆盖同一个安装位置。没有配置开机自动启动。

## 使用与账号数据

第一次启动会读取 `~/.codex/auth.json` 中现有的登录账号。点击「添加新账号」会打开官方浏览器登录流程；重复登录同一账号会更新已有记录。

**切换或添加账号会退出当前 ChatGPT / Codex 桌面应用和 Codex app-server，包括 IDE 启动的 app-server，正在运行的任务会被中断。先等任务结束再操作。** 普通 Codex CLI 命令和无关进程不在终止目标内。如果 IDE 持续重启后台，启动器会停止切换并提示先关闭相关 IDE。

- 所有账号共用 `~/.codex` 和 `~/Library/Application Support/Codex`，不提供独立工作区或浏览器配置隔离。
- 每次切换会备份最新认证，再原子替换 `~/.codex/auth.json`。本地历史共用，云端资源的访问权限取决于当前账号。
- 账号备份、索引和头像位于 `~/Library/Application Support/Codex Launcher/`。目录权限为 700，认证文件为 600；认证文件**没有额外加密**。
- 只支持文件认证。若 `~/.codex/config.toml` 配置了非 `file` 的 `cli_auth_credentials_store`，应用会提示并停止，不自动改钥匙串或配置。
- 可导入作者早期 A/B 工具的本地备份；普通用户无需创建该目录。
- 不要上传或分享 `auth.json`、账号备份目录、访问令牌或刷新令牌。本仓库只有源码、图标、虚构账号预览和测试。

## 用量说明与常见问题

用量读取使用各账号已有的 access token，请求 ChatGPT 的 `/backend-api/wham/usage`。这是可能变化的接口；读取失败不代表额度为零。

- **提示登录已过期**：重新添加该账号以更新认证。用量刷新不会主动轮换 refresh token，避免与运行中的 Codex 争用认证。
- **只有一个圆环**：接口当前仅返回一个用量周期，这是正常情况。
- **灰色圆环 / 等待更新**：数据过期、刷新失败或上个周期已经结束。应用保留旧数据但不会假设自动恢复 100%。
- **底部“几分钟前刷新”**：指上次刷新尝试；失败原因在相应账号卡片显示。
- **打不开目标应用**：检查 `APP_PATH` 和内置 `codex` 的位置。
- **找不到 Python**：检查上面的固定搜索路径。`PYTHON_BIN` 只选择构建时 Python，不改变运行时搜索路径。
- **Xcode license 错误**：按系统提示完成许可设置。构建脚本在独立 Command Line Tools 可用时优先使用它；也可检查 `xcode-select -p` 和 `xcrun --find swiftc`。

## 验证

后台测试使用临时目录、虚构认证和模拟网络/进程，不切换你的真实账号：

```sh
/opt/homebrew/bin/python3 -m unittest discover -s tests -p 'test_*.py'
"dist/Codex Launcher.app/Contents/MacOS/Codex Launcher" --self-test
```

生成虚构账号界面预览（不会读取真实账号）：

```sh
"dist/Codex Launcher.app/Contents/MacOS/Codex Launcher" --render-preview "$PWD/.build/preview.png"
```

当前有 28 项 Python 测试，覆盖账号保存与切换、取消恢复、互斥、进程识别、用量解析和刷新缓存。另有原生解码和周期进度检查。真实多账号切换会终止相关应用，请不要在 Codex 正执行任务时测试。

## 给协助安装的 Codex

先读取本 README 和 `AGENTS.md`，确认系统架构、Python、目标应用路径及文件认证模式，再构建。不要把用户认证内容打印到终端或复制进仓库。默认执行测试、构建与安装即可；不要为了验证安装而执行账号切换或添加账号。

## 参考和图标

订阅名称映射与用量接口参考 [CodexBar](https://github.com/steipete/CodexBar/tree/main/Sources/CodexBarCore/Providers/Codex)。

应用图标为生成的自定义图像；菜单栏图标来自本机 OpenAI 桌面应用的模板图标。OpenAI / ChatGPT 名称和标识属于其各自权利人，本项目不代表其官方产品或授权。界面预览中的姓名和邮箱为虚构数据。
