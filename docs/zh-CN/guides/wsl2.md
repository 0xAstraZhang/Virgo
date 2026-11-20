# 在 Windows 上通过 WSL2 运行 Virgo

在 Windows 上，[原生 Windows（标准 AppContainer）](windows-sandbox.md)与 **WSL2（Windows Subsystem for Linux 2）** 均为**一级支持**环境，默认启用 **Workspace-Edit** 且无需管理员权限。如果你更偏好 Linux 工具链、POSIX 文件系统语义，或需要在沙箱命令中访问**本机回环服务（`127.0.0.1`）**（非管理员 Windows AppContainer 默认阻止回环连接），推荐选择 WSL2。在 WSL2 内，Virgo 直接使用完整的 Linux 沙箱（`bubblewrap` + `libseccomp2`）。

## 1. 安装 WSL2 与 Ubuntu

在 Windows PowerShell 中执行（首次启用 WSL2）：

```powershell
wsl --install -d Ubuntu
```

按提示重启后，从开始菜单启动 **Ubuntu** 并创建 Linux 用户。确认版本为 WSL2：

```powershell
wsl -l -v
```

确认 Ubuntu 对应的 `VERSION` 列为 `2`。

## 2. 安装沙箱依赖与 `uv`

在 WSL2 Ubuntu 终端内执行：

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap libseccomp2 git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

重新加载 Shell（`exec "$SHELL"`）以使 `uv` 进入 `PATH`。

> **Ubuntu 24.04+ 提示：** 若 `bwrap` 报用户命名空间权限错误（`Operation not permitted`），可为当前启动关闭 AppArmor 非特权命名空间限制，或为 `/usr/bin/bwrap` 配置 AppArmor 策略：
>
> ```bash
> sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
> ```

## 3. 将工作区放在 WSL2 的 Linux 文件系统内

请务必把代码仓库克隆并放在 **WSL2 的 Linux 文件系统内**（例如 `~/src/my-project`），**不要**放在 `/mnt/c/...` 下：

- **性能差异**：`/mnt/c` 经过 9P DrvFS 桥接，git、测试套件和包管理器的 I/O 性能远低于 WSL2 内部的 ext4 文件系统。
- **权限与文件系统语义差异**：`/mnt/c` 将 NTFS 权限和大小写不敏感行为映射到 Linux，与 Linux 工具及 `bubblewrap` 预期的 POSIX 权限、符号链接和原子替换语义不同。

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

## 4. 验证 WSL2 原生沙箱验收

在 WSL2 Linux 文件系统内的仓库根目录执行验收脚本：

```bash
./ci/wsl2_acceptance.sh
```

脚本会检查 `bubblewrap` 与 `libseccomp2`、运行带 `VIRGO_REQUIRE_SANDBOX_TESTS=1` 的 Linux 原生沙箱测试，并校验 **Workspace-Edit** 下的冷启动与 `Read` 性能门槛。

另见：[执行边界与沙箱](sandbox.md) · [原生 Windows 沙箱](windows-sandbox.md)
