# Running Virgo on Windows with WSL2

On Windows, both [native Windows 11 (Standard AppContainer with `null.sys` Feature `60692039`)](windows-sandbox.md) and **WSL2 (Windows Subsystem for Linux 2)** are **Tier 1** supported environments defaulting to **Workspace-Edit** without requiring Administrator privileges. Choose WSL2 when you are on Windows 10 / older Windows 11 / Windows Server builds without Feature `60692039`, prefer Linux toolchains and POSIX filesystem semantics, or need **localhost/loopback (`127.0.0.1`)** access inside sandboxed commands (which Windows blocks inside non-admin AppContainer). Inside WSL2, Virgo uses the full Linux sandbox (`bubblewrap` + `libseccomp2`).

## 1. Install WSL2 and Ubuntu

Open Windows PowerShell (once, to enable WSL2):

```powershell
wsl --install -d Ubuntu
```

Restart if prompted, then launch **Ubuntu** from the Start Menu and finish creating your Linux user account. Confirm you are running WSL2:

```powershell
wsl -l -v
```

The `VERSION` column for Ubuntu should show `2`.

## 2. Install sandbox dependencies and `uv`

Inside your WSL2 Ubuntu terminal:

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap libseccomp2 git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Reload your shell (`exec "$SHELL"`) so `uv` is on `PATH`.

> **Ubuntu 24.04+ note:** If `bwrap` reports a user-namespace permission error (`Operation not permitted`), allow unprivileged user namespaces for the current boot or configure an AppArmor profile for `/usr/bin/bwrap`:
>
> ```bash
> sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
> ```

## 3. Keep repositories inside the WSL2 Linux filesystem

Always clone and work on projects **inside the WSL2 Linux filesystem** (for example `~/src/my-project`), **not** under `/mnt/c/...`:

- **Performance**: `/mnt/c` crosses the 9P DrvFS bridge; git operations, test suites, and package managers are dramatically slower there than on ext4 inside WSL2.
- **Filesystem semantics**: `/mnt/c` uses NTFS permissions and case-insensitivity mapped through DrvFS, which differs from native POSIX permissions, symlinks, and atomic `os.replace` behavior expected by Linux tools and `bubblewrap`.

```bash
mkdir -p ~/src && cd ~/src
git clone https://github.com/0xAstraZhang/Virgo.git
cd Virgo
uv sync --locked
uv run virgo
```

## 4. Verify WSL2 native sandbox acceptance

From your WSL2 checkout inside the Linux filesystem, run the bundled acceptance script:

```bash
./ci/wsl2_acceptance.sh
```

This verifies `bubblewrap` + `libseccomp2`, runs the required Linux native sandbox tests (`VIRGO_REQUIRE_SANDBOX_TESTS=1`), and checks the cold-start and `Read` performance budgets in **Workspace-Edit**.

See also: [Execution boundaries](sandbox.md) · [Native Windows sandbox](windows-sandbox.md)
