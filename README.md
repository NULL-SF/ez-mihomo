# ez-mihomo

基于 [mihomo](https://github.com/MetaCubeX/mihomo) 的轻量代理管理脚本，支持 VLESS、VMess、Hysteria2、Trojan、Shadowsocks、TUIC、AnyTLS。

- [ezm-client：本机代理客户端](ezm-client/README.md)
- [ezm-server：VPS 节点服务端](ezm-server/README.md)

两个目录都是完整、独立的项目，各自包含入口脚本、核心、许可证和配置。不依赖仓库根目录或另一个项目，可以整体克隆，也可以通过稀疏检出、导出或复制只取所需目录；保持原目录名或自行改名均可。具体用法见各自 README。

一键下载项目（root 用户，无需 Git）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/NULL-SF/ez-mihomo/main/install.sh)
```

安装到当前目录的 `ez-mihomo/`；下载工具准备及其他用法见[服务端文档](ezm-server/README.md#5-迁移)。
