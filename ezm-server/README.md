# ezm-server：ez-mihomo 服务端

本项目使用 mihomo 1.19.33 提供七种协议的节点服务，每种协议独立端口。核心随目录提供，仅支持 Linux x86_64。`start` 检查各协议的配置和链接，并在后台启动服务；节点链接保存在 `output/links/`，可直接复制。

## 1 基本配置

### 1.1 服务端口

`config/ports/` 每种协议一个 `.txt` 文件，只填写一个 `1024–65535` 的整数，七个端口不能重复。

| 协议 | 默认端口 | 监听传输 | 基础方案 | TLS 证书 |
|---|---|---|---|---|
| VLESS | 11801 | TCP | REALITY + Vision | 不需要 |
| VMess | 11802 | TCP | AEAD + TCP + TLS | 需要 |
| Hysteria2 | 11803 | UDP | QUIC + Salamander | 需要 |
| Trojan | 11804 | TCP | TCP + TLS | 需要 |
| Shadowsocks | 11805 | TCP、UDP | SS2022 | 不需要 |
| TUIC | 11806 | UDP | v5 + QUIC | 需要 |
| AnyTLS | 11807 | TCP | TCP + TLS | 需要 |

“监听传输”表示客户端连接 VPS 时使用 TCP 还是 UDP，用于放行对应的安全组和防火墙端口。五种需要证书的方案共用一套自动申请的 TLS 服务器证书，用于客户端验证服务器身份，客户端通常无需另行配置。

VLESS 的 REALITY 握手目标固定为 `www.microsoft.com:443`，VPS 需能访问该目标。VMess 由客户端直接连接本 VPS，不需要额外的中转服务器。

`start` 重建节点时按对应端口文件生成配置和链接；完整且对应的节点直接沿用，修改端口文件不会自动修改已有节点。

### 1.2 节点链接

`start` 在 `output/links/` 下按需生成 `vless.txt`、`vmess.txt`、`hysteria2.txt`、`trojan.txt`、`shadowsocks.txt`、`tuic.txt`、`anytls.txt`。每个文件一条完整链接，可以打开文件复制，或用 `links` 查看。

链接包含连接凭据，应私下保存。`start` 逐协议检查配置与链接：两者完整且对应时沿用；任一缺失、损坏或不对应时，会提示并仅重新生成该协议的配置和链接，完成后提醒客户端导入新链接，其他节点不变。

需要重建某个节点时，删除 `output/links/协议名.txt` 后执行 `start`。重建会更换该节点的凭据，客户端需重新导入新链接；修改端口时先修改对应的端口文件。

### 1.3 自动 TLS 证书

没有现有证书时，自动选择本机启用网卡上的公网地址：优先 IPv4，否则使用 IPv6；IPv6 优先使用非临时、未弃用的地址。仅有公网 IPv6 时不使用共享 IPv4 出口。网卡上没有公网地址时，才尝试探测映射的 IPv4 出口；此时需自行确认公网端口已映射到本机。

已有证书时沿用证书中的地址重建节点。公网 TCP 80 和各节点端口必须能入站访问；使用 IPv6 节点时，客户端网络也需支持 IPv6。

