# CTFCode独立CLI的源码与构建

普通使用者下载包含定制CLI的preview.4便携包即可，不需要安装Bun或自己编译。开发源码、便携布局和本机原3/7号入口均使用独立TUI；完整通过项及保留边界见[TUI整合进度](TUI整合进度.md)。

## CTFCode启动入口（不等于重编OpenCode）

`launcher/Program.cs` 是约11KB的Windows控制台入口，包含产品版本与项目图标，使用包内相对路径运行Python和既有TUI。普通用户双击 `CTFCode.exe`；旧BAT保留兼容。它不安装服务、不提权、不寻找全局Python/OpenCode、不经过cmd拼接用户路径。

在Windows开发环境执行 `python script/launcher.py --output <不存在的新目录>`，使用系统.NET Framework编译器生成EXE、ICO和 `launcher.json`。便携构建时提供 `--launcher <该EXE路径>`；构建器校验EXE与四个源码文件的SHA256。当前未签名；.NET Framework由受支持的Windows 10/11提供，运行时无需SDK。

## 定制内容

- 上游OpenCode `v2.3.0`，commit `a6b15d33daf6ab7d35569f6d711fdb630b9f04e7`，保留MIT许可。
- 当前定制版本 `2.3.0-ctfcode.2`；不是把官方原版EXE改名。
- `patches/ctfcode-v2.3.0.patch` 删除内置自动日期来源，跨午夜也不追加日期通知；不删除用户自己输入的日期。
- `/cd` 目录参数支持Windows反斜杠/成对双引号；输入完整参数后Enter提交原文，Tab仍用于补全。目录权限、归档和新会话策略由CTFCode插件负责，不在补全器中跳过校验。
- 退出后显示归档位置与空白新会话说明，不再建议 `opencode -s ...` 恢复旧上下文。
- 已完成的原生压缩消息为本地插件提供可信来源标记，允许附上本会话的原始要求和执行/读回证据索引。用户文字或普通消息的同名字段不能冒充此来源；标记不进入模型HTTP请求，不自动恢复其他会话。
- 构建不包含Web UI资源，不修改桌面版、不打包模型；运行时仍只开放CTFCode审查过的本地模型和工具配置。

## 官方输入与校验

所有锁定值在 `script/native-lock.json`。下载可直连，不需要模型文件。

| 输入 | 官方地址 | SHA256 |
| --- | --- | --- |
| OpenCode源码tgz | https://codeload.github.com/anomalyco/opencode/legacy.tar.gz/refs/tags/v2.3.0 | `327af8ce7f5538036e9b49aaefd1d644dc6d739c8c4a3c7b6c71d0bb8ece6a79` |
| Bun 1.4.4 Windows x64 zip | https://github.com/oven-sh/bun/releases/download/bun-v1.4.4/bun-windows-x64.zip | `cd2c0fccc78f3ede1f50ca827a15f063eda13d4d150a3d0674f4f6e323d864a5` |

Bun官方校验表：https://github.com/oven-sh/bun/releases/download/bun-v1.4.4/SHASUMS256.txt 。解压后 `bun.exe` 的SHA256还须是 `cb1d8e6564e655e49d30cead359750a1cbbd12f42651caa4b62244d7bd93c244`。

## 开发者构建

依赖Windows x64、Python 3.11及Git。以下以检出位置 `E:\CTFCode` 为例；`--output` 必须是不存在的新目录，不覆盖上一次构建。

```powershell
python E:\CTFCode\script\native.py `
  --archive E:\CTFCode\.local\downloads\opencode-v2.3.0.tgz `
  --bun E:\CTFCode\.local\downloads\bun-windows-x64\bun.exe `
  --output E:\CTFCode\.local\build-ctfcode
```

只核验源码、解包、应用补丁：在同一命令增加 `--prepare-only`，并换一个新输出目录。它不会安装依赖、编译或加载模型。

完整构建会：

1. 核验源码、Bun和补丁SHA256，核对每个补丁文件的原始/最终哈希。
2. 在新目录解包；资源符号链接仅物化为源码包内的普通目标文件，不创建外部链接。
3. 使用冻结锁文件安装所需工作区依赖，禁止安装期脚本，保留依赖完整性检查；此步骤需要直连npm源。
4. 从各package目录运行定向测试和 `bun run typecheck`，不从仓库根目录运行测试、不直接运行tsc。
5. 仅编译Windows x64独立CLI，使用仓库内置模型目录快照，不下载模型或Web UI。
6. 输出 `build.json`、各阶段日志和 `artifacts\cli-windows-x64\bin\opencode.exe`。

Bun 1.4.4在中文可执行路径下自复制编译会失败。构建器会将**哈希匹配的Bun程序**临时复制到英文临时目录，完成后只删除该临时程序；不搬动模型、源码或题目目录。若系统TEMP/TMP本身包含中文，先为这次构建指定英文临时目录。

`build.json` 是构建证据，不代表所有CTFCode功能均已验收；二进制不承诺逐字节可复现。完成原生TUI与生命周期验收后，维护者再把该次二进制SHA256写入 `portable\成熟Agent接入\OpenCode\v2\cli.json`。运行器按SHA256和版本双重核验，不接受随便替换的原版/全局/桌面CLI。

白名单构建器 `script/portable.py` 必须通过 `--cli` 显式接收哈希匹配的定制程序，并带入对应插件、运行控制、指南及依赖；不能只复制EXE覆盖旧Release。模型、题目附件、会话、本机配置和日志不上传GitHub；旧preview.2资产不覆盖。

## 当前构建的验收边界

- `2.3.0-ctfcode.2` 的二进制SHA256为 `48764ac3c52d56ac8f74198ce61f201d2545ad6a97469f56d409e4735c985276`，已写入 `v2/cli.json`；旧 `ctfcode.1` 仅作为先前里程碑保留。
- 该构建实际完成Core 15项、TUI 69项通过（另2项跳过），Core/TUI/CLI分别typecheck通过。构建证据在本机忽略目录 `.local/native-recipe-progress/build.json`，不进入发布包。
- 新版先完成无模型PTY及合成协议验证，随后真实完成32K生成/执行/读回/卸载重载、64K生成及运行中停止、原生压缩和外部退出归档。64K短测不等于近满窗口长期稳定，详见最新验收记录。
