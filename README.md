# 板块雷达 Sector Radar

板块雷达是一款在 Windows 本机运行的中证行业指数研究、基金候选比较、决策草稿和个人交易账本工具。

当前公开版本：`4.4.0-alpha.41`

> 这是 Alpha 测试版。软件输出仅用于个人研究与记录，不构成投资建议，也不保证收益。市场数据、基金申购状态、费用和净值可能延迟或变化，请在实际交易平台再次核对。

## 下载与运行

1. 打开本仓库右侧的 **Releases**。
2. 下载最新版 `SectorRadar_v4.4.0-alpha.41_Windows_x64.zip`。
3. 解压整个 ZIP；不要只单独复制 EXE。
4. 双击 `SectorRadar_v4.4.0-alpha.41.exe`。
5. 程序会在软件目录旁的 `data` 文件夹保存本机数据。

系统要求：Windows 10/11，64 位。

当前 Alpha 包尚未购买商业代码签名证书，Windows 首次运行时可能显示 SmartScreen 提示；请只从本仓库 Releases 下载，并核对 Release 页面公布的 SHA-256。

## 从源码运行与构建

运行环境为 Python 3.11 或更高版本，日常运行只使用 Python 标准库：

```powershell
python sector_radar.pyw
```

运行自动测试：

```powershell
python tools/run_public_tests.py
python sector_radar.pyw --self-test
python sector_radar.pyw --acceptance-test
```

在 Windows 上双击 `生成便携EXE.bat`，或运行 `BUILD_PORTABLE_EXE.cmd`，可构建 PyInstaller 便携应用文件夹。

## 主要功能

- 研究当前严格准入的中证 `.CSI` 行业指数；
- 比较与指数精确对应的场外被动 C 类基金候选；
- 区分 AI 建议、用户草稿、待确认交易和已入账交易；
- 使用主动现金账本、持仓、成本批次与部分卖出记录；
- 支持定投、净值更新、策略复盘、备份与恢复；
- 可由用户自行配置 DeepSeek API Key，付费请求前显示预算并要求确认。

## 隐私与安全

- 发布包不包含作者的数据库、交易记录、API Key、日志或备份；
- API Key 由 Windows 当前用户的 DPAPI 加密后保存在本机数据库；
- 软件不连接证券或基金交易账户，不会自动下单；
- 请勿上传或公开自己的 `data` 文件夹。

更多说明见 [隐私说明](PRIVACY.md)、[安全说明](SECURITY.md) 和 [分发说明](DISTRIBUTION_NOTICE.md)。

## 开源许可证

源代码使用 [MIT License](LICENSE)。第三方服务、市场数据及基金资料仍分别受其提供方条款约束。

## 反馈问题

请在 GitHub Issues 中说明：软件版本、Windows 版本、操作步骤、预期结果和实际结果。上传截图或诊断文件前，请先确认其中不含个人交易数据或密钥。
