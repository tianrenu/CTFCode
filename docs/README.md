# CTFCode 本地部署指南

面向 Windows 上的 **OpenCode CLI/TUI + Strata + GSQ-RCO Coder IQ1_M**。权重与第三方程序从各自上游下载，不随 GitHub 源码仓库发布。

> **正常用户先看[便携版使用](便携版使用.md)。** `v0.1.0-preview.2` 的 windows-x64 包已整合 CLI、CTF 定制层、Python、Strata Control、32K/64K 引擎和依赖；无需照下面的手动资料逐个安装。模型仍需单独下载或选择已有目录。

> 本仓库基于 OpenCode `v1.2.26`，提交 `d954026dd855e018302a6c0733a1dd74140931df`。便携版尚未完成干净机器及真实模型长任务的重新验收；下面的手动步骤只保留为开发、资源复核和排障资料，不能代替包含定制层的便携版入口。状态见[定制层迁移状态](定制层迁移状态.md)。

## 手动部署/开发资料（便携包用户无需逐项安装）

1. [模型下载与校验](模型下载与校验.md)：两个分片、固定版本、官方/镜像直链、断点续传、SHA256。
2. [引擎下载与配置](引擎下载与配置.md)：Windows Control、32K 基线引擎、可选 64K 引擎、模型 pack 准备和启停。
3. [首次连接与故障排查](首次连接与故障排查.md)：独立 OpenCode CLI 下载、独立配置、连接测试和排错。

## 到底要下载哪些东西？

| 用途 | 下载项 | 是否必需 |
| --- | --- | --- |
| 文本模型 | `Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF` 的 **IQ1_M 两个分片** | 必需，合计 58,408,584,928 bytes，约 58.41GB / 54.4GiB |
| 管理/准备模型 | `strata-control-0.1.52-windows-x86_64.zip` | 必需，自带 Python，不是推理引擎 |
| 32K 推理 | `strata-engine-0.1.52-windows-x86_64-cuda-cuda13.tar.gz` | 首次部署使用的基线 |
| 64K 推理 | `strata-engine-0.1.59-windows-x86_64-cuda-cuda13.tar.gz` | 可选候选，不与 32K 同时运行 |
| 交互 Agent | OpenCode `v1.2.26` 的 `opencode-windows-x64.zip` | 现阶段手动接入所需，不是桌面版安装器 |

**不要下载整个模型仓库、其他量化、MTP drafter 或视觉模型。不要把 `strata-dsv41-*` 当成这套 Qwen 的引擎。** Release 标签和组件内部版本不是同一回事：Control/32K 来自 `v0.2.7`，64K 候选来自 `v0.2.13`。

机器可读清单也进入公开源码：

- [模型清单](../config/models/gsq-rco-coder-iq1-m.json)
- [Windows 引擎清单](../config/engines/windows-cuda13.json)
- [独立 OpenCode 配置示例](../config/opencode.example.json)

## 硬件与空间

参考环境为 Windows、Core Ultra 7 255HX、RTX 5060 Laptop 8GB、32GB RAM、SSD。它只是已使用过的参考机器，不代表任意 8GB 显卡都兼容或同速。

- 建议 **32GB 或更多系统内存**；加载前至少准备约 **16GiB 可用内存**，不能理解成“整机 16GB 就够”。
- 建议 NVMe SSD。权重不是全部放入显存，CPU、系统内存、文件缓存和磁盘性能都会影响速度。
- 两个 GGUF 下载约 54.4GiB；准备 pack 后，现有模型目录约 79GiB。首次部署建议预留 **150GB 以上磁盘空闲**，这是保守建议，不是实测峰值承诺。
- 本指南是 **Windows x86_64 + NVIDIA CUDA 13** 路线。64K 候选包的 manifest 要求 NVIDIA driver 至少 `580.65`，仍应以所选包的 `doctor` 检查为准。其他 GPU/平台请查 [Strata 上游](https://github.com/Niko1221/Strata)，不能照搬这些二进制。
- 不承诺稳定 `10 token/s`。已有短采样与真实 TUI 速度不同；长提示词冷预读可能很慢。64K 通过过有限的续聊/压缩测试，但尚不能宣称与 32K 同等稳定。

## 手动部署的文件放哪里？

以下是手动部署目录，不是便携包目录。便携包将运行组件放在自己的解压目录；用户仅选择外部模型目录，路径自动生成。手动资料则以 `$env:LOCALAPPDATA\CTFCode` 为默认值；空间不足可在执行前改到自己 SSD 上的绝对路径。

```text
CTFCode 源码/
  docs/
  config/

用户选择的数据目录/
  downloads/                       下载包，校验后再解压
  runtime/control-0.1.52/           含 strata.py、python/python.exe、bin/
  runtime/opencode-1.2.26/          独立 CLI，不替换桌面版
  components32/engine/cuda13-0.1.52/  保留全部引擎文件
  components64/engine/cuda13-0.1.59/  可选，独立命名空间
  models/GSQ-RCO-Coder/
    IQ1_M/                         两个 GGUF
    packs/coder-iq1_m/              由 Strata 准备，不是另一个下载包
  state/                           本机运行状态
  opencode/                        独立配置、缓存、会话
```

## 发布与许可边界

- OpenCode 原始源码采用 [MIT](../LICENSE)，保留原作者署名。模型、Strata、CUDA 运行库和其他依赖有各自许可，不能用本项目许可证替代。
- 模型仓库当前元数据标注 `apache-2.0`，使用或再分发前仍需阅读对应 revision 的 model card 及原模型条款。
- GitHub 发布的是指南和代码，**不是几十 GB 权重包**。不要直接把作者整个工作目录压缩上传；`.gitignore` 不会替你清理 ZIP 内的模型、整合材料、日志、会话或密钥。
- 下载清单已复核；便携版的本轮验证项及限制见[使用与验收说明](便携版使用.md)。没有在干净的新机器上完成真实模型长任务，不能用旧工具箱的测试替代。

来源核查日期：2026-11-15。固定版本用于复现，不代表永远推荐使用旧版；升级应另做兼容性和安全验证。
