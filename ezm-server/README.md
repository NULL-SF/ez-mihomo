# ezm-server：ez-mihomo 服务端

本项目使用 mihomo 1.19.33 提供七种协议的节点服务，每种协议独立端口。核心随目录提供，仅支持 Linux x86_64；首次 `start` 自动配置，之后可直接复制 `output/links/` 中的节点链接。

服务端收到认证连接后直接访问目标，不自动使用同仓库客户端的代理。启动后保持后台运行。

## 1 基本配置

### 1.1 服务端口

`config/ports/` 每种协议一个 `.txt` 文件，只填写一个 `1024–65535` 的整数，七个端口不能重复。

| 协议 | 默认端口 | 监听传输 | 基础方案 | 申请证书 |
|---|---|---|---|---|
| VLESS | 11801 | TCP | REALITY + Vision | 不需要 |
| VMess | 11802 | TCP | AEAD + TCP + TLS | 需要 |
| Hysteria2 | 11803 | UDP | QUIC + Salamander | 需要 |
| Trojan | 11804 | TCP | TCP + TLS | 需要 |
| Shadowsocks | 11805 | TCP、UDP | SS2022 | 不需要 |
| TUIC | 11806 | UDP | v5 + QUIC | 需要 |
| AnyTLS | 11807 | TCP | TCP + TLS | 需要 |

“监听传输”表示客户端连接 VPS 时使用 TCP 还是 UDP，用于放行对应的安全组和防火墙端口。五种需要证书的方案共用一套自动证书，客户端通常无需另行配置。

VLESS 的 REALITY 握手目标固定为 `www.microsoft.com:443`，VPS 需能访问该目标。VMess 由客户端直接连接本 VPS，不需要额外的中转服务器。

首次启动前可修改端口文件；`start` 按这些端口生成运行配置和链接。之后的启停沿用已有配置，修改端口文件不会自动修改已经生成的节点。

### 1.2 节点链接

首次启动在 `output/links/` 下生成 `vless.txt`、`vmess.txt`、`hysteria2.txt`、`trojan.txt`、`shadowsocks.txt`、`tuic.txt`、`anytls.txt`。每个文件一条完整链接，可以打开文件复制，或用 `links` 查看。

链接包含连接凭据，应私下保存。手改链接不会修改服务端，启停和重复启动不更换节点凭据。

### 1.3 自动证书

首次 `start` 先尝试探测公网 IPv4 出口地址，失败后再尝试 IPv6；两者均未取得有效结果时终止初始化。探测到出口地址不代表该地址可从公网入站访问，所选地址仍需能通过验证端口和节点端口访问本机。

