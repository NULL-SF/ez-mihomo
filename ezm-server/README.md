# ezm-server：ez-mihomo 服务端

本项目使用 mihomo 1.19.33 提供七种协议的节点服务，每种协议独立端口。核心随目录提供，仅支持 Linux x86_64。`start` 在后台启动节点和 HTTPS 订阅服务；节点链接及订阅链接保存在 `output/links/`，可直接复制。

如需快速部署使用，可直接跳到[迁移与部署](#5-迁移与部署)。

## 1 基本配置

### 1.1 服务端口

`config/ports/` 每种入站协议一个 `.port` 文件，只填写一个 `1024–65535` 的整数。七种协议及非零本机中继端口的端口号不能重复。`start` 时若端口配置文件缺失或为空，会自动创建并写入默认值；内容无效或端口重复时报错。`local_relay.port` 单独配置[本机中继端口](#14-本机中继端口)。

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

修改协议端口后执行 `start` 或 `restart`，会重建对应节点，客户端需重新导入链接或更新订阅。

`config/ports/` 整个目录缺失时会按默认值重新创建，不会恢复原来的自定义端口；这可能触发对应节点重建。

VLESS 的 REALITY 握手目标固定为 `www.microsoft.com:443`，VPS 需能访问该目标。VMess 由客户端直接连接本 VPS，不需要额外的中转服务器。

### 1.2 节点链接

每次成功执行 `start` 后，会在 `output/links/` 下重新生成并覆盖 `vless.link`、`vmess.link`、`hysteria2.link`、`trojan.link`、`shadowsocks.link`、`tuic.link`、`anytls.link`。每个文件一条完整链接，可以打开文件复制，或用 `links` 查看。

链接包含 UUID、密码等认证信息，应私下保存。`start` 逐协议检查节点配置和端口配置：两者一致时沿用；节点配置缺失、损坏，或两者不一致时，会提示并仅重新生成该协议的配置和链接，完成后提醒客户端导入新链接。每个节点独立互不影响。输出链接不参与节点校验，删除或修改输出文件不会触发重建。

需要重建某个节点时，可删除 `runtime/协议名/config.json` 后执行 `start`，保留同目录的 `mihomo.pid`。重建会更换该节点的 UUID、密码等认证信息，客户端需重新导入新链接或更新订阅。

### 1.3 自动 TLS 证书

没有现有证书时，自动选择本机启用网卡上的公网地址：优先 IPv4，否则使用 IPv6；IPv6 优先使用非临时、未弃用的地址。仅有公网 IPv6 时不使用共享 IPv4 出口。网卡上没有公网地址时，才尝试探测映射的 IPv4 出口；此时需自行确认公网端口已映射到本机。

已有证书时，需要生成或重建节点则沿用证书中的地址。公网 TCP 80 端口必须能入站访问；使用 IPv6 时，客户端网络也需支持 IPv6。

没有现有证书时，自动向 [Let’s Encrypt](https://letsencrypt.org/) 为所选公网 IP 申请免费的受信任 TLS 服务器证书，无需域名或手动准备证书文件。申请时会联网验证，需要等待完成；之后按证书剩余有效期决定是否更新。

启动自动申请即表示同意签发机构的[服务条款](https://letsencrypt.org/repository/)。验证需要公网 TCP 80 可达、未被占用；程序以 root 身份临时监听该端口。脚本不会停止占用验证端口的其他程序，也不修改其他防火墙或云安全组。

证书由 Certbot 自动申请和续期。已有兼容版本时直接使用，缺少时在项目内自动安装。

VMess、Hysteria2、Trojan、TUIC、AnyTLS 及订阅服务直接读取 `runtime/certs/config/live/ezm-node/fullchain.pem`、`privkey.pem`，不另存部署副本。VLESS 使用 REALITY 公钥验证，链接已包含所需公钥；Shadowsocks 不需要证书。私钥只留在服务端，客户端通常导入链接即可。

IP 证书[有效期为 160 小时](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)。`start` 立即检查证书，并登记每小时整点检查任务；开启 `boot` 后，重启可自动恢复节点、订阅服务及整点证书检查。每次整点检查时，证书材料或续期配置缺失、无效，或证书剩余有效时间不超过 48 小时，触发自动申请换证。正常换证不中断转发，也不更换节点链接。

全部核心未运行时不执行整点检查。`stop` 取消整点检查并终止证书自动续期，下次 `start` 恢复。申请换证失败保留旧证书，日志见 `log/certs/renewal.log`；证书过期后新连接将验证失败。

### 1.4 本机中继端口

`config/ports/local_relay.port` 只填写一个 `0–65535` 的整数。默认 `0` 表示直接出口；填写其他端口（如 `10808`）时，七种协议承接的流量统一通过 SOCKS 协议转交给 `127.0.0.1:该端口` 的代理服务。文件缺失或为空时，写入默认值 `0`；端口内容无效时报错。

修改后执行 `start` 或 `restart` 生效。仅修改本机中继端口时，会更新七种协议的出站配置并重启运行中的服务，现有连接会中断；各协议的入站端口、UUID、密码和节点链接保持不变，客户端无需重新导入链接。

本机中继端口须部署有支持无认证连接的 SOCKS 代理服务监听，可由本项目客户端或其他程序提供，否则数据链路中断且不回退直连。证书申请等服务端本身的管理操作不经本机中继端口。

### 1.5 订阅

`start` 同时启动 HTTPS 订阅服务，复用现有 TLS 证书，无需另配域名。订阅需要公网 TCP 443 可达，且该端口未被其他程序占用。

用 `links` 选择「白名单订阅链接」或「黑名单订阅链接」，也可复制对应文件中的完整链接，供使用 mihomo / Clash Meta 核心的客户端订阅：

| 订阅 | 链接文件 | 分流方式（规则模式） |
|---|---|---|
| 白名单 | `output/links/subscription-whitelist.link` | 直连名单中的域名和国内 IP 直连，其余代理。 |
| 黑名单 | `output/links/subscription-blacklist.link` | 代理名单中的域名和 Telegram IP 代理，其余直连。 |

下载文件分别为 `mihomo-whitelist.yaml` 和 `mihomo-blacklist.yaml`。两份订阅均优先直连局域网域名和地址，包含相同的七个节点；「节点选择」组提供七个节点及 `DIRECT`（直连）、`REJECT`（拒绝连接）选项，表中的代理流量使用该组所选出口。

名单采用社区项目 [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules)，由客户端在运行期间每隔 24 小时尝试更新，无需在服务端自行维护。

`config/subscription.token` 保存订阅链接中的访问令牌。缺失或为空时，`start` 随机生成并保存，有值则沿用。手动填写时使用字母、数字、下划线或连字符，最长 256 字符。修改令牌后执行 `start`，客户端需改用新链接；不会因此重建节点。完整订阅链接可下载全部节点信息，应与节点密码一样私下保存。

服务器地址和令牌不变时，重建节点不会改变订阅链接；客户端更新订阅即可取得新节点。

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

`links`、`status`、`log` 正常使用无需 sudo；各命令会检查所需依赖，缺失时通过 sudo 调用 apt-get、dnf 或 yum 补装，完成后继续以原用户执行。

### 2.1 服务管理

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh start` | 为缺失或为空的端口配置文件写回默认值并检查证书。然后逐协议核对节点配置和端口配置，两者一致时沿用；不一致或节点配置缺损时警告并重建对应节点。最后启动节点和订阅服务，刷新输出链接。 |
| `sudo bash ./server.sh stop` | 停止全部代理、订阅服务及证书自动续期，保留证书、节点配置和链接。 |
| `sudo bash ./server.sh restart` | 先执行 `stop`，再执行 `start`。 |

服务端每次执行 `start` 都检查运行依赖：root 下缺少时通过 apt-get、dnf 或 yum 安装，并启用 cron。已有依赖直接复用，不执行整机软件升级；RHEL 系使用兼容的 Python 命令，不替换系统默认 Python。系统软件源需能提供 Python 3.10+，不支持的旧系统需自行准备兼容环境。

`start` 添加节点所需的 TCP／UDP 端口、订阅端口 TCP 443 及证书验证端口 TCP 80 的 UFW 放行规则；`stop` 只移除本项目添加的规则，保留原有规则。未安装 UFW 时跳过；已安装但未启用时也会写入规则，不会启用、关闭或重置防火墙。其他防火墙和云安全组需自行放行相应端口。

在已启动或部分启动协议服务的情况下再次执行 `start`，会逐协议核对节点配置和端口配置：对于两者一致的，已运行的协议服务继续保持运行，未运行的启动；对于不一致的，无论是否正在运行，重建节点并重启协议服务，同时更新认证信息和服务链接。

在已启动或部分启动协议服务且中继端口配置改变时再次执行 `start`，按新的中继端口配置重启/启动所有协议服务；若未触发节点重建，则入站端口、认证信息和服务链接保持不变。

`restart` 则先执行 `stop` 停止全部协议及订阅服务，再执行 `start`。手动修改有效节点配置后，应执行 `restart` 让进程重新加载；`start` 不会无条件重载已运行的节点。

停止和重启会中断现有代理连接；证书正常自动续期无需中断或重启代理（支持热更新）。

### 2.2 状态、链接与日志

| 命令 | 说明 |
|---|---|
| `bash ./server.sh status` | 查看出站配置、核心版本，以及各协议的部署端口、传输及运行状态，另显示订阅服务的状态和端口，不进行连接测试。 |
| `bash ./server.sh links` | 菜单选择全部节点链接、某个协议、白名单或黑名单订阅链接，回车默认显示七种协议的节点链接；只读取已导出的文件，不生成或修复链接。 |
| `bash ./server.sh log` | 菜单选择全部节点、证书及订阅日志，或单独选择其中一项，回车默认全部；按 `Ctrl+C` 退出查看。 |

### 2.3 开机自启与定时任务

| 命令 | 说明 |
|---|---|
| `sudo bash ./server.sh boot` | 选择开启或关闭节点及订阅服务的开机自启，不影响当前监听和续期。 |

`boot` 选择开启后，先准备完整启动依赖和 cron，成功后将开机任务写入 root 的 crontab；选择关闭则删除该任务。开机时，cron 等待 20 秒后以 root 身份执行 `start`，无需输入 sudo 密码。

## 3 项目文件与目录

所有路径均相对于本项目目录。**一般只需确认端口后执行 `start`**；输出链接由程序生成，不要手改。项目配置、日志和密钥等文件由安装用户管理，可直接读写；启停 root 服务仍需 sudo。

| 路径 | 是什么 | 是否手动修改 |
|---|---|---|
| `server.sh` | 管理入口。 | 不改 |
| `scripts/dependencies.sh` | 检查、补齐各命令所需依赖。 | 不改 |
| `scripts/ports.py` | 统一读取、补全和校验端口配置。 | 不改 |
| `scripts/subscription.py` | 管理 HTTPS 订阅服务并生成 mihomo 配置。 | 不改 |
| `scripts/permissions.py` | 整理项目文件归属和权限。 | 不改 |
| `scripts/firewall.py` | 添加及移除本项目的 UFW 放行规则。 | 不改 |
| `scripts/configure.py` | 逐协议检查并生成配套的节点配置和链接。 | 不改 |
| `scripts/certificate.py` | 准备 cron、申请证书、管理续期及热更新。 | 不改 |
| `scripts/service.py` | 进程身份、监听检查及启停。 | 不改 |
| `scripts/outbound.py` | 读取本机中继端口并校验、应用各协议的出站配置。 | 不改 |
| `bin/mihomo_1.19.33`、`bin/LICENSE.mihomo` | 核心及许可证。 | 不改 |
| `config/ports/` | 各协议的 `.port` 文件和本机中继端口 `local_relay.port`。 | 按需修改，生效方式见对应配置说明 |
| `config/subscription.token` | 订阅访问令牌。 | 通常沿用；需要更换订阅链接时改 |
| `runtime/certs/` | 证书、私钥、签发账户、续期状态和证书工具。 | 不改 |
| `runtime/协议名/` | 对应协议的 `config.json` 和 `mihomo.pid`；配置含节点认证信息。 | 通常不改；重建方法见 1.2 节 |
| `runtime/subscription/` | 订阅服务的 `subscription.pid`。 | 不改 |
| `runtime/` | 各协议及订阅运行目录、证书目录和运行状态。 | 需保留，不要当缓存清空 |
| `log/` | 各协议日志、订阅日志 `subscription.log` 和开机启动日志。 | 按需查看 |
| `log/certs/` | `renewal.log` 为检查及续期结果，`letsencrypt.log` 为 Certbot 详细日志及其轮转文件。 | 用 `log` 菜单查看 |
| `output/links/` | 七种协议的 `.link` 节点链接、`subscription-whitelist.link` 及 `subscription-blacklist.link`。 | 直接复制，不改；缺失时执行 `start` 重新导出 |

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

UUID、密码和混淆密码自动生成，正常启停不更换这些信息。链接遵循对应协议的分享格式；实际可导入范围取决于客户端实现。

## 5 迁移与部署

### 5.1 部署与启动

若尚未安装下载和解压工具，先按系统准备（root 用户可省略 sudo）：

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y curl ca-certificates tar gzip

# Fedora / RHEL 系
sudo dnf install -y curl ca-certificates tar gzip
# 使用 yum 的系统将 dnf 换成 yum
```

运行安装脚本：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/NULL-SF/ez-mihomo/main/install.sh)
```

运行后选择 `1`（服务端），将独立项目下载到当前目录的 `ezm-server/`。

下载或复制后进入目录。手动复制或上传可能丢失执行权限，启动前统一补齐：

```bash
cd ezm-server
chmod +x server.sh scripts/*.sh bin/mihomo_1.19.33
```

**本机中继（可选）**

如需将流量交给本机其他程序转发，在 `config/ports/local_relay.port` 中填写该程序的 SOCKS 监听端口作为[本机中继端口](#14-本机中继端口)（如 `10808`）。无需中继则不必配置，文件缺失或为空时会自动写入 `0`，表示直连。

**启动服务**

```bash
sudo bash ./server.sh start
```

**开机自启（可选）**

如需开机自启，执行：

```bash
sudo bash ./server.sh boot
```

在提示中选择开启。

### 5.2 获取节点与订阅

启动成功后，节点链接和订阅链接保存在 `output/links/` 中，可直接打开对应的 `.link` 文件查看：

- `协议名.link`：对应协议的节点链接。
- `subscription-whitelist.link`：白名单订阅链接。
- `subscription-blacklist.link`：黑名单订阅链接。

也可通过菜单查看：

```bash
bash ./server.sh links
```

建议将 `output/links/` 目录下载到本地并妥善保存，方便后续快速配置 `ezm-client` 或查看订阅链接。文件包含节点凭据和订阅令牌，请勿公开分享。