随后自动向 [Let’s Encrypt](https://letsencrypt.org/) 为所选公网 IP 申请免费的受信任 TLS 服务器证书，无需域名或手动准备证书文件。申请时会联网验证，需要等待完成；之后按证书剩余有效期决定是否更新。

启动自动申请即表示同意签发机构的[服务条款](https://letsencrypt.org/repository/)。验证需要公网 TCP 80 可达、未被占用；程序以 root 身份临时监听该端口。脚本不会停止占用验证端口的其他程序，也不修改其他防火墙或云安全组。

证书由 Certbot 自动申请和续期。已有兼容版本时直接使用，缺少时在项目内自动安装。

VMess、Hysteria2、Trojan、TUIC、AnyTLS 直接读取 `runtime/certs/config/live/ezm-node/fullchain.pem`、`privkey.pem`，不另存部署副本。VLESS 使用 REALITY 公钥验证，链接已包含所需公钥；Shadowsocks 不需要证书。私钥只留在服务端，客户端通常导入链接即可。

IP 证书有效期约 6 天半。每次 `start` 都检查 cron 和当前证书，并按本次启动时间重新登记任务：下一次约 24 小时后检查，之后每天检查，无需额外常驻进程；TCP 80 在后续验证时也需可达。

每次启动校验 Certbot 保存的原始证书和私钥。原件或续期配置缺失、无效时重新申请；失败时保留旧数据并报错或安排重试，不更换节点凭据和链接。

每次检查先读取证书的实际到期时间，仅在原件缺失、无效或剩余不足 48 小时时更新；有效期充足时不请求签发。证书热加载，不重启监听。

续期失败保留旧证书，约 1 小时后重试；证书机构要求等待更久时遵循其时间，成功后移除重试任务，恢复每日检查。定时任务的错误写入 `log/certs/renewal.log`；若一直失败直至过期，新连接将验证失败。

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

`start`、`stop`、`restart`、`boot` 必须以 root 身份执行，普通用户需使用 sudo，root 用户可省略。启动需要管理 UFW、安装缺失的系统依赖，并临时监听 TCP 80 完成公网 IP 控制权验证，以申请或续期 TLS 服务器证书；启停涉及 root 进程和证书续期任务，开机自启写入 root 的 crontab，因此统一由 root 管理。

查看状态、链接和日志无需 sudo；PID、链接和日志允许安装用户组读取，其他运行文件保留默认权限。

### 2.1 服务管理

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh start` | 先关闭 UFW，再检查 cron 和证书；逐协议检查配置和链接，异常时仅重建对应节点；重排续期检查并在后台启动服务，完整且已运行的节点不重复启动，不询问配置。 |
| `sudo bash ./server.sh stop` | 停止全部代理及自动续期，保留证书、配置和节点凭据。 |
| `sudo bash ./server.sh restart` | 先执行 `stop`，再执行 `start`。 |

`start` 先关闭 UFW，再检查并安装缺少的运行依赖、启用 cron、检查或申请证书，最后启动节点。未安装 UFW 时跳过关闭操作；关闭失败则不继续启动。

服务端每次执行 `start` 都检查运行依赖：root 下缺少时通过 apt-get、dnf 或 yum 安装，并启用 cron。已有依赖直接复用，不执行整机软件升级；RHEL 系使用兼容的 Python 命令，不替换系统默认 Python。系统软件源需能提供 Python 3.10+，不支持的旧系统需自行准备兼容环境。

这些命令统一操作七种协议，不接受协议参数。停止和重启会中断现有代理连接；证书正常续期不重启代理。

### 2.2 状态、链接与日志

| 命令 | 说明 |
|---|---|
| `bash ./server.sh status` | 查看核心版本和本项目后台进程，不进行连接测试。 |
| `bash ./server.sh links` | 菜单选择全部链接或某个协议，回车默认全部。 |
| `bash ./server.sh log` | 菜单选择全部日志、某个协议或证书申请与续期日志，回车默认全部；按 `Ctrl+C` 退出查看。 |

### 2.3 开机自启与定时任务

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh boot` | 选择开启或关闭代理开机自启，不影响当前监听和续期。 |

`boot` 选择开启后，将开机任务写入 root 的 crontab；选择关闭则删除该任务。开机时，cron 等待 20 秒后以 root 身份执行 `start`，无需输入 sudo 密码。

## 3 项目文件与目录

所有路径均相对于本项目目录。**一般只需确认端口后执行 `start`**；生成的配置和链接不要当作手动配置修改。

| 路径 | 是什么 | 是否手动修改 |
|---|---|---|
| `server.sh` | 管理入口。 | 不改 |
| `scripts/dependencies.sh` | 启动时检查、补齐系统运行依赖。 | 不改 |
| `scripts/configure.py` | 逐协议检查并生成配套的节点配置和链接。 | 不改 |
| `scripts/certificate.py` | 准备 cron、申请证书、管理续期及热更新。 | 不改 |
| `scripts/service.py` | 进程身份、监听检查及启停。 | 不改 |
| `bin/mihomo_1.19.33`、`bin/LICENSE.mihomo` | 核心及许可证。 | 不改 |
| `config/ports/` | 各协议的端口文件。 | 生成节点前按需修改 |
| `runtime/certs/` | 证书、私钥、签发账户、续期状态和证书工具。 | 不改 |
| `runtime/协议名/` | 对应协议的 `config.json` 和 `mihomo.pid`；配置含凭据。 | 不改 |
| `runtime/` | 各协议运行目录、证书目录和运行状态。 | 需保留，不要当缓存清空 |
| `log/` | 各协议日志、开机启动日志。 | 按需查看 |
| `log/certs/` | `renewal.log` 为检查及续期结果，`letsencrypt.log` 为 Certbot 详细日志及其轮转文件。 | 用 `log` 菜单查看 |
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

若尚未安装 curl 或系统 CA 信任证书，先按系统安装下载工具：

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y curl ca-certificates

# Fedora / RHEL 系
sudo dnf install -y curl ca-certificates
# 使用 yum 的系统将 dnf 换成 yum
```

运行安装脚本：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/NULL-SF/ez-mihomo/main/install.sh)
```

运行后选择 `1`（服务端），将独立项目下载到当前目录的 `ezm-server/`。

下载后进入目录并手动启动：

```bash
cd ezm-server
sudo bash ./server.sh start
```

如需开机自启，执行：

```bash
sudo bash ./server.sh boot
```

在提示中选择开启。
