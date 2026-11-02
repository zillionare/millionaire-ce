# 原生安装与后台服务

发布流水线构建包含 Python 3.13 和固定版本依赖的安装包，无需用户自行安装 Python。
发布前对同一次构建执行安装、启停、崩溃恢复、升级和卸载保留数据验收。
只有质量、测试、真实源检查和安装验收全部通过，tag 流水线才发布 GitHub Release。

| 平台 | 产物 | 默认服务 |
| --- | --- | --- |
| macOS Apple Silicon（ARM64） | `.tar.gz`，解压后运行 `sh install.sh` | 当前用户的 LaunchAgent，登录后启动 |
| Ubuntu x86_64 | `.deb`，`sudo apt install ./millionaire-ce-…deb` | systemd 系统服务，开机启动 |
| Windows x64 | `.exe` 图形安装向导，需要管理员权限 | WinSW Windows 系统服务，开机启动 |

macOS 官方支持范围仅包含 Apple Silicon（M 系列芯片）；不提供 Intel（x86_64）安装包或验收支持。

当前工程验证状态见 `review/2026-11-02/IMPLEMENTATION.md`；尚未通过的目标不能作为
已发布支持证据。macOS 包目前没有 Developer ID 签名或 Apple 公证；内置动态库使用
ad-hoc 签名。Windows 没有配置发行者代码签名证书。WinSW 和 OpenMP 的版权许可声明随相应安装包提供。

安装后通过 `millionaire-ce control` 打开前台控制器，启停服务、查看状态或打开浏览器。
Windows 安装器会创建控制器快捷方式。关闭控制器窗口不影响后台服务。
默认浏览器地址为 `http://127.0.0.1:8130`。

```bash
millionaire-ce service status
millionaire-ce service stop
millionaire-ce service start
```

macOS 默认安装到 `~/.local/share/millionaire-ce`，CLI 链接在 `~/.local/bin`。
若终端找不到命令，将该目录加入 PATH。升级时运行新版安装脚本；服务检查失败时
恢复上一版本。卸载执行安装脚本输出的 `uninstall.sh` 路径，默认保留用户数据。

macOS 服务器可安装为无需登录的系统 LaunchDaemon，并指定运行账户：

```bash
sudo sh install.sh --system --user quantide
sudo millionaire-ce service status --system
sudo millionaire-ce service stop --system
sudo millionaire-ce service start --system
```

该账户必须预先存在且不能是 root。系统安装默认代码在 `/usr/local/share/millionaire-ce`，
CLI 在 `/usr/local/bin`；配置、扩展和数据在 `/Library/Application Support/MillionaireCE`。

Ubuntu 默认代码位于 `/opt/millionaire-ce`，运行用户为 `millionaire-ce`，持久数据在
`/var/lib/millionaire-ce`。使用 `sudo apt remove millionaire-ce` 卸载程序时保留数据；
显式执行 `apt purge` 会删除该目录。系统服务管理命令需要 sudo。

Windows 默认程序目录为 `C:\Program Files\MillionaireCE`，持久数据位于
`C:\ProgramData\MillionaireCE`。升级再次运行新版安装器。卸载默认保留数据；交互卸载
会另行提供删除数据选项，默认回答否。非管理员启动/停止系统服务时控制器会请求 UAC。

所有安装包及 `.sha256` 校验文件由 GitHub Releases 提供。开发源码安装方式仍见
[Installation](installation.md)，实时行情配置见[行情扩展](quote-extensions.md)。