随后自动向 [Let’s Encrypt](https://letsencrypt.org/) 申请免费受信任证书，无需域名或手动准备证书文件。首次申请会联网验证，需要等待完成；之后按证书剩余有效期决定是否更新。

启动自动申请即表示同意签发机构的[服务条款](https://letsencrypt.org/repository/)。验证需要公网 TCP 80 可达、未被占用，并有监听权限，通常以 root 运行。脚本不会停止占用验证端口的其他程序，也不修改其他防火墙或云安全组。

证书由 Certbot 自动申请和续期。已有兼容版本时直接使用，否则首次启动会在项目内自动安装。

VMess、Hysteria2、Trojan、TUIC、AnyTLS 共用 `certs/server.crt`、`certs/server.key`。VLESS 使用 REALITY 公钥验证，链接已包含所需公钥；Shadowsocks 不需要证书。私钥只留在服务端，客户端通常导入链接即可。

IP 证书有效期约 6 天半。每次 `start` 都检查 cron 和当前证书，并按本次启动时间重新登记任务：下一次约 24 小时后检查，之后每天检查，无需额外常驻进程；TCP 80 在后续验证时也需可达。

每次检查先读取证书的实际到期时间，仅在证书缺失或剩余不足 48 小时时更新；有效期充足时不请求签发。证书热加载，不重启监听。

续期失败保留旧证书，约 1 小时后重试；证书机构要求等待更久时遵循其时间，成功后移除重试任务，恢复每日检查。错误写入 `log/certificate.log`；若一直失败直至过期，新连接将验证失败。

`stop` 会暂停续期、删除日检和失败重试任务，并终止正在运行的续期进程。下一次 `start` 恢复续期，证书缺失或剩余不足 48 小时时先更新证书，再启动代理。更新暂时失败但旧证书仍有效时可沿用旧证书启动，并在后台重试；旧证书已不可用时启动会报错，更新成功后可重新执行 `start`。

## 2 命令

先进入项目目录（路径按实际位置修改）：

```bash
cd /path/to/ezm-server
```

管理命令格式：

```text
sudo bash ./server.sh <命令>
```

以下命令统一以 sudo 管理为例，已有 root 身份时可省略 sudo。sudo 会在终端按需询问密码；启动后也使用同一用户管理，包括 `stop`，避免进程权限和 crontab 分属不同用户。

### 2.1 服务管理

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh start` | 先关闭 UFW，再检查 cron 和证书；首次自动初始化，之后沿用配置并重排续期检查，不询问配置。 |
| `sudo bash ./server.sh stop` | 停止全部代理及自动续期，保留证书、配置和节点凭据。 |
| `sudo bash ./server.sh restart` | 先执行 `stop`，再执行 `start`。 |

`start` 先关闭 UFW，再检查并安装缺少的运行依赖、启用 cron、检查或申请证书，最后启动节点。未安装 UFW 时跳过关闭操作；关闭失败则不继续启动。

这些命令统一操作七种协议，不接受协议参数。停止和重启会中断现有代理连接；证书正常续期不重启代理。

### 2.2 状态、链接与日志

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh status` | 查看核心版本和本项目后台进程，不进行连接测试。 |
| `sudo bash ./server.sh links` | 菜单选择全部链接或某个协议，回车默认全部。 |
| `sudo bash ./server.sh log` | 菜单选择全部日志、某个协议或证书续期日志，回车默认全部；按 `Ctrl+C` 退出查看。 |

### 2.3 开机自启与定时任务

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh boot` | 选择开启或关闭代理开机自启，不影响当前监听和续期。 |

开机任务写入执行命令用户的 crontab（使用 sudo 时为 root），开机后等待 20 秒再执行 `start`。通过 sudo 登记的任务会以 root 身份运行，开机时无需输入密码。`start` 本身不询问或改变代理开机自启设置；需要时单独执行 `boot`。`stop` 不取消代理的开机自启登记，下次开机仍可启动并恢复续期。开机任务只启动一次，核心异常退出不会自动重启。

服务端每次启动都检查运行依赖：root 下缺少时通过 apt-get、dnf 或 yum 安装，并启用 cron。已有依赖直接复用，不执行整机软件升级；RHEL 系使用兼容的 Python 命令，不替换系统默认 Python。系统软件源需能提供 Python 3.10+，不支持的旧系统需自行准备兼容环境。

## 3 项目文件与目录

所有路径均相对于本项目目录。**一般只需确认端口后执行 `start`**；生成的配置和链接不要当作手动配置修改。

| 路径 | 是什么 | 是否手动修改 |
|---|---|---|
| `server.sh` | 管理入口。 | 不改 |
| `scripts/dependencies.sh` | 首次及后续启动时检查、补齐系统运行依赖。 | 不改 |
| `scripts/configure.py` | 首次自动初始化，生成节点配置、凭据和链接。 | 不改 |
| `scripts/certificate.py` | 准备 cron、申请证书、管理续期及热更新。 | 不改 |
| `scripts/service.py` | 进程身份、监听检查及启停。 | 不改 |
| `bin/mihomo_1.19.33`、`bin/LICENSE.mihomo` | 核心及许可证。 | 不改 |
| `config/ports/` | 各协议的端口文件。 | 首次启动前按需修改 |
| `certs/` | 当前使用的证书和私钥。 | 不改 |
| `runtime/` | 节点配置、进程状态、锁、ACME 账户和续期状态；含凭据。 | 需保留，不要当缓存清空 |
| `log/` | 各协议日志、开机启动与证书续期日志。 | 按需查看 |
| `output/links/` | 七种协议的节点链接。 | 直接复制，不改 |

核心来源：[MetaCubeX/mihomo v1.19.33 官方发行](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.33)。

## 4 节点方案与链接

方案选择的优先级：**安全正确 → 稳定性与兼容性 → 抗识别与抗干扰 → 性能**。

每种协议保留一套基础方案，避免把所有核心参数都变成配置项。

| 选择 | 原因 |
|---|---|
| TLS 链接默认要求客户端验证证书 | 确保客户端能验证服务器身份。 |
| VLESS 使用 REALITY + Vision，VMess 保留 TLS | VLESS 使用 REALITY 的伪装机制并免去证书申请；VMess 保留 TLS 保护。 |
| Hysteria2 启用 Salamander，AnyTLS 使用核心默认填充 | 使用协议已有的混淆与填充机制。 |
| Shadowsocks 使用 SS2022，不额外叠加插件 | 减少客户端依赖和配置组合。 |
| 固定核心版本，不做内核性能调优 | 便于维护和复现，性能调优放在后面。 |

UUID、密码和混淆密码自动生成，启停不更换凭据。链接遵循对应协议的分享格式；实际可导入范围取决于客户端实现。

## 5 迁移

本目录可独立运行，不需要仓库根目录或客户端目录。需要 Linux x86_64、Bash、可用的软件源和出网能力。运行依赖包括 Python 3.10+、venv、OpenSSL、CA 证书、curl、flock 和 cron，首次 `start` 会检查并自动补齐缺少的组件。

首次安装可在 root 用户下执行，无需 Git：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/NULL-SF/ez-mihomo/main/install.sh)
```

运行后选择 `1`（服务端），将独立项目下载到当前目录的 `ezm-server/`，不启动服务。已有安装目录时不会覆盖；也可在脚本后传入其他安装目录。下载后手动启动：

```bash
cd ezm-server
bash ./server.sh start
```

若尚未安装 curl 或 CA 证书，先按系统安装下载工具：

```bash
# Debian / Ubuntu（root 用户）
apt-get update && apt-get install -y curl ca-certificates

# Fedora / RHEL 系（root 用户）
dnf install -y curl ca-certificates
# 使用 yum 的系统将 dnf 换成 yum
```

非 root 用户可先通过 `sudo -i` 切换到 root。安装入口会检查并补齐 tar、gzip 等下载组件，运行依赖仍由 `start` 自动准备。

也可以使用 Git 下载：

```bash
git clone --depth 1 https://github.com/NULL-SF/ez-mihomo.git
cd ez-mihomo/ezm-server
sudo bash ./server.sh start
```

若是从其他机器复制项目，则进入实际的 `ezm-server` 目录后执行 `start`。核心和脚本需保留执行权限；Git 下载会保留这些权限。

迁移已有节点且公网地址不变时，先停止旧实例，保留并复制 `config/`、`certs/`、`runtime/` 和 `output/links/`。目标目录执行 `start` 会沿用凭据、恢复续期并登记当前路径；如需开机自启，再执行 `boot`。

换用新公网 IP 的 VPS 时，使用全新目录，首次 `start` 自动生成对应地址的证书和链接。目录自带 `.gitignore` 排除凭据、证书和运行产物，可以单独作为项目使用。
