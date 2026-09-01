from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from .config import (
    APP_NAME, APP_VERSION, BACKUP_DIR, COLORS, DB_PATH, EXPORT_DIR,
    RUNTIME_DIR, TASK_DIR, app_root, local_now, local_today,
)
from .database import Database
from .deepseek import DeepSeekClient, DeepSeekError
from .detailed_research import DetailedResearchPromptService
from .decision_drafts import DecisionDraftError, DecisionDraftService
from .dpi import normalize_tk_scaling
from .engine import (
    UnknownFeeScheduleError, estimate_sale, estimate_sale_by_lots, evaluate_holding,
    evaluate_intraday, money, pct, validate_allocation,
)
from .fund_valuation import FundValuationError, FundValuationService
from .fund_discovery import FundDiscoveryError, FundDiscoveryService, FundFeeSyncService
from .fund_catalog_sync import CSIOfficialProductService, CSIFieldFundSyncService
from .investable import DraftSettlementService, FundMetadataService, InvestableMappingService
from .ledger import LedgerError, PortfolioLedgerService
from .market_data import CURATED_ETF_FALLBACKS, MarketDataPipeline
from .orchestrator import ResearchOrchestrator
from .platform_catalog import PlatformCatalogService
from .protocol import build_task_package, parse_result, repair_prompt
from .portfolio import infer_active_capital
from .ranking import RANKING_FORMULA_VERSION
from .recurring import RecurringInvestmentError, RecurringInvestmentService
from .recommendation_performance import (
    PERFORMANCE_HORIZONS,
    RecommendationPerformanceService,
    recommendation_performance_summary,
)
from .runtime_housekeeping import (
    cleanup_managed_storage, runtime_cleanup_due, storage_inventory,
)
from .secrets import SecretError, api_key_from_db, clear_api_key, save_api_key
from .sector_resolver import SectorMappingResolver
from .services import (
    action_capital_context, daily_workflow_status, dashboard_metrics, holding_recommendations_for_run, import_radar_payload,
    holding_segment_metrics, holding_segment_review, latest_current_radar_run, log_failed_import,
    portfolio_equity_drawdown, realized_exit_trades, remove_stale_capital_claim,
    research_run_status, strategy_review_metrics,
)
from .trading_calendar import ChinaTradingCalendar, infer_fund_kind


_SINGLE_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """Return False after bringing the existing Windows instance to front."""
    global _SINGLE_INSTANCE_MUTEX
    if not sys.platform.startswith("win"):
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, "Local\\SectorRadar-Desktop-SingleInstance")
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法建立板块雷达单实例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def bring_forward(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.startswith(APP_NAME):
                user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return False
            return True

        callback = enum_proc_type(bring_forward)
        user32.EnumWindows(callback, 0)
        user32.MessageBoxW(None, "板块雷达已经在运行，现有窗口已切换到前台。", APP_NAME, 0x40)
        return False
    _SINGLE_INSTANCE_MUTEX = (kernel32, handle)
    return True


def release_single_instance() -> None:
    global _SINGLE_INSTANCE_MUTEX
    if _SINGLE_INSTANCE_MUTEX is None:
        return
    kernel32, handle = _SINGLE_INSTANCE_MUTEX
    kernel32.CloseHandle(handle)
    _SINGLE_INSTANCE_MUTEX = None


FONT = "Microsoft YaHei UI"

# Alpha.6 was technically DPI-aware, but most controls still used 8--10 point
# fonts.  These semantic sizes keep the application comfortable on the user's
# 2048x1151 Windows display and make later tuning consistent across pages.
FONT_SIZE = {
    "micro": 12,
    "hint": 10,
    "small": 13,
    "body": 15,
    "button": 14,
    "section": 18,
    "title": 30,
    "metric": 28,
}

# Every action stays self-explanatory without requiring a manual or a hover.
# The hint is deliberately short so horizontal toolbars remain readable on the
# user's maximised 2048px-wide Windows screen.
BUTTON_HELP = {
    "AI补全今日行动": "复用本轮目录，补齐买卖金额",
    "输入盘中数据": "录入14:40真实盘中涨跌",
    "检查拟买仓位": "检查金额、主题与相关性上限",
    "复制14:45任务": "复制备用盘中研究任务",
    "刷新": "重新读取最新研究和持仓",
    "把选中AI建议加入草稿": "复制建议，不会直接记账",
    "手工添加操作": "记录你盘中的拟买或拟卖",
    "编辑选中草稿": "修改仍未收盘入账的操作",
    "删除/撤销草稿": "撤单后不写入交易记录",
    "确认原日已扣款": "只记原日扣款，等待平台结算",
    "仍打算今天操作": "非定投旧意向才可带到今天",
    "确认原日未执行": "明确放弃，不进入交易记录",
    "检查草稿仓位": "检查拟买金额和主动仓位上限",
    "收盘后一键写入仍存活草稿": "15:00后生成待确认记录",
    "保存并连接 DeepSeek": "加密保存，并立即检查连接",
    "仅保存设置": "只保存参数，本次不联网",
    "测试已保存连接": "不计Token，检查模型和余额",
    "清除密钥": "删除本机加密保存的Key",
    "无API模拟日扫池": "零费用检查当前已准入的CSI叶子行业流程",
    "去做10项行情抽测": "切到行情页，免费抽查10项真实行情",
    "10方向API测试": "从全目录抽取10个细分行业，小额检查Flash联网检索",
    "Pro结构API测试": "小额检查V4-Pro结构",
    "完整扫描全部CSI行业": "按长期严格C目录研究；今日执行覆盖不足时不创建付费任务",
    "续跑选中失败任务": "复用当天成功行情，只补未完成项",
    "停止后续API": "阻止任务发送下一次请求",
    "导出选中任务诊断": "导出脱敏故障信息便于修复",
    "测试10项行情（零API）": "从全目录抽取10项检查公开行情，不调用AI",
    "抽测10项行情（零API）": "只抽取10项检查公开行情，不调用AI",
    "更新全部行情（零API）": "后台更新当前完整目录，不调用DeepSeek",
    "核验CSI代表行情": "联网核对当前全部叶子指数行情",
    "导出映射CSV": "保存当前映射清单",
    "编辑选中映射": "手工修正选中的板块代理",
    "夜间全市场研究": "生成网页版全市场任务包",
    "14:45盘中决策": "生成网页版盘中复核任务",
    "基金核查": "生成基金资料核对任务",
    "结果修复": "生成失败JSON修复任务",
    "打开任务文件夹": "查看已生成的任务文件",
    "校验并导入": "严格检查后写入研究结果",
    "从剪贴板粘贴": "粘贴网页版返回的完整结果",
    "清空": "清除当前粘贴内容",
    "去一键AI扫描": "前往自动研究页面",
    "生成网页版备用任务包": "API异常时改用网页版研究",
    "导出候选CSV": "保存本轮AI候选清单",
    "导出明细CSV": "保存本轮动态目录明细",
    "全量收录关联基金": "官网产品全留档，再核验场外被动C类；零AI费用",
    "联网寻找同类基金": "零AI费用搜索并比较被动基金",
    "保存推荐待核对": "加入基金库，但不标记为可买",
    "确认公开可申购并绑定": "核对同指数和公开申购状态后绑定",
    "打开官方资料页": "查看选中基金公开档案",
    "添加基金": "把新基金加入本地基金库",
    "编辑选中": "修改当前选中的记录",
    "设置赎回费": "维护真实阶梯赎回费率",
    "生成基金核查任务": "交给网页版核对基金资料",
    "添加持仓": "录入一笔当前真实持仓",
    "更新净值/份额": "手工修正选中持仓数据",
    "记录卖出": "登记已在支付宝确认的卖出",
    "新买入请到交易日志": "保持交易、现金和成本批次一致",
    "修正持仓资料": "只改净值、备注和资金分类",
    "加入卖出草稿": "拟卖出，不会立刻减少持仓",
    "新建待确认交易": "先记录，正式数据确认后才入账",
    "撤销已入账交易": "同步恢复持仓、批次和现金",
    "更正已入账费用": "只改买入费用并保留更正审计",
    "更新近14日净值/盈亏": "下载正式净值并重算盈亏",
    "恢复可验证V3快照": "仅恢复完整原始研究，不联网不记账",
    "仅重算本地推荐表现": "用已缓存行情重算，不联网",
    "联网补齐推荐基金净值": "后台补齐净值和费率证据",
    "停止净值更新": "当前基金完成后停止下一只",
    "仅重算本地推荐表现": "使用已下载行情核算，不联网、不调用AI",
    "联网补齐推荐基金净值": "后台下载推荐基金净值，不调用DeepSeek",
    "停止净值更新": "停止下一只基金的公开净值下载",
    "记录交易": "登记已经确认的实际交易",
    "补全/确认选中": "填入支付宝最终成交数据",
    "联网取净值/估算份额": "按交易日取正式净值，不会冒充最终成交",
    "联网补全全部待确认": "逐笔取公开净值，仍不自动入账",
    "导出CSV": "把当前记录保存为表格",
    "新建计划": "添加一条新的定投计划",
    "编辑计划": "修改选中的定投规则",
    "启用/暂停": "切换选中计划的运行状态",
    "生成到今天": "补齐截至今天的计划流水",
    "联网校准净值": "下载正式净值，不自动入账",
    "选中全部可确认": "勾选已具备净值的流水",
    "批量确认入账": "确认支付宝扣款后写入持仓",
    "标记未扣款/跳过": "保留记录但不计入持仓",
    "校正选中数据": "修正确认前的净值或份额",
    "保存设置": "保存本页全部风控参数",
    "保存存储策略": "保存自动备份与缓存保留范围",
    "创建完整备份": "复制全部数据库以便恢复",
    "恢复数据库": "用备份替换当前数据",
    "打开程序目录": "查看当前EXE和运行组件",
    "打开数据目录": "查看随软件移动的全部数据",
    "打开备份目录": "查看自动与人工数据库备份",
    "重新统计占用": "后台读取分类数量与大小",
    "立即安全清理": "只清理已核验的软件缓存",
    "取消": "放弃本次修改并关闭",
    "保存": "确认填写内容并保存",
}

PAGE_META = {
    "decision": ("clock", "#C86478"), "api": ("robot", "#6B9C8D"),
    "market": ("signal", "#5B7EA6"), "ai": ("arrows", "#A26A8D"),
    "radar": ("moon", "#715B91"), "audit": ("chart", "#9B6B43"), "fundselect": ("telescope", "#4C8B78"),
    "funds": ("telescope", "#4C8B78"), "holdings": ("wallet", "#B66A55"),
    "trades": ("book", "#6D7894"), "recurring": ("calendar", "#D47A63"),
    "review": ("bars", "#8B7654"), "help": ("book", "#B06D83"),
    "settings": ("gear", "#777777"),
}

PAGE_GUIDES = {
    "decision": ("三层决策流程", "①AI建议只供参考 → ②点选建议或手工操作加入草稿，盘中可删改 → ③15:00后把仍存活草稿一键写入待确认记录。", "AI建议不等于你的实际操作"),
    "api": ("AI扫描顺序", "先跑日扫池无API模拟和10项真实行情抽测 → 连接后分别测试Flash 10方向与Pro结构 → 最后扫描全部准入叶子行业。", "核心失败就停止"),
    "market": ("行情页怎么用", "核验当前CSI叶子行业的精确行情；失败时查看具体证券ID和原因。", "核验映射不调用DeepSeek"),
    "ai": ("备用通道", "API或行情暂不可用时生成任务包，交给网页版AI研究，再把完整V2结果粘贴回来校验。", "导入前严格校验"),
    "radar": ("阅读研究结果", "本页只显示完整扫描成功或备用结果导入后的研究；为空时先去‘一键AI扫描’运行完整扫描。", "生成备用任务包不会自动研究"),
    "audit": ("检查中证叶子目录", "每天扫描细分叶子行业；一旦子行业进入目录，对应宽泛父级就退出日扫，避免父子重复仓位。", "支持导出CSV"),
    "fundselect": ("基金怎么选", "选择一个CSI指数 → 零API联网找同指数被动C类 → 比较跟踪误差、费率、规模和成立时间 → 拟买前执行当日可申购复核。", "默认不调用DeepSeek"),
    "funds": ("维护基金库", "新增基金后补齐指数与相关性桶，再设置真实赎回费阶梯；未知资料保持待核验。", "双击可编辑基金"),
    "holdings": ("查看真实持仓", "点‘更新本段持仓净值/盈亏’取得正式净值；拟卖出只加入草稿，不会提前减少持仓。新买入统一从交易日志确认入账。", "净值不是盘中成交价"),
    "trades": ("补全正式成交", "收盘草稿先以‘待确认’进入这里 → 支付宝出结果后补全实际净值、份额、费用和确认日 → 统一账本同步持仓、成本批次和现金。", "已入账交易不可直接改"),
    "recurring": ("定投怎么用", "计划会自动生成待核对流水 → 联网校准公开净值 → 核对支付宝确有扣款 → 批量确认入账。", "绝不猜成交"),
    "review": ("两套复盘不要混淆", "真实交易复盘只统计你确认入账的买卖；AI推荐验证只观察模型当时意见之后的表现，不代表你下过单。", "未知结果不按0计算"),
    "help": ("像游戏教程一样阅读", "点击左侧主题切换章节；第一次使用建议按顺序读完快速开始、AI扫描和每日操作。", "说明已内置"),
    "settings": ("设置与数据安全", "在‘风险与排名’调整额度和策略；在‘存储与备份’查看实际位置、占用与保留规则。重大操作前创建完整备份。", "重要资料不会自动删除"),
}

NAV_HELP = {
    "decision": "看今天买什么、卖多少",
    "api": "连接AI并运行完整研究",
    "market": "核对CSI代表行情与精确代码",
    "ai": "API故障时使用网页版",
    "radar": "阅读市场结论与风险",
    "audit": "核对CSI代表指数与基金状态",
    "fundselect": "同指数基金联网比较",
    "funds": "维护基金名称与费率",
    "holdings": "查看净值、盈亏和持仓",
    "trades": "补全草稿并记录真实成交",
    "recurring": "管理计划与扣款确认",
    "review": "复盘收益、回撤和规则",
    "help": "查看内置玩法和说明",
    "settings": "调整风控、备份数据",
}

HELP_TOPICS = [
    ("第一次打开",
     "【三步开始】\n"
     "1. 先进入‘一键AI扫描’，填写并连接 DeepSeek API Key。Key 只使用 Windows 当前用户加密保存。\n"
     "2. 到‘中证代表目录’查看当前CSI叶子行业和同指数基金状态；宽泛父级与旧186项不参与新扫描。\n"
     "3. 先运行‘无API模拟日扫池’和10项行情抽测，再运行 Flash 10方向测试；都通过后运行‘完整扫描全部CSI行业’。\n\n"
     "软件不会连接支付宝，不会自动买卖，也不会替你确认真实成交。所有投资动作都必须由你本人在外部平台完成并核对。"),
    ("AI扫描",
     "【推荐顺序】\n"
     "CSI叶子目录 → 日扫池无API模拟 → 10项行情抽测 → 连接 DeepSeek → Flash小样本 → 完整扫描全部准入行业 → 夜间雷达。\n\n"
     "只有‘完整扫描全部CSI行业’成功，夜间雷达才会出现自动研究结果。完整扫描会产生API费用，按钮会先显示实际目标数、预算并要求你确认。\n\n"
     "Flash 搜索若返回解释文字而不是纯 JSON，软件会在原费用上限内复用原搜索结果，只进行一次禁用搜索的结构化收尾。"
     "收尾不会重新搜索；预计超预算时不会发送，失败后也不会循环重试。\n\n"
     "‘停止后续API’只阻止下一次请求，已经发出的请求仍可能等待并产生费用。"),
    ("每日使用",
     "【AI建议 → 我的草稿 → 正式记录】\n"
     "1. 完整扫描成功后，到‘今日决策’查看AI板块行动和当前持仓行动。蓝色AI区域只是参考，不代表你已经买卖。\n"
     "2. 双击或选中一条可执行AI建议，把它加入粉色‘我的盘中草稿’；也可以手工添加新基金买入或当前持仓卖出。定投计划会在当天自动出现在草稿中。\n"
     "3. 草稿才代表你的拟操作。盘中改变主意时可编辑或删除；被删除的撤单不会进入交易日志。草稿只输入基金代码、操作金额和净值交易日；基金名称、净值和估算份额可稍后免费联网补全。\n"
     "4. 15:00收盘后点击一键处理，只把仍存活草稿写入记录。普通买卖先标记为‘待确认’，支付宝出结果后到‘交易日志’补全正式净值、份额和费用；定投继续等待净值和扣款核对。\n\n"
     "没有可执行基金映射时，软件仍显示AI选中的板块和配置比例，但标为‘基金待绑定’，不会伪造基金或自动下单。"),
    ("持仓与交易",
     "【只记录已发生的事实】\n"
     "‘当前持仓’展示统一账本生成的份额、成本和最新净值；已经入账的份额与成本不能在表单中直接篡改。\n\n"
     "点击‘更新本段持仓净值/盈亏’会从每只基金本段持仓起点保存正式公布净值；有逐笔成本记录时按每天实际累计份额计算，没有逐笔记录时才使用汇总持仓回看。"
     "每只基金保留自己的净值日期；开放式基金正式净值不是盘中实时成交价。\n\n"
     "卖出先进入草稿，收盘后成为待确认记录；只有你补齐平台实际净到账、份额、净值、费用和确认日后，系统才按FIFO减少持仓。已入账记录如录错，必须审计式撤销再新建更正交易。"
     "软件不会登录交易账户，也不会发起买入或卖出。"),
    ("定投管理",
     "【核对流程】\n"
     "定投计划先生成待核对流水，然后联网校准公开净值。你需要确认外部平台确实扣款，并核对净值日期、手续费和份额，最后才批量确认入账。\n\n"
     "公开净值缺失时，记录会保持待处理，不会猜测成交。自动同步只更新可核验的数据，不会代替你的最终确认。"),
    ("数据与备份",
     "【数据放在哪里】\n"
     "板块雷达是一个完整的应用文件夹：主程序、_internal运行组件和旁边的data目录必须放在一起。数据库、备份、导出、任务和运行缓存全部位于data中，路径会根据主程序所在位置自动确定。"
     "以后搬盘时先关闭软件，再复制整个应用文件夹；不要只复制EXE。可在‘设置与备份 → 存储与备份’直接查看程序目录、数据目录和分类占用。\n\n"
     "自动清理只管理可严格识别的自动备份、旧运行版本、迁移演练副本、脱敏诊断和临时缓存；人工备份、任务包、CSV、截图、正式数据库和未识别资料均受保护。重大导入或恢复前先创建完整备份。恢复数据库会替换当前数据，软件会先创建安全副本。"
     "API Key 使用 Windows 当前用户加密，复制数据库到其他账户并不能直接解密。"),
    ("费用与失败处理",
     "【先看费用再继续】\n"
     "每个任务会记录模型、Token、搜索次数、阶段和估算费用。核心批次失败时任务会停止；续跑只处理未完成批次，不重复已经成功的批次。\n\n"
     "‘AI补全今日行动’默认最多使用0.50元，并且不会超过原完整扫描任务的剩余预算。按钮会在发送前显示本次实际可用上限；只发送一次Pro请求，失败不自动重发。\n\n"
     "如果来源缺少有效 http(s) 链接、日期语义不正确、结构不完整或超出预算，结果会被严格拒绝，不会作为证据写入。"
     "严格失败比悄悄接受错误结果更安全。"),
    ("版本与边界",
     f"【当前版本】\n板块雷达 {APP_VERSION}\n\n"
     "当前版本使用可移动应用文件夹：数据始终跟随主程序旁边的data目录，不写死任何盘符，也不回落到C盘用户目录。当前开发和日用仍只在G盘进行；达到完整验收标准前不会在E盘建立半成品。\n\n"
     "【能力边界】\n这是本地研究、记录和风控辅助工具，不是券商客户端，不连接支付宝，不自动交易，也不保证收益。"
     "市场信息可能延迟或错误，任何实际投资决定都应由你独立核对。"),
]

LEADING_SYMBOLS = "⏰🤖🧠📡🔄🌙📈🔭💰📒📖📊⚙️🐱📝🧮📋✅🚫⚠️🧭🔐🔌🧪🚀🔬✏️🔍🛠️🧹📂💾📦♻️📌💡⚡🧾💸▶⏹⏸🌐📅📥🔁"


def plain_ui_text(value: str) -> str:
    """Remove emoji glyphs that Tk renders as unreliable monochrome symbols."""
    return str(value).lstrip(LEADING_SYMBOLS + "\ufe0f ")


def leading_ui_symbol(value: str) -> str:
    """Return the decorative leading symbol without asking Tk to render it."""
    text = str(value).lstrip()
    for symbol in LEADING_SYMBOLS:
        if text.startswith(symbol):
            return symbol
    return ""


def make_action_icon(widget: tk.Widget, symbol: str, kind: str) -> tk.PhotoImage | None:
    """Create a small colourful emoji-style badge using Tk pixels only.

    Windows Tk does not reliably support COLR/CPAL emoji fonts.  A generated
    PhotoImage stays colourful in source and portable EXE builds alike.
    """
    if not symbol:
        return None
    colors = {
        "primary": COLORS["primary"], "secondary": "#A88578",
        "success": COLORS["accent"], "danger": COLORS["danger"],
    }
    image = tk.PhotoImage(master=widget, width=20, height=20)
    color = colors.get(kind, COLORS["primary"])
    # Soft round badge. PhotoImage starts transparent; colouring only the
    # circle avoids Tcl treating empty colour cells as a short malformed row.
    for y in range(20):
        for x in range(20):
            inside = (x - 9.5) ** 2 + (y - 9.5) ** 2 <= 82
            if inside:
                image.put(color, (x, y))
    # A tiny white sparkle/check/heart-like mark chosen by the original emoji.
    if symbol in "✅💾📦♻️":
        pixels = [(5,10),(6,11),(7,12),(8,13),(9,12),(10,11),(11,10),(12,9),(13,8),(14,7)]
    elif symbol in "⚠️🚫":
        pixels = [(9,y) for y in range(5,12)] + [(10,y) for y in range(5,12)] + [(9,14),(10,14)]
    elif symbol in "🌙":
        pixels = [(7,5),(6,6),(5,7),(5,8),(5,9),(5,10),(6,11),(7,12),(8,13),(9,13),(10,13),(11,12)]
    elif symbol in "💰📈📊🧮":
        pixels = [(5,14),(5,12),(6,12),(6,14),(8,14),(8,9),(9,9),(9,14),(11,14),(11,6),(12,6),(12,14),(14,14),(14,4)]
    else:
        pixels = [(9,4),(10,4),(9,5),(10,5),(9,8),(10,8),(5,9),(6,9),(13,9),(14,9),(8,10),(9,10),(10,10),(11,10),(9,11),(10,11),(9,12),(10,12),(9,15),(10,15)]
    for x, y in pixels:
        if 0 <= x < 20 and 0 <= y < 20:
            image.put("#FFFFFF", (x, y))
    return image


def open_path(path: Path):
    path = Path(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def format_float(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def trade_detail_summary(row: dict[str, Any], *, posted: bool) -> tuple[str, str, str, str, str]:
    """Build a full, auditable trade description for the selected-row panel.

    The wide transaction table is deliberately concise.  This summary keeps
    long fund names, provenance, rationale and the ledger state readable
    without asking the user to infer anything from a clipped cell.
    """

    status = str(row.get("status") or "已确认")
    fund_name = str(row.get("fund_name") or "名称待补全")
    title = (
        f"交易 #{row.get('id', '—')}｜{row.get('fund_code') or '代码待补全'} {fund_name}｜"
        f"{row.get('action') or '操作待补全'}｜{status}"
    )
    confirm_date = str(row.get("confirm_date") or "").strip() or "待支付宝确认"
    nav_date = str(row.get("nav_date") or "").strip() or "待补全"
    result_status = str(row.get("result_pnl_status") or "").strip()
    if row.get("result_pnl") is None or result_status in {"待核算", "未知", "unknown"}:
        result_text = "待核算"
    else:
        result_text = money(row.get("result_pnl"))
    capital_scope = "计入主动资金" if int(row.get("counts_toward_active_capital") or 0) else "不计入主动资金"
    ledger_state = "已过账并锁定" if posted else "尚未过账"
    facts = (
        f"操作日：{row.get('trade_date') or '待补全'}　｜　净值日：{nav_date}　｜　确认日：{confirm_date}\n"
        f"金额：{money(row.get('amount'))}　｜　份额：{format_float(row.get('units')) or '待补全'}　｜　"
        f"净值：{format_float(row.get('nav'), 4) or '待补全'}　｜　费用：{money(row.get('fee'))}　｜　结果盈亏：{result_text}\n"
        f"资金口径：{capital_scope}　｜　账本状态：{ledger_state}"
    )
    source = str(row.get("source") or "手工录入").strip()
    rationale = str(row.get("rationale") or "未填写").strip()
    ai_advice = str(row.get("ai_advice") or "无；这不影响你的真实交易记录").strip()
    context = f"来源：{source}\n你的理由：{rationale}\nAI建议摘要：{ai_advice}"

    nav_ready = float(row.get("nav") or 0) > 0
    if posted:
        next_action = "这笔交易已同步持仓、成本批次和现金并锁定。若只有买入费用录错，可使用“更正已入账费用”；其他正式资料录错仍须使用“撤销已入账交易”留下冲销轨迹，再新建正确记录。"
        next_kind = "locked"
    elif status == "待确认" and nav_ready:
        next_action = "公开净值已取得，但仍未成交入账。请对照支付宝核对实际金额、份额、费用和确认日，再点“补全/确认选中”。"
        next_kind = "ready"
    elif status == "待确认":
        next_action = "尚缺正式净值或资料。可先点“联网取净值/补全估算”；联网结果只供核对，不会自动确认或改变持仓。"
        next_kind = "waiting"
    elif status == "已撤销":
        next_action = "这笔记录已撤销，仅作为审计轨迹保留，不会改变持仓或现金。"
        next_kind = "muted"
    else:
        next_action = "状态显示已确认，但未找到标准过账凭证。请先导出记录并检查账本，禁止直接改写这笔历史交易。"
        next_kind = "warning"
    return title, facts, context, next_kind, next_action


def ranking_formula_text(
    row: dict[str, Any],
    components: dict[str, Any],
    frozen_policy: dict[str, Any],
    formula_version: str,
) -> str:
    """Explain a stored score with the policy frozen for that research round.

    The settings page may have changed since a round was completed, so this
    function must never read live settings.  It also distinguishes the v4
    pre-cap score from the final score when the extreme-position safety cap
    applies; otherwise the reader would see a mathematically false equality.
    """

    def number(value: Any, default: float = 0.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed == parsed and abs(parsed) != float("inf") else default

    coefficient = max(0.0, number(frozen_policy.get("crowding_penalty"), 0.45))
    neutral = number(components.get("crowding_neutral_point"), 50.0)
    crowding = number(row.get("crowding_risk_score"))
    opportunity = number(row.get("opportunity_score"))
    recommendation = number(row.get("recommendation_score"))
    if formula_version == "four-dimension-v4":
        adjustment = number(
            components.get("crowding_adjustment"),
            coefficient * (neutral - crowding),
        )
        pre_cap = number(
            components.get("pre_cap_recommendation_score"),
            max(0.0, min(100.0, opportunity + adjustment)),
        )
        text = (
            f"基础推荐分{pre_cap:.1f}＝机会分{opportunity:.1f}＋{coefficient:.2f}×"
            f"（中性{neutral:.0f}－拥挤风险{crowding:.1f}）＝拥挤调整{adjustment:+.1f}"
        )
        if bool(components.get("high_position_cap_applied")):
            return (
                text
                + f"；20日极高位且未确认有效突破，执行59分上限，最终推荐分{recommendation:.1f}"
            )
        return text + f"；最终推荐分{recommendation:.1f}"
    return (
        f"推荐分{recommendation:.1f}＝机会分{opportunity:.1f}"
        f"－{coefficient:.2f}×拥挤风险{crowding:.1f}（历史v3公式）"
    )


def format_bytes(value: Any) -> str:
    """Small, stable storage label for users who should not need byte maths."""
    try:
        size = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        size = 0.0
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    digits = 0 if unit == "B" else (1 if size >= 10 else 2)
    return f"{size:.{digits}f} {unit}"


class CuteButton(tk.Frame):
    """Pastel action card: a clear button with an always-visible tiny hint."""

    def __init__(self, parent, text, command=None, kind="primary", **kwargs):
        palette = {
            "primary": (COLORS["primary"], "white", COLORS["primary_dark"], "#FFF0F4", "#E7BCC7"),
            "secondary": ("#F2E8E3", COLORS["text"], "#E4D5CF", "#FFF8F5", "#E7D8D2"),
            "success": (COLORS["accent"], "white", "#527D71", "#EEF8F4", "#BDD9CF"),
            "danger": (COLORS["danger"], "white", "#8E3037", "#FFF0F1", "#E6B9BD"),
        }
        bg, fg, active, hint_bg, border = palette[kind]
        help_text = kwargs.pop("help_text", None)
        plain_text = plain_ui_text(text)
        help_text = help_text or BUTTON_HELP.get(plain_text, f"点击执行：{plain_text}")
        try:
            parent_bg = parent.cget("bg")
        except tk.TclError:
            parent_bg = COLORS["bg"]
        super().__init__(parent, bg=parent_bg, highlightbackground=border, highlightthickness=1, bd=0)
        symbol = leading_ui_symbol(text)
        icon = make_action_icon(self, symbol, kind)
        options = {
            "text": plain_text, "image": icon, "compound": "left",
            "command": command, "bg": bg, "fg": fg,
            "activebackground": active, "activeforeground": fg,
            "relief": "flat", "bd": 0, "padx": 14, "pady": 7,
            "font": (FONT, FONT_SIZE["button"], "bold"), "cursor": "hand2",
        }
        # Custom widgets must behave like normal Tk widgets: callers may
        # override a default without causing Python to receive the same keyword
        # twice.  Alpha.7 violated this rule in Card and crashed at startup.
        options.update(kwargs)
        self.button = tk.Button(self, **options)
        self.button.pack(fill="x")
        self.hint = tk.Label(
            self, text=help_text, bg=hint_bg, fg=COLORS["muted"],
            font=(FONT, FONT_SIZE["hint"]), justify="center", anchor="center",
            wraplength=178, padx=5, pady=3,
        )
        self.hint.pack(fill="x")
        self.bind(
            "<Configure>",
            lambda event: self.hint.config(wraplength=max(110, event.width - 12)),
        )
        self.button.bind("<Enter>", lambda _e: self.button.config(bg=active))
        self.button.bind("<Leave>", lambda _e: self.button.config(bg=bg))
        self._action_icon = icon
        self._normal_bg = bg
        self._active_bg = active

    def config(self, cnf=None, **kwargs):
        """Delegate normal button state changes to the inner action button."""
        if cnf:
            kwargs.update(cnf)
        return self.button.config(**kwargs)

    def invoke(self):
        return self.button.invoke()

    def set_enabled(self, enabled: bool):
        """Keep long-running actions visibly and safely disabled."""
        self.button.config(
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
        )
        self.hint.config(fg=COLORS["muted"] if enabled else "#B8ADAF")


def show_nonmodal_task_result(parent, title: str, message: str) -> tk.Toplevel:
    """Show a readable task result without locking the rest of the application."""
    root = parent.winfo_toplevel()
    window = tk.Toplevel(root)
    window.title(title)
    window.configure(bg=COLORS["bg"])
    window.transient(root)
    window.resizable(True, True)
    window.minsize(620, 360)

    header_color = COLORS["danger"] if "失败" in title else COLORS["accent"]
    tk.Label(
        window, text=title, bg=COLORS["bg"], fg=header_color,
        font=(FONT, FONT_SIZE["section"], "bold"), anchor="w", padx=20, pady=14,
    ).pack(fill="x")

    body = tk.Frame(window, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
    body.pack(fill="both", expand=True, padx=20, pady=(0, 12))
    scrollbar = ttk.Scrollbar(body, orient="vertical")
    scrollbar.pack(side="right", fill="y")
    text = tk.Text(
        body, wrap="word", yscrollcommand=scrollbar.set, relief="flat", bd=0,
        bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, FONT_SIZE["body"]),
        padx=14, pady=12, spacing1=3, spacing3=5,
    )
    text.insert("1.0", message)
    text.config(state="disabled")
    text.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=text.yview)

    actions = tk.Frame(window, bg=COLORS["bg"])
    actions.pack(fill="x", padx=20, pady=(0, 16))
    CuteButton(
        actions, "关闭提示", window.destroy, kind="secondary",
        help_text="只关闭这张提示卡；主界面始终可以继续操作",
    ).pack(side="right")

    root.update_idletasks()
    width = min(920, max(620, int(root.winfo_width() * 0.56)))
    height = min(620, max(360, int(root.winfo_height() * 0.56)))
    x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
    y = root.winfo_rooty() + max(0, (root.winfo_height() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.lift()
    return window


class Card(tk.Frame):
    def __init__(self, parent, **kwargs):
        options = {
            "bg": COLORS["panel"],
            "highlightbackground": COLORS["border"],
            "highlightthickness": 1,
        }
        options.update(kwargs)
        super().__init__(parent, **options)


class ScrollableContent(tk.Frame):
    """A full-width vertical page that keeps readable controls from clipping."""

    def __init__(self, parent):
        super().__init__(parent, bg=COLORS["bg"])
        self.canvas=tk.Canvas(
            self,bg=COLORS["bg"],highlightthickness=0,bd=0,
        )
        self.scrollbar=ttk.Scrollbar(
            self,orient="vertical",command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right",fill="y")
        self.canvas.pack(side="left",fill="both",expand=True)
        self.content=tk.Frame(self.canvas,bg=COLORS["bg"])
        self._window=self.canvas.create_window(
            (0,0),window=self.content,anchor="nw",
        )
        self.content.bind(
            "<Configure>",
            lambda _event:self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event:self.canvas.itemconfigure(self._window,width=event.width),
        )

    def _on_mousewheel(self,event):
        if event.delta:
            self.canvas.yview_scroll(-1 if event.delta>0 else 1,"units")

    def bind_mousewheel_tree(self):
        """Bind only this tab's descendants, never steal another page's wheel."""
        def visit(widget):
            widget.bind("<MouseWheel>",self._on_mousewheel,add="+")
            for child in widget.winfo_children():visit(child)
        visit(self.content)


class CuteSticker(tk.Canvas):
    """A portable colourful decoration that does not depend on emoji fonts."""
    def __init__(self, parent, mood="paw", size=46, bg="#FFF3DE"):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        s = size
        if mood == "heart":
            self.create_oval(s*.20,s*.24,s*.54,s*.58,fill="#F28DA5",outline="")
            self.create_oval(s*.46,s*.24,s*.80,s*.58,fill="#F28DA5",outline="")
            self.create_polygon(s*.20,s*.40,s*.80,s*.40,s*.50,s*.82,fill="#F28DA5",outline="")
        elif mood == "star":
            pts = [(s*.50,s*.12),(s*.60,s*.39),(s*.88,s*.40),(s*.66,s*.57),(s*.74,s*.85),
                   (s*.50,s*.68),(s*.26,s*.85),(s*.34,s*.57),(s*.12,s*.40),(s*.40,s*.39)]
            self.create_polygon(*sum(([x,y] for x,y in pts),[]),fill="#F2B84B",outline="#D99A25",width=1)
        else:
            self.create_oval(s*.31,s*.36,s*.69,s*.75,fill="#D77A91",outline="")
            for x,y in ((.24,.26),(.43,.18),(.67,.18),(.79,.30)):
                self.create_oval(s*(x-.09),s*(y-.09),s*(x+.09),s*(y+.09),fill="#D77A91",outline="")


class Pictogram(tk.Canvas):
    """Colour pictograms that stay crisp without relying on emoji-font support."""
    def __init__(self, parent, kind="paw", color="#C86478", size=42, bg=None):
        bg = bg or parent.cget("bg")
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, cursor="hand2")
        s = size; ink = color; dark = COLORS["text"]
        if kind == "clock":
            self.create_oval(s*.13,s*.13,s*.87,s*.87,fill="#FFF7F4",outline=ink,width=3)
            self.create_line(s*.5,s*.5,s*.5,s*.27,fill=dark,width=3);self.create_line(s*.5,s*.5,s*.68,s*.60,fill=dark,width=3)
        elif kind == "robot":
            self.create_rectangle(s*.16,s*.25,s*.84,s*.78,fill="#FFF7F4",outline=ink,width=3)
            self.create_line(s*.5,s*.25,s*.5,s*.10,fill=ink,width=3);self.create_oval(s*.45,s*.05,s*.55,s*.15,fill=ink,outline="")
            self.create_oval(s*.30,s*.43,s*.40,s*.53,fill=dark,outline="");self.create_oval(s*.60,s*.43,s*.70,s*.53,fill=dark,outline="")
        elif kind in {"signal","chart","bars"}:
            for i,h in enumerate((.28,.48,.70)):
                self.create_rectangle(s*(.16+i*.24),s*(.82-h),s*(.31+i*.24),s*.82,fill=ink,outline="")
        elif kind == "moon":
            self.create_oval(s*.15,s*.12,s*.80,s*.82,fill=ink,outline="");self.create_oval(s*.35,s*.04,s*.88,s*.66,fill=bg,outline="")
        elif kind == "wallet":
            self.create_rectangle(s*.10,s*.25,s*.88,s*.78,fill="#FFF1D5",outline=ink,width=3)
            self.create_rectangle(s*.55,s*.42,s*.92,s*.65,fill="#FFF7F4",outline=ink,width=2);self.create_oval(s*.69,s*.50,s*.76,s*.57,fill=ink,outline="")
        elif kind == "book":
            self.create_rectangle(s*.13,s*.15,s*.47,s*.84,fill="#FFF7F4",outline=ink,width=2);self.create_rectangle(s*.53,s*.15,s*.87,s*.84,fill="#FFF7F4",outline=ink,width=2)
            self.create_line(s*.5,s*.17,s*.5,s*.82,fill=ink,width=3)
        elif kind == "calendar":
            self.create_rectangle(s*.12,s*.20,s*.88,s*.84,fill="#FFF7F4",outline=ink,width=3);self.create_rectangle(s*.12,s*.20,s*.88,s*.38,fill=ink,outline="")
            for x in (.30,.50,.70): self.create_oval(s*(x-.05),s*.52,s*(x+.05),s*.62,fill=ink,outline="")
        elif kind == "gear":
            self.create_oval(s*.18,s*.18,s*.82,s*.82,fill=ink,outline="");self.create_oval(s*.38,s*.38,s*.62,s*.62,fill=bg,outline="")
        elif kind == "telescope":
            self.create_polygon(s*.12,s*.35,s*.68,s*.20,s*.76,s*.48,s*.20,s*.62,fill=ink,outline=dark);self.create_line(s*.49,s*.53,s*.30,s*.87,fill=dark,width=3);self.create_line(s*.52,s*.53,s*.72,s*.87,fill=dark,width=3)
        elif kind == "arrows":
            self.create_line(s*.15,s*.35,s*.80,s*.35,fill=ink,width=4,arrow=tk.LAST);self.create_line(s*.85,s*.65,s*.20,s*.65,fill=ink,width=4,arrow=tk.LAST)
        else:
            CuteSticker(self, "paw", size, bg)


class PageGuide(Card):
    def __init__(self, parent, title: str, instruction: str, note: str):
        super().__init__(parent, bg="#FFF3DE", highlightbackground="#E9D4A9")
        self.columnconfigure(1, weight=1)
        CuteSticker(self, "paw", 44, "#FFF3DE").grid(row=0, column=0, rowspan=2, padx=(14,10), pady=9)
        tk.Label(self, text=title, bg="#FFF3DE", fg=COLORS["primary_dark"],
                 font=(FONT, FONT_SIZE["body"], "bold")).grid(row=0, column=1, sticky="sw", pady=(9,1))
        label = tk.Label(self, text=instruction, justify="left", anchor="w", bg="#FFF3DE",
                         fg=COLORS["text"], font=(FONT, FONT_SIZE["small"]), wraplength=1100)
        label.grid(row=1, column=1, sticky="new", pady=(0,9))
        chip = tk.Label(self, text=note, bg="#F5D9DD", fg=COLORS["primary_dark"],
                        font=(FONT, FONT_SIZE["micro"], "bold"), padx=11, pady=5)
        chip.grid(row=0, column=2, rowspan=2, sticky="e", padx=14)
        self.bind("<Configure>", lambda event: label.config(wraplength=max(420, event.width - 330)))


class BasePage(tk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, parent, app: "SectorRadarApp"):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self.db = app.db
        header = tk.Frame(self, bg=COLORS["bg"])
        header.pack(fill="x", padx=28, pady=(20, 10))
        page_key = getattr(self, "page_key", "")
        badge_kind, badge_color = PAGE_META.get(page_key, ("paw", COLORS["primary"]))
        badge = Pictogram(header, badge_kind, badge_color, 52, COLORS["bg"])
        badge.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12), pady=(2, 0))
        title = tk.Label(header, text=plain_ui_text(self.title), bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, FONT_SIZE["title"], "bold"))
        title.grid(row=0, column=1, sticky="w")
        subtitle = tk.Label(
            header, text=self.subtitle, justify="left", anchor="w", bg=COLORS["bg"],
            fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]), wraplength=1120,
        )
        subtitle.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        header.columnconfigure(1, weight=1)
        header.bind("<Configure>", lambda event: subtitle.config(wraplength=max(320, event.width - 90)))
        decorations = tk.Frame(header, bg=COLORS["bg"])
        decorations.grid(row=0, column=2, rowspan=2, sticky="e", padx=(16,0))
        CuteSticker(decorations, "star", 32, COLORS["bg"]).pack(side="left")
        CuteSticker(decorations, "heart", 32, COLORS["bg"]).pack(side="left", padx=3)
        CuteSticker(decorations, "paw", 32, COLORS["bg"]).pack(side="left")
        guide_title, guide_text, guide_note = PAGE_GUIDES.get(page_key, ("本页怎么用", "按照页面顺序完成操作。", "数据在本机"))
        self.guide = PageGuide(self, guide_title, guide_text, guide_note)
        self.guide.pack(fill="x", padx=28, pady=(0, 12))
        self.body = tk.Frame(self, bg=COLORS["bg"])
        self.body.pack(fill="both", expand=True, padx=28, pady=(0, 20))

    def refresh(self):
        pass


class DashboardPage(BasePage):
    title = "⏰ 今日决策台"
    subtitle = "AI只提出建议；你决定哪些操作进入草稿；15:00后仅把仍存活的草稿写入记录。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._action_active = False
        self._draft_enrichment_active = False
        self._draft_enrichment_stop = threading.Event()
        self._idle_name_stop = threading.Event()
        self._idle_name_started = False
        self._idle_name_after_id = None
        self._ai_capital_context = {"stale": False, "current": {}}
        self.draft_service = DecisionDraftService(self.db)

        workflow = Card(self.body, bg="#FFFCF9", highlightbackground="#E5D6CF")
        workflow.pack(fill="x", pady=(0, 10))
        workflow_head = tk.Frame(workflow, bg="#FFFCF9"); workflow_head.pack(fill="x", padx=12, pady=(8, 5))
        tk.Label(workflow_head, text="今天按这 7 步走", bg="#FFFCF9", fg=COLORS["text"], font=(FONT, FONT_SIZE["section"], "bold")).pack(side="left")
        self.workflow_next = tk.Label(workflow_head, text="", bg="#FFFCF9", fg=COLORS["primary_dark"], font=(FONT, FONT_SIZE["small"], "bold"))
        self.workflow_next.pack(side="right")
        self.workflow_cards = []
        workflow_grid = tk.Frame(workflow, bg="#FFFCF9")
        workflow_grid.pack(fill="x", padx=8, pady=(0, 9))
        # Twelve equal grid columns give the seven steps a stable 4 + 3 layout:
        # four cards span 3 columns on the first row and three cards span 4 on
        # the second.  A single packed row used each label's natural text width
        # and overflowed to the right on Windows high-DPI/full-screen layouts.
        for column in range(12):
            workflow_grid.columnconfigure(column, weight=1, uniform="daily-workflow")
        for number, title, page in [
            (1, "更新数据", "market"), (2, "AI扫描", "api"), (3, "查看推荐", "radar"),
            (4, "加入草稿", "decision"), (5, "收盘确认", "decision"),
            (6, "更新持仓", "trades"), (7, "查看复盘", "review"),
        ]:
            if number <= 4:
                grid_row, grid_column, columnspan = 0, (number - 1) * 3, 3
            else:
                grid_row, grid_column, columnspan = 1, (number - 5) * 4, 4
            card = tk.Frame(workflow_grid, bg="#FFFFFF", highlightbackground=COLORS["border"], highlightthickness=1, cursor="hand2")
            card.grid(
                row=grid_row, column=grid_column, columnspan=columnspan,
                sticky="nsew", padx=3, pady=(0, 5) if grid_row == 0 else (0, 0),
            )
            title_label = tk.Label(card, text=f"{number}  {title}", bg="#FFFFFF", fg=COLORS["text"], font=(FONT, FONT_SIZE["small"], "bold"), cursor="hand2")
            title_label.pack(anchor="w", padx=9, pady=(7, 1))
            status_label = tk.Label(card, text="检查中", bg="#FFFFFF", fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]), cursor="hand2")
            status_label.pack(anchor="w", padx=9)
            detail_label = tk.Label(
                card, text="", bg="#FFFFFF", fg=COLORS["muted"],
                font=(FONT, FONT_SIZE["hint"]), anchor="w", justify="left",
                wraplength=220, cursor="hand2",
            )
            detail_label.pack(fill="x", padx=9, pady=(1, 7))
            card.bind(
                "<Configure>",
                lambda event, label=detail_label: label.config(wraplength=max(100, event.width - 20)),
                add="+",
            )
            for widget in (card, title_label, status_label, detail_label):
                widget.bind("<Button-1>", lambda _event, key=page: self._open_workflow_page(key))
            self.workflow_cards.append((card, title_label, status_label, detail_label))

        summary = tk.Frame(self.body, bg=COLORS["bg"]); summary.pack(fill="x")
        self.card_values = []
        for title in ("可执行买入 / AI建议", "存活草稿", "今日已写入", "收盘入账"):
            card = Card(summary); card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(card, text=title, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"])).pack(anchor="w", padx=12, pady=(8, 1))
            value = tk.Label(card, text="—", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, FONT_SIZE["section"], "bold")); value.pack(anchor="w", padx=12, pady=(0, 8)); self.card_values.append(value)
        self.money_summary = tk.Label(self.body, text="", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]), anchor="w")
        self.money_summary.pack(fill="x", pady=(7, 8))
        self.capital_guard = tk.Label(
            self.body, text="", bg="#FFF4D6", fg=COLORS["warning"],
            font=(FONT, FONT_SIZE["small"], "bold"), anchor="w", justify="left",
            padx=10, pady=6, wraplength=1500,
        )
        self.capital_guard.pack(fill="x", pady=(0, 7))

        self.work_tabs = ttk.Notebook(self.body)
        self.work_tabs.pack(fill="both", expand=True, pady=(4, 10))
        self.ai_panel = Card(self.work_tabs, bg="#F5F9FE", highlightbackground="#B9CCE3")
        ai_panel = self.ai_panel
        self.work_tabs.add(ai_panel, text="① AI建议（参考·不记账）")
        ai_header = tk.Frame(ai_panel, bg="#F5F9FE"); ai_header.pack(fill="x", padx=14, pady=(10, 5))
        ai_title = tk.Frame(ai_header, bg="#F5F9FE"); ai_title.pack(side="left", fill="x", expand=True)
        tk.Label(ai_title, text="① AI建议｜先看，再决定是否加入草稿", bg="#F5F9FE", fg=COLORS["info"], font=(FONT, FONT_SIZE["section"], "bold")).pack(anchor="w")
        tk.Label(ai_title, text="蓝色区域只是模型意见，不会自动记账或下单。", bg="#F5F9FE", fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"])).pack(anchor="w", pady=(1, 0))
        self.action_button = CuteButton(ai_header, "🧠 AI补全今日行动", self.generate_action_advice, "success"); self.action_button.pack(side="right")

        ai_actions = tk.Frame(ai_panel, bg="#F5F9FE"); ai_actions.pack(fill="x", padx=14, pady=(0, 7))
        CuteButton(ai_actions, "➕ 把选中AI建议加入草稿", self.add_selected_ai_to_draft, "success").pack(side="left")
        CuteButton(ai_actions, "📝 输入盘中数据", self.edit_intraday, "secondary").pack(side="left", padx=7)
        CuteButton(ai_actions, "📋 复制14:45任务", lambda: self.app.generate_task("intraday"), "secondary").pack(side="left")
        CuteButton(ai_actions, "🔄 刷新", self.refresh, "secondary").pack(side="right")

        self.ai_tabs = ttk.Notebook(ai_panel); self.ai_tabs.pack(fill="x", padx=14)
        buy_tab = tk.Frame(self.ai_tabs, bg=COLORS["panel"]); hold_tab = tk.Frame(self.ai_tabs, bg=COLORS["panel"])
        self.ai_tabs.add(buy_tab, text="板块与新买入建议"); self.ai_tabs.add(hold_tab, text="当前持仓建议")
        columns = ("rank", "tilt", "timing", "execution", "sector", "fund", "score", "allocation", "amount", "change", "decision")
        self.tree = ttk.Treeview(buy_tab, columns=columns, show="headings", height=4)
        for key, label, width in [
            ("rank","排名",45),("tilt","七档研究结论",100),("timing","当前择时",85),
            ("execution","基金执行状态",105),("sector","板块",125),("fund","对应基金",190),
            ("score","推荐分",65),("allocation","目标占比",75),("amount","当前可新增",95),
            ("change","盘中涨跌",80),("decision","盘中复核",100),
        ]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width,anchor="w" if key in {"tilt","execution","sector","fund"} else "center")
        self.tree.pack(fill="x"); self.tree.bind("<<TreeviewSelect>>", self.show_detail); self.tree.bind("<Double-1>", lambda _e:self.add_selected_ai_to_draft())
        holding_columns = ("sleeve","fund","value","active_pct","action","sell_amount")
        self.holding_tree = ttk.Treeview(hold_tab, columns=holding_columns, show="headings", height=4)
        for key,label,width in [("sleeve","资金桶",75),("fund","当前持仓",320),("value","当前市值",105),("active_pct","占主动上限",110),("action","AI动作",135),("sell_amount","AI建议操作金额",145)]:
            self.holding_tree.heading(key,text=label); self.holding_tree.column(key,width=width,anchor="w" if key in {"fund","action"} else "center")
        self.holding_tree.pack(fill="x"); self.holding_tree.bind("<<TreeviewSelect>>", self.show_holding_detail); self.holding_tree.bind("<Double-1>", lambda _e:self.add_selected_ai_to_draft())
        self.detail = tk.Text(ai_panel, height=4, wrap="word", bg="#FBFDFF", fg=COLORS["text"], relief="flat", highlightbackground="#C9D9EA", highlightthickness=1, font=(FONT,FONT_SIZE["small"]), padx=10, pady=7)
        self.detail.pack(fill="x", padx=14, pady=(6, 12))

        self.draft_panel = Card(self.work_tabs, bg="#FFF6F8", highlightbackground="#E7BCC7")
        draft_panel = self.draft_panel
        self.work_tabs.add(draft_panel, text="② 我的草稿（拟操作）")
        draft_header = tk.Frame(draft_panel, bg="#FFF6F8"); draft_header.pack(fill="x", padx=14, pady=(10, 5))
        draft_title = tk.Frame(draft_header, bg="#FFF6F8"); draft_title.pack(side="left", fill="x", expand=True)
        tk.Label(draft_title, text="② 我的盘中草稿｜这里才代表我准备怎么做", bg="#FFF6F8", fg=COLORS["primary_dark"], font=(FONT,FONT_SIZE["section"],"bold")).pack(anchor="w")
        tk.Label(draft_title, text="可新增、编辑或撤销；收盘前都只是草稿。", bg="#FFF6F8", fg=COLORS["muted"], font=(FONT,FONT_SIZE["small"])).pack(anchor="w", pady=(1, 0))
        self.draft_status = tk.Label(draft_header, text="", bg="#FFF6F8", fg=COLORS["muted"], font=(FONT,FONT_SIZE["small"])); self.draft_status.pack(side="right")
        draft_actions = tk.Frame(draft_panel, bg="#FFF6F8"); draft_actions.pack(fill="x", padx=14, pady=(0, 7))
        CuteButton(draft_actions,"➕ 手工添加操作",self.add_manual_draft).pack(side="left")
        CuteButton(draft_actions,"✏️ 编辑选中草稿",self.edit_selected_draft,"secondary").pack(side="left",padx=7)
        CuteButton(draft_actions,"🗑 删除/撤销草稿",self.delete_selected_draft,"danger").pack(side="left")
        CuteButton(draft_actions,"🧮 检查草稿仓位",self.check_draft_allocation,"secondary").pack(side="left",padx=7)
        self.draft_enrich_button=CuteButton(draft_actions,"🌐 联网补全名称/净值/份额",self.enrich_selected_drafts,"success")
        self.draft_enrich_button.pack(side="left")
        self.draft_stop_button=CuteButton(draft_actions,"⏹ 停止补全",self.stop_draft_enrichment,"danger")
        self.draft_stop_button.pack(side="left",padx=7);self.draft_stop_button.set_enabled(False)
        stale_actions = tk.Frame(draft_panel, bg="#FFF6F8"); stale_actions.pack(fill="x", padx=14, pady=(0, 7))
        tk.Label(
            stale_actions, text="跨日草稿处理：", bg="#FFF6F8", fg=COLORS["primary_dark"],
            font=(FONT, FONT_SIZE["small"], "bold"),
        ).pack(side="left", padx=(0, 7))
        CuteButton(
            stale_actions,"✅ 确认原日已扣款",self.acknowledge_selected_stale_recurring,"success",
        ).pack(side="left")
        CuteButton(
            stale_actions,"↪️ 仍打算今天操作",self.carry_selected_stale_draft,"secondary",
        ).pack(side="left",padx=7)
        CuteButton(
            stale_actions,"🚫 确认原日未执行",self.abandon_selected_stale_draft,"danger",
        ).pack(side="left")
        draft_cols=("source","fund","action","amount","pricing_date","nav_units","settlement","note")
        self.draft_tree=ttk.Treeview(draft_panel,columns=draft_cols,show="headings",height=5)
        for key,label,width in [("source","来源",105),("fund","基金代码与自动名称",270),("action","操作",75),("amount","操作金额",105),("pricing_date","净值交易日",105),("nav_units","净值 / 估算份额",150),("settlement","联网核算状态",205),("note","我的理由/说明",260)]:
            self.draft_tree.heading(key,text=label);self.draft_tree.column(key,width=width,anchor="w" if key in {"fund","note"} else "center")
        self.draft_tree.pack(fill="x", padx=14, pady=(0, 12));self.draft_tree.bind("<Double-1>",lambda _e:self.edit_selected_draft())

        self.record_panel = Card(self.work_tabs, bg="#F3FBF7", highlightbackground="#AFCFBE")
        self.work_tabs.add(self.record_panel, text="③ 收盘后记录（真实流水）")
        record_header=tk.Frame(self.record_panel,bg="#F3FBF7");record_header.pack(fill="x",padx=14,pady=(10,6))
        record_title=tk.Frame(record_header,bg="#F3FBF7");record_title.pack(side="left",fill="x",expand=True)
        tk.Label(record_title,text="③ 收盘后记录｜草稿写入后在这里核对",bg="#F3FBF7",fg=COLORS["accent"],font=(FONT,FONT_SIZE["section"],"bold")).pack(anchor="w")
        tk.Label(record_title,text="待确认不等于持仓；支付宝正式份额、净值和费用确认后，才会自动进入当前持仓。",bg="#F3FBF7",fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"])).pack(anchor="w")
        CuteButton(record_header,"✅ 收盘后一键写入仍存活草稿",self.finalize_drafts,"success").pack(side="right")
        record_cols=("source","fund","action","amount","pricing","status","trade")
        self.record_tree=ttk.Treeview(self.record_panel,columns=record_cols,show="headings",height=8)
        for key,label,width in [("source","草稿来源",120),("fund","基金代码与名称",310),("action","操作",80),("amount","金额",110),("pricing","净值交易日",110),("status","当前状态",150),("trade","交易日志",120)]:
            self.record_tree.heading(key,text=label);self.record_tree.column(key,width=width,anchor="w" if key in {"source","fund"} else "center")
        self.record_tree.pack(fill="both",expand=True,padx=14,pady=(0,8))
        self.record_help=tk.Label(self.record_panel,text="",bg="#F3FBF7",fg=COLORS["text"],justify="left",font=(FONT,FONT_SIZE["small"]));self.record_help.pack(anchor="w",padx=14,pady=(0,12))

        close_bar=Card(self.body,bg="#EEF8F4",highlightbackground=COLORS["accent"]);close_bar.pack(fill="x",pady=(10,0))
        close_text=tk.Frame(close_bar,bg="#EEF8F4");close_text.pack(side="left",fill="both",expand=True,padx=14,pady=10)
        tk.Label(close_text,text="③ 15:00收盘后确认",bg="#EEF8F4",fg=COLORS["accent"],font=(FONT,FONT_SIZE["section"],"bold")).pack(anchor="w")
        self.close_note=tk.Label(close_text,text="",bg="#EEF8F4",fg=COLORS["text"],justify="left",font=(FONT,FONT_SIZE["small"]));self.close_note.pack(anchor="w")
        CuteButton(close_bar,"✅ 收盘后一键写入仍存活草稿",self.finalize_drafts,"success").pack(side="right",padx=14,pady=10)

    def refresh(self):
        try:
            self.draft_service.ensure_today_recurring()
            recurring_error = ""
        except Exception as exc:
            recurring_error = str(exc)
        m=dashboard_metrics(self.db);run=m["latest_run"]
        self._refresh_workflow()
        self._ai_capital_context=action_capital_context(run,m)
        capital_stale=bool(self._ai_capital_context.get("stale"))
        self.tree.delete(*self.tree.get_children());self.holding_tree.delete(*self.holding_tree.get_children())
        self.candidate_map={};self.holding_action_map={}
        candidates=[];holding_actions=[];live_amounts={};self._candidate_live_amounts={}
        if run:
            candidates=list(m.get("candidates") or [])
            live_rows=self.draft_service.live_candidate_allocations(candidates)
            candidates=[live_rows.get(int(row["id"]),row) for row in candidates]
            live_amounts={
                int(row["id"]): round(float(row.get("suggested_amount") or 0),2)
                for row in candidates
            }
            self._candidate_live_amounts=live_amounts
            settings=self.db.settings()
            for c in candidates:
                result=evaluate_intraday(c,settings)
                if c["current_change"] is not None:
                    self.db.execute("UPDATE candidates SET decision=?,decision_reason=? WHERE id=?",(result.decision,"\n".join(result.reasons),c["id"]));c["decision"]=result.decision;c["decision_reason"]="\n".join(result.reasons)
                iid=str(c["id"]);self.candidate_map[iid]=c
                tag="profit" if result.level=="profit" else "danger" if result.level=="danger" else "warning" if result.level=="warning" else "normal"
                identity=InvestableMappingService(self.db).identity_for(str(c.get("universe_id") or ""))
                fund=f"{c['fund_code']} {c['fund_name']}".strip()
                if not fund and identity:
                    fund=f"先选基金｜{identity.get('ant_index_code') or identity.get('official_index_code')} {identity.get('ant_index_name') or identity.get('official_index_name')}"
                if not fund:
                    fund="可投身份待核验·不可执行"
                amount=live_amounts[int(c["id"])]
                allocation_text=f"{float(c.get('target_allocation_pct') or c.get('allocation_pct') or 0):.1f}%"
                amount_text=money(amount)
                self.tree.insert("","end",iid=iid,values=(
                    c["rank_no"],c.get("research_tilt") or c.get("status") or "旧制未分档",
                    c.get("timing_status") or "待核验",c.get("execution_status") or "不可执行",
                    c["sector_name"],fund,format_float(c.get("score"),1),allocation_text,amount_text,
                    pct(c["current_change"]),c["decision"],
                ),tags=(tag,))
            self.tree.tag_configure("profit",foreground=COLORS["profit"]);self.tree.tag_configure("danger",foreground=COLORS["danger"]);self.tree.tag_configure("warning",foreground=COLORS["warning"])
            holding_actions=list(m.get("holding_actions") or [])
            for number,action in enumerate(holding_actions,1):
                iid=f"holding-{number}";self.holding_action_map[iid]=action;amount=self.draft_service.live_holding_action_amount(action)
                action_text=(f"{action.get('action') or '—'}·金额实时重算" if capital_stale else action.get("action") or "—")
                amount_text="待量化" if amount is None else money(amount)
                self.holding_tree.insert("","end",iid=iid,values=(action.get("sleeve") or "—",f"{action.get('fund_code','')} {action.get('fund_name','')}".strip(),money(action.get("market_value",0)),f"{float(action.get('position_pct_active') or 0):.1f}%",action_text,amount_text))
        self._refresh_drafts()
        self._refresh_close_records()
        drafts=self.draft_service.list_for_day();alive=sum(row["status"]=="草稿" for row in drafts);posted=sum(row["status"]!="草稿" for row in drafts)
        suggested=sum(str(row.get("research_tilt") or "") in {"非常推荐买","推荐买","有点推荐买"} for row in candidates)
        executable=sum(live_amounts[int(row["id"])]>0 and bool(str(row.get("fund_code") or "").strip()) for row in candidates)
        self.card_values[0].config(text=f"{executable} / {suggested}")
        self.card_values[1].config(text=str(alive),fg=COLORS["primary_dark"])
        self.card_values[2].config(text=str(posted),fg=COLORS["accent"])
        closed=local_now().hour>=int(float(self.db.setting("decision_close_hour","15")))
        self.card_values[3].config(text="可以入账" if closed else "等待15:00",fg=COLORS["accent"] if closed else COLORS["warning"])
        self.money_summary.config(
            text=(
                f"主动现金 {money(m['active_cash_before_reservations'])}　｜　"
                f"草稿/待确认预占 {money(m['active_reserved'])}　｜　"
                f"尚可安排 {money(m['active_cash_available'])}　｜　"
                f"当前市值敞口 {money(m['active_market_exposure'])}　｜　"
                f"用户额度 {money(m['active_limit'])}　｜　全部持仓市值 {money(m['total_value'])}"
            )
        )
        if capital_stale:
            self.capital_guard.config(
                text=(f"↻ 旧AI资金快照已失效：现金账本 {money(m['active_cash_before_reservations'])}，"
                      f"扣除存活草稿和待确认买单预占 {money(m['active_reserved'])}，尚可安排 {money(m['active_cash_available'])}；"
                      f"当前市值敞口 {money(m['active_market_exposure'])} 只用于风险观察，不参与现金加减。"
                      "旧研究可以阅读，但旧金额不会沿用。"),
                bg="#EEF4FB", fg=COLORS["info"],
            )
        else:
            self.capital_guard.config(
                text=(
                    (f"⚠ 当前预占超过可用现金 {money(m['active_over_limit'])}；新的买入会被硬拦截。请删除草稿、撤销待确认买单或到设置调整额度。"
                     if float(m.get("active_over_limit") or 0) > 0 else
                     f"✓ 真实现金口径：现金账本 {money(m['active_cash_before_reservations'])}－预占 {money(m['active_reserved'])}"
                     f"＝尚可安排 {money(m['active_cash_available'])}。基金涨跌只改变市值敞口，不会凭空增减现金。")
                ),
                bg="#FFF4D6" if float(m.get("active_over_limit") or 0) > 0 else "#EEF8F4",
                fg=COLORS["warning"] if float(m.get("active_over_limit") or 0) > 0 else COLORS["accent"],
            )
        stale_count=len(self.draft_service.list_stale_drafts())
        stale_note=(f"；另有 {stale_count} 项旧草稿待确认原日结果" if stale_count else "")
        self.draft_status.config(
            text=(f"今天仍存活 {alive} 项；自动定投已加入{stale_note}" +
                  (f"；定投生成异常：{recurring_error}" if recurring_error else "")),
            fg=COLORS["danger"] if recurring_error or stale_count else COLORS["muted"],
        )
        self.close_note.config(text=f"当前有 {alive} 项仍存活。普通买卖会写为“待确认”；定投会等待正式净值和支付宝扣款核对。\n已撤销草稿不会入账；重复点击不会重复创建记录。")
        if run:
            self._show_ai_summary(run,candidates,holding_actions,m,live_amounts)
        else:
            state=m.get("research_status") or {}
            self.detail.delete("1.0","end");self.detail.insert(
                "1.0",
                (state.get("reason") or "还没有可用于今天的当前目录研究。")
                + "\n你仍可手工建立盘中草稿；旧研究已隔离为只读，不会再次付费或加入草稿。",
            )
        if not self._idle_name_started and self.db.setting("fund_name_idle_resolve","1") == "1":
            self._idle_name_started = True
            self._idle_name_after_id = self.after(
                1200, self._start_idle_name_resolution,
            )

    def _open_workflow_page(self, page_key, step_key=""):
        self.app.show(page_key)
        if page_key == "decision":
            target = {"draft": 1, "close": 2}.get(step_key)
            if target is not None:
                self.work_tabs.select(target)

    def _refresh_workflow(self):
        workflow = daily_workflow_status(self.db)
        palette = {
            "done": ("#EEF8F4", COLORS["accent"]),
            "ready": ("#EEF4FB", COLORS["info"]),
            "todo": ("#FFF4D6", COLORS["warning"]),
            "waiting": ("#FFF8F0", COLORS["muted"]),
            "locked": ("#F2EFEE", COLORS["muted"]),
        }
        for widgets, step in zip(self.workflow_cards, workflow["steps"]):
            card, title_label, status_label, detail_label = widgets
            bg, fg = palette.get(step["state"], ("#FFFFFF", COLORS["text"]))
            card.config(bg=bg, highlightbackground=fg)
            title_label.config(bg=bg, fg=fg)
            status_label.config(bg=bg, fg=fg, text=step["status"])
            detail_label.config(bg=bg, text=step["detail"])
            for widget in (card, title_label, status_label, detail_label):
                widget.bind(
                    "<Button-1>",
                    lambda _event, page=step["page"], key=step["key"]: self._open_workflow_page(page, key),
                )
        next_step = workflow["next_step"]
        self.workflow_next.config(
            text=(
                f"建议下一步：{next_step['title']} · {next_step['status']}"
                f"｜{workflow.get('next_reason') or '按日常流程推进'}"
            )
        )

    def _refresh_drafts(self):
        self.draft_tree.delete(*self.draft_tree.get_children());self.draft_map={}
        today=local_today().isoformat()
        rows=[*self.draft_service.list_stale_drafts(today),*self.draft_service.list_for_day(today)]
        for row in rows:
            iid=str(row["id"]);self.draft_map[iid]=row
            stale=row["status"]=="草稿" and str(row["trade_date"] or "")<today
            tag="stale" if stale else "alive" if row["status"]=="草稿" else "posted"
            note=row["rationale"] or row["result_note"] or row["ai_advice"]
            nav_units = "—"
            if float(row.get("settlement_nav") or 0) > 0:
                nav_units = f"{float(row['settlement_nav']):.4f} / {float(row.get('settlement_units') or 0):.2f}份"
            source=(f"旧草稿·{row['trade_date']}" if stale else row["source_kind"])
            if stale and row.get("recurring_occurrence_id"):
                settlement="请确认：原日已扣款 / 原日未扣款"
            elif stale:
                settlement="请确认：今天续做 / 原日未执行"
            else:
                settlement=row.get("settlement_status") or row["status"]
            self.draft_tree.insert("","end",iid=iid,values=(source,f"{row['fund_code']} {row['fund_name']}",row["action"],money(row["planned_amount"]),row.get("pricing_date") or row["trade_date"],nav_units,settlement,note),tags=(tag,))
        self.draft_tree.tag_configure("stale",foreground=COLORS["danger"])
        self.draft_tree.tag_configure("alive",foreground=COLORS["primary_dark"]);self.draft_tree.tag_configure("posted",foreground=COLORS["accent"])

    def _refresh_close_records(self):
        self.record_tree.delete(*self.record_tree.get_children())
        rows=self.db.rows(
            """SELECT d.*,t.status AS trade_status FROM decision_drafts d
                 LEFT JOIN trades t ON t.id=d.trade_id
                WHERE d.trade_date=? AND d.status<>'草稿' ORDER BY d.id DESC""",
            (local_today().isoformat(),),
        )
        for row in rows:
            status=row.get("trade_status") or row.get("status") or "—"
            trade=f"#{row['trade_id']}" if row.get("trade_id") else "定投待核对"
            self.record_tree.insert("","end",values=(row["source_kind"],f"{row['fund_code']} {row['fund_name']}",row["action"],money(row["planned_amount"]),row.get("pricing_date") or row["trade_date"],status,trade),tags=("confirmed" if status=="已确认" else "pending",))
        self.record_tree.tag_configure("confirmed",foreground=COLORS["accent"]);self.record_tree.tag_configure("pending",foreground=COLORS["warning"])
        self.record_help.config(text=(f"今天已有 {len(rows)} 条收盘后记录。待确认记录请到‘交易日志’补全；确认买入后会自动增加持仓，重复确认不会重复入账。" if rows else "今天还没有收盘后记录。15:00后点击上方按钮，只有当时仍留在草稿区的操作会写入。"))

    def _show_ai_summary(self,run,candidates,holding_actions,metrics,live_amount_by_id):
        live_amounts=[live_amount_by_id.get(int(row["id"]),0.0) for row in candidates]
        positive=sum(amount>0 for amount in live_amounts);executable=sum(amount>0 and bool(str(row.get("fund_code") or "").strip()) for row,amount in zip(candidates,live_amounts));summary=run.get("action_summary") or run.get("summary") or "";no_buy=run.get("no_buy_reason") or ""
        stale=bool(self._ai_capital_context.get("stale"))
        if stale:
            summary=remove_stale_capital_claim(summary)
            no_buy=remove_stale_capital_claim(no_buy) if no_buy else ""
        prefix="AI市场判断（金额已按实时仓位重算）" if stale else "AI市场判断"
        text=(
            f"真实现金：账本{money(metrics['active_cash_before_reservations'])}－预占{money(metrics['active_reserved'])}"
            f"＝尚可安排{money(metrics['active_cash_available'])}；当前市值敞口{money(metrics['active_market_exposure'])}。\n"
            f"{prefix}：{run['market_regime']}；研究覆盖{run['analyzed_total']}/{run['universe_total']}；"
            f"研究正向{sum(str(row.get('research_tilt') or '') in {'非常推荐买','推荐买','有点推荐买'} for row in candidates)}项；"
            f"当前正金额{positive}项，其中可加入草稿{executable}项。\n{summary}"
        )
        if no_buy:
            label="AI观望理由（旧资金数字已删除）" if stale else "AI不买原因"
            text+=f"\n{label}：{no_buy}"
        if len(candidates)<10:text+="\n最终主榜以10项为上限并设绝对合格线；少于10项是正常结果，不会为了凑数补入低质量方向。"
        text+="\n蓝色区域全部只是AI意见；只有粉色草稿区代表你的拟操作。"
        self.detail.delete("1.0","end");self.detail.insert("1.0",text)

    def selected(self):
        selected=self.tree.selection();return self.candidate_map.get(selected[0]) if selected else None

    def show_detail(self,_event=None):
        c=self.selected()
        if not c:return
        live_amount=getattr(self,"_candidate_live_amounts",{}).get(int(c["id"]))
        if live_amount is None:
            live_amount=self.draft_service.live_candidate_amount(c)
        self.detail.delete("1.0","end");self.detail.insert("1.0",(
            f"研究结论：{c.get('research_tilt') or c.get('status') or '旧制未分档'}　"
            f"择时：{c.get('timing_status') or '待核验'}　执行：{c.get('execution_status') or '不可执行'}\n"
            f"推荐分：{float(c.get('score') or 0):.1f}　目标占主动上限"
            f"{float(c.get('target_allocation_pct') or c.get('allocation_pct') or 0):.1f}%　"
            f"按实时现金/同指数/相关桶/主题上限可新增：{money(live_amount)}\n"
            f"执行限制：{c.get('execution_reason') or '无额外限制'}\n逻辑：{c['thesis']}\n"
            f"买入条件：{c['buy_conditions']}　放弃条件：{c['abandon_conditions']}\n"
            f"风险：{c['risk']}　盘中复核：{c.get('decision_reason') or '尚未输入盘中数据'}"
        ))

    def show_holding_detail(self,_event=None):
        selected=self.holding_tree.selection();action=self.holding_action_map.get(selected[0]) if selected else None
        if not action:return
        amount=self.draft_service.live_holding_action_amount(action)
        self.detail.delete("1.0","end");self.detail.insert("1.0",f"AI持仓建议（尚未成为你的操作）：{action.get('fund_code')} {action.get('fund_name')}\n动作：{action.get('action')}　AI建议操作金额：{'待量化' if amount is None else money(amount)}\n理由：{action.get('reason') or '未说明'}\n风险：{action.get('risk') or '未说明'}\n加入草稿后只保留金额；场外份额等净值发布后再联网估算。")

    def add_selected_ai_to_draft(self):
        try:
            if self.ai_tabs.index("current")==0:
                candidate=self.selected()
                if not candidate:raise DecisionDraftError("请先选中一条AI板块建议。")
                self.draft_service.add_candidate(candidate["id"])
            else:
                selected=self.holding_tree.selection();action=self.holding_action_map.get(selected[0]) if selected else None
                if not action:raise DecisionDraftError("请先选中一条AI持仓建议。")
                self.draft_service.add_holding_action(action["task_id"],action["fund_code"])
        except DecisionDraftError as exc:messagebox.showinfo("不能加入草稿",str(exc),parent=self);return
        self.refresh();self.work_tabs.select(self.draft_panel);messagebox.showinfo("已加入我的草稿","已切换到粉色草稿页。这只是你的盘中拟操作，收盘前仍可编辑或删除；现在还没有写入交易记录。",parent=self)

    def add_manual_draft(self):self._draft_form(None)
    def selected_draft(self):
        selected=self.draft_tree.selection();return self.draft_map.get(selected[0]) if selected else None
    def edit_selected_draft(self):
        row=self.selected_draft()
        if not row:messagebox.showinfo("请先选择","请先选择一条仍存活的草稿。",parent=self);return
        if row.get("status")=="草稿" and str(row.get("trade_date") or "")<local_today().isoformat():
            messagebox.showinfo(
                "请先处理旧草稿",
                "旧草稿不能直接改成今天的真实操作。定投请选择“确认原日已扣款”或“确认原日未执行”；"
                "其他旧意向请选择“仍打算今天操作”或“确认原日未执行”。",
                parent=self,
            );return
        self._draft_form(row)
    def _draft_form(self,row):
        row=row or {}
        if row.get("status") not in {None,"草稿"}:messagebox.showinfo("已经入账","已写入或等待净值的草稿不能再编辑。",parent=self);return
        default_pricing=ChinaTradingCalendar.from_db(self.db).resolve_order(local_now()).pricing_date.isoformat()
        fields=[("基金代码（只填6位代码）","fund_code",row.get("fund_code","")),("操作","action",row.get("action","买入")),("操作金额 元（买/卖都填金额）","planned_amount",row.get("planned_amount",0)),("预计/实际净值交易日 YYYY-MM-DD","pricing_date",row.get("pricing_date") or default_pricing),("我的操作理由","rationale",row.get("rationale",""))]
        dialog=FormDialog(self,"我的盘中操作草稿",fields,choices={"action":["买入","卖出","清仓","转换","其他"]})
        if not dialog.result:return
        v=dialog.result
        try:
            values={"fund_code":v["fund_code"],"action":v["action"],"planned_amount":float(v["planned_amount"] or 0),"pricing_date":v["pricing_date"],"rationale":v["rationale"]}
            if row:self.draft_service.update_draft(row["id"],**values)
            else:self.draft_service.add_manual(**values)
        except (ValueError,DecisionDraftError) as exc:messagebox.showerror("草稿保存失败",str(exc),parent=self);return
        self.refresh()

    def _start_idle_name_resolution(self):
        self._idle_name_after_id = None
        if self.app._closing:
            return
        service=FundMetadataService(self.db)
        def worker():
            try:result=service.resolve_pending_if_idle(stop_requested=self._idle_name_stop.is_set)
            except Exception as exc:
                try:self.db.save_settings({"fund_name_idle_last_error":str(exc)[:2000]})
                except Exception:pass
                return
            if result is None:return
            resolved,_errors=result
            if resolved:
                try:self.after(0,self.refresh)
                except Exception:pass
        self.app.start_background(worker,label="空闲基金名称补全",cancel=self._idle_name_stop.set)

    def cancel_delayed_background_work(self):
        self._idle_name_stop.set()
        token = self._idle_name_after_id
        self._idle_name_after_id = None
        if token is not None:
            try:self.after_cancel(token)
            except tk.TclError:pass

    def enrich_selected_drafts(self):
        if self._draft_enrichment_active:
            messagebox.showinfo("正在联网补全","名称、净值和份额正在补全，请稍候。",parent=self);return
        selected=list(self.draft_tree.selection())
        ids=[int(value) for value in selected] if selected else [int(row["id"]) for row in self.draft_service.list_for_day()]
        if not ids:
            messagebox.showinfo("没有草稿","今天还没有可以补全的草稿。",parent=self);return
        self._draft_enrichment_active=True;self._draft_enrichment_stop.clear()
        self.draft_enrich_button.set_enabled(False);self.draft_stop_button.set_enabled(True)
        self.draft_status.config(text="正在免费联网核对基金名称与正式净值；不会调用DeepSeek……",fg=COLORS["info"])
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self.draft_status.config(
                text=f"草稿联网补全 {d}/{t}｜{value}；停止会在当前请求结束后生效。",fg=COLORS["info"],
            ))
        def worker():
            try:result=DraftSettlementService(self.db).enrich(
                ids,stop_requested=self._draft_enrichment_stop.is_set,progress=progress,
            )
            except Exception as exc:self.after(0,lambda error=exc:self._finish_draft_enrichment(None,error))
            else:self.after(0,lambda:self._finish_draft_enrichment(result,None))
        self.app.start_background(worker,label="草稿联网补全",cancel=self._draft_enrichment_stop.set)

    def stop_draft_enrichment(self):
        if not self._draft_enrichment_active:return
        self._draft_enrichment_stop.set()
        self.draft_status.config(text="已请求停止；当前基金请求结束后不再处理下一条草稿。",fg=COLORS["warning"])
        self.draft_stop_button.set_enabled(False)

    def _finish_draft_enrichment(self,result,error):
        self._draft_enrichment_active=False
        self.draft_enrich_button.set_enabled(True);self.draft_stop_button.set_enabled(False)
        self.refresh()
        if error:
            messagebox.showerror("联网补全失败",str(error),parent=self);return
        fee_note=(
            f"；赎回费阶梯新补全 {result.fee_schedules_resolved} 只"
            f"；费率暂未补齐 {result.fee_schedule_failures} 只"
        )
        title="联网补全已停止" if result.stopped else "联网补全完成"
        messagebox.showinfo(title,f"检查 {result.checked} 项；名称补全 {result.names_resolved} 项{fee_note}；正式净值已取得 {result.navs_resolved} 项；净值未发布 {result.waiting} 项；失败 {result.failed} 项。\n\n份额是按基金库费率估算，支付宝折扣和最终确认份额仍以成交页面为准；费率暂未补齐的基金可在基金库再次免费核验。",parent=self)

    def delete_selected_draft(self):
        row=self.selected_draft()
        if not row:messagebox.showinfo("请先选择","请选择准备撤销的草稿。",parent=self);return
        if str(row.get("trade_date") or "")<local_today().isoformat():
            messagebox.showinfo(
                "请明确旧草稿的真实结果",
                "旧草稿不能用普通删除含糊处理。\n\n"
                "定投：请选择“确认原日已扣款”或“确认原日未执行”。\n"
                "其他操作：请选择“仍打算今天操作”或“确认原日未执行”。",
                parent=self,
            );return
        if not messagebox.askyesno("确认撤销",f"删除后不会写入交易记录：\n{row['fund_code']} {row['fund_name']}　{row['action']}　{money(row['planned_amount'])}\n\n自动定投草稿删除后会把今日流水标记为未扣款/跳过。",parent=self):return
        try:
            self.draft_service.delete_draft(row["id"])
        except DecisionDraftError as exc:messagebox.showerror("无法删除",str(exc),parent=self);return
        self.refresh()

    def acknowledge_selected_stale_recurring(self):
        row=self.selected_draft()
        if not row:messagebox.showinfo("请先选择","请先选择一条红色的跨日定投草稿。",parent=self);return
        if row.get("status")!="草稿" or str(row.get("trade_date") or "")>=local_today().isoformat():
            messagebox.showinfo("不是跨日待处理项","只有早于今天且仍未处理的红色定投草稿可以确认原日扣款。",parent=self);return
        if not row.get("recurring_occurrence_id"):
            messagebox.showinfo("这不是自动定投","非定投旧意向请使用“仍打算今天操作”或“确认原日未执行”。",parent=self);return
        try:preview=self.draft_service.preview_stale_recurring_deduction(row["id"])
        except DecisionDraftError as exc:messagebox.showerror("无法确认扣款",str(exc),parent=self);return
        matches=preview.get("matching_trades") or []
        linked=preview.get("linked_trade")
        if len(matches)>1:
            ids="、".join(f"#{int(item['id'])}" for item in matches)
            messagebox.showerror(
                "发现多笔疑似重复交易",
                f"同日、同基金、同金额存在多笔待确认交易：{ids}。\n\n"
                "软件无法安全判断应与哪一笔合并，请先到交易日志核对或导出诊断。",
                parent=self,
            );return
        link_trade_id=int(linked["id"]) if linked else None
        if matches and linked is None:
            candidate=matches[0]
            if not messagebox.askyesno(
                "避免重复登记",
                f"交易日志中已经有一笔同日、同基金、同金额的待确认买入 #{int(candidate['id'])}。\n\n"
                "是否把它明确认作这笔定投并合并？选择“否”会停止，不会新增第二笔。",
                parent=self,
            ):return
            link_trade_id=int(candidate["id"])
        occurrence=preview["occurrence"]
        nav=float(occurrence.get("nav") or 0);units=float(occurrence.get("units") or 0)
        nav_note=(
            f"\n已取得公开净值 {nav:.4f}，当前只估算为 {units:.2f} 份；最终份额仍以支付宝为准。"
            if nav>0 and units>0 else
            "\n正式净值和份额尚未取得，之后可联网补全并按支付宝结果确认。"
        )
        if not messagebox.askyesno(
            "确认原交易日确实扣款",
            f"原交易日：{row['trade_date']}\n基金：{row['fund_code']} {row['fund_name']}\n"
            f"实际扣款：{money(row['planned_amount'])}{nav_note}\n\n"
            "此操作只记录“支付宝确实扣了这笔钱”：不会提前增加持仓、不会扣主动策略现金、"
            "不会生成成本批次。待正式份额和确认日核对后才过账。确定记录吗？",
            parent=self,
        ):return
        try:
            result=self.draft_service.acknowledge_stale_recurring_deduction(
                row["id"],link_trade_id=link_trade_id,
            )
        except DecisionDraftError as exc:messagebox.showerror("无法确认扣款",str(exc),parent=self);return
        self.refresh()
        merge_note=(f"\n已与交易日志 #{result['linked_trade_id']} 去重关联。" if result.get("linked_trade_id") else "\n目前未新建交易；待正式结算时再生成并过账。")
        messagebox.showinfo(
            "原日扣款已记录",
            f"已记录 {row['trade_date']} 实际扣款 {money(row['planned_amount'])}。{merge_note}\n\n"
            "持仓、主动现金和成本批次均未提前改变。接下来请到“定投管理”联网补全，并按支付宝正式结果确认入账。",
            parent=self,
        )

    def abandon_selected_stale_draft(self):
        row=self.selected_draft()
        if not row:messagebox.showinfo("请先选择","请先选择一条红色旧草稿。",parent=self);return
        if row.get("status")!="草稿" or str(row.get("trade_date") or "")>=local_today().isoformat():
            messagebox.showinfo("不是跨日待处理项","只有早于今天且仍未处理的红色草稿可以确认原日未执行。",parent=self);return
        wording="支付宝当日没有实际扣款" if row.get("recurring_occurrence_id") else "当日没有实际下单或已经撤单"
        if not messagebox.askyesno(
            "确认原日未执行",
            f"原草稿日期：{row['trade_date']}\n{row['fund_code']} {row['fund_name']}　{row['action']}　{money(row['planned_amount'])}\n\n"
            f"你确认{wording}吗？确认后会保留“已放弃/已跳过”的审计记录，但不会进入交易和持仓。",
            parent=self,
        ):return
        reason=("用户确认原交易日支付宝未扣款" if row.get("recurring_occurrence_id") else "用户确认原交易日未实际执行或已撤单")
        try:self.draft_service.abandon_stale_draft(row["id"],reason=reason)
        except DecisionDraftError as exc:messagebox.showerror("无法放弃旧草稿",str(exc),parent=self);return
        self.refresh()

    def carry_selected_stale_draft(self):
        row=self.selected_draft()
        if not row:messagebox.showinfo("请先选择","请先选择一条红色旧草稿。",parent=self);return
        if row.get("status")!="草稿" or str(row.get("trade_date") or "")>=local_today().isoformat():
            messagebox.showinfo("无需结转","只有早于今天且仍未处理的红色草稿可以结转。",parent=self);return
        if row.get("recurring_occurrence_id"):
            messagebox.showinfo(
                "定投不能顺延",
                "定投必须保留原扣款日期，不能顺延成今天的新操作。请选择“确认原日已扣款”或“确认原日未执行”。",
                parent=self,
            );return
        if not messagebox.askyesno(
            "确认结转到今天",
            f"原草稿日期：{row['trade_date']}\n{row['fund_code']} {row['fund_name']}　{row['action']}　{money(row['planned_amount'])}\n\n"
            "结转只表示你今天仍打算这样做，不代表已经下单；净值交易日会按今天的交易日历重新计算。继续吗？",
            parent=self,
        ):return
        try:self.draft_service.carry_stale_draft(row["id"])
        except DecisionDraftError as exc:messagebox.showerror("无法结转",str(exc),parent=self);return
        self.refresh()

    def check_draft_allocation(self):
        drafts=[row for row in self.draft_service.list_for_day() if row["status"]=="草稿" and row["action"] in {"买入","定投"}]
        proposals=[{"name":row["fund_name"],"amount":row["planned_amount"],"correlation_bucket":self.db.row("SELECT correlation_bucket FROM funds WHERE fund_code=?",(row["fund_code"],)).get("correlation_bucket","") if self.db.row("SELECT correlation_bucket FROM funds WHERE fund_code=?",(row["fund_code"],)) else "","fund_code":row["fund_code"]} for row in drafts]
        current=self.db.rows("SELECT f.fund_name AS name,f.fund_code,f.correlation_bucket,h.units*h.latest_nav AS value FROM holdings h JOIN funds f USING(fund_code) WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1")
        result=validate_allocation(proposals,current,self.db.settings());messagebox.showinfo(f"草稿仓位检查：{result.decision}","\n".join(result.reasons),parent=self)

    def finalize_drafts(self):
        alive=[row for row in self.draft_service.list_for_day() if row["status"]=="草稿"]
        if not alive:messagebox.showinfo("没有存活草稿","今天没有尚未写入的草稿。",parent=self);return
        if not messagebox.askyesno("确认收盘入账",f"将处理今天仍存活的 {len(alive)} 项草稿。\n\n普通买卖只写为“待确认”，不会伪造净值、份额或费用；自动定投转入待净值流水。已删除的撤单不会入账。继续吗？",parent=self):return
        try:result=self.draft_service.finalize_after_close()
        except DecisionDraftError as exc:messagebox.showwarning("现在不能入账",str(exc),parent=self);return
        self.app.refresh_all();messagebox.showinfo("收盘草稿已处理",f"共处理 {result['total']} 项：\n普通买卖待确认记录 {result['trades']} 项\n自动定投待净值流水 {result['recurring']} 项\n\n请在支付宝结果出来后补齐正式净值、份额和费用。",parent=self)

    def generate_action_advice(self):
        if self._action_active:
            messagebox.showwarning("任务运行中", "今日行动正在生成，请不要重复点击。", parent=self)
            return
        run = latest_current_radar_run(self.db)
        if not run:
            messagebox.showinfo(
                "没有今天可执行的研究",
                research_run_status(self.db)["reason"] + "\n\n请新建并完成一次当前目录的完整扫描。",
                parent=self,
            )
            return
        job = self.db.row("SELECT * FROM analysis_jobs WHERE task_id=?", (run["task_id"],))
        if not job:
            messagebox.showerror("任务记录缺失", "找不到这轮研究的费用与任务记录。", parent=self)
            return
        limit = min(
            float(self.db.setting("api_action_cost_limit_cny", "0.50")),
            max(0.0, float(job["cost_limit_cny"]) - float(job["estimated_cost_cny"])),
        )
        if not messagebox.askokcancel(
            "确认AI补全今日行动",
            f"将使用这轮已经完成的动态目录研究和当前持仓，只调用一次DeepSeek V4-Pro。\n"
            f"本次可用费用上限约¥{limit:.4f}；失败时不会自动重发。\n\n"
            f"Pro会给出前10个排序板块和逐只持仓动作；界面只保留可操作金额。软件按设置中的{money(self.db.setting('active_capital_limit','8000'))}主动额度计算目标，"
            "按当前持仓市值换算卖出金额，场外份额等净值发布后再补。继续吗？",
            parent=self,
        ):
            return
        try:
            client = DeepSeekClient(
                api_key_from_db(self.db),
                base_url=self.db.setting("deepseek_base_url", "https://api.deepseek.com"),
                timeout_seconds=int(float(self.db.setting("api_timeout_seconds", "360"))),
            )
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc), parent=self)
            return
        self._action_active = True
        self.action_button.config(state="disabled")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "正在使用现有动态目录研究生成今日板块与持仓行动；不会重新运行Flash搜索，请勿重复点击……")
        def worker():
            try:
                result = ResearchOrchestrator(self.db, client).run_action_advice(run["task_id"])
                self.after(0, lambda: self._finish_action_advice(result, None))
            except Exception as exc:
                self.after(0, lambda error=exc: self._finish_action_advice(None, error))
        self.app.start_background(worker,label="今日行动Pro",cancel=lambda:None)

    def _finish_action_advice(self, result, error):
        self._action_active = False
        self.action_button.config(state="normal")
        if error:
            messagebox.showerror("今日行动生成失败", str(error), parent=self)
            self.refresh()
            return
        self.app.refresh_all()
        messagebox.showinfo("今日行动已生成", result.message, parent=self)

    def edit_intraday(self):
        c = self.selected()
        if not c:
            messagebox.showinfo("先选一个候选", "请先在表格里选中要输入盘中数据的板块。", parent=self)
            return
        fields = [
            ("当前涨跌幅 %", "current_change", c["current_change"]), ("今开涨跌幅 %", "open_change", c["open_change"]),
            ("最高涨跌幅 %", "high_change", c["high_change"]), ("最低涨跌幅 %", "low_change", c["low_change"]),
            ("当前成交额（亿元）", "turnover", c["turnover"]), ("最近十分钟走势", "short_trend", c["short_trend"]),
            ("手工备注", "manual_note", c["manual_note"]),
        ]
        dialog = FormDialog(self, f"📝 {c['sector_name']} 盘中数据", fields,
                            choices={"short_trend": ["走高", "横盘", "回落", "低位回升", "高位震荡"]})
        if not dialog.result:
            return
        values = dialog.result
        numeric = []
        try:
            for key in ("current_change", "open_change", "high_change", "low_change", "turnover"):
                numeric.append(float(values[key]) if values[key] not in ("", None) else None)
        except ValueError:
            messagebox.showerror("数字格式错误", "涨跌幅和成交额必须填写数字，不要输入百分号。", parent=self)
            return
        self.db.execute(
            """UPDATE candidates SET current_change=?,open_change=?,high_change=?,low_change=?,turnover=?,
               short_trend=?,manual_note=?,updated_at=? WHERE id=?""",
            (*numeric, values["short_trend"], values["manual_note"], local_now().isoformat(timespec="seconds"), c["id"]),
        )
        self.refresh()
        self.tree.selection_set(str(c["id"]))
        self.show_detail()

    def check_allocation(self):
        selected = [self.candidate_map[i] for i in self.tree.selection() if i in self.candidate_map]
        if not selected:
            selected = [c for c in self.candidate_map.values() if c.get("decision") == "条件买入"]
        proposals = [{"name": c["sector_name"], "amount": c["suggested_amount"], "correlation_bucket": c["correlation_bucket"], "fund_code": c["fund_code"]} for c in selected]
        current = self.db.rows(
            """SELECT f.fund_name AS name,f.fund_code,f.correlation_bucket,h.units*h.latest_nav AS value
               FROM holdings h JOIN funds f USING(fund_code) WHERE h.active=1 AND COALESCE(h.counts_toward_active_capital,0)=1"""
        )
        result = validate_allocation(proposals, current, self.db.settings())
        messagebox.showinfo(f"仓位检查：{result.decision}", "\n".join(result.reasons), parent=self)


class APIRadarPage(BasePage):
    title = "🤖 一键AI扫描"
    subtitle = "Flash负责受控联网检索，V4-Pro正式版负责终审；硬行情缺失时软件会停止，不消耗API冒充完成。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._scan_active = False
        self._scan_started_at: datetime | None = None
        self._scan_status_base = "等待任务"
        self._scan_clock_after_id = None
        self._active_orchestrator: ResearchOrchestrator | None = None
        self._connection_active = False
        settings = Card(self.body)
        settings.pack(fill="x", pady=(0, 12))
        heading = tk.Frame(settings, bg=COLORS["panel"])
        heading.pack(fill="x", padx=16, pady=(13, 4))
        tk.Label(
            heading, text="连接 DeepSeek", bg=COLORS["panel"], fg=COLORS["text"],
            font=(FONT, FONT_SIZE["section"], "bold"),
        ).pack(side="left")
        self.connection_badge = tk.Label(
            heading, text="未连接", bg="#EEE3DE", fg=COLORS["muted"],
            font=(FONT, FONT_SIZE["small"], "bold"), padx=10, pady=3,
        )
        self.connection_badge.pack(side="right")
        self.pro_release_badge = tk.Label(
            heading, text="V4-Pro 正式版", bg="#E8F5EF", fg=COLORS["accent"],
            font=(FONT, FONT_SIZE["small"], "bold"), padx=10, pady=3,
        )
        self.pro_release_badge.pack(side="right", padx=(0, 8))
        tk.Label(
            settings,
            text="粘贴API Key后，点击下方绿色按钮。软件会先在本机加密保存，再立即测试连接；Key不会显示在任务日志中。",
            justify="left", anchor="w", wraplength=1100, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]),
        ).pack(fill="x", padx=16, pady=(0, 7))
        self.pro_release_note = tk.Label(
            settings, text="", justify="left", anchor="w", wraplength=1350,
            bg="#F1F8F5", fg=COLORS["accent"], font=(FONT, FONT_SIZE["hint"]),
            padx=9, pady=5,
        )
        self.pro_release_note.pack(fill="x", padx=16, pady=(0, 8))
        form = tk.Frame(settings, bg=COLORS["panel"])
        form.pack(fill="x", padx=16, pady=(0, 8))
        self.base_var = tk.StringVar()
        self.flash_var = tk.StringVar()
        self.pro_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.api_test_budget_var = tk.StringVar()
        self.api_pro_test_budget_var = tk.StringVar()
        self.api_full_budget_var = tk.StringVar()
        self.api_action_budget_var = tk.StringVar()
        self.api_timeout_var = tk.StringVar()
        self.key_entry = None
        for index, (label, variable, show) in enumerate([
            ("API地址", self.base_var, None),
            ("API Key（仅本机加密保存）", self.key_var, "•"),
            ("Flash模型", self.flash_var, None),
            ("Pro正式版模型", self.pro_var, None),
        ]):
            row, col = divmod(index, 2)
            box = tk.Frame(form, bg=COLORS["panel"])
            box.grid(row=row, column=col, sticky="ew", padx=(0, 12 if col == 0 else 0), pady=(0, 8))
            tk.Label(box, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"], "bold")).pack(anchor="w")
            entry = ttk.Entry(box, textvariable=variable, show=show)
            entry.pack(fill="x", pady=(4, 0))
            if variable is self.key_var:
                self.key_entry = entry
            form.columnconfigure(col, weight=1)
        actions = tk.Frame(settings, bg=COLORS["panel"])
        actions.pack(fill="x", padx=16, pady=(0, 14))
        CuteButton(actions, "🔌 保存并连接 DeepSeek", self.save_and_connect, "success").pack(side="left")
        CuteButton(actions, "💾 仅保存设置", self.save_api_settings, "secondary").pack(side="left", padx=8)
        CuteButton(actions, "🧪 测试已保存连接", self.test_connection, "secondary").pack(side="left")
        CuteButton(actions, "🔐 清除密钥", self.clear_key, "secondary").pack(side="left")
        self.key_status = tk.Label(actions, text="", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]))
        self.key_status.pack(side="right")
        limits = tk.Frame(settings, bg=COLORS["panel"])
        limits.pack(fill="x", padx=16, pady=(0, 14))
        for index, (label, variable) in enumerate([
            ("Flash 10行业预算阈值（元）", self.api_test_budget_var),
            ("Pro结构测试预算阈值（元）", self.api_pro_test_budget_var),
            ("完整扫描预算阈值（元）", self.api_full_budget_var),
            ("AI今日行动预算阈值（元）", self.api_action_budget_var),
            ("单次网络无进展超时（秒）", self.api_timeout_var),
        ]):
            row, col = divmod(index, 3)
            box = tk.Frame(limits, bg=COLORS["panel"]); box.grid(row=row, column=col, sticky="ew", padx=(0, 10), pady=(0, 7))
            tk.Label(box, text=label, bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["micro"], "bold")).pack(anchor="w")
            ttk.Entry(box, textvariable=variable, width=16).pack(fill="x", pady=(3, 0))
            limits.columnconfigure(col, weight=1)

        scan = Card(self.body)
        scan.pack(fill="x", pady=(0, 12))
        bar = tk.Frame(scan, bg=COLORS["panel"])
        bar.pack(fill="x", padx=16, pady=12)
        tk.Label(bar, text="运行扫描｜按顺序从左到右", bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, FONT_SIZE["section"], "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            bar, text="行情计算 → Flash分批搜索 → 逐项校验 → Pro终审 → 基金候选",
            justify="left", anchor="w", bg=COLORS["panel"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]),
        ).grid(row=0, column=1, sticky="ew", padx=(12, 0))
        scan_buttons = tk.Frame(bar, bg=COLORS["panel"])
        scan_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        phases = [
            ("① 免费检查", "不调用DeepSeek", "#FAF5F2", "#D9C6BE", [
                ("🧪 无API模拟日扫池", lambda: self.start_scan("无API模拟"), "secondary"),
                ("📡 去做10项行情抽测", lambda: self.app.show("market"), "secondary"),
            ]),
            ("② 小额试跑", "先确认两种模型可用", "#F1F8F5", "#BDD9CF", [
                ("🔬 10方向API测试", lambda: self.start_scan("10方向测试"), "success"),
                ("🧠 Pro结构API测试", lambda: self.start_scan("Pro结构测试"), "success"),
            ]),
            ("③ 正式研究", "只扫描长期已有严格C类基金的指数；今天须先完成可申购复核", "#FFF3F6", "#E7BCC7", [
                ("🚀 完整扫描全部CSI行业", lambda: self.start_scan("完整扫描"), "primary"),
                ("▶ 续跑选中失败任务", self.resume_selected_job, "secondary"),
            ]),
            ("安全与诊断", "随时停止下一次请求", "#FFF8F3", "#E7D8D2", [
                ("⏹ 停止后续API", self.stop_scan, "danger"),
                ("📦 导出选中任务诊断", self.export_job_diagnostic, "secondary"),
            ]),
        ]
        for column, (title, note, bg, border, actions_for_phase) in enumerate(phases):
            phase = Card(scan_buttons, bg=bg, highlightbackground=border)
            phase.grid(row=0, column=column, sticky="nsew", padx=(0, 8 if column < len(phases)-1 else 0))
            tk.Label(phase, text=title, bg=bg, fg=COLORS["text"], font=(FONT, FONT_SIZE["body"], "bold")).pack(anchor="w", padx=10, pady=(8, 0))
            tk.Label(phase, text=note, bg=bg, fg=COLORS["muted"], font=(FONT, FONT_SIZE["hint"])).pack(anchor="w", padx=10, pady=(0, 6))
            for button_text, command, kind in actions_for_phase:
                CuteButton(phase, button_text, command, kind).pack(fill="x", padx=8, pady=(0, 7))
            scan_buttons.columnconfigure(column, weight=1, uniform="scan-phase")
        self.scan_scope_label = tk.Label(
            bar, text="", justify="left", anchor="w", bg=COLORS["panel"],
            fg=COLORS["info"], font=(FONT, FONT_SIZE["small"], "bold"),
        )
        self.scan_scope_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bar.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(self.body, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x")
        self.status = tk.Label(self.body, text="等待任务", anchor="w", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["body"]))
        self.status.pack(fill="x", pady=(6, 8))
        columns = ("time", "mode", "stage", "progress", "cost", "status")
        task_box = tk.Frame(self.body, bg=COLORS["bg"])
        task_box.pack(fill="x")
        self.tree = ttk.Treeview(task_box, columns=columns, show="headings", height=4)
        for key, label, width in [("time", "创建时间", 160), ("mode", "模式", 110), ("stage", "当前阶段", 150), ("progress", "完成", 90), ("cost", "估算费用", 90), ("status", "状态", 150)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center" if key not in {"stage", "status"} else "w")
        task_scroll = ttk.Scrollbar(task_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=task_scroll.set)
        self.tree.pack(side="left", fill="x", expand=True)
        task_scroll.pack(side="right", fill="y")
        self.tree.bind("<MouseWheel>", lambda e: self.tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.detail = tk.Text(self.body, height=3, wrap="word", bg="#FFFCFA", fg=COLORS["text"], relief="flat",
                              highlightbackground=COLORS["border"], highlightthickness=1, font=("Consolas", FONT_SIZE["small"]), padx=10, pady=8)
        self.detail.pack(fill="x", pady=(10, 0))
        self.tree.bind("<<TreeviewSelect>>", self.show_job_detail)
        self.refresh()

    def refresh(self):
        settings = self.db.settings()
        self.base_var.set(settings.get("deepseek_base_url", "https://api.deepseek.com"))
        self.flash_var.set(settings.get("deepseek_flash_model", "deepseek-v4-flash"))
        self.pro_var.set(settings.get("deepseek_pro_model", "deepseek-v4-pro"))
        official_pro = (
            self.pro_var.get().strip() == "deepseek-v4-pro"
            and settings.get("deepseek_pricing_revision", "").startswith("official-v4-")
        )
        self.pro_release_badge.config(
            text="V4-Pro 正式版" if official_pro else "自定义 Pro 配置",
            bg="#E8F5EF" if official_pro else "#FFF4D6",
            fg=COLORS["accent"] if official_pro else COLORS["warning"],
        )
        if official_pro:
            try:
                configured_cap = int(float(settings.get("api_pro_max_tokens", "12000")))
            except (TypeError, ValueError):
                configured_cap = 12000
            try:
                hard_cap = int(float(settings.get("api_pro_hard_output_cap_tokens", "12000")))
            except (TypeError, ValueError):
                hard_cap = 12000
            try:
                official_context = int(float(settings.get("deepseek_v4_context_tokens", "1000000")))
                official_output = int(float(settings.get("deepseek_v4_max_output_tokens", "384000")))
            except (TypeError, ValueError):
                official_context, official_output = 1000000, 384000
            local_cap = max(4000, min(12000, hard_cap, configured_cap))
            self.pro_release_note.config(
                text=(
                    "正式版计费口径：未缓存输入 ¥"
                    f"{settings.get('pro_input_price_cny_per_million', '3')} / 百万Token · "
                    f"缓存命中 ¥{settings.get('pro_cached_input_price_cny_per_million', '0.025')} / 百万Token · "
                    f"输出 ¥{settings.get('pro_output_price_cny_per_million', '6')} / 百万Token。"
                    f"官方能力为 {official_context // 10000} 万上下文、最高 {official_output / 10000:.1f} 万输出；"
                    f"本软件为控制费用和超长JSON风险，将完整扫描单次输出限制为 {local_cap:,} Token。"
                    "软件仍会在每次付费扫描前显示预算并要求你确认。"
                )
            )
        else:
            self.pro_release_note.config(
                text="当前不是软件已核验的 deepseek-v4-pro 正式版组合；保存前请确认模型ID和计费参数。"
            )
        self.api_test_budget_var.set(settings.get("api_test_cost_limit_cny", "0.30"))
        self.api_pro_test_budget_var.set(settings.get("api_pro_test_cost_limit_cny", "0.10"))
        self.api_full_budget_var.set(settings.get("api_full_cost_limit_cny", "2.00"))
        self.api_action_budget_var.set(settings.get("api_action_cost_limit_cny", "0.50"))
        self.api_timeout_var.set(settings.get("api_timeout_seconds", "360"))
        saved = bool(settings.get("deepseek_api_key_dpapi", ""))
        self.key_status.config(text="已保存本机加密密钥" if saved else "尚未保存密钥")
        if not saved:
            self.connection_badge.config(text="未连接", bg="#EEE3DE", fg=COLORS["muted"])
        catalog = PlatformCatalogService(self.db)
        current_count = len(catalog.radar_universe(refresh_admission=False))
        execution = catalog.execution_summary()
        self.scan_scope_label.config(
            text=(
                f"长期严格C研究目录：{current_count} 个指数｜"
                f"今日可执行覆盖：{int(execution['ready'])}/{int(execution['total'])}。"
                "长期收录不等于今日可下单；覆盖不足不会创建付费任务。"
            )
        )
        self.tree.delete(*self.tree.get_children())
        for job in self.db.rows("SELECT * FROM analysis_jobs ORDER BY id DESC LIMIT 30"):
            self.tree.insert("", "end", iid=str(job["id"]), values=(job["created_at"], job["mode"], job["stage"],
                f"{job['completed_items']}/{job['total_items']}", f"¥{float(job['estimated_cost_cny']):.4f}", job["status"]))

    def _validated_api_settings(self):
        base = DeepSeekClient.validate_base_url(self.base_var.get())
        flash, pro = DeepSeekClient.validate_models(self.flash_var.get(), self.pro_var.get())
        try:
            test_budget = float(self.api_test_budget_var.get())
            pro_test_budget = float(self.api_pro_test_budget_var.get())
            full_budget = float(self.api_full_budget_var.get())
            action_budget = float(self.api_action_budget_var.get())
            timeout = int(float(self.api_timeout_var.get()))
        except ValueError as exc:
            raise DeepSeekError("API预算与超时必须填写数字。") from exc
        if not 0.05 <= test_budget <= 10:
            raise DeepSeekError("10行业预算阈值必须在0.05—10元之间。")
        if not 0.01 <= pro_test_budget <= 5:
            raise DeepSeekError("Pro结构测试预算阈值必须在0.01—5元之间。")
        if not 0.10 <= full_budget <= 100:
            raise DeepSeekError("完整扫描预算阈值必须在0.10—100元之间。")
        if not 0.05 <= action_budget <= 5:
            raise DeepSeekError("AI今日行动预算阈值必须在0.05—5元之间。")
        if not 60 <= timeout <= 900:
            raise DeepSeekError("API超时必须在60—900秒之间。")
        return {
            "deepseek_base_url": base, "deepseek_flash_model": flash, "deepseek_pro_model": pro,
            "api_test_cost_limit_cny": f"{test_budget:.2f}",
            "api_pro_test_cost_limit_cny": f"{pro_test_budget:.2f}",
            "api_full_cost_limit_cny": f"{full_budget:.2f}",
            "api_action_cost_limit_cny": f"{action_budget:.2f}", "api_timeout_seconds": str(timeout),
        }

    def save_api_settings(self):
        try:
            values = self._validated_api_settings()
        except DeepSeekError as exc:
            messagebox.showerror("设置无效", str(exc), parent=self)
            return
        self.db.save_settings(values)
        try:
            if self.key_var.get().strip():
                save_api_key(self.db, self.key_var.get())
                self.key_var.set("")
        except SecretError as exc:
            messagebox.showerror("密钥保存失败", str(exc), parent=self)
            return
        self.refresh()
        messagebox.showinfo("已保存", "模型设置已保存；API Key使用Windows当前用户加密。", parent=self)

    def save_and_connect(self):
        try:
            values = self._validated_api_settings()
        except DeepSeekError as exc:
            messagebox.showerror("设置无效", str(exc), parent=self)
            return
        self.db.save_settings(values)
        try:
            if self.key_var.get().strip():
                save_api_key(self.db, self.key_var.get())
                self.key_var.set("")
            elif not self.db.setting("deepseek_api_key_dpapi", ""):
                raise SecretError("请先把DeepSeek API Key粘贴到输入框。")
        except (SecretError, DeepSeekError) as exc:
            messagebox.showerror("无法连接", str(exc), parent=self)
            return
        self.refresh()
        self.test_connection()

    def clear_key(self):
        clear_api_key(self.db)
        self.key_var.set("")
        self.refresh()
        self.connection_badge.config(text="未连接", bg="#EEE3DE", fg=COLORS["muted"])

    def _client(self):
        return DeepSeekClient(
            api_key_from_db(self.db),
            base_url=self.db.setting("deepseek_base_url", "https://api.deepseek.com"),
            timeout_seconds=int(float(self.db.setting("api_timeout_seconds", "120"))),
        )

    def test_connection(self):
        if self._connection_active:
            messagebox.showwarning("正在连接", "连接检查尚未结束，请不要重复点击。", parent=self)
            return
        self._connection_active = True
        self.status.config(text="正在连接DeepSeek…", fg=COLORS["info"])
        self.connection_badge.config(text="连接中", bg="#E8EEF6", fg=COLORS["info"])
        def worker():
            try:
                report = self._client().connection_report(
                    self.db.setting("deepseek_flash_model", "deepseek-v4-flash"),
                    self.db.setting("deepseek_pro_model", "deepseek-v4-pro"),
                )
                message = (
                    "连接成功。网络通道：" + (report.transport or "系统默认")
                    + "；模型：" + "、".join(report.models)
                    + "；可用余额：" + report.balance_text()
                )
                self.after(0, lambda: messagebox.showinfo("连接成功", message, parent=self))
                self.after(0, lambda: self.status.config(text=message, fg=COLORS["accent"]))
                self.after(0, lambda: self.connection_badge.config(text="已连接", bg="#DCEFE7", fg=COLORS["accent"]))
            except Exception as exc:
                error_text = str(exc)
                self.after(0, lambda message=error_text: messagebox.showerror("连接失败", message, parent=self))
                self.after(0, lambda message=error_text: self.status.config(text=f"连接失败：{message}", fg=COLORS["danger"]))
                self.after(0, lambda: self.connection_badge.config(text="连接失败", bg="#F7DFE1", fg=COLORS["danger"]))
            finally:
                self.after(0, lambda: setattr(self, "_connection_active", False))
        self.app.start_background(worker,label="DeepSeek连接检查")

    def start_scan(self, mode, task_id=None):
        if self._scan_active:
            messagebox.showwarning("已有任务运行中", "请等待当前任务结束，不要重复点击扫描按钮。", parent=self)
            return
        if mode in {"10方向测试", "20方向测试"} and task_id is None:
            limit = float(self.db.setting("api_test_cost_limit_cny", "0.30"))
            if not messagebox.askokcancel(
                "确认小额API测试",
                f"本测试会调用DeepSeek并产生费用，当前预算阈值为¥{limit:.2f}。\n"
                "即使DeepSeek返回空正文或任务失败，上游已经完成的调用仍可能收费。\n\n"
                "测试按每批3项运行；软件会根据上一批实耗决定是否发出下一批，但无法预知单个在途请求的最终费用。\n"
                "若搜索已完成但正文未通过严格JSON校验，软件会在预算内自动复用搜索结果，"
                "只做一次禁用搜索的JSON收尾；失败后不会循环调用。\n\n"
                "继续运行吗？",
                parent=self,
            ):
                return
        elif mode == "Pro结构测试" and task_id is None:
            limit = float(self.db.setting("api_pro_test_cost_limit_cny", "0.10"))
            if not messagebox.askokcancel(
                "确认Pro结构测试",
                f"本测试会对Pro发出一次极小的真实调用，验证Chat Completions、思考模式、JSON、结束原因与费用审计。\n"
                f"预算阈值为¥{limit:.2f}；不会生成或导入投资建议。继续吗？",
                parent=self,
            ):
                return
        elif mode == "完整扫描" and task_id is None:
            execution = PlatformCatalogService(self.db).execution_summary()
            if int(execution["ready"]) != int(execution["total"]):
                messagebox.showwarning(
                    "先免费刷新今日可申购",
                    f"严格C类研究目录共有 {execution['total']} 项，但今天只有 "
                    f"{execution['ready']} 项具备可执行基金证据。\n\n"
                    "为避免先支付Flash/Pro费用、最后又把基金代码清空，软件已在计费前停止。"
                    "请到“中证代表目录”点击“刷新今日可申购”，覆盖完整后再扫描。",
                    parent=self,
                )
                return
            try:
                cost_estimate = ResearchOrchestrator(self.db).estimate_full_scan_cost(
                    int(execution["total"])
                )
                cost_text = cost_estimate.confirmation_text()
            except Exception as exc:
                messagebox.showerror(
                    "无法核算扫描费用",
                    f"本地费用预估失败：{exc}\n\n软件尚未创建任务，也没有调用DeepSeek。",
                    parent=self,
                )
                return
            if not messagebox.askokcancel(
                "确认完整API扫描",
                f"完整扫描会依次调用Flash多批联网检索和Pro终审。\n\n{cost_text}\n\n"
                f"只有当前{execution['total']}个CSI叶子指数的行情、量能、严格C映射与当天可申购复核全部通过，才会发出首个计费请求。\n"
                "当日联网搜索内容、缓存命中和输出长度会使实际费用波动；若某批搜索已完成但JSON损坏，"
                "软件最多在预算内做一次无搜索收尾。单个在途请求仍可能使最终费用略高于阈值；"
                "软件不会自动重发失败调用。\n\n继续运行吗？",
                parent=self,
            ):
                return
        try:
            client = None if mode in {"无API模拟", "10项行情测试", "20项行情测试", "全目录行情更新"} else self._client()
            orchestrator = ResearchOrchestrator(self.db, client)
            task_id = task_id or orchestrator.create_job(mode)
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc), parent=self)
            return
        self._scan_active = True
        self._active_orchestrator = orchestrator
        self._scan_started_at = datetime.now()
        self._scan_status_base = f"{task_id} 已启动；正在准备行情数据"
        self.status.config(text=self._scan_status_base, fg=COLORS["info"])
        self.refresh()
        self._tick_scan_clock()
        def progress(stage, completed, total, message):
            self.after(0, lambda: self._progress_update(stage, completed, total, message))
        def worker():
            try:
                result = orchestrator.run(task_id, progress)
            except Exception as exc:
                self.after(0,lambda error=exc,failed_task=task_id:self._finish_scan_error(failed_task,error))
                return
            self.after(0, lambda completed_result=result: self._finish_scan(completed_result))
        self.app.start_background(worker,label=f"扫描任务 {task_id}",cancel=orchestrator.request_stop)

    def _progress_update(self, stage, completed, total, message):
        self.progress["maximum"] = max(1, total)
        self.progress["value"] = completed
        self._scan_status_base = f"{stage}：{completed}/{total}　{message}"
        self.status.config(text=self._scan_status_base, fg=COLORS["info"])
        # The progress widgets already contain everything the user needs while
        # a task runs.  Rebuilding the settings and 30-row task table for every
        # item flooded Tk's event queue and contended with the worker's writes.
        # The table is refreshed once when the task finishes.

    def _tick_scan_clock(self):
        if not self._scan_active or self._scan_started_at is None:
            return
        elapsed = max(0, int((datetime.now() - self._scan_started_at).total_seconds()))
        minutes, seconds = divmod(elapsed, 60)
        self.status.config(text=f"{self._scan_status_base}　｜已运行{minutes:02d}:{seconds:02d}", fg=COLORS["info"])
        self._scan_clock_after_id=self.after(1000, self._tick_scan_clock)

    def _stop_scan_clock(self):
        if self._scan_clock_after_id is not None:
            try:self.after_cancel(self._scan_clock_after_id)
            except tk.TclError:pass
            self._scan_clock_after_id=None

    def _finish_scan(self, result):
        self._scan_active = False
        self._active_orchestrator = None
        self._scan_started_at = None
        self._stop_scan_clock()
        self.refresh()
        self.status.config(text=result.message, fg=COLORS["accent"] if "完成" in result.status else COLORS["danger"])
        self.app.refresh_all()
        show_nonmodal_task_result(
            self, "任务结果" if result.status != "失败" else "任务失败", result.message,
        )

    def _finish_scan_error(self,task_id,error):
        self._scan_active=False;self._active_orchestrator=None;self._scan_started_at=None;self._stop_scan_clock()
        message=f"扫描线程异常退出：{error}"
        job=self.db.row("SELECT status FROM analysis_jobs WHERE task_id=?",(task_id,))
        if job and str(job.get("status") or "") in {"等待","运行中"}:
            self.db.execute(
                "UPDATE analysis_jobs SET status='失败',error=?,updated_at=? WHERE task_id=?",
                (message,local_now().isoformat(timespec="seconds"),task_id),
            )
        self.refresh();self.status.config(text=message,fg=COLORS["danger"]);self.app.refresh_all()
        show_nonmodal_task_result(self,"任务异常",message)

    def stop_scan(self):
        if not self._scan_active or not self._active_orchestrator:
            messagebox.showinfo("没有运行中任务", "当前没有可停止的扫描任务。", parent=self)
            return
        self._active_orchestrator.request_stop()
        self._scan_status_base = "已请求停止：不会再发送下一次API；当前在途请求可能仍需等待返回并可能计费"
        self.status.config(text=self._scan_status_base, fg=COLORS["warning"])

    def resume_selected_job(self):
        if self._scan_active:
            messagebox.showwarning("已有任务运行中", "请先等待或停止当前任务。", parent=self)
            return
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("请选择任务", "请先在任务表中选中失败或已停止的API任务。", parent=self)
            return
        job = self.db.row("SELECT * FROM analysis_jobs WHERE id=?", (selected[0],))
        if not job or job["mode"] not in {"10方向测试", "20方向测试", "完整扫描"} or job["status"] not in {"失败", "已停止"}:
            messagebox.showwarning("不能续跑", "只有失败或已停止的10行业测试/完整扫描可以续跑。", parent=self)
            return
        try:
            resume_plan = ResearchOrchestrator(self.db).flash_resume_plan(job["task_id"])
        except (DeepSeekError, ValueError) as exc:
            messagebox.showwarning("不能安全续跑", str(exc), parent=self)
            return
        if not resume_plan["same_research_day"]:
            messagebox.showwarning(
                "不能跨日续跑",
                "该任务不是今天的研究任务。为避免混用旧新闻和新行情，请新建今天的扫描任务。",
                parent=self,
            )
            return
        if resume_plan["blocked_items"]:
            reasons = "；".join(resume_plan["blocked_reasons"]) or "存在不可自动重发的批次"
            messagebox.showwarning(
                "发现禁止自动续跑的批次",
                f"共有{resume_plan['blocked_items']}项被安全门槛阻止：{reasons}。"
                "请先导出任务诊断，不会发送任何新请求。",
                parent=self,
            )
            return
        setting_key = "api_full_cost_limit_cny" if job["mode"] == "完整扫描" else "api_test_cost_limit_cny"
        try:
            configured_limit = float(self.db.setting(setting_key, str(job["cost_limit_cny"])))
        except (TypeError, ValueError):
            configured_limit = float(job["cost_limit_cny"])
        if configured_limit > float(job["cost_limit_cny"]):
            self.db.execute(
                "UPDATE analysis_jobs SET cost_limit_cny=?,updated_at=? WHERE id=?",
                (configured_limit, local_now().isoformat(timespec="seconds"), job["id"]),
            )
            job = self.db.row("SELECT * FROM analysis_jobs WHERE id=?", (selected[0],))
        plan_lines = [
            f"已安全入库：{resume_plan['already_saved_items']}/{resume_plan['total_items']}项。",
        ]
        if resume_plan["local_repair_items"]:
            plan_lines.append(
                f"本机直接恢复：{resume_plan['local_repair_items']}项 / "
                f"{resume_plan['local_repair_batches']}批（0次API，不重复扣费）。"
            )
        if resume_plan["finalize_only_items"]:
            plan_lines.append(
                f"复用已有搜索、仅做一次收尾：{resume_plan['finalize_only_items']}项 / "
                f"{resume_plan['finalize_only_batches']}批（会产生收尾费用，不重新搜索）。"
            )
        if resume_plan["new_search_items"]:
            plan_lines.append(
                f"仍需新联网研究：{resume_plan['new_search_items']}项 / "
                f"{resume_plan['new_search_batches']}批（会产生新费用）。"
            )
        if resume_plan["pro_may_run"]:
            plan_lines.append("全部Flash完成后仍可能发送1次V4-Pro终审。")
        plan_text = "\n".join(plan_lines)
        if not messagebox.askokcancel(
            "确认续跑",
            f"将续跑任务{job['task_id']}。\n\n{plan_text}\n\n"
            f"任务费用上限¥{float(job['cost_limit_cny']):.2f}，"
            f"已核算或预留¥{float(job['estimated_cost_cny']):.4f}。"
            "发送前会先预留本次费用上限，失败也不会自动释放或循环重试。继续吗？",
            parent=self,
        ):
            return
        self.start_scan(job["mode"], task_id=job["task_id"])

    @staticmethod
    def _response_structure(raw_text: str) -> dict[str, Any]:
        try:
            raw = json.loads(raw_text)
        except (TypeError, json.JSONDecodeError):
            return {"parseable": False}
        if not isinstance(raw, dict):
            return {"parseable": True, "top_type": type(raw).__name__}
        output = raw.get("output")
        output_items = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                content_types = []
                text_lengths = []
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            content_types.append(str(part.get("type", "?")))
                            part_text = part.get("text", "")
                            if isinstance(part_text, dict):
                                part_text = part_text.get("value", "")
                            text_lengths.append(len(str(part_text or "")))
                output_items.append({
                    "type": str(item.get("type", "?")),
                    "status": str(item.get("status", "")),
                    "content_types": content_types,
                    "text_lengths": text_lengths,
                })
        choices = raw.get("choices")
        choice_items = []
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                choice_items.append({
                    "finish_reason": str(choice.get("finish_reason", "")),
                    "content_length": len(str(message.get("content") or "")),
                    "has_reasoning_content": bool(message.get("reasoning_content")),
                    "tool_call_count": len(message.get("tool_calls") or []) if isinstance(message.get("tool_calls"), list) else 0,
                })
        return {
            "parseable": True,
            "top_keys": sorted(raw.keys()),
            "id": str(raw.get("id", "")),
            "status": str(raw.get("status", "")),
            "model": str(raw.get("model", "")),
            "error": raw.get("error"),
            "incomplete_details": raw.get("incomplete_details"),
            "output_items": output_items,
            "choices": choice_items,
            "usage": raw.get("usage", {}),
        }

    def export_job_diagnostic(self):
        selected = self.tree.selection()
        if selected:
            job = self.db.row("SELECT * FROM analysis_jobs WHERE id=?", (selected[0],))
        else:
            job = self.db.row("SELECT * FROM analysis_jobs ORDER BY id DESC LIMIT 1")
        if not job:
            messagebox.showwarning("没有任务", "尚无可导出的任务记录。", parent=self)
            return
        events = self.db.rows("SELECT * FROM analysis_events WHERE task_id=? ORDER BY id", (job["task_id"],))
        calls = self.db.rows("SELECT * FROM model_calls WHERE task_id=? ORDER BY id", (job["task_id"],))
        market_attempts = self.db.rows(
            "SELECT universe_id,security_id,provider,outcome,detail,created_at "
            "FROM market_fetch_attempts WHERE task_id=? ORDER BY id",
            (job["task_id"],),
        )
        safe_calls = []
        for call in calls:
            try:
                context = json.loads(call.get("request_context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                context = {}
            safe_context = {
                key: context[key] for key in ("phase", "batch_ids", "parent_response_id") if key in context
            } if isinstance(context, dict) else {}
            safe_calls.append({
                key: call[key] for key in (
                    "stage", "model", "input_tokens", "output_tokens", "cached_tokens",
                    "reasoning_tokens", "web_search_calls", "estimated_cost_cny", "response_kind",
                    "response_status", "finish_reason", "response_id", "created_at"
                )
            } | {"request_context": safe_context, "response_structure": self._response_structure(call["response_json"])})
        payload = {
            "software": f"{APP_NAME} {APP_VERSION}",
            "exported_at": local_now().isoformat(timespec="seconds"),
            "note": "脱敏结构诊断：不含API Key，也不含模型正文与搜索正文；包含公开行情网络路径与退出原因。",
            "job": job,
            "events": events,
            "market_fetch_attempts": market_attempts,
            "model_calls": safe_calls,
        }
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        default_name = f"API任务诊断_{job['task_id']}.json"
        path = filedialog.asksaveasfilename(
            parent=self, title="导出脱敏任务诊断", initialdir=str(EXPORT_DIR), initialfile=default_name,
            defaultextension=".json", filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("导出完成", f"已保存脱敏诊断：\n{path}", parent=self)

    def show_job_detail(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        job = self.db.row("SELECT * FROM analysis_jobs WHERE id=?", (selected[0],))
        events = self.db.rows("SELECT * FROM analysis_events WHERE task_id=? ORDER BY id", (job["task_id"],)) if job else []
        calls = self.db.rows("SELECT * FROM model_calls WHERE task_id=? ORDER BY id", (job["task_id"],)) if job else []
        market_attempts = self.db.rows(
            "SELECT * FROM market_fetch_attempts WHERE task_id=? ORDER BY id", (job["task_id"],)
        ) if job else []
        lines = [f"{e['created_at']} [{e['level']}] {e['stage']}：{e['message']}" for e in events]
        lines += [
            f"MARKET {m['universe_id']}｜{m['security_id']}｜{m['provider']}｜{m['outcome']}｜{m['detail']}"
            for m in market_attempts
        ]
        lines += [f"MODEL {c['model']}｜{c['stage']}｜in {c['input_tokens']}｜out {c['output_tokens']}｜search {c['web_search_calls']}｜¥{float(c['estimated_cost_cny']):.4f}" for c in calls]
        if job and job["error"]:
            lines.append("ERROR: " + job["error"])
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines) or "暂无事件。")


class MarketDataPage(BasePage):
    title = "📡 行情与代理映射"
    subtitle = "这里只核对当前CSI叶子指数的行情；研究ID、中证.CSI代码（六位数字或H加五位数字）和行情证券ID始终分栏保存。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._resolve_active=False
        self._resolve_stop=threading.Event()
        top = tk.Frame(self.body, bg=COLORS["bg"])
        top.pack(fill="x", pady=(0, 8))
        self.coverage_label = tk.Label(
            top, text="", bg=COLORS["bg"], fg=COLORS["muted"],
            font=(FONT, FONT_SIZE["body"], "bold"), anchor="w", justify="left",
        )
        self.coverage_label.pack(fill="x")
        actions = tk.Frame(top, bg=COLORS["bg"])
        actions.pack(fill="x", pady=(7, 0))
        CuteButton(actions, "✏️ 编辑选中映射", self.edit, "secondary").pack(side="left")
        CuteButton(actions, "📋 导出映射CSV", self.export_csv, "secondary").pack(side="left", padx=8)
        self.resolve_button=CuteButton(actions,"🔍 核验目录中的行情映射",self.resolve_all,"primary")
        self.resolve_button.pack(side="left")
        self.resolve_stop_button=CuteButton(actions,"⏹ 停止映射核验",self.stop_resolve,"danger")
        self.resolve_stop_button.pack(side="left",padx=8);self.resolve_stop_button.set_enabled(False)
        CuteButton(actions, "🧪 抽测10项行情（零API）", self.test_market, "secondary").pack(side="left")
        CuteButton(actions, "📡 更新全部行情（零API）", self.update_all_market, "success").pack(side="left", padx=8)

        columns = ("id", "category", "target", "proxy", "provider", "kind", "confidence", "status")
        self.tree = ttk.Treeview(self.body, columns=columns, show="headings", height=18)
        specs = [
            ("id", "ID", 55), ("category", "分类", 95), ("target", "研究方向", 145),
            ("proxy", "行情代理", 205), ("provider", "数据源", 135), ("kind", "映射类型", 90),
            ("confidence", "置信度", 70), ("status", "状态", 130),
        ]
        for key, label, width in specs:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w" if key in {"target", "proxy", "provider"} else "center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Double-1>", lambda _event: self.edit())
        self.detail = tk.Text(
            self.body, height=6, wrap="word", bg="#FFFCFA", fg=COLORS["text"], relief="flat",
            highlightbackground=COLORS["border"], highlightthickness=1, font=(FONT, FONT_SIZE["small"]), padx=10, pady=8,
        )
        self.detail.pack(fill="x", pady=(10, 0))
        self.resolve_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.resolve_progress.pack(fill="x",before=self.tree,pady=(0,7))

    def refresh(self):
        current_radar = PlatformCatalogService(self.db).radar_universe(refresh_admission=False)
        ids = [row["index_code"] for row in current_radar]
        coverage = MarketDataPipeline(self.db).coverage(ids)
        admitted = len(current_radar)
        self.coverage_label.config(
            text=(f"CSI代表目录：{coverage['total']}项｜日扫准入{admitted}项｜"
                  f"已映射{coverage['mapped']}项（精确{coverage['exact']}）　"
                  "旧186项与历史平台方向不显示、不计数"),
            fg=COLORS["muted"],
        )
        self.tree.delete(*self.tree.get_children())
        self.row_map = {}
        rows = self.db.rows(
            """SELECT u.index_code,u.index_name,u.category,p.provider,p.security_id,p.proxy_code,p.proxy_name,
                       p.mapping_kind,p.confidence,p.rationale,p.active,p.reviewed_at,
                       i.radar_enabled,i.admission_status,i.quote_history_status
               FROM investable_indices i JOIN universe u ON u.index_code=i.universe_id
               LEFT JOIN proxy_mappings p ON p.universe_id=u.index_code
               WHERE i.active=1 AND i.radar_enabled=1
               ORDER BY i.industry_group,i.display_order,i.id"""
        )
        for row in rows:
            uid = row["index_code"]
            self.row_map[uid] = row
            configured = bool(row.get("security_id") and int(row.get("active") or 0))
            mapped = configured and float(row.get("confidence") or 0) >= 0.75 and row.get("mapping_kind") != "待核验"
            proxy = f"{row.get('proxy_code') or ''} {row.get('proxy_name') or ''}".strip() or "—"
            radar_enabled = int(row.get("radar_enabled") or 0)
            self.tree.insert("", "end", iid=uid, values=(
                uid, row["category"], row["index_name"], proxy, row.get("provider") or "—",
                row.get("mapping_kind") or "—", f"{float(row.get('confidence') or 0):.2f}" if mapped else "—",
                "日扫准入" if radar_enabled else "研究候选",
            ), tags=("mapped" if mapped and radar_enabled else "candidate" if not radar_enabled else "missing",))
        self.tree.tag_configure("mapped", foreground=COLORS["text"])
        self.tree.tag_configure("candidate", foreground=COLORS["muted"])
        self.tree.tag_configure("missing", foreground=COLORS["danger"])

    def selected(self):
        selected = self.tree.selection()
        return self.row_map.get(selected[0]) if selected else None

    def show_detail(self, _event=None):
        row = self.selected()
        if not row:
            return
        text = (
            f"研究方向：{row['index_code']} {row['index_name']}\n"
            f"源内证券ID：{row.get('security_id') or '未设置'}\n"
            f"映射理由：{row.get('rationale') or '未设置'}\n"
            f"复核时间：{row.get('reviewed_at') or '未复核'}\n"
            f"日扫状态：{row.get('admission_status') or '待检查'}；"
            f"{row.get('quote_history_status') or '行情历史未核验'}"
        )
        fallback = CURATED_ETF_FALLBACKS.get(str(row.get("security_id") or ""))
        if fallback:
            text += (
                f"\n审核备用路线：{fallback.security_id} / {fallback.proxy_code} {fallback.proxy_name}"
                f"（{fallback.mapping_kind}，置信度{fallback.confidence:.2f}）"
                f"\n备用说明：{fallback.rationale}"
            )
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)

    def edit(self):
        row = self.selected()
        if not row:
            messagebox.showinfo("请先选择", "请先选择一个研究方向。", parent=self)
            return
        fields = [
            ("数据源", "provider", row.get("provider") or "eastmoney_public"),
            ("源内证券ID", "security_id", row.get("security_id") or ""),
            ("代理代码", "proxy_code", row.get("proxy_code") or ""),
            ("代理名称", "proxy_name", row.get("proxy_name") or ""),
            ("映射类型", "mapping_kind", row.get("mapping_kind") or "待核验"),
            ("置信度 0-1", "confidence", row.get("confidence") or 0),
            ("映射理由", "rationale", row.get("rationale") or ""),
            ("启用 1/0", "active", row.get("active") if row.get("active") is not None else 1),
        ]
        dialog = FormDialog(self, f"✏️ {row['index_code']} {row['index_name']}", fields,
                            choices={"mapping_kind": ["精确指数", "精确板块", "显式代理", "ETF代理", "待核验"]})
        if not dialog.result:
            return
        values = dialog.result
        try:
            confidence = float(values["confidence"])
            active = int(values["active"])
            if not 0 <= confidence <= 1 or active not in {0, 1}:
                raise ValueError
        except ValueError:
            messagebox.showerror("格式错误", "置信度必须在0到1之间；启用只能填1或0。", parent=self)
            return
        required = [values[key].strip() for key in ("provider", "security_id", "proxy_code", "proxy_name", "rationale")]
        if active and not all(required):
            messagebox.showerror("信息不完整", "启用映射时，数据源、证券ID、代理代码、名称和理由都必须填写。", parent=self)
            return
        self.db.execute(
            """INSERT INTO proxy_mappings(universe_id,provider,security_id,proxy_code,proxy_name,mapping_kind,
                   confidence,rationale,active,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(universe_id) DO UPDATE SET provider=excluded.provider,security_id=excluded.security_id,
                   proxy_code=excluded.proxy_code,proxy_name=excluded.proxy_name,mapping_kind=excluded.mapping_kind,
                   confidence=excluded.confidence,rationale=excluded.rationale,active=excluded.active,
                   reviewed_at=excluded.reviewed_at""",
            (row["index_code"], *required[:4], values["mapping_kind"], confidence, required[4], active,
             local_now().isoformat(timespec="seconds")),
        )
        self.refresh()

    def test_market(self):
        self.app.show("api")
        self.app.pages["api"].start_scan("10项行情测试")

    def update_all_market(self):
        count = len(PlatformCatalogService(self.db).radar_universe(refresh_admission=False))
        if not count:
            messagebox.showwarning("当前目录为空", "当前没有已准入的CSI指数，无法更新行情。", parent=self)
            return
        if not messagebox.askokcancel(
            "更新全部行情",
            f"将后台联网更新当前完整目录的 {count} 个指数，并校验交易日与数据来源。\n\n"
            "本任务不会创建DeepSeek客户端，不会消耗Token或API余额；运行时界面仍可操作，也可点击停止。继续吗？",
            parent=self,
        ):
            return
        self.app.show("api")
        self.app.pages["api"].start_scan("全目录行情更新")

    def resolve_all(self):
        if self._resolve_active:
            messagebox.showinfo("正在核验","目录行情映射仍在后台核验，可点击停止。",parent=self);return
        if not messagebox.askyesno(
            "核验CSI代表行情",
            f"软件只访问当前{len(PlatformCatalogService(self.db).radar_universe(refresh_admission=False))}个CSI叶子指数，逐项核对真实历史行情。\n\n"
            "只接受名称完全一致、唯一规范化一致或软件内已披露的透明代理；不会调用DeepSeek，也不会产生API费用。继续吗？",
            parent=self,
        ):
            return
        self._resolve_active=True;self._resolve_stop.clear()
        self.resolve_button.set_enabled(False);self.resolve_stop_button.set_enabled(True)
        self.resolve_progress["value"]=0
        self.coverage_label.config(text="正在核验当前全部CSI叶子指数的行情……",fg=COLORS["info"])
        def progress(done,total,message):
            self.after(0,lambda d=done,t=total,value=message:self._update_resolve_progress(d,t,value))
        def worker():
            try:
                universe=PlatformCatalogService(self.db).radar_universe()
                result=SectorMappingResolver(self.db).refresh(
                    universe=universe,progress=progress,stop_requested=self._resolve_stop.is_set,
                )
            except Exception as exc:
                self.after(0,lambda error=exc:self._finish_resolve(None,error))
                return
            self.after(0,lambda:self._finish_resolve(result,None))
        self.app.start_background(worker,label="CSI行情映射核验",cancel=self._resolve_stop.set)

    def _update_resolve_progress(self,done,total,message):
        self.resolve_progress["maximum"]=max(1,total);self.resolve_progress["value"]=done
        self.coverage_label.config(text=f"映射核验 {done}/{total}　{message}",fg=COLORS["info"])

    def stop_resolve(self):
        if not self._resolve_active:return
        self._resolve_stop.set();self.resolve_stop_button.set_enabled(False)
        self.coverage_label.config(text="已请求停止；当前网络请求结束后不再核验下一项。",fg=COLORS["warning"])

    def _finish_resolve(self,result,error):
        self._resolve_active=False
        self.resolve_button.set_enabled(True);self.resolve_stop_button.set_enabled(False)
        if error:
            self.refresh();messagebox.showerror("映射核验失败",str(error),parent=self);return
        self.refresh()
        coverage=MarketDataPipeline(self.db).coverage(
            [row["index_code"] for row in PlatformCatalogService(self.db).radar_universe(refresh_admission=False)]
        )
        text=(f"当前目录可信覆盖：{coverage['mapped']}/{coverage['total']}\n"
              f"本次新增或更新：{result['resolved']}\n复用已核验：{result['skipped']}")
        if result.get("stopped"):
            text="已按你的要求停止；已完成的映射均已安全保存。\n"+text
        if result.get("stopped"):
            messagebox.showinfo("映射核验已停止",text,parent=self)
        elif result["errors"] or result["unresolved"]:
            text += "\n\n仍未通过：\n" + "\n".join((result["unresolved"]+result["errors"])[:12])
            messagebox.showwarning("映射部分完成",text,parent=self)
        else:
            messagebox.showinfo("目录映射完成",text+"\n\n日扫准入要求CSI官方身份、精确行情以及至少20个交易日历史；基金可执行状态另行显示。",parent=self)

    def export_csv(self):
        rows = self.db.rows(
            """SELECT u.index_code,u.index_name,u.category,p.provider,p.security_id,p.proxy_code,p.proxy_name,
                      p.mapping_kind,p.confidence,p.rationale,p.active,p.reviewed_at
               FROM investable_indices i JOIN universe u ON u.index_code=i.universe_id
               LEFT JOIN proxy_mappings p ON p.universe_id=u.index_code
               WHERE i.active=1 AND i.radar_enabled=1
               ORDER BY i.industry_group,i.display_order,i.id"""
        )
        path = EXPORT_DIR / f"板块雷达_代理映射_{local_now().strftime('%Y%m%d_%H%M')}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        messagebox.showinfo("导出完成", str(path), parent=self)


class AIHubPage(BasePage):
    title = "🔄 备用AI中转站"
    subtitle = "DeepSeek服务、余额或行情模块不可用时，仍可导出自包含任务包到网页版研究。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        steps = Card(self.body)
        steps.pack(fill="x", pady=(0, 12))
        tk.Label(steps, text="备用流程：1 生成任务包  →  2 粘贴到网页版AI并联网研究  →  3 复制完整V2结果回来校验入库",
                 bg=COLORS["panel"], fg=COLORS["text"], font=(FONT, FONT_SIZE["section"], "bold"), pady=15).pack()
        buttons = tk.Frame(self.body, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(0, 10))
        for text, task, kind in [
            ("🌙 夜间全市场研究", "night", "primary"), ("⏰ 14:45盘中决策", "intraday", "success"),
            ("🔍 基金核查", "fund_check", "secondary"), ("🛠️ 结果修复", "repair", "secondary")]:
            CuteButton(buttons, text, lambda t=task: self.app.generate_task(t), kind).pack(side="left", padx=(0, 8))
        CuteButton(buttons, "📂 打开任务文件夹", lambda: open_path(TASK_DIR), "secondary").pack(side="right")

        tk.Label(self.body, text="把ChatGPT的完整回复粘贴到下面（软件会自动寻找JSON结果块）：", bg=COLORS["bg"], fg=COLORS["text"], font=(FONT, FONT_SIZE["body"], "bold")).pack(anchor="w")
        self.text = tk.Text(self.body, wrap="none", bg="#FFFCFA", fg=COLORS["text"], insertbackground=COLORS["text"],
                            relief="flat", highlightbackground=COLORS["border"], highlightthickness=1, font=("Consolas", FONT_SIZE["body"]), padx=10, pady=10)
        self.text.pack(fill="both", expand=True, pady=(6, 8))
        bottom = tk.Frame(self.body, bg=COLORS["bg"])
        bottom.pack(fill="x")
        CuteButton(bottom, "✅ 校验并导入", self.validate_import).pack(side="left")
        CuteButton(bottom, "📋 从剪贴板粘贴", self.paste, "secondary").pack(side="left", padx=8)
        CuteButton(bottom, "🚫 清空", lambda: self.text.delete("1.0", "end"), "secondary").pack(side="left")
        self.status = tk.Label(bottom, text="等待导入", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["body"]))
        self.status.pack(side="right")

    def paste(self):
        try:
            content = self.clipboard_get()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "没有读取到文本。", parent=self)

    def validate_import(self):
        text = self.text.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("还没有内容", "请先粘贴ChatGPT的完整回复。", parent=self)
            return
        known = {r["fund_code"] for r in self.db.rows("SELECT fund_code FROM funds")}
        expected = PlatformCatalogService(self.db).radar_universe()
        result = parse_result(
            text,
            known,
            float(self.db.setting("active_capital_limit", "8000")),
            expected,
        )
        if not result.ok:
            log_failed_import(self.db, text, result.errors)
            repair = repair_prompt(result.errors, text)
            self.app.clipboard_clear()
            self.app.clipboard_append(repair)
            self.status.config(text="未通过；修复提示已复制", fg=COLORS["danger"])
            messagebox.showerror("结果未通过校验", "\n".join("• " + e for e in result.errors) + "\n\n修复提示词已复制到剪贴板。", parent=self)
            return
        import_radar_payload(self.db, result.payload, text, result.warnings)
        warning_text = "\n".join("• " + w for w in result.warnings)
        self.status.config(text=f"已导入 {result.payload['task_id']}", fg=COLORS["accent"])
        messagebox.showinfo("导入成功", f"已导入{len(result.payload['candidates'])}个候选。" + (f"\n\n待核验提醒：\n{warning_text}" if warning_text else ""), parent=self)
        self.app.refresh_all()


class RadarPage(BasePage):
    title = "🌙 夜间雷达"
    subtitle = "查看每轮研究的市场状态、覆盖率、候选逻辑、拥挤度和放弃条件。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        top = tk.Frame(self.body, bg=COLORS["bg"])
        top.pack(fill="x", pady=(0, 8))
        self.run_var = tk.StringVar()
        self.run_box = ttk.Combobox(top, textvariable=self.run_var, state="readonly", width=38)
        self.run_box.pack(side="left")
        self.run_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_candidates())
        CuteButton(top, "🚀 去一键AI扫描", lambda: self.app.show("api"), "primary").pack(side="left", padx=8)
        CuteButton(top, "📋 生成网页版备用任务包", lambda: self.app.generate_task("night"), "secondary").pack(side="left")
        CuteButton(top, "📊 导出候选CSV", self.export_csv, "secondary").pack(side="right")
        self.summary = tk.Label(self.body, text="", anchor="w", justify="left", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, FONT_SIZE["body"]))
        self.summary.pack(fill="x", pady=(0, 8))
        self.tabs = ttk.Notebook(self.body); self.tabs.pack(fill="both", expand=True)
        main_tab=tk.Frame(self.tabs,bg=COLORS["panel"]); audit_tab=tk.Frame(self.tabs,bg=COLORS["panel"])
        alt_tab=tk.Frame(self.tabs,bg=COLORS["panel"]); avoid_tab=tk.Frame(self.tabs,bg=COLORS["panel"])
        self.tabs.add(main_tab,text="① 最终主榜（最多10项）")
        self.tabs.add(audit_tab,text="② 全目录七档结论")
        self.tabs.add(alt_tab,text="③ 同类备选/候选池")
        self.tabs.add(avoid_tab,text="④ Pro终审回避（独立）")

        columns = ("rank","sector","index","score","tilt","timing","execution","fund","allocation","amount")
        self.tree = ttk.Treeview(main_tab, columns=columns, show="headings", height=12)
        for key,label,width in [
            ("rank","排名",45),("sector","板块",125),("index","CSI指数",90),("score","推荐分",65),
            ("tilt","七档研究结论",100),("timing","当前择时",85),("execution","基金执行状态",105),
            ("fund","严格C类基金",180),("allocation","目标占比",75),("amount","当前可新增",95),
        ]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,anchor="w" if key in {"sector","tilt","execution","fund"} else "center")
        main_y=ttk.Scrollbar(main_tab,orient="vertical",command=self.tree.yview)
        main_x=ttk.Scrollbar(main_tab,orient="horizontal",command=self.tree.xview)
        self.tree.configure(yscrollcommand=main_y.set,xscrollcommand=main_x.set)
        main_y.pack(side="right",fill="y");main_x.pack(side="bottom",fill="x")
        self.tree.pack(side="left",fill="both",expand=True)

        audit_cols=("rank","sector","index","score","tilt","gate","trend","entry","crowding","confidence","timing","execution")
        self.audit_tree=ttk.Treeview(audit_tab,columns=audit_cols,show="headings",height=12)
        for key,label,width in [
            ("rank","全目录排名",70),("sector","板块",130),("index","CSI指数",90),("score","推荐分",65),
            ("tilt","七档结论",95),("gate","安全门槛",75),("trend","趋势机会",70),("entry","入场性价比",80),
            ("crowding","拥挤风险",70),("confidence","数据置信度",80),("timing","择时",85),("execution","执行",100),
        ]:
            self.audit_tree.heading(key,text=label);self.audit_tree.column(key,width=width,anchor="w" if key in {"sector","tilt","execution"} else "center")
        audit_y=ttk.Scrollbar(audit_tab,orient="vertical",command=self.audit_tree.yview)
        audit_x=ttk.Scrollbar(audit_tab,orient="horizontal",command=self.audit_tree.xview)
        self.audit_tree.configure(yscrollcommand=audit_y.set,xscrollcommand=audit_x.set)
        audit_y.pack(side="right",fill="y");audit_x.pack(side="bottom",fill="x")
        self.audit_tree.pack(side="left",fill="both",expand=True)

        alt_cols=("sector","index","bucket","route","score","tilt","reason")
        self.alt_tree=ttk.Treeview(alt_tab,columns=alt_cols,show="headings",height=12)
        for key,label,width in [
            ("sector","板块",140),("index","CSI指数",95),("bucket","相关性桶",130),
            ("route","进入候选池路线",145),("score","推荐分",70),("tilt","七档结论",100),("reason","未进主榜原因",260),
        ]:
            self.alt_tree.heading(key,text=label);self.alt_tree.column(key,width=width,anchor="w" if key not in {"score"} else "center")
        alt_y=ttk.Scrollbar(alt_tab,orient="vertical",command=self.alt_tree.yview)
        alt_x=ttk.Scrollbar(alt_tab,orient="horizontal",command=self.alt_tree.xview)
        self.alt_tree.configure(yscrollcommand=alt_y.set,xscrollcommand=alt_x.set)
        alt_y.pack(side="right",fill="y");alt_x.pack(side="bottom",fill="x")
        self.alt_tree.pack(side="left",fill="both",expand=True)

        avoid_cols=("risk","sector","index","reason")
        self.avoid_tree=ttk.Treeview(avoid_tab,columns=avoid_cols,show="headings",height=12)
        for key,label,width in [("risk","风险",60),("sector","板块",160),("index","CSI指数",105),("reason","为什么回避",650)]:
            self.avoid_tree.heading(key,text=label);self.avoid_tree.column(key,width=width,anchor="w" if key!="risk" else "center")
        avoid_y=ttk.Scrollbar(avoid_tab,orient="vertical",command=self.avoid_tree.yview)
        avoid_x=ttk.Scrollbar(avoid_tab,orient="horizontal",command=self.avoid_tree.xview)
        self.avoid_tree.configure(yscrollcommand=avoid_y.set,xscrollcommand=avoid_x.set)
        avoid_y.pack(side="right",fill="y");avoid_x.pack(side="bottom",fill="x")
        self.avoid_tree.pack(side="left",fill="both",expand=True)
        self.detail = tk.Text(self.body, height=8, wrap="word", bg="#FFFCFA", relief="flat", highlightbackground=COLORS["border"], highlightthickness=1,
                              fg=COLORS["text"], font=(FONT, FONT_SIZE["body"]), padx=12, pady=10)
        self.detail.pack(fill="x", pady=(10, 0))
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.audit_tree.bind("<<TreeviewSelect>>", self.show_audit_detail)

    def refresh(self):
        state = research_run_status(self.db)
        self.research_state = state
        display_run = state.get("research") or state.get("current")
        self.runs = [display_run] if display_run else []
        self.archived_count = int(state.get("archived_count") or 0)
        self.no_current_reason = str(state.get("reason") or "")
        labels = [f"{r['task_id']}｜{r['market_regime']}｜{r['analyzed_total']}/{r['universe_total']}" for r in self.runs]
        self.run_box["values"] = labels
        if labels and self.run_var.get() not in labels:
            self.run_var.set(labels[0])
        self.refresh_candidates()

    def current_run(self):
        try:
            return self.runs[list(self.run_box["values"]).index(self.run_var.get())]
        except (ValueError, IndexError, AttributeError):
            return None

    def refresh_candidates(self):
        self.tree.delete(*self.tree.get_children())
        self.audit_tree.delete(*self.audit_tree.get_children())
        self.alt_tree.delete(*self.alt_tree.get_children())
        self.avoid_tree.delete(*self.avoid_tree.get_children())
        self.detail.delete("1.0", "end")
        run = self.current_run()
        metrics = dashboard_metrics(self.db, research_state=getattr(self,"research_state",None))
        self.capital_context = action_capital_context(run, metrics)
        self.candidate_map = {}
        if not run:
            self.summary.config(text=(
                f"{self.no_current_reason}\n历史研究共{self.archived_count}轮，已作为只读审计隔离，不会在本页生成行动。\n"
                "正常用法：进入‘一键AI扫描’ → 确认DeepSeek已连接 → "
                "运行‘完整扫描全部CSI行业’；只有扫描成功后，本页才会出现候选。\n"
                "‘生成网页版备用任务包’只是复制给网页版ChatGPT的备用材料，不会自动研究，也不会直接填充本页。"
            ))
            self.detail.insert("1.0", (
                "如何让夜间雷达出现内容：\n"
                "1. 在‘中证代表目录’确认叶子行业准入，再在‘行情与映射’确认全部有精确行情。\n"
                "2. 在‘一键AI扫描’确认DeepSeek连接与预算。\n"
                "3. 点击‘完整扫描全部CSI行业’，等待行情校验、Flash检索和Pro终审全部成功。\n"
                "4. 完成后回到本页查看候选、催化、风险、买入条件和放弃条件。\n\n"
                "完整扫描会产生API费用，开始前软件会再次显示预算并要求确认。"
            ))
            return
        candidates = list(metrics.get("candidates") or [])
        try:
            payload = json.loads(run.get("raw_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        audit_rows=self.db.rows(
            """SELECT a.*,
                      (SELECT COALESCE(NULLIF(i.ant_index_code,''),i.official_index_code)
                         FROM investable_indices i WHERE i.universe_id=a.universe_id AND i.active=1
                         ORDER BY i.radar_enabled DESC,i.id LIMIT 1) AS csi_code
                 FROM radar_audit a WHERE a.task_id=? ORDER BY a.local_rank,a.universe_id""",
            (run["task_id"],),
        )
        self.audit_map={}
        stale = bool(self.capital_context.get("stale"))
        draft_service=DecisionDraftService(self.db)
        live_rows=draft_service.live_candidate_allocations(candidates)
        candidates=[live_rows.get(int(row["id"]),row) for row in candidates]
        live_amounts={
            int(row["id"]): round(float(row.get("suggested_amount") or 0),2)
            for row in candidates
        }
        self.live_amounts=live_amounts
        positive = sum(live_amounts[int(c["id"])] > 0 for c in candidates)
        research_positive=sum(str(row.get("research_tilt") or "") in {"非常推荐买","推荐买","有点推荐买"} for row in audit_rows)
        waiting=sum(str(row.get("timing_status") or "")=="等待回调" for row in audit_rows)
        executable=sum(str(row.get("execution_status") or "")=="可执行" for row in audit_rows)
        holding_actions = list(metrics.get("holding_actions") or [])
        capital_note = "　↻ 资金已变化，金额已按实时持仓重算。" if stale else ""
        read_only_note = (
            "\n⚠ 只读研究：中证官网日线仍在分批发布，行情未到最近交易日；"
            "本轮所有买入金额为0，不能作为今日执行依据。"
            if bool(getattr(self, "research_state", {}).get("read_only")) else ""
        )
        self.summary.config(text=(
            f"市场状态：{run['market_regime']}　全目录：{run['analyzed_total']}/{run['universe_total']}　数据截止：{run['data_cutoff']}　"
            f"研究正向：{research_positive}　等待回调：{waiting}　基金可执行：{executable}　当前正金额：{positive}　"
            f"主榜：{len(candidates)}/10　Pro独立回避：{len(payload.get('avoid_list') or [])}{capital_note}"
            f"{read_only_note}\n{run['summary']}\n"
            "七档只按推荐分分档：≥80非常推荐、≥70推荐、≥60有点推荐、≥45观察、"
            "≥35不太推荐、≥20不推荐、<20千万别买；安全硬门槛与Pro回避另行显示。"
        ))
        for c in candidates:
            iid = str(c["id"]); self.candidate_map[iid] = c
            amount = live_amounts[int(c["id"])]
            allocation = f"{float(c.get('target_allocation_pct') or c.get('allocation_pct') or 0):.1f}%"
            amount_text = money(amount)
            self.tree.insert("", "end", iid=iid, values=(
                c["rank_no"],c["sector_name"],c["index_code"],format_float(c["score"],1),
                c.get("research_tilt") or c.get("status") or "旧制未分档",
                c.get("timing_status") or "待核验",c.get("execution_status") or "不可执行",
                f"{c['fund_code']} {c['fund_name']}".strip() or "基金待绑定",allocation,amount_text,
            ))
        for row in audit_rows:
            iid=f"audit-{row['id']}";self.audit_map[iid]=row
            try:audit_components=json.loads(row.get("score_components_json") or "{}")
            except (TypeError,json.JSONDecodeError):audit_components={}
            gate_text="硬否决" if bool(audit_components.get("hard_avoid")) else "无"
            self.audit_tree.insert("","end",iid=iid,values=(
                row.get("local_rank") or "—",row.get("sector_name") or "",row.get("csi_code") or row.get("universe_id") or "",
                format_float(row.get("recommendation_score"),1),row.get("research_tilt") or "旧制未分档",
                gate_text,
                format_float(row.get("trend_score"),1),format_float(row.get("entry_score"),1),
                format_float(row.get("crowding_risk_score"),1),format_float(row.get("data_confidence"),1),
                row.get("timing_status") or "待核验",row.get("execution_status") or "不可执行",
            ))
        for number,row in enumerate(payload.get("same_bucket_alternatives") or [],start=1):
            if not isinstance(row,dict):continue
            self.alt_tree.insert("","end",iid=f"alt-{number}",values=(
                row.get("sector_name") or "",row.get("index_code") or "",row.get("correlation_bucket") or "未分类",
                row.get("pool_route") or "",format_float(row.get("recommendation_score"),1),
                row.get("research_tilt") or "",row.get("suppression_reason") or "",
            ))
        for number,row in enumerate(payload.get("avoid_list") or [],start=1):
            if isinstance(row,dict):
                values=(row.get("risk_level") or "",row.get("sector_name") or "",row.get("index_code") or "",row.get("reason") or "")
            else:
                values=("旧制",str(row),"","旧V2回避项未结构化，仅供历史阅读")
            self.avoid_tree.insert("","end",iid=f"avoid-{number}",values=values)
        if not candidates:
            lines = [
                "本轮最终主榜为0项；这不是研究缺失。请切到“全目录七档结论”查看全部研究。",
                (("历史观望理由（旧资金数字已删除）：" if stale else "不买原因：") +
                 (remove_stale_capital_claim(run.get('no_buy_reason')) if stale else
                  (run.get('no_buy_reason') or '多数强势方向短期过热、拥挤且量能不足。'))),
                "", "现有持仓AI文字建议：",
            ]
            for action in holding_actions:
                lines.append(f"• {action.get('fund_code')} {action.get('fund_name')}：{action.get('action')}；{action.get('reason')}")
            lines.extend(["", "Pro独立回避始终显示在第④页签；主榜少于10项不会触发低质量凑数。"])
            self.detail.insert("1.0", "\n".join(lines))

    def show_detail(self, _event=None):
        sel = self.tree.selection()
        if not sel: return
        c = self.candidate_map[sel[0]]
        stale=bool(getattr(self,"capital_context",{}).get("stale"))
        live_amount=getattr(self,"live_amounts",{}).get(int(c["id"]))
        if live_amount is None:
            live_amount=DecisionDraftService(self.db).live_candidate_amount(c)
        action_line=(
            f"七档研究结论：{c.get('research_tilt') or c.get('status') or '旧制未分档'}　"
            f"择时：{c.get('timing_status') or '待核验'}　执行：{c.get('execution_status') or '不可执行'}\n"
            f"目标占用户主动额度：{float(c.get('target_allocation_pct') or c.get('allocation_pct') or 0):.1f}%　"
            f"按实时硬门槛可新增：{money(live_amount)}" + ("（资金变化后已重算）" if stale else "")
        )
        metric=self.db.row(
            "SELECT base_score,score_components_json FROM sector_metrics WHERE task_id=? AND universe_id=?",
            (c["task_id"],c.get("universe_id") or ""),
        ) or {}
        try:components=json.loads(metric.get("score_components_json") or "{}")
        except (TypeError,json.JSONDecodeError):components={}
        labels={"trend_opportunity":"趋势机会","entry_quality":"入场性价比","crowding_risk":"拥挤风险",
                "crowding_adjustment":"拥挤调整",
                "data_confidence":"数据置信度","catalyst_quality":"催化","relative_strength":"相对强弱",
                "volume_breadth":"量价/真实扩散","opportunity_score":"机会分","recommendation_score":"推荐分"}
        numeric_components=[
            f"{labels[key]}{float(components[key]):.1f}"
            for key in labels if isinstance(components.get(key),(int,float)) and not isinstance(components.get(key),bool)
        ]
        score_line = (
            "本地四维推荐分：" + format_float(metric.get("base_score"), 3) + "；" + "、".join(numeric_components)
            if components
            else "本轮没有可复算评分明细（旧结果不会用于今日行动）。"
        )
        hard_reason=str(components.get("hard_avoid_reason") or "")
        gate_line=(
            f"安全硬门槛：已触发（{hard_reason or '原因待复核'}）；它独立阻止入场和进入候选池，不会篡改推荐分档。"
            if bool(components.get("hard_avoid")) else
            "安全硬门槛：未触发；七档结论由推荐分按固定区间直接生成。"
        )
        text = (f"{action_line}\n{score_line}\n{gate_line}\n"
                f"执行限制：{c.get('execution_reason') or '无额外限制'}\n"
                f"逻辑：{c['thesis']}\n催化：{c['catalyst']}\n风险：{c['risk']}\n买入条件：{c['buy_conditions']}\n放弃条件：{c['abandon_conditions']}")
        self.detail.delete("1.0", "end"); self.detail.insert("1.0", text)

    def show_audit_detail(self, _event=None):
        selected=self.audit_tree.selection()
        row=self.audit_map.get(selected[0]) if selected else None
        if not row:return
        try:components=json.loads(row.get("score_components_json") or "{}")
        except (TypeError,json.JSONDecodeError):components={}
        breadth=components.get("true_breadth_pct")
        breadth_text=(f"真实成分股上涨比例{float(breadth):.1f}%" if isinstance(breadth,(int,float)) else
                      "真实成分股扩散度：缺失；量能单独计分，扩散部分按中性50分处理，绝不使用指数涨跌替代")
        hard_reason=str(components.get("hard_avoid_reason") or "")
        gate_text=(
            f"已触发硬否决：{hard_reason or '原因待复核'}；只影响择时/候选池，不改写分数档位"
            if bool(components.get("hard_avoid")) else
            "未触发；七档结论严格按推荐分生成"
        )
        policy_row=self.db.row(
            "SELECT ranking_policy_version,ranking_policy_json FROM analysis_jobs WHERE task_id=?",
            (row.get("task_id") or "",),
        ) or {}
        try:frozen_policy=json.loads(policy_row.get("ranking_policy_json") or "{}")
        except (TypeError,json.JSONDecodeError):frozen_policy={}
        formula_version=str(
            components.get("formula_version") or policy_row.get("ranking_policy_version") or ""
        )
        formula_text=ranking_formula_text(row,components,frozen_policy,formula_version)
        text=(
            f"{row.get('sector_name')}｜{row.get('csi_code') or row.get('universe_id')}｜全目录排名{row.get('local_rank') or '—'}\n"
            f"七档结论：{row.get('research_tilt') or '旧制未分档'}　择时：{row.get('timing_status') or '待核验'}　"
            f"执行：{row.get('execution_status') or '不可执行'}　候选池路线：{row.get('pool_route') or '未进入质量池'}\n"
            f"{formula_text}；"
            f"趋势机会{float(row.get('trend_score') or 0):.1f}，入场性价比{float(row.get('entry_score') or 0):.1f}，"
            f"数据置信度{float(row.get('data_confidence') or 0):.1f}。\n{breadth_text}\n"
            f"安全门槛：{gate_text}\n"
            f"催化：{row.get('catalyst') or '未说明'}\n拥挤：{row.get('crowding') or '未说明'}\n{row.get('note') or ''}"
        )
        self.detail.delete("1.0","end");self.detail.insert("1.0",text)

    def export_csv(self):
        run = self.current_run()
        if not run:
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出候选", initialdir=EXPORT_DIR, initialfile=f"候选_{run['task_id']}.csv", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        rows = self.db.rows("SELECT * FROM candidates WHERE task_id=? ORDER BY rank_no", (run["task_id"],))
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys() if rows else ["task_id"]); writer.writeheader(); writer.writerows(rows)
        messagebox.showinfo("导出成功", str(path), parent=self)


class AuditPage(BasePage):
    title = "🏷️ 中证代表目录"
    subtitle = "当前目录只保留至少一只严格C类基金的CSI指数；无C条目只在历史审计中查看。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._product_sync_active=False
        self._execution_refresh_active=False
        self._product_sync_stop=threading.Event()
        self._execution_stop=threading.Event()
        toolbar = tk.Frame(self.body, bg=COLORS["bg"]); toolbar.pack(fill="x", pady=(0, 8))
        top = tk.Frame(toolbar, bg=COLORS["bg"]); top.pack(fill="x")
        self.filter_var = tk.StringVar(value="严格C类研究目录")
        filter_box = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=18,
                                 values=("严格C类研究目录", "已准入雷达", "历史/审计（含无C）"))
        filter_box.pack(side="left"); filter_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_rows())
        CuteButton(top, "➕ 补充CSI指数", self.add_candidate, "primary").pack(side="left", padx=8)
        CuteButton(top, "🧪 重新检查准入", self.recheck, "success").pack(side="left", padx=8)
        CuteButton(top, "✏️ 核对选中CSI代码", self.verify_selected, "secondary").pack(side="left")
        CuteButton(top, "📊 导出目录CSV", self.export_csv, "secondary").pack(side="right")
        CuteButton(top, "🌐 复制DeepSeek详细研究", lambda: self.open_prompt("deepseek"), "success").pack(side="right",padx=8)
        CuteButton(top, "🌐 复制ChatGPT详细研究", lambda: self.open_prompt("chatgpt"), "primary").pack(side="right")

        network = tk.Frame(toolbar, bg=COLORS["bg"]); network.pack(fill="x", pady=(7,0))
        self.product_sync_button = CuteButton(
            network, "🌐 全量收录关联基金", self.sync_all_products, "success",
        )
        self.product_sync_button.pack(side="left")
        self.execution_refresh_button = CuteButton(
            network, "☀️ 刷新今日可申购", self.refresh_daily_execution, "primary",
        )
        self.execution_refresh_button.pack(side="left", padx=8)
        self.catalog_stop_button=CuteButton(network,"⏹ 停止联网任务",self.stop_catalog_network,"danger")
        self.catalog_stop_button.pack(side="left");self.catalog_stop_button.set_enabled(False)
        self.force_refresh_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            network, text="同时重查已完成项（推荐）", variable=self.force_refresh_var,
            bg=COLORS["bg"], fg=COLORS["muted"], activebackground=COLORS["bg"],
            font=(FONT, FONT_SIZE["small"]), selectcolor=COLORS["panel"],
        ).pack(side="left")
        self.mapping_coverage = tk.Label(
            self.body, text="", bg="#FFF5E6", fg=COLORS["warning"], anchor="w", justify="left",
            font=(FONT, FONT_SIZE["small"]), padx=10, pady=7,
        ); self.mapping_coverage.pack(fill="x", pady=(0, 8))
        self.catalog_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.catalog_progress.pack(fill="x",pady=(0,8))
        columns = ("status", "industry", "platform", "official", "products", "collection", "passive", "eligible", "evidence", "checked")
        self.tree = ttk.Treeview(self.body, columns=columns, show="headings", height=16)
        spec = [
            ("status", "雷达准入", 205), ("industry", "行业", 105),
            ("platform", "CSI展示代码", 205), ("official", "中证正式指数", 245),
            ("products", "官网产品", 82),
            ("collection", "收录状态", 120), ("passive", "场外被动C类", 105),
            ("eligible", "当日可执行", 90),
            ("evidence", "官方依据", 130), ("checked", "行情历史", 150),
        ]
        for key, label, width in spec:
            self.tree.heading(key, text=label); self.tree.column(key, width=width, anchor="center" if key in {"products", "passive", "eligible", "collection"} else "w")
        self.tree.pack(fill="both", expand=True); self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        detail_box = tk.Frame(self.body, bg=COLORS["bg"])
        detail_box.pack(fill="x", pady=(10, 0))
        self.detail = tk.Text(detail_box, height=8, wrap="word", bg="#FFFCFA", fg=COLORS["text"], relief="flat",
                              highlightbackground=COLORS["border"], highlightthickness=1,
                              font=(FONT, FONT_SIZE["small"]), padx=10, pady=8)
        detail_scroll = ttk.Scrollbar(detail_box, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="x", expand=True)
        detail_scroll.pack(side="right", fill="y")

    def refresh(self):
        self.catalog = PlatformCatalogService(self.db)
        coverage = self.catalog.coverage()
        self.mapping_coverage.config(text=(
            f"严格C类研究目录 {coverage['candidates']} 个｜可扫描研究 {coverage['admitted']} 个｜今日已有基金可执行 {coverage['executable_indices']} 个行业｜"
            f"中证官网产品已覆盖 {coverage['official_covered_indices']}/{coverage['candidates']} 个指数、共 {coverage['official_products']} 只｜"
            f"场外C类候选 {coverage['passive_funds']} 只。\n"
            "长期目录门槛是精确同指数、场外被动、C类且公开可申购；“今日可执行”还必须完成当天轻量联网复核。"
        ),fg=COLORS["warning"])
        self.refresh_rows()

    def refresh_rows(self):
        self.tree.delete(*self.tree.get_children()); self.row_map = {}
        mode = self.filter_var.get()
        history_mode = mode == "历史/审计（含无C）"
        catalog = PlatformCatalogService(self.db)
        for row in catalog.list_indices(include_inactive=history_mode):
            if history_mode and int(row.get("active") or 0):
                continue
            if not history_mode and int(row.get("strict_catalog_fund_total") or 0) <= 0:
                continue
            if mode == "已准入雷达" and not int(row.get("radar_enabled") or 0):
                continue
            iid = str(row["id"]); self.row_map[iid] = row
            self.tree.insert("", "end", iid=iid, values=(
                row["admission_status"], row.get("industry_group") or row.get("research_name"),
                f"{row.get('ant_index_code') or '待核验'} {row.get('ant_index_name') or ''}".strip(),
                f"{row.get('official_index_code') or '待核验'} {row.get('official_index_name') or ''}".strip(),
                row.get("official_product_total", 0),
                row.get("collection_state") or "待同步",
                row.get("passive_total", 0), row.get("fresh_eligible_fund_total", 0),
                row.get("platform_evidence_kind") or "无", row.get("quote_history_status") or "未核验",
            ), tags=("history" if history_mode else "admitted" if int(row.get("radar_enabled") or 0) else "candidate",))
        self.tree.tag_configure("admitted", foreground=COLORS["accent"])
        self.tree.tag_configure("candidate", foreground=COLORS["warning"])
        self.tree.tag_configure("history", foreground=COLORS["muted"])
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0]); self.show_detail()
        else:
            self.detail.delete("1.0", "end"); self.detail.insert("1.0", "当前筛选下没有目录条目。")

    def selected(self):
        selection = self.tree.selection()
        return self.row_map.get(selection[0]) if selection else None

    def show_detail(self, _event=None):
        row = self.selected()
        if not row: return
        catalog = PlatformCatalogService(self.db)
        funds = catalog.funds(int(row["id"]))
        strict_c_funds = catalog.strict_catalog_funds(int(row["id"]))
        executable_codes = {
            str(fund.get("fund_code") or "") for fund in strict_c_funds
            if catalog._fund_is_executable(fund)
        }
        other_local_funds = [fund for fund in funds if fund not in strict_c_funds]
        official_products = catalog.official_products(int(row["id"]))
        fund_lines = []
        for fund in strict_c_funds:
            flag = "当日可执行" if str(fund.get("fund_code") or "") in executable_codes else "下单前需刷新"
            fund_lines.append(
                f"{flag}｜{fund['fund_code']} {fund['fund_name']}｜{fund['strategy_kind']}｜"
                f"公开申购：{fund['public_buyable_status']}｜资料：{fund.get('verified_at') or '待核验'}"
            )
        official_lines = []
        for product in official_products:
            official_lines.append(
                f"{product['product_code']} {product.get('fund_name') or '名称待官网补充'}｜"
                f"{product.get('strategy_kind') or '待分类'}｜{product.get('fund_manager') or '管理人待补'}"
            )
        other_lines = [
            f"{fund['fund_code']} {fund['fund_name']}｜{fund.get('share_class') or '份额待核验'}｜"
            f"{fund.get('strategy_kind') or '策略待核验'}｜{fund.get('relation_kind') or '关系待核验'}"
            for fund in other_local_funds
        ]
        text = (
            ("【历史审计】该条目不在当前目录，不扫描、不生成AI提示或推荐。\n\n"
             if not int(row.get("active") or 0) else "") +
            f"行业：{row.get('industry_group') or row.get('research_name')}\n"
            f"CSI展示代码：{row.get('ant_index_code') or '待核验'}\n"
            f"中证正式指数：{row.get('official_index_code') or '待核验'} {row.get('official_index_name') or ''}\n"
            f"精确行情：{row.get('security_id') or '待核验'}｜{row.get('proxy_code') or ''} {row.get('proxy_name') or ''}\n"
            f"代表理由：{row.get('representative_reason') or '待补充'}\n"
            f"准入结论：{row.get('admission_status')}\n"
            f"未准入原因：{row.get('exclusion_reason') or '无'}\n\n"
            f"收录结论：{row.get('collection_state') or '待同步'}｜官方根产品：{row.get('official_root_state') or '待同步'}｜"
            f"场外C类：{row.get('fund_collection_state') or '待同步'}\n"
            f"官网同步：{row.get('product_sync_state') or '待同步'}（{len(official_products)}只）｜"
            f"尝试：{row.get('product_sync_attempted_at') or '无'}｜成功：{row.get('product_sync_success_at') or '无'}\n"
            f"场外同步：{row.get('fund_sync_state') or '待同步'}（结构严格{len(strict_c_funds)}只；当日可执行{len(executable_codes)}只）｜"
            f"尝试：{row.get('fund_sync_attempted_at') or '无'}｜成功：{row.get('fund_sync_success_at') or '无'}\n"
            f"官网说明：{row.get('product_sync_detail') or '无'}\n"
            f"场外说明：{row.get('fund_sync_detail') or '无'}\n\n"
            "中证官网关联产品（完整留档，不等于可买）：\n"
            + ("\n".join(official_lines) if official_lines else "尚未从中证官网同步")
            + "\n\n场外被动C类（精确同指数；是否可执行另行标注）：\n"
            + ("\n".join(fund_lines) if fund_lines else "没有已核验的场外被动C类")
            + "\n\n本地其他关联或待核验记录（不会冒充上述C类）：\n"
            + ("\n".join(other_lines) if other_lines else "无")
        )
        self.detail.delete("1.0", "end"); self.detail.insert("1.0", text)

    def recheck(self):
        result = PlatformCatalogService(self.db).refresh_admission()
        self.refresh()
        messagebox.showinfo(
            "准入检查完成",
            f"已检查 {result['checked']} 个CSI代表，可研究 {result['admitted']} 个，其中 {result.get('executable', 0)} 个已有可执行基金。\n"
            "没有通过的条目不会进入扫描，也不会退回旧186项固定池。",
            parent=self,
        )

    def refresh_daily_execution(self):
        if self._execution_refresh_active or self._product_sync_active:
            messagebox.showinfo("正在复核", "今日可申购状态仍在联网复核，请稍候。", parent=self)
            return
        before = PlatformCatalogService(self.db).execution_summary()
        if before["total"] <= 0:
            messagebox.showwarning("没有研究目录", "当前没有严格C类研究目录，不能执行当日复核。", parent=self)
            return
        if before["ready"] == before["total"]:
            messagebox.showinfo(
                "今日资格已完整",
                f"当前 {before['ready']}/{before['total']} 个研究指数均已有当天可执行基金，无需重复联网。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "免费刷新今日可申购",
            f"当前仅 {before['ready']}/{before['total']} 个指数具备今天的执行证据。\n"
            "软件将最多6路分批查询公开基金状态，同一基金只查一次；"
            "每个指数确认至少一只严格C类基金后即停止。\n"
            "这一步不调用DeepSeek、不消耗Token，界面可继续响应，也可随时停止。继续吗？",
            parent=self,
        ):
            return
        self._execution_refresh_active = True;self._execution_stop.clear()
        self._set_catalog_network_controls(True)
        self.catalog_progress["value"]=0
        self.mapping_coverage.config(text="正在免费刷新今日可申购状态……", fg=COLORS["info"])

        def progress(done, total, label):
            self.after(0, lambda d=done, t=total, value=label: self._update_catalog_progress(
                d,t,f"今日可申购复核｜{value}",
            ))

        def worker():
            try:
                discovery = FundDiscoveryService(self.db)
                result = PlatformCatalogService(self.db).revalidate_radar_execution(
                    checker=lambda fund: discovery.public_execution_snapshot(fund["fund_code"]),
                    progress=progress,
                    stop_requested=self._execution_stop.is_set,
                )
            except Exception as exc:
                self.after(0, lambda error=exc: self._finish_daily_execution(None, error))
            else:
                self.after(0, lambda value=result: self._finish_daily_execution(value, None))

        self.app.start_background(
            worker,label="全目录今日可申购复核",cancel=self._execution_stop.set,
        )

    def _finish_daily_execution(self, result, error):
        self._execution_refresh_active = False
        self._set_catalog_network_controls(False)
        self.refresh()
        if error:
            messagebox.showerror("今日复核未完成", str(error), parent=self)
            return
        errors = list(result.get("errors") or [])
        text = (
            f"今日执行覆盖：{result.get('ready', 0)}/{result.get('total', 0)} 个指数。\n"
            f"本次联网尝试 {result.get('network_attempts', result.get('network_funds', 0))} 只基金，"
            f"成功取得 {result.get('network_funds', 0)} 份证据；"
            f"仍缺 {len(result.get('missing') or [])} 个指数。"
        )
        if result.get("stopped"):
            text="已按你的要求停止；已完成的当日证据均已保存。\n"+text
        if errors:
            text += "\n\n未完成明细（可稍后重试）：\n" + "\n".join(errors[:12])
        if result.get("stopped"):
            messagebox.showinfo("今日可申购复核已停止",text,parent=self)
        elif int(result.get("ready") or 0) == int(result.get("total") or 0):
            messagebox.showinfo("今日可申购复核完成", text, parent=self)
        else:
            messagebox.showwarning("今日执行覆盖不完整", text, parent=self)

    def sync_all_products(self):
        if self._product_sync_active or self._execution_refresh_active:
            messagebox.showinfo("正在收录", "全量关联基金仍在联网核验，请稍候。", parent=self)
            return
        force_refresh = bool(self.force_refresh_var.get())
        if force_refresh and not messagebox.askyesno(
            "确认强制重查",
            "将重新联网核验已经完成或已确认无C的条目；耗时会明显增加，但不会删除已保存证据。是否继续？",
            parent=self,
        ):
            return
        self._product_sync_active = True;self._product_sync_stop.clear()
        self._product_sync_force = force_refresh
        self._set_catalog_network_controls(True);self.catalog_progress["value"]=0
        self.mapping_coverage.config(
            text=("正在强制重查全部指数的官网产品与场外被动C类。" if force_refresh else
                  "正在续跑未完成的官网产品与场外被动C类收录。") + " 全程不调用DeepSeek。",
            fg=COLORS["info"],
        )

        def official_progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_catalog_progress(
                d,max(1,t*2),f"官网产品｜{value}",
            ))
        def field_progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_catalog_progress(
                t+d,max(1,t*2),f"场外被动C类｜{value}",
            ))

        def worker():
            try:
                official = CSIOfficialProductService(self.db).sync_all(
                    incomplete_only=True,force_refresh=force_refresh,progress=official_progress,
                    stop_requested=self._product_sync_stop.is_set,
                )
                field_c = CSIFieldFundSyncService(self.db).sync_all(
                    incomplete_only=True,force_refresh=force_refresh,progress=field_progress,
                    stop_requested=self._product_sync_stop.is_set,
                )
            except Exception as exc:
                self.after(0, lambda error=exc: self._finish_product_sync(None, None, error))
            else:
                self.after(0, lambda: self._finish_product_sync(official, field_c, None))

        self.app.start_background(
            worker,label="中证基金产品同步",cancel=self._product_sync_stop.set,
        )

    def _finish_product_sync(self, official, field_c, error):
        self._product_sync_active = False
        self._set_catalog_network_controls(False)
        self.refresh()
        if error:
            messagebox.showerror("收录未完成", str(error), parent=self)
            return
        errors = list(official.errors) + list(field_c.errors)
        text = (
            f"中证官网：完成 {official.completed} 个、跳过 {official.skipped} 个，当前批次收录 {official.products} 只产品。\n"
            f"场外C类：完成 {field_c.completed} 个、跳过 {field_c.skipped} 个，新核验/更新 {field_c.exact_c_funds} 只；"
            f"已确认无C {field_c.confirmed_no_c} 个，已从当前跟踪与扫描目录移出 {getattr(field_c, 'retired_indices', 0)} 个。\n"
            f"网络失败 {official.network_failures + field_c.network_failures} 个；"
            f"资料待复核 {official.pending_review + field_c.pending_review} 个。"
        )
        stopped=bool(getattr(official,"stopped",False) or getattr(field_c,"stopped",False))
        if stopped:text="已按你的要求停止；已完成指数的证据均已分批保存。\n"+text
        if stopped:
            messagebox.showinfo("关联基金收录已停止",text,parent=self)
        elif errors:
            text += "\n\n未完成项已保留为可续跑状态：\n" + "\n".join(errors[:12])
            messagebox.showwarning("关联基金已分批保存", text, parent=self)
        else:
            messagebox.showinfo("关联基金收录完成", text, parent=self)

    def _set_catalog_network_controls(self,active):
        self.product_sync_button.set_enabled(not active)
        self.execution_refresh_button.set_enabled(not active)
        self.catalog_stop_button.set_enabled(active)

    def _update_catalog_progress(self,done,total,label):
        self.catalog_progress["maximum"]=max(1,total);self.catalog_progress["value"]=done
        self.mapping_coverage.config(
            text=f"联网进度 {done}/{total}｜{label}\n已成功项目立即保存；停止会在当前请求结束后生效。",
            fg=COLORS["info"],
        )

    def stop_catalog_network(self):
        if not (self._product_sync_active or self._execution_refresh_active):return
        self._product_sync_stop.set();self._execution_stop.set();self.catalog_stop_button.set_enabled(False)
        self.mapping_coverage.config(text="已请求停止；当前网络请求结束后不再处理下一项。",fg=COLORS["warning"])

    def add_candidate(self):
        dialog = FormDialog(self, "补充CSI代表指数", [
            ("行业分类", "industry", ""),
            ("CSI代码（如000990.CSI）", "platform_code", ""),
            ("中证正式名称", "platform_name", ""),
            ("正式六位代码", "official_code", ""),
            ("中证正式全名", "official_name", ""),
            ("中证官网事实表网址", "official_url", ""),
            ("官方依据类型", "evidence_kind", "中证官方事实表"),
            ("官方依据网址", "platform_url", ""),
            ("精确行情证券ID", "security_id", ""),
            ("精确行情代码", "proxy_code", ""),
            ("精确行情名称", "proxy_name", ""),
            ("行情映射置信度 0—1", "confidence", 0),
            ("核对备注", "note", ""),
        ])
        if not dialog.result: return
        value = dialog.result
        try:
            index_id = PlatformCatalogService(self.db).add_candidate(
                industry_group=value["industry"], platform_code=value["platform_code"],
                platform_name=value["platform_name"], official_code=value["official_code"],
                official_name=value["official_name"], official_source_url=value["official_url"],
                evidence_kind=value["evidence_kind"], platform_source_url=value["platform_url"],
                note=value["note"], security_id=value["security_id"], proxy_code=value["proxy_code"],
                proxy_name=value["proxy_name"], mapping_confidence=float(value["confidence"] or 0),
            )
        except Exception as exc:
            messagebox.showerror("不能新增", str(exc), parent=self); return
        self.refresh()
        if str(index_id) in self.row_map:
            self.tree.selection_set(str(index_id)); self.tree.see(str(index_id)); self.show_detail()
        messagebox.showinfo(
            "候选已加入",
            "该指数已作为CSI候选保存。只有官方身份、20日历史和精确行情全部通过才会进入扫描；基金是否可执行另行核验。",
            parent=self,
        )

    def verify_selected(self):
        row = self.selected()
        if not row:
            messagebox.showinfo("请先选择", "请先选中一个指数。", parent=self); return
        dialog = FormDialog(self, "核对CSI官方身份", [
            ("CSI代码（如000990.CSI）", "code", row.get("ant_index_code") or ""),
            ("中证正式名称", "name", row.get("official_index_name") or ""),
            ("官方依据类型", "kind", row.get("platform_evidence_kind") or "中证官方事实表"),
            ("中证官网资料网址", "url", row.get("official_source_url") or ""),
            ("核对备注", "note", row.get("ant_source_note") or ""),
        ])
        if not dialog.result: return
        try:
            check = PlatformCatalogService(self.db).mark_platform_verified(
                int(row["id"]), platform_code=dialog.result["code"], platform_name=dialog.result["name"],
                evidence_kind=dialog.result["kind"], source_url=dialog.result["url"], note=dialog.result["note"],
            )
        except Exception as exc:
            messagebox.showerror("不能保存", str(exc), parent=self); return
        self.refresh()
        messagebox.showinfo("核验已保存", "已准入雷达。" if check.admitted else "指数核验已保存，但仍未准入：" + "；".join(check.reasons), parent=self)

    def open_prompt(self, target):
        row = self.selected()
        if not row:
            messagebox.showinfo("请先选择", "请先选中要详细研究的指数。", parent=self); return
        catalog = PlatformCatalogService(self.db)
        if not int(row.get("active") or 0) or not int(row.get("radar_enabled") or 0):
            messagebox.showwarning(
                "仅供历史审计",
                "该指数不在当前可买目录，不能生成AI详细研究提示。",
                parent=self,
            )
            return
        if not catalog.strict_catalog_funds(int(row["id"])):
            messagebox.showwarning(
                "没有严格C类基金",
                "该指数没有通过严格条件的场外被动C类基金，不能生成AI提示。",
                parent=self,
            )
            return
        try:
            prompt = DetailedResearchPromptService(self.db).build(int(row["id"]), target)
        except Exception as exc:
            messagebox.showerror("无法生成", str(exc), parent=self); return
        self.clipboard_clear(); self.clipboard_append(prompt); self.update_idletasks()
        win = tk.Toplevel(self); win.title("详细研究提示词（已复制）"); win.geometry("980x720"); win.configure(bg=COLORS["bg"])
        tk.Label(win, text="提示词已复制。你可以先检查，再直接粘贴到联网的网页窗口。", bg=COLORS["bg"], fg=COLORS["accent"], font=(FONT, FONT_SIZE["body"], "bold")).pack(fill="x", padx=14, pady=10)
        text = tk.Text(win, wrap="word", font=(FONT, FONT_SIZE["small"]), padx=12, pady=10)
        text.pack(fill="both", expand=True, padx=14); text.insert("1.0", prompt)
        actions = tk.Frame(win, bg=COLORS["bg"]); actions.pack(fill="x", padx=14, pady=12)
        def copy_again():
            win.clipboard_clear(); win.clipboard_append(text.get("1.0", "end-1c")); win.update_idletasks()
            messagebox.showinfo("已复制", "提示词已经重新复制。", parent=win)
        CuteButton(actions, "📋 重新复制", copy_again, "primary").pack(side="right")
        CuteButton(actions, "关闭", win.destroy, "secondary").pack(side="right", padx=8)

    def export_csv(self):
        rows = PlatformCatalogService(self.db).list_indices()
        path = EXPORT_DIR / f"CSI代表行业目录_{local_now().strftime('%Y%m%d-%H%M')}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["id"])
            writer.writeheader(); writer.writerows(rows)
        messagebox.showinfo("导出完成", str(path), parent=self)


class FundDiscoveryPage(BasePage):
    title = "🧭 基金优选"
    subtitle = "先确认要买的指数，再用公开数据比较跟踪误差、费用、规模和运作时间；默认零DeepSeek费用。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._network_active=False
        self._network_stop=threading.Event()
        top = tk.Frame(self.body, bg=COLORS["bg"]); top.pack(fill="x", pady=(0, 8))
        self.target_var = tk.StringVar(); self.days_var = tk.StringVar(value="30天")
        self.amount_var = tk.StringVar(value="0")
        self.target_box = ttk.Combobox(top, textvariable=self.target_var, state="readonly", width=48)
        self.target_box.pack(side="left"); self.target_box.bind("<<ComboboxSelected>>", lambda _e:self.load_latest())
        self.days_box = ttk.Combobox(top, textvariable=self.days_var, state="readonly", values=("30天","90天","365天"), width=9)
        self.days_box.pack(side="left", padx=8)
        tk.Label(top,text="拟买金额",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"])).pack(side="left")
        ttk.Entry(top,textvariable=self.amount_var,width=10).pack(side="left",padx=(4,8))
        self.search_button = CuteButton(top, "🌐 联网搜索并硬指标排名", self.discover, "success")
        self.search_button.pack(side="left")
        self.network_stop_button=CuteButton(top,"⏹ 停止联网",self.stop_network,"danger")
        self.network_stop_button.pack(side="left",padx=8);self.network_stop_button.set_enabled(False)
        CuteButton(top,"✏️ 查看/核对CSI身份",self.edit_identity,"secondary").pack(side="left",padx=8)
        self.status = tk.Label(self.body, text="选择板块后点击联网搜索；只抓C类份额，这一步不调用DeepSeek。", bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT,FONT_SIZE["small"]), anchor="w", justify="left")
        self.status.pack(fill="x", pady=(0,8))
        self.network_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.network_progress.pack(fill="x",pady=(0,8))
        self.score_help = tk.Label(
            self.body,
            text=("综合分（满分100）：指数一致性30＋跟踪质量25＋所选持有期成本20＋规模10＋成立年限5＋公开可申购10；"
                  "跟踪质量=max(0, 25－5×年化误差百分点)，指数增强另减3分。缺失数据一律按0分，不会把‘没有样本’当成‘误差为0’。"),
            bg="#EEF4FB", fg=COLORS["info"], font=(FONT,FONT_SIZE["small"]),
            anchor="w", justify="left", wraplength=1550, padx=10, pady=6,
        )
        self.score_help.pack(fill="x", pady=(0,8))
        identity_card=Card(self.body,bg="#FFF8FB",highlightbackground="#E7BCC7");identity_card.pack(fill="x",pady=(0,8))
        self.selected_fund_name=tk.Label(identity_card,text="基金完整名称：请先选择一行",bg="#FFF8FB",fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["body"],"bold"),anchor="w",justify="left",wraplength=1200)
        self.selected_fund_name.pack(fill="x",padx=12,pady=(8,2))
        self.selected_target_name=tk.Label(identity_card,text="实际跟踪指数：—",bg="#FFF8FB",fg=COLORS["text"],font=(FONT,FONT_SIZE["small"]),anchor="w",justify="left",wraplength=1200)
        self.selected_target_name.pack(fill="x",padx=12,pady=(0,8))
        cols=("rank","recommend","fund","target","strategy","match","horizon","cost","size","age","track","buyable","score")
        self.tree=ttk.Treeview(self.body,columns=cols,show="headings",height=12)
        for key,label,width in [("rank","排名",48),("recommend","结论",175),("fund","基金",235),("target","实际跟踪指数",175),("strategy","策略类型",95),("match","匹配",100),("horizon","持有期成本",100),("cost","年运作费",90),("size","规模/亿元",90),("age","成立日",100),("track","跟踪误差",120),("buyable","公开申购状态",135),("score","综合分",70)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,anchor="w" if key in {"recommend","fund","target","buyable"} else "center")
        self.tree.pack(fill="both",expand=True);self.tree.bind("<<TreeviewSelect>>",self.show_detail)
        actions=tk.Frame(self.body,bg=COLORS["bg"]);actions.pack(fill="x",pady=(8,6))
        CuteButton(actions,"📌 保存到基金库（候补）",self.save_pending,"secondary").pack(side="left")
        CuteButton(actions,"✅ 确认同指数并绑定",self.bind_verified,"success").pack(side="left",padx=8)
        self.execution_button=CuteButton(actions,"🛡️ 当日可申购复核",self.recheck_buyability,"primary")
        self.execution_button.pack(side="left")
        CuteButton(actions,"🔍 打开公开资料页",self.open_source,"secondary").pack(side="left")
        self.detail=tk.Text(self.body,height=6,wrap="word",bg="#FFFCFA",fg=COLORS["text"],relief="flat",highlightbackground=COLORS["border"],highlightthickness=1,font=(FONT,FONT_SIZE["small"]),padx=10,pady=8)
        self.detail.pack(fill="x")

    def refresh(self):
        self.current_research_run=latest_current_radar_run(self.db)
        current_task_id=(self.current_research_run or {}).get("task_id") or "__NO_CURRENT_RESEARCH__"
        rows=self.db.rows("""SELECT i.id AS platform_index_id,u.index_code AS universe_id,u.index_name,p.proxy_code,p.proxy_name,
            COALESCE(i.official_index_code,'') official_index_code,COALESCE(i.official_index_name,'') official_index_name,
            COALESCE(i.ant_index_code,'') ant_index_code,COALESCE(i.ant_index_name,'') ant_index_name,
            COALESCE(i.verification_status,'待核验') investable_status,
            COALESCE(i.admission_status,'候选·证据未齐') admission_status,
            CASE WHEN c.universe_id IS NULL THEN 1 ELSE 0 END AS candidate_order,COALESCE(c.rank_no,999) AS candidate_rank
            FROM investable_indices i JOIN universe u ON u.index_code=i.universe_id
            LEFT JOIN proxy_mappings p ON p.universe_id=u.index_code
            LEFT JOIN (SELECT universe_id,MIN(rank_no) rank_no FROM candidates WHERE task_id=? GROUP BY universe_id) c ON c.universe_id=u.index_code
            WHERE i.active=1
            ORDER BY i.radar_enabled DESC,candidate_order,candidate_rank,i.industry_group,i.display_order,i.id""",(current_task_id,))
        catalog = PlatformCatalogService(self.db)
        strict_index_ids = catalog.strict_catalog_index_ids(
            [int(row["platform_index_id"]) for row in rows]
        )
        rows = [row for row in rows if int(row["platform_index_id"]) in strict_index_ids]
        self.option_map={f"{r.get('admission_status')}｜{r['index_name']}｜{r.get('ant_index_code') or 'CSI代码待核验'}":r for r in rows}
        self.target_box["values"]=list(self.option_map)
        if self.option_map and self.target_var.get() not in self.option_map:self.target_var.set(next(iter(self.option_map)))
        saved=self.db.setting("fund_discovery_holding_days","30");self.days_var.set(saved+"天" if saved in {"30","90","365"} else "30天")
        self.load_latest()

    def current_target(self): return self.option_map.get(self.target_var.get())

    def research_context(self):
        run=getattr(self,"current_research_run",None)
        if run:
            return f"今日当前研究：{run['task_id']}；只有这轮候选参与优先排序。"
        return "今日尚无当前目录的完整成功研究；旧轮次已隔离，不参与基金优选排序。"

    def edit_identity(self):
        target=self.current_target()
        if not target:return
        existing=InvestableMappingService(self.db).identity_for(target["universe_id"]) or {}
        dialog=FormDialog(self,"核对CSI代表指数身份",[
            ("研究方向（只读参考）","research",f"{target['universe_id']} {target['index_name']}"),
            ("官方指数代码","official_index_code",existing.get("official_index_code","")),
            ("官方指数名称","official_index_name",existing.get("official_index_name","")),
            ("CSI展示代码（六位数字或H+五位数字）","ant_index_code",existing.get("ant_index_code","")),
            ("CSI展示名称","ant_index_name",existing.get("official_index_name","")),
            ("与研究方向的关系","mapping_relation",existing.get("mapping_relation","中证官方同一指数")),
            ("中证官网资料网址","official_source_url",existing.get("official_source_url","")),
        ])
        if not dialog.result:return
        v=dialog.result
        if not v["official_index_name"].strip():messagebox.showerror("不能保存","请填写官方指数名称。",parent=self);return
        now=local_now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
            if existing:
                conn.execute("""UPDATE investable_indices SET official_index_code=?,official_index_name=?,ant_index_code=?,
                    ant_index_name=?,mapping_relation=?,mapping_confidence=0.95,verification_status=?,official_source_url=?,
                    ant_source_note=?,verified_at=?,platform_evidence_kind=?,platform_checked_at=? WHERE id=?""",
                    (v["official_index_code"].strip(),v["official_index_name"].strip(),v["ant_index_code"].strip(),v["ant_index_name"].strip(),
                     v["mapping_relation"].strip() or "中证官方同一指数","中证官方资料已核对",
                     v["official_source_url"].strip(),"CSI展示代码与中证官方六位代码一致。",now,
                     "中证官方事实表",now,existing["id"]))
            else:
                conn.execute("""INSERT INTO investable_indices(universe_id,official_index_code,official_index_name,
                    ant_index_code,ant_index_name,mapping_relation,mapping_confidence,verification_status,
                    official_source_url,ant_source_note,verified_at,active,display_order,
                    platform_evidence_kind,platform_checked_at)
                    VALUES(?,?,?,?,?,?,0.95,?,?,?,?,1,1,?,?)""",
                    (target["universe_id"],v["official_index_code"].strip(),v["official_index_name"].strip(),v["ant_index_code"].strip(),
                     v["ant_index_name"].strip(),v["mapping_relation"].strip() or "中证官方同一指数",
                     "中证官方资料已核对",v["official_source_url"].strip(),
                     "CSI展示代码与中证官方六位代码一致。",now,"中证官方事实表",now))
        if existing:
            PlatformCatalogService(self.db).refresh_admission(int(existing["id"]))
        self.refresh();messagebox.showinfo("CSI身份已保存","研究ID、精确行情证券ID、正式六位代码和.CSI展示代码继续分栏保存，并已重新检查雷达准入。",parent=self)

    def discover(self):
        if self._network_active:
            messagebox.showinfo("正在联网","基金搜索或当日复核仍在运行，可点击停止。",parent=self);return
        target=self.current_target()
        if not target:return
        days=int(self.days_var.get().replace("天",""));self.db.save_settings({"fund_discovery_holding_days":days})
        self._set_network_active(True);self._network_stop.clear();self.network_progress["value"]=0
        self.status.config(text="正在只搜索C类基金、核对跟踪标的并计算硬指标……通常需要十几秒。",fg=COLORS["info"])
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_network_progress(d,t,value))
        def worker():
            try:result=FundDiscoveryService(self.db).discover(
                target["universe_id"],days,
                stop_requested=self._network_stop.is_set,progress=progress,
            )
            except Exception as exc:self.after(0,lambda error=exc:self._finish_discovery(None,error))
            else:self.after(0,lambda:self._finish_discovery(result,None))
        self.app.start_background(worker,label="基金优选联网搜索",cancel=self._network_stop.set)

    def _finish_discovery(self,result,error):
        stopped=self._network_stop.is_set();self._set_network_active(False)
        if error:
            if stopped:
                self.status.config(text="基金搜索已停止；未保存不完整排名。",fg=COLORS["warning"]);return
            self.status.config(text=f"搜索失败：{error}",fg=COLORS["danger"]);messagebox.showerror("基金搜索失败",str(error),parent=self);return
        self.run_id=result.run_id;self.load_run(result.run_id)

    def load_latest(self):
        target=self.current_target()
        if not target:return
        row=self.db.row("SELECT id FROM fund_discovery_runs WHERE universe_id=? ORDER BY created_at DESC LIMIT 1",(target["universe_id"],))
        if row:self.run_id=row["id"];self.load_run(self.run_id)
        else:
            self.run_id=None;self.row_map={};self.tree.delete(*self.tree.get_children());self.detail.delete("1.0","end")
            mapped=InvestableMappingService(self.db).funds_for(target["universe_id"])
            identity=(f"CSI：{target.get('ant_index_code') or '待核验'} {target.get('official_index_name') or '待核验'}　｜　"
                      f"正式代码：{target.get('official_index_code') or '待核验'}　｜　"
                      f"精确行情：{target.get('proxy_code') or '待核验'} {target.get('proxy_name') or ''}")
            self.status.config(text=self.research_context()+"\n"+identity+f"\n已收录 {len(mapped)} 只关联基金；点击联网搜索可刷新硬指标，这一步不调用DeepSeek。",fg=COLORS["accent"] if mapped and self.current_research_run else COLORS["warning"])
            if mapped:
                passive=[f"{r['fund_code']} {r['fund_name']}" for r in mapped if r['strategy_kind']!='指数增强']
                enhanced=[f"{r['fund_code']} {r['fund_name']}" for r in mapped if r['strategy_kind']=='指数增强']
                self.detail.insert("1.0","已核对的可投资身份（不是行情代理）：\n"+identity+"\n\n被动指数基金："+("；".join(passive) or "待补")+"\n指数增强（单独列示，不冒充被动）："+("；".join(enhanced) or "无"))

    def load_run(self,run_id):
        run=self.db.row("SELECT * FROM fund_discovery_runs WHERE id=?",(run_id,));rows=self.db.rows("SELECT * FROM fund_discovery_candidates WHERE run_id=? ORDER BY rank_no",(run_id,))
        self.tree.delete(*self.tree.get_children());self.row_map={}
        for row in rows:
            iid=str(row["id"]);self.row_map[iid]=row
            if row["match_kind"] != "同一指数":
                track="不适用·不同指数"
            elif row.get("tracking_error_pct") is not None:
                track=f"{row['tracking_error_pct']:.2f}%/{row['tracking_days']}日"
            else:
                track=f"{int(row.get('tracking_days') or 0)}日·样本不足"
            cost=f"{float(row['annual_cost_pct']):.2f}%" if row.get("annual_cost_pct") is not None else "待核验"
            horizon=f"{float(row['horizon_cost_pct']):.2f}%" if row.get("horizon_cost_pct") is not None else "资料不全"
            tag="best" if "首选" in row.get("recommendation","") else "exact" if row["match_kind"]=="同一指数" else "similar"
            self.tree.insert("","end",iid=iid,values=(row["rank_no"],row.get("recommendation") or "—",f"{row['fund_code']} {row['fund_name']}",row["target_index"],row.get("strategy_kind") or "被动指数",row["match_kind"],horizon,cost,format_float(row.get("asset_size_billion"),2) if row.get("asset_size_billion") is not None else "—",row["inception_date"] or "—",track,row["buyable_status"],format_float(row["score"],1)),tags=(tag,))
        self.tree.tag_configure("best",foreground=COLORS["primary_dark"]);self.tree.tag_configure("exact",foreground=COLORS["profit"]);self.tree.tag_configure("similar",foreground=COLORS["warning"])
        warning=(run.get("warning") or "同指数候选已按硬指标排序。")
        exact_count=sum(row["match_kind"]=="同一指数" for row in rows)
        self.status.config(text=self.research_context()+f"\n基金数据时间：{run['created_at']}　持有口径：{run['holding_days']}天　同指数{exact_count}只、相似候补{len(rows)-exact_count}只。{warning}\n普通联网负责找基金和算硬指标，DeepSeek不参与排名；绑定只确认跟踪关系，点击“当日可申购复核”后才会形成当天的执行证据。",fg=COLORS["warning"] if run.get("warning") or not self.current_research_run else COLORS["accent"])
        if rows:self.tree.selection_set(str(rows[0]["id"]));self.show_detail()

    def selected(self):
        s=self.tree.selection();return self.row_map.get(s[0]) if s else None

    def show_detail(self,_event=None):
        row=self.selected()
        if not row:return
        self.selected_fund_name.config(text=f"基金完整名称：{row['fund_code']}　{row['fund_name']}")
        self.selected_target_name.config(text=f"实际跟踪指数：{row['target_index']}　｜　份额类别：{row['share_class'] or '待核验'}　｜　策略：{row.get('strategy_kind') or '被动指数'}")
        text=f"结论：{row.get('recommendation') or '—'}\n推荐依据：{row['reason']}\n\n数据边界：基金规模、费率和跟踪标的来自公开基金档案；跟踪误差由公开净值与本地指数收盘序列计算，少于60个重合交易日不参与排名。实际折扣、限购及可买状态以你下单页面为准。\n资料：{row['source_url']}\n费率：{row['fee_url']}"
        self.detail.delete("1.0","end");self.detail.insert("1.0",text)

    def save_pending(self): self._save_mapping(False)
    def bind_verified(self): self._save_mapping(True)

    def _save_mapping(self,verified):
        row=self.selected();target=self.current_target()
        if not row or not target:messagebox.showinfo("请先选择","请先选中一只基金。",parent=self);return
        if verified and row["match_kind"]!="同一指数":
            messagebox.showerror("不能绑定相似指数", "这只基金跟踪的是相似指数，不是当前板块所用的同一指数。可以保存到基金库观察，但不能绑定为该板块的可买基金。", parent=self);return
        if verified:
            warning="你是否已经从基金公开资料确认：它精确跟踪当前CSI指数、属于场外被动C类基金？这一步只绑定跟踪关系，不会冒充今天可申购；下单前还要点击“当日可申购复核”。"
            if row["match_kind"]!="同一指数":warning="这只基金跟踪的是相似指数，并非当前行情代理的同一指数。\n\n"+warning
            if not messagebox.askyesno("确认公开资料已核对",warning,parent=self):return
        identity=InvestableMappingService(self.db).identity_for(target["universe_id"])
        if verified and not identity:
            messagebox.showerror("先核对指数身份","请先点击“查看/核对CSI身份”，确认这只基金究竟跟踪哪个正式指数。",parent=self);return
        now=local_now().isoformat(timespec="seconds")
        status="同指数已绑定-待当日申购复核" if verified else "候补-待核验"
        mapping_status="同指数已核对·待当日申购复核" if verified else "待公开资料核验"
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO funds(fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,purchase_fee_pct,status,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fund_code) DO UPDATE SET fund_name=excluded.fund_name,index_code=excluded.index_code,index_name=excluded.index_name,share_class=excluded.share_class,purchase_fee_pct=excluded.purchase_fee_pct,status=excluded.status,notes=excluded.notes,updated_at=excluded.updated_at""",
                (row["fund_code"],row["fund_name"],(identity or {}).get("official_index_code") or "",row["target_index"],row["share_class"],target["index_name"],float(row.get("purchase_fee_pct") or 0),status,f"基金优选排名{row['rank_no']}；{row['match_kind']}；当日申购状态待轻量联网复核。",now))
            if row["match_kind"]=="同一指数":
                conn.execute("""INSERT INTO fund_mappings(universe_id,index_code,fund_code,fund_name,share_class,buyable_status,source_url,verified_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(universe_id,fund_code) DO UPDATE SET index_code=excluded.index_code,fund_name=excluded.fund_name,share_class=excluded.share_class,buyable_status=excluded.buyable_status,source_url=excluded.source_url,verified_at=excluded.verified_at""",
                    (target["universe_id"],(identity or {}).get("official_index_code") or "",row["fund_code"],row["fund_name"],row["share_class"],mapping_status,row["source_url"],now if verified else ""))
            if identity and row["match_kind"] == "同一指数":
                passive_verified = 1 if (
                    verified
                    and str(row.get("share_class") or "").upper() == "C"
                    and (row.get("strategy_kind") or "被动指数") in {"被动ETF联接", "被动指数"}
                ) else 0
                conn.execute("""INSERT INTO investable_funds(investable_index_id,fund_code,fund_name,strategy_kind,
                    share_class,public_buyable_status,ant_buyable_status,quality_rank,source_url,source_note,verified_at,active,
                    passive_verified,platform_evidence_url,platform_checked_at,radar_eligible,relation_kind)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,0,'同一指数') ON CONFLICT(investable_index_id,fund_code) DO UPDATE SET
                    fund_name=excluded.fund_name,strategy_kind=excluded.strategy_kind,share_class=excluded.share_class,
                    public_buyable_status=excluded.public_buyable_status,ant_buyable_status=excluded.ant_buyable_status,
                    quality_rank=excluded.quality_rank,source_url=excluded.source_url,verified_at=excluded.verified_at,active=1,
                    passive_verified=excluded.passive_verified,platform_evidence_url=excluded.platform_evidence_url,
                    platform_checked_at=excluded.platform_checked_at,radar_eligible=0,relation_kind='同一指数'""",
                    (identity["id"],row["fund_code"],row["fund_name"],row.get("strategy_kind") or "被动指数",row["share_class"],"待当日申购复核",
                     "不作为CSI准入条件",row["rank_no"],row["source_url"],"基金优选公开资料整理",now if verified else "",
                     passive_verified,"",""))
        if identity:
            check = PlatformCatalogService(self.db).refresh_admission(int(identity["id"]))
        else:
            check = {"admitted": 0}
        self.app.refresh_all()
        result_text=f"{row['fund_code']} 已保存为“{mapping_status}”。" if row["match_kind"]=="同一指数" else f"{row['fund_code']} 已保存到基金库作为相似指数候补，没有绑定到当前板块。"
        if verified and (row.get("strategy_kind") or "被动指数") not in {"被动ETF联接", "被动指数"}:
            result_text += "\n这是一只增强或主动产品，已单列保存，但不会使指数进入雷达。"
        elif verified:
            result_text += "\n跟踪关系已保存；它还不是当日执行证据，请点击“当日可申购复核”。"
        messagebox.showinfo("保存完成",result_text,parent=self)

    def recheck_buyability(self):
        if self._network_active:
            messagebox.showinfo("正在联网","基金搜索或当日复核仍在运行，可点击停止。",parent=self);return
        row=self.selected();target=self.current_target()
        if not row or not target:
            messagebox.showinfo("请先选择","请先选中一只基金。",parent=self);return
        if row.get("match_kind")!="同一指数":
            messagebox.showerror("不能复核相似指数","相似指数基金不能作为当前指数的执行基金。",parent=self);return
        identity=InvestableMappingService(self.db).identity_for(target["universe_id"])
        if not identity:
            messagebox.showerror("先核对指数身份","请先核对CSI身份并绑定同指数基金。",parent=self);return
        saved=self.db.row("SELECT fund_code FROM investable_funds WHERE investable_index_id=? AND fund_code=? AND active=1",(identity["id"],row["fund_code"]))
        if not saved:
            messagebox.showinfo("先绑定基金","请先点击“确认同指数并绑定”，再进行当日可申购复核。",parent=self);return
        try:
            intended_amount=float(self.amount_var.get().strip() or 0)
            if intended_amount<0:raise ValueError
        except ValueError:
            messagebox.showerror("金额错误","拟买金额必须是0或正数；它只用于限购额度核对。",parent=self);return
        self._set_network_active(True);self._network_stop.clear()
        self.network_progress.configure(mode="indeterminate");self.network_progress.start(12)
        self.status.config(text=f"正在轻量联网复核 {row['fund_code']} 今天是否公开可申购……",fg=COLORS["info"])

        def worker():
            try:
                service=FundDiscoveryService(self.db)
                evidence=service.public_execution_snapshot(row["fund_code"])
                result=PlatformCatalogService(self.db).revalidate_fund_execution(
                    int(identity["id"]),row["fund_code"],checker=lambda _fund:evidence,
                    intended_amount=intended_amount,
                )
            except Exception as exc:
                self.after(0,lambda error=exc:self._finish_execution_check(None,error))
            else:
                self.after(0,lambda:self._finish_execution_check(result,None))
        self.app.start_background(worker,label="基金执行资格复核",cancel=self._network_stop.set)

    def _finish_execution_check(self,result,error):
        stopped=self._network_stop.is_set();self._set_network_active(False)
        if stopped and error is None:
            self.status.config(text="停止请求在当前单只基金复核结束后生效；本次证据已安全保存。",fg=COLORS["warning"])
        if error:
            if stopped:
                self.status.config(text="当日申购复核已停止。",fg=COLORS["warning"]);return
            self.status.config(text=f"当日申购复核失败：{error}",fg=COLORS["danger"])
            messagebox.showerror("当日复核失败",str(error),parent=self);return
        self.app.refresh_all()
        text=f"复核结论：{result.state}\n复核时间：{result.checked_at or '无'}\n有效至：{result.expires_at or '无'}"
        if result.reasons:text+="\n原因："+"；".join(result.reasons)
        if result.executable:
            messagebox.showinfo("当日可申购复核通过",text,parent=self)
        else:
            messagebox.showwarning("当前不可执行",text,parent=self)

    def _set_network_active(self,active):
        self._network_active=bool(active)
        self.search_button.set_enabled(not active);self.execution_button.set_enabled(not active)
        self.network_stop_button.set_enabled(active)
        if not active:
            self.network_progress.stop();self.network_progress.configure(mode="determinate")

    def _update_network_progress(self,done,total,label):
        self.network_progress.configure(mode="determinate",maximum=max(1,total),value=done)
        self.status.config(text=f"基金联网核验 {done}/{total}｜{label}；停止会在当前请求结束后生效。",fg=COLORS["info"])

    def stop_network(self):
        if not self._network_active:return
        self._network_stop.set();self.network_stop_button.set_enabled(False)
        self.status.config(text="已请求停止；当前网络请求结束后不再继续。",fg=COLORS["warning"])

    def open_source(self):
        row=self.selected()
        if row:webbrowser.open(row["source_url"])


class FundPage(BasePage):
    title = "🔭 基金库"
    subtitle = "代码、完整名称、跟踪指数和阶梯费率是本地权威数据；AI只能提出待核验更新。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._fee_sync_active = False
        self._fund_task_active=False
        self._fund_task_stop=threading.Event()
        bar = tk.Frame(self.body, bg=COLORS["bg"]); bar.pack(fill="x", pady=(0, 8))
        CuteButton(bar, "📌 添加基金", self.add).pack(side="left")
        CuteButton(bar, "✏️ 编辑选中", self.edit, "secondary").pack(side="left", padx=8)
        CuteButton(bar, "🧾 设置赎回费", self.edit_fees, "secondary").pack(side="left")
        self.fee_sync_button=CuteButton(bar,"🌐 补全缺失赎回费",self.sync_missing_fees,"success");self.fee_sync_button.pack(side="left",padx=8)
        self.name_sync_button=CuteButton(bar, "🌐 按代码补全基金名称", self.resolve_names, "success")
        self.name_sync_button.pack(side="left", padx=8)
        self.fund_stop_button=CuteButton(bar,"⏹ 停止联网",self.stop_fund_task,"danger")
        self.fund_stop_button.pack(side="left");self.fund_stop_button.set_enabled(False)
        CuteButton(bar, "🔍 生成基金核查任务", lambda: self.app.generate_task("fund_check"), "success").pack(side="right")
        cols = ("code", "name", "index", "bucket", "buyfee", "fees", "status")
        self.tree = ttk.Treeview(self.body, columns=cols, show="headings", height=20)
        for key,label,width in [("code","代码",80),("name","基金全名",260),("index","跟踪指数",180),("bucket","相关性桶",130),("buyfee","买入费",75),("fees","赎回费阶梯",240),("status","状态",100)]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width,anchor="w" if key in {"name","index","fees"} else "center")
        self.tree.pack(fill="both", expand=True); self.tree.bind("<Double-1>", lambda _e:self.edit())
        self.fund_task_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.fund_task_progress.pack(fill="x",pady=(7,3))
        self.fund_task_status=tk.Label(self.body,text="",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),anchor="w")
        self.fund_task_status.pack(fill="x")

    def refresh(self):
        self.tree.delete(*self.tree.get_children()); self.map={}
        for f in self.db.rows("SELECT * FROM funds ORDER BY fund_code"):
            tiers=self.db.rows("SELECT * FROM fee_tiers WHERE fund_code=? ORDER BY min_days",(f["fund_code"],))
            fee_text="；".join(f"{t['min_days']}-{t['max_days'] if t['max_days'] is not None else '∞'}天 {t['redeem_fee_pct']}%" for t in tiers) or "未设置"
            self.map[f["fund_code"]]=f
            self.tree.insert("","end",iid=f["fund_code"],values=(f["fund_code"],f["fund_name"],f"{f['index_code']} {f['index_name']}".strip(),f["correlation_bucket"],f"{f['purchase_fee_pct']}%",fee_text,f["status"]))

    def selected(self):
        s=self.tree.selection(); return self.map.get(s[0]) if s else None

    def resolve_names(self):
        if self._fund_task_active:
            messagebox.showinfo("正在联网","基金名称或费率核验仍在运行，可点击停止。",parent=self);return
        selected=self.selected()
        self._set_fund_task_active(True);self._fund_task_stop.clear();self.fund_task_progress["value"]=0
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_fund_task_progress(d,t,f"基金名称｜{value}"))
        def worker():
            try:
                if selected:
                    result=(1,[]) if FundMetadataService(self.db).resolve_one(selected["fund_code"]) else (0,[])
                    progress(1,1,selected["fund_code"])
                else:
                    result=FundMetadataService(self.db).resolve_pending(
                        30,stop_requested=self._fund_task_stop.is_set,progress=progress,
                    )
            except Exception as exc:self.after(0,lambda error=exc:self._finish_resolve_names(None,error))
            else:self.after(0,lambda:self._finish_resolve_names(result,None))
        self.app.start_background(worker,label="基金名称补全",cancel=self._fund_task_stop.set)

    def _finish_resolve_names(self,result,error):
        stopped=self._fund_task_stop.is_set();self._set_fund_task_active(False)
        if error:
            if stopped:self.fund_task_status.config(text="基金名称补全已停止。",fg=COLORS["warning"])
            else:messagebox.showerror("名称补全失败",str(error),parent=self)
            return
        self.app.refresh_all();resolved,errors=result
        self.fund_task_status.config(text=("名称补全已停止" if stopped else "名称补全完成")+f"｜已识别 {resolved} 只",fg=COLORS["warning"] if stopped else COLORS["accent"])
        messagebox.showinfo("名称补全已停止" if stopped else "名称补全完成",f"已识别 {resolved} 只基金。"+("\n"+"\n".join(errors[:5]) if errors else ""),parent=self)

    def sync_missing_fees(self):
        if self._fund_task_active:
            messagebox.showinfo("正在联网","赎回费阶梯正在核验，请稍候。",parent=self);return
        selected=self.selected();codes=[selected["fund_code"]] if selected else None
        self._fee_sync_active=True;self._fund_task_stop.clear();self._set_fund_task_active(True);self.fund_task_progress["value"]=0
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_fund_task_progress(d,t,f"赎回费｜{value}"))
        def worker():
            try:result=FundFeeSyncService(self.db).sync(
                codes,only_missing=selected is None,
                stop_requested=self._fund_task_stop.is_set,progress=progress,
            )
            except Exception as exc:self.after(0,lambda error=exc:self._finish_fee_sync(None,error))
            else:self.after(0,lambda:self._finish_fee_sync(result,None))
        self.app.start_background(worker,label="赎回费阶梯同步",cancel=self._fund_task_stop.set)

    def _finish_fee_sync(self,result,error):
        stopped=self._fund_task_stop.is_set() or bool((result or {}).get("stopped"))
        self._fee_sync_active=False;self._set_fund_task_active(False)
        if error:messagebox.showerror("费率核验失败",str(error),parent=self);return
        self.refresh()
        text=f"共检查 {result['total']} 只；新补全 {result['updated']} 只；已有阶梯跳过 {result['skipped']} 只。"
        if stopped:text="已按你的要求停止；已完成费率均已保存。\n"+text
        if result["errors"]:text+="\n\n仍需人工复核：\n"+"\n".join(result["errors"][:10])
        messagebox.showwarning("费率核验完成",text,parent=self) if result["errors"] else messagebox.showinfo("费率核验已停止" if stopped else "费率核验完成",text,parent=self)

    def _set_fund_task_active(self,active):
        self._fund_task_active=bool(active)
        self.name_sync_button.set_enabled(not active);self.fee_sync_button.set_enabled(not active)
        self.fund_stop_button.set_enabled(active)

    def _update_fund_task_progress(self,done,total,label):
        self.fund_task_progress["maximum"]=max(1,total);self.fund_task_progress["value"]=done
        self.fund_task_status.config(text=f"联网进度 {done}/{total}｜{label}；停止会在当前请求结束后生效。",fg=COLORS["info"])

    def stop_fund_task(self):
        if not self._fund_task_active:return
        self._fund_task_stop.set();self.fund_stop_button.set_enabled(False)
        self.fund_task_status.config(text="已请求停止；当前网络请求结束后不再处理下一只基金。",fg=COLORS["warning"])

    def add(self): self._form(None)
    def edit(self):
        f=self.selected()
        if not f: messagebox.showinfo("请先选择","请先选中一只基金。",parent=self); return
        self._form(f)

    def _form(self, f):
        f=f or {}
        fields=[("基金代码","fund_code",f.get("fund_code","")),("基金完整名称","fund_name",f.get("fund_name","")),("指数代码","index_code",f.get("index_code","")),
                ("指数名称","index_name",f.get("index_name","")),("份额类别","share_class",f.get("share_class","")),("相关性桶","correlation_bucket",f.get("correlation_bucket","")),
                ("买入费 %","purchase_fee_pct",f.get("purchase_fee_pct",0)),("状态","status",f.get("status","可买")),("备注","notes",f.get("notes",""))]
        d=FormDialog(self,"✏️ 基金资料",fields,choices={"status":["可买","持有","定投","观察","AI建议-待核验","不可买"]})
        if not d.result:return
        v=d.result; code=v["fund_code"].strip()
        if len(code)!=6 or not code.isdigit(): messagebox.showerror("代码错误","基金代码必须是6位数字。",parent=self);return
        try: buyfee=float(v["purchase_fee_pct"] or 0)
        except ValueError: messagebox.showerror("费率错误","买入费必须是数字。",parent=self);return
        old=f.get("fund_code")
        if old and old!=code: messagebox.showerror("不能改代码","如基金代码错误，请新建正确记录。",parent=self);return
        try:
            self.db.execute("""INSERT INTO funds(fund_code,fund_name,index_code,index_name,share_class,correlation_bucket,purchase_fee_pct,status,notes,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fund_code) DO UPDATE SET fund_name=excluded.fund_name,index_code=excluded.index_code,index_name=excluded.index_name,
                share_class=excluded.share_class,correlation_bucket=excluded.correlation_bucket,purchase_fee_pct=excluded.purchase_fee_pct,status=excluded.status,notes=excluded.notes,updated_at=excluded.updated_at""",
                (code,v["fund_name"].strip(),v["index_code"].strip(),v["index_name"].strip(),v["share_class"].strip(),v["correlation_bucket"].strip(),buyfee,v["status"],v["notes"],local_now().isoformat(timespec="seconds")))
        except Exception as exc: messagebox.showerror("保存失败",str(exc),parent=self);return
        self.refresh()

    def edit_fees(self):
        f=self.selected()
        if not f: messagebox.showinfo("请先选择","请先选中一只基金。",parent=self);return
        tiers=self.db.rows("SELECT * FROM fee_tiers WHERE fund_code=? ORDER BY min_days",(f["fund_code"],))
        initial="\n".join(f"{t['min_days']},{'' if t['max_days'] is None else t['max_days']},{t['redeem_fee_pct']}" for t in tiers)
        d=TextDialog(self,f"🧾 {f['fund_code']} 赎回费阶梯", "每行格式：最少持有天数,最多持有天数,赎回费%\n最多天数留空表示无限，例如：\n0,6,1.5\n7,29,0.1\n30,,0", initial)
        if d.result is None:return
        parsed=[]
        try:
            for line in d.result.splitlines():
                if not line.strip():continue
                parts=[x.strip() for x in line.split(",")]
                if len(parts)!=3: raise ValueError(f"格式错误：{line}")
                parsed.append((f["fund_code"],int(parts[0]),int(parts[1]) if parts[1] else None,float(parts[2])))
        except ValueError as exc: messagebox.showerror("费率格式错误",str(exc),parent=self);return
        with self.db.connect() as conn:
            conn.execute("DELETE FROM fee_tiers WHERE fund_code=?",(f["fund_code"],)); conn.executemany("INSERT INTO fee_tiers(fund_code,min_days,max_days,redeem_fee_pct,source_url,checked_at) VALUES(?,?,?,?,?,?)",[(code,minimum,maximum,rate,"用户手工核对",local_now().isoformat(timespec="seconds")) for code,minimum,maximum,rate in parsed])
        self.refresh()


class HoldingPage(BasePage):
    title = "💰 当前持仓"
    subtitle = "一键更新本段持仓以来的正式净值和每日盈亏；各基金从自己的持仓起点计算，不把收盘净值冒充盘中实时价。"

    def __init__(self,parent,app):
        super().__init__(parent,app)
        self._nav_sync_active=False
        self._nav_sync_stop=threading.Event()
        bar=tk.Frame(self.body,bg=COLORS["bg"]);bar.pack(fill="x",pady=(0,8))
        CuteButton(bar,"➕ 新买入请到交易日志",self.add).pack(side="left");CuteButton(bar,"✏️ 修正持仓资料",self.edit,"secondary").pack(side="left",padx=8)
        CuteButton(bar,"💸 加入卖出草稿",self.sell,"danger").pack(side="left")
        self.sync_button=CuteButton(bar,"🔄 更新本段持仓净值/盈亏",self.sync_recent_navs,"success")
        self.sync_button.pack(side="left",padx=8)
        self.nav_stop_button=CuteButton(bar,"⏹ 停止净值更新",self.stop_nav_sync,"danger")
        self.nav_stop_button.pack(side="left");self.nav_stop_button.set_enabled(False)
        self.summary=tk.Label(bar,text="",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["body"],"bold"));self.summary.pack(side="right")
        cols=("fund","capital","sleeve","date","days","units","nav","value","fee","pnl","return","risk")
        self.tree=ttk.Treeview(self.body,columns=cols,show="headings",height=8)
        for key,label,width in [("fund","基金",230),("capital","主动资金口径",110),("sleeve","资金桶",70),("date","本段起点",90),("days","天数",55),("units","份额",90),("nav","净值",75),("value","市值",100),("fee","赎回费",80),("pnl","费后盈亏",100),("return","费后收益",85),("risk","风控",100)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,anchor="w" if key=="fund" else "center")
        self.tree.pack(fill="x",expand=False);self.tree.bind("<Double-1>",lambda _e:self.edit())
        history_header=tk.Frame(self.body,bg=COLORS["bg"]);history_header.pack(fill="x",pady=(12,5))
        tk.Label(history_header,text="本段持仓以来的正式净值与每日盈亏（按当日实际累计份额）",bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["section"],"bold")).pack(side="left")
        self.sync_status=tk.Label(history_header,text="",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]));self.sync_status.pack(side="right")
        self.nav_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.nav_progress.pack(fill="x",pady=(0,7))
        daily_cols=("nav_date","fund","nav","change","daily_pnl","value","total_pnl","source")
        daily_frame=tk.Frame(self.body,bg=COLORS["bg"]);daily_frame.pack(fill="both",expand=True)
        daily_scroll=ttk.Scrollbar(daily_frame,orient="vertical")
        self.daily_tree=ttk.Treeview(daily_frame,columns=daily_cols,show="headings",height=9,yscrollcommand=daily_scroll.set)
        daily_scroll.config(command=self.daily_tree.yview);daily_scroll.pack(side="right",fill="y")
        for key,label,width in [("nav_date","净值日期",100),("fund","基金",250),("nav","单位净值",90),("change","当日涨跌",90),("daily_pnl","当日盈亏",100),("value","当日持仓市值",130),("total_pnl","当日累计盈亏",120),("source","计算口径",150)]:
            self.daily_tree.heading(key,text=label);self.daily_tree.column(key,width=width,anchor="w" if key in {"fund","source"} else "center")
        self.daily_tree.pack(side="left",fill="both",expand=True)

    def refresh(self):
        self.tree.delete(*self.tree.get_children());self.map={};total=cost=0
        settings=self.db.settings()
        rows=self.db.rows("""SELECT h.*,f.fund_name,f.purchase_fee_pct FROM holdings h JOIN funds f USING(fund_code) WHERE h.active=1 ORDER BY h.sleeve,h.fund_code""")
        for h in rows:
            tiers=self.db.rows("SELECT * FROM fee_tiers WHERE fund_code=? ORDER BY min_days",(h["fund_code"],))
            lots=self.db.rows("SELECT * FROM cost_lots WHERE fund_code=? AND remaining_units>0 ORDER BY confirmed_date,id",(h["fund_code"],))
            market_value=float(h["units"] or 0)*float(h["latest_nav"] or 0)
            try:
                e=estimate_sale_by_lots(units=h["units"],cost=h["cost"],latest_nav=h["latest_nav"],confirmed_date=h["confirmed_date"],tiers=tiers,lots=lots,purchase_fee_pct=h["purchase_fee_pct"])
            except UnknownFeeScheduleError:
                e=None
            h["estimate"]=e;self.map[str(h["id"])]=h;total+=market_value;cost+=h["cost"]
            days=max(1,(local_today()-date.fromisoformat(str(h["confirmed_date"]))).days+1)
            if e is None:
                values=(f"{h['fund_code']} {h['fund_name']}","计入" if int(h.get("counts_toward_active_capital") or 0) else "不计入",h["sleeve"],h["confirmed_date"],days,format_float(h["units"]),format_float(h["latest_nav"],4),money(market_value),"待核验","待费率","—","先补赎回费")
                tag="normal"
            else:
                risk=evaluate_holding(e,settings)
                values=(f"{h['fund_code']} {h['fund_name']}","计入" if int(h.get("counts_toward_active_capital") or 0) else "不计入",h["sleeve"],h["confirmed_date"],e.holding_days,format_float(h["units"]),format_float(h["latest_nav"],4),money(e.market_value),f"{e.redeem_fee_pct:.2f}%",money(e.net_pnl),pct(e.net_return_pct),risk.decision)
                tag="profit" if e.net_pnl>=0 else "loss"
            self.tree.insert("","end",iid=str(h["id"]),values=values,tags=(tag,))
        self.tree.tag_configure("profit",foreground=COLORS["profit"]);self.tree.tag_configure("loss",foreground=COLORS["loss"])
        nav_dates=sorted({str(row.get("nav_date") or "") for row in rows if row.get("nav_date")})
        date_text=(nav_dates[0] if len(nav_dates)==1 else f"{nav_dates[0]}～{nav_dates[-1]}") if nav_dates else "未更新"
        self.summary.config(text=f"最新正式净值市值 {money(total)}　浮动 {money(total-cost)}　净值日 {date_text}")
        self._refresh_daily_history()

    def _refresh_daily_history(self):
        self.daily_tree.delete(*self.daily_tree.get_children())
        for row in FundValuationService(self.db).daily_rows():
            daily_pnl=row["daily_pnl_current_units"]
            change=row["daily_change_pct"]
            tag="profit" if daily_pnl is not None and daily_pnl>=0 else "loss" if daily_pnl is not None else "normal"
            self.daily_tree.insert("","end",values=(
                row["nav_date"],f"{row['fund_code']} {row['fund_name']}",format_float(row["nav"],4),
                "—" if change is None else pct(change),"—" if daily_pnl is None else money(daily_pnl),
                money(row["market_value_current_units"]),money(row["pnl_vs_current_cost"]),row["valuation_basis"],
            ),tags=(tag,))
        self.daily_tree.tag_configure("profit",foreground=COLORS["profit"])
        self.daily_tree.tag_configure("loss",foreground=COLORS["loss"])
        last=self.db.setting("fund_nav_last_sync_at","")
        self.sync_status.config(text=f"上次按键核算：{last or '尚未执行'}")

    def sync_recent_navs(self):
        if self._nav_sync_active:
            messagebox.showinfo("正在更新","持仓期净值仍在后台更新，可点击停止。",parent=self);return
        self._nav_sync_active=True;self._nav_sync_stop.clear()
        self.sync_button.set_enabled(False);self.nav_stop_button.set_enabled(True);self.nav_progress["value"]=0
        self.sync_status.config(text="正在从每只基金本段持仓起点下载正式净值……")
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_nav_progress(d,t,value))
        def worker():
            try:
                result=FundValuationService(self.db).sync_holding_period(
                    stop_requested=self._nav_sync_stop.is_set,progress=progress,
                )
            except Exception as exc:
                self.after(0,lambda error=exc:self._finish_nav_sync(None,error))
                return
            self.after(0,lambda:self._finish_nav_sync(result,None))
        self.app.start_background(worker,label="持仓期净值核算",cancel=self._nav_sync_stop.set)

    def _update_nav_progress(self,done,total,label):
        self.nav_progress["maximum"]=max(1,total);self.nav_progress["value"]=done
        self.sync_status.config(text=f"持仓净值 {done}/{total}｜{label}；停止会在当前请求结束后生效。",fg=COLORS["info"])

    def stop_nav_sync(self):
        if not self._nav_sync_active:return
        self._nav_sync_stop.set();self.nav_stop_button.set_enabled(False)
        self.sync_status.config(text="已请求停止；当前基金完成后不再下载下一只。",fg=COLORS["warning"])

    def _finish_nav_sync(self,result,error):
        self._nav_sync_active=False
        self.sync_button.set_enabled(True);self.nav_stop_button.set_enabled(False)
        if error:
            messagebox.showerror("净值更新失败",str(error),parent=self)
            self.sync_status.config(text="更新失败；原有净值未被伪造或清空")
            return
        self.refresh();self.app.refresh_all()
        summary=result["summary"]
        stopped=bool(result.get("stopped"))
        text=(f"已更新 {result['funds_updated']}/{result['funds_total']} 只基金，保存 {result['nav_rows_saved']} 条正式净值。\n"
              f"按键时点：{summary['captured_at']}\n最新正式净值市值：{money(summary['official_value'])}\n"
              f"相对当前成本浮动盈亏：{money(summary['official_pnl'])}\n"
              f"净值日期范围：{summary['oldest_nav_date'] or '—'} ～ {summary['latest_nav_date'] or '—'}\n\n"
              "这是截至按键时点能取得的最新正式净值核算，不是开放式基金盘中实时成交价。")
        if stopped:
            text="已按你的要求停止；已完成基金的正式净值均已保存。\n"+text
            messagebox.showinfo("净值更新已停止",text,parent=self)
        elif result["errors"]:
            text += "\n\n部分失败：\n" + "\n".join(result["errors"][:5])
            messagebox.showwarning("净值部分更新",text,parent=self)
        else:
            messagebox.showinfo("净值与盈亏已更新",text,parent=self)

    def selected(self):
        s=self.tree.selection();return self.map.get(s[0]) if s else None

    def add(self):
        messagebox.showinfo("先记录交易","为保证现金、持仓和成本批次一致，不能在这里直接造一笔新持仓。请在“交易日志”新建待确认买入，补齐支付宝正式数据后系统会自动增加持仓。",parent=self)
        self.app.show("trades")
    def edit(self):
        h=self.selected()
        if not h:messagebox.showinfo("请先选择","请先选择一笔持仓。",parent=self);return
        self._form(h)
    def _form(self,h):
        fields=[("基金（只读说明）","fund_label",f"{h['fund_code']} {h['fund_name']}"),("最新正式净值","latest_nav",h.get("latest_nav",0)),("净值日期","nav_date",h.get("nav_date",local_today().isoformat())),
                ("资金桶","sleeve",h.get("sleeve","主动")),("主动资金口径","capital_scope","计入主动资金" if int(h.get("counts_toward_active_capital") or 0) else "不计入主动资金"),("买入逻辑/备注","thesis",h.get("thesis",""))]
        d=FormDialog(self,"💰 修正持仓显示资料",fields,choices={"sleeve":["主动","防守","定投","其他"],"capital_scope":["计入主动资金","不计入主动资金"]})
        if not d.result:return
        v=d.result
        try:latest_nav=float(v["latest_nav"]);nav_day=date.fromisoformat(v["nav_date"])
        except ValueError:messagebox.showerror("格式错误","日期使用YYYY-MM-DD，净值使用数字。",parent=self);return
        if latest_nav<=0 or not ChinaTradingCalendar.from_db(self.db).is_trading_day(nav_day):
            messagebox.showerror("净值资料不可能","正式净值必须大于0，净值日期必须是交易日。",parent=self);return
        new_counts=1 if v["capital_scope"]=="计入主动资金" else 0
        try:PortfolioLedgerService(self.db).reclassify_holding(h["id"],new_counts,"用户在持仓资料中修改资金分类")
        except LedgerError as exc:messagebox.showerror("资金分类修改失败",str(exc),parent=self);return
        self.db.execute("UPDATE holdings SET latest_nav=?,nav_date=?,sleeve=?,thesis=? WHERE id=?",(latest_nav,v["nav_date"],v["sleeve"],v["thesis"],h["id"]))
        self.refresh()

    def sell(self):
        h=self.selected()
        if not h:messagebox.showinfo("请先选择","请先选择要拟卖出的持仓。",parent=self);return
        e=h.get("estimate")
        estimated=float(e.net_proceeds) if e is not None else float(h["units"])*float(h["latest_nav"])
        pricing=ChinaTradingCalendar.from_db(self.db).resolve_order(local_now(),fund_kind=infer_fund_kind(h.get("fund_name","") or "")).pricing_date.isoformat()
        fields=[("操作","action","卖出"),("拟卖出到账金额 元","amount",round(estimated,2)),("预计净值交易日","pricing_date",pricing),("我的操作理由","rationale","盘中拟卖；最终以支付宝确认份额、净值、费用和到账金额为准")]
        d=FormDialog(self,"💸 加入卖出草稿",fields,choices={"action":["卖出","清仓"]})
        if not d.result:return
        try:
            self.draft_service=DecisionDraftService(self.db)
            self.draft_service.add_manual(
                fund_code=h["fund_code"],fund_name=h["fund_name"],action=d.result["action"],
                planned_amount=float(d.result["amount"] or 0),pricing_date=d.result["pricing_date"],
                rationale=d.result["rationale"],
                counts_toward_active_capital=int(h.get("counts_toward_active_capital") or 0),
            )
        except (ValueError,DecisionDraftError) as exc:messagebox.showerror("无法加入草稿",str(exc),parent=self);return
        self.app.refresh_all();messagebox.showinfo("已加入盘中草稿","当前持仓和成本批次没有改变。收盘后先写为待确认；只有你补齐支付宝实际份额、净值、费用和到账金额并确认后，才会按FIFO减少持仓。",parent=self)


class TradePage(BasePage):
    title="📒 交易日志"
    subtitle="记录AI建议、你的实际决定和最终结果；软件不会替你在支付宝下单。"
    def __init__(self,parent,app):
        super().__init__(parent,app)
        self._online_fill_active=False
        self._online_fill_stop=threading.Event()
        flow=tk.Frame(self.body,bg=COLORS["bg"]);flow.pack(fill="x",pady=(0,8))
        for title,hint,color in [
            ("① 联网补公开数据","补净值和估算；不会确认",COLORS["info"]),
            ("② 核对支付宝结果","逐笔填写实际金额、份额、费用和确认日",COLORS["primary"]),
            ("③ 确认后自动过账","同步持仓、成本批次与主动现金",COLORS["accent"]),
        ]:
            card=Card(flow,bg="#FFFDFC");card.pack(side="left",fill="x",expand=True,padx=(0,7))
            tk.Label(card,text=title,bg="#FFFDFC",fg=color,font=(FONT,FONT_SIZE["small"],"bold"),anchor="w").pack(fill="x",padx=10,pady=(7,1))
            tk.Label(card,text=hint,bg="#FFFDFC",fg=COLORS["muted"],font=(FONT,FONT_SIZE["hint"]),anchor="w",wraplength=420).pack(fill="x",padx=10,pady=(0,7))
        bar=tk.Frame(self.body,bg=COLORS["bg"]);bar.pack(fill="x",pady=(0,8))
        CuteButton(bar,"📝 新建待确认交易",self.add).pack(side="left")
        CuteButton(bar,"✅ 补全/确认选中",self.confirm_selected,"success").pack(side="left",padx=7)
        CuteButton(bar,"↩️ 撤销已入账交易",self.reverse_selected,"danger").pack(side="left")
        CuteButton(bar,"🧾 更正已入账费用",self.correct_posted_fee,"secondary").pack(side="left",padx=(7,0))
        self.online_fill_button=CuteButton(bar,"🌐 联网取净值/补全估算",self.online_fill_selected,"secondary")
        self.online_fill_button.pack(side="left",padx=(7,0))
        self.bulk_fill_button=CuteButton(bar,"🌐 联网补全全部待确认",self.online_fill_all_pending,"secondary")
        self.bulk_fill_button.pack(side="left",padx=(7,0))
        self.online_stop_button=CuteButton(bar,"⏹ 停止补全",self.stop_online_fill,"danger")
        self.online_stop_button.pack(side="left",padx=7);self.online_stop_button.set_enabled(False)
        CuteButton(bar,"📊 导出CSV",self.export,"secondary").pack(side="right")
        self.pending_status=tk.Label(self.body,text="",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),anchor="w")
        self.pending_status.pack(fill="x",pady=(0,6))
        self.pending_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.pending_progress.pack(fill="x",pady=(0,6))
        cols=("date","confirm","fund","action","amount","units","nav","fee","status","source","pnl","reason")
        table=tk.Frame(self.body,bg=COLORS["bg"]);table.pack(fill="both",expand=True)
        table.columnconfigure(0,weight=1);table.rowconfigure(0,weight=1)
        table_y=ttk.Scrollbar(table,orient="vertical")
        table_x=ttk.Scrollbar(table,orient="horizontal")
        self.tree=ttk.Treeview(
            table,columns=cols,show="headings",height=12,
            yscrollcommand=table_y.set,xscrollcommand=table_x.set,
        )
        table_y.config(command=self.tree.yview);table_x.config(command=self.tree.xview)
        for key,label,width in [("date","交易日",100),("confirm","确认日",100),("fund","基金代码＋全名",270),("action","操作",75),("amount","金额",95),("units","份额",90),("nav","净值",85),("fee","费用",75),("status","状态",90),("source","来源",125),("pnl","结果盈亏",100),("reason","理由",250)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,anchor="w" if key in {"fund","reason","source"} else "center")
        self.tree.grid(row=0,column=0,sticky="nsew");table_y.grid(row=0,column=1,sticky="ns");table_x.grid(row=1,column=0,sticky="ew")
        self.tree.bind("<<TreeviewSelect>>",self.show_trade_detail)
        detail=Card(self.body,bg="#FFFDFC");detail.pack(fill="x",pady=(8,0))
        self.trade_detail_title=tk.Label(
            detail,text="选择一笔交易，查看完整资料和下一步",bg="#FFFDFC",fg=COLORS["info"],
            font=(FONT,FONT_SIZE["small"],"bold"),anchor="w",
        );self.trade_detail_title.pack(fill="x",padx=10,pady=(7,2))
        self.trade_detail_text=tk.Text(
            detail,height=5,wrap="word",bg="#FFFDFC",fg=COLORS["text"],
            font=(FONT,FONT_SIZE["hint"]),relief="flat",bd=0,padx=8,pady=2,
            selectbackground="#F2CBD4",cursor="arrow",
        );self.trade_detail_text.pack(fill="x",padx=2)
        self.trade_detail_next=tk.Label(
            detail,text="",bg="#EEF8F4",fg=COLORS["accent"],font=(FONT,FONT_SIZE["hint"],"bold"),
            anchor="w",justify="left",wraplength=1500,padx=10,pady=6,
        );self.trade_detail_next.pack(fill="x",padx=1,pady=(1,1))
        self.trade_detail_next.bind(
            "<Configure>",lambda event:self.trade_detail_next.config(wraplength=max(260,event.width-20)),
        )
        self._set_trade_detail()
    def refresh(self):
        previous=self.tree.selection()
        previous_id=previous[0] if previous else ""
        self.tree.delete(*self.tree.get_children())
        self.trade_map={}
        pending_total=ready_total=0
        for r in self.db.rows("SELECT t.*,f.fund_name FROM trades t LEFT JOIN funds f USING(fund_code) ORDER BY trade_date DESC,t.id DESC"):
            iid=str(r["id"]);self.trade_map[iid]=r
            is_pending=(r.get("status") or "已确认")=="待确认"
            if is_pending:
                pending_total+=1
                if float(r.get("nav") or 0)>0:ready_total+=1
            tag="ready" if is_pending and float(r.get("nav") or 0)>0 else "pending" if is_pending else "confirmed"
            pnl_text=(money(r["result_pnl"]) if r.get("result_pnl") is not None else "待核算")
            self.tree.insert("","end",iid=iid,values=(r["trade_date"],r.get("confirm_date","") or "—",f"{r['fund_code']} {r['fund_name'] or ''}",r["action"],money(r["amount"]),format_float(r["units"]),format_float(r["nav"],4),money(r["fee"]),r.get("status","") or "已确认",r.get("source","") or "手工录入",pnl_text,r["rationale"]),tags=(tag,))
        self.tree.tag_configure("pending",foreground=COLORS["warning"])
        self.tree.tag_configure("ready",foreground=COLORS["accent"])
        self.tree.tag_configure("confirmed",foreground=COLORS["text"])
        self.pending_status.config(text=(
            f"待确认 {pending_total} 笔　｜　已取得公开净值、等待你核对 {ready_total} 笔　｜　"
            f"尚待净值/资料 {max(0,pending_total-ready_total)} 笔。绿色仅表示可核对，不表示已经成交入账。"
        ))
        children=self.tree.get_children()
        target=previous_id if previous_id in self.trade_map else (children[0] if children else "")
        if target:
            self.tree.selection_set(target);self.tree.focus(target);self.tree.see(target)
            self.show_trade_detail()
        else:self._set_trade_detail()

    def _set_trade_detail(self,title="选择一笔交易，查看完整资料和下一步",facts="",context="",next_kind="muted",next_text=""):
        self.trade_detail_title.config(text=title)
        self.trade_detail_text.config(state="normal")
        self.trade_detail_text.delete("1.0","end")
        if facts or context:self.trade_detail_text.insert("1.0",facts+("\n" if facts and context else "")+context)
        else:self.trade_detail_text.insert("1.0","表格保留摘要；选中后会在这里完整显示长基金名、来源、理由、账本状态和下一步。")
        self.trade_detail_text.config(state="disabled")
        palette={
            "locked":("#EEF3FA",COLORS["info"]),"ready":("#EEF8F4",COLORS["accent"]),
            "waiting":("#FFF7E7",COLORS["warning"]),"warning":("#FFF0F1",COLORS["danger"]),
            "muted":("#F5F1EF",COLORS["muted"]),
        }
        bg,fg=palette.get(next_kind,palette["muted"])
        self.trade_detail_next.config(text=("下一步："+next_text) if next_text else "",bg=bg,fg=fg)

    def show_trade_detail(self,_event=None):
        selected=self.tree.selection();row=self.trade_map.get(selected[0]) if selected else None
        if not row:self._set_trade_detail();return
        summary=list(trade_detail_summary(row,posted=PortfolioLedgerService(self.db).is_posted(row["id"])))
        correction=self.db.row(
            """SELECT old_value,new_value,reason,created_at FROM trade_corrections
                 WHERE trade_id=? AND field_name='fee' ORDER BY id DESC LIMIT 1""",
            (row["id"],),
        )
        if correction:
            summary[2]+=(
                f"\n费用更正审计：¥{float(correction['old_value']):,.2f} → "
                f"¥{float(correction['new_value']):,.2f}｜{correction['created_at']}｜"
                f"{correction['reason']}"
            )
        self._set_trade_detail(*summary)
    def add(self):
        schedule=ChinaTradingCalendar.from_db(self.db).resolve_order(local_now())
        fields=[("下单/操作日期","trade_date",local_today().isoformat()),("预计净值交易日","nav_date",schedule.pricing_date.isoformat()),("基金代码","fund_code",""),("操作","action","买入"),("现金金额 元（买入支出/卖出净到账）","amount",0),("份额（未知可留0）","units",0),("正式净值（未知可留0）","nav",0),("实际费用 元（未知可留0）","fee",0),("主动资金口径","capital_scope","自动判断"),("操作理由","rationale",""),("AI建议摘要","ai_advice","")]
        d=FormDialog(self,"📒 新建待确认交易",fields,choices={"action":["买入","卖出","清仓","转换转入","转换转出","定投"],"capital_scope":["自动判断","计入主动资金","不计入主动资金"]})
        if not d.result:return
        v=d.result
        try:
            nums=[float(v[k] or 0) for k in ("amount","units","nav","fee")]
            date.fromisoformat(v["trade_date"]);pricing=date.fromisoformat(v["nav_date"])
        except ValueError:messagebox.showerror("格式错误","请检查日期和数字。",parent=self);return
        action=str(v["action"] or "")
        exit_actions={"卖出","清仓","转换转出"}
        if min(nums)<0:messagebox.showerror("数字不合法","金额、份额、净值和费用不能小于0。",parent=self);return
        if action in exit_actions and nums[0]<=0 and nums[1]<=0:messagebox.showerror("卖出资料不完整","卖出时至少填写已提交的份额；若只知道预计到账金额，也可只填金额。",parent=self);return
        if action not in exit_actions and nums[0]<=0:messagebox.showerror("金额不完整","买入、定投或转入金额必须大于0；未知份额、净值和费用可以暂时填0。",parent=self);return
        if not ChinaTradingCalendar.from_db(self.db).is_trading_day(pricing):messagebox.showerror("净值日不是交易日",f"{pricing.isoformat()}不是交易日，请填写预计或实际的净值交易日。",parent=self);return
        fund=self.db.row("SELECT * FROM funds WHERE fund_code=?",(v["fund_code"],))
        if not fund:messagebox.showerror("基金不存在","请先在基金库添加。",parent=self);return
        counts=1 if v["capital_scope"]=="计入主动资金" else 0 if v["capital_scope"]=="不计入主动资金" else (1 if infer_active_capital(fund) else 0)
        now=local_now().isoformat(timespec="seconds")
        self.db.execute("""INSERT INTO trades(
              trade_date,fund_code,action,amount,units,nav,fee,rationale,ai_advice,created_at,
              confirm_date,status,source,nav_date,counts_toward_active_capital,result_pnl,result_pnl_status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (v["trade_date"],v["fund_code"],v["action"],*nums,v["rationale"],v["ai_advice"],now,"","待确认","用户手工待确认",v["nav_date"],counts,None,"待核算" if v["action"] in {"卖出","清仓","转换转出"} else "不适用"))
        self.app.refresh_all();messagebox.showinfo("已建立待确认记录","尚未改变持仓、成本批次或现金。请等待支付宝给出正式份额、净值、费用、确认日和实际到账金额后，再点“补全/确认选中”。",parent=self)

    def confirm_selected(self):
        selected=self.tree.selection();row=self.trade_map.get(selected[0]) if selected else None
        if not row:messagebox.showinfo("请先选择","请选择要补全正式成交数据的记录。",parent=self);return
        ledger=PortfolioLedgerService(self.db)
        if ledger.is_posted(row["id"]):
            messagebox.showinfo("已入账交易已锁定","这笔交易已经改变持仓、成本批次和现金，不能在表单中直接修改。若支付宝数据确实录错，请点“撤销已入账交易”，再新建一笔更正记录。",parent=self);return
        fields=[
            ("下单/操作日期","trade_date",row["trade_date"]),("支付宝实际确认日（待确认可留空）","confirm_date",row.get("confirm_date","") or ""),
            ("基金代码","fund_code",row["fund_code"]),("操作","action",row["action"]),("实际金额 元","amount",row["amount"]),
            ("实际份额","units",row["units"]),("正式净值","nav",row["nav"]),("实际费用 元","fee",row["fee"]),
            ("净值日期","nav_date",row.get("nav_date","") or row["trade_date"]),("状态","status",row.get("status","") or "待确认"),
            ("主动资金口径","capital_scope","计入主动资金" if int(row.get("counts_toward_active_capital") or 0) else "不计入主动资金"),
            ("实际操作理由","rationale",row.get("rationale","") or ""),
        ]
        d=FormDialog(self,"✅ 补全支付宝正式成交",fields,choices={"action":["买入","卖出","清仓","转换转入","转换转出","定投"],"status":["待确认","已确认","已撤销"],"capital_scope":["计入主动资金","不计入主动资金"]})
        if not d.result:return
        v=d.result
        try:
            date.fromisoformat(v["trade_date"]);nav_day=date.fromisoformat(v["nav_date"])
            confirm_text=str(v.get("confirm_date") or "").strip()
            confirm_day=date.fromisoformat(confirm_text) if confirm_text else None
            amount,units,nav,fee=[float(v[key] or 0) for key in ("amount","units","nav","fee")]
        except ValueError:messagebox.showerror("格式错误","日期必须是YYYY-MM-DD，金额、份额、净值和费用必须是数字。",parent=self);return
        if v["status"]=="已确认" and (amount<=0 or units<=0 or nav<=0):
            messagebox.showerror("正式数据不完整","标记为已确认前，实际金额、份额和正式净值都必须大于0。",parent=self);return
        calendar=ChinaTradingCalendar.from_db(self.db)
        if v["status"]=="已确认" and not calendar.is_trading_day(nav_day):
            messagebox.showerror("净值日期不可能",f"{nav_day.isoformat()}不是交易日，不能作为这笔基金成交的正式净值日期。",parent=self);return
        if v["status"]=="已确认" and confirm_day is None:
            messagebox.showerror("确认日期缺失","只有支付宝已经给出实际确认日后，才能标记为已确认。尚未确认时请保持“待确认”。",parent=self);return
        if v["status"]=="已确认" and (not calendar.is_trading_day(confirm_day) or confirm_day<nav_day):
            messagebox.showerror("确认日期不可能","确认日必须是交易日，并且不能早于净值日期。周末、法定休市日不会被当作正式确认日。",parent=self);return
        if not self.db.row("SELECT 1 FROM funds WHERE fund_code=?",(v["fund_code"],)):
            messagebox.showerror("基金不存在","请先在基金库添加或核对这只基金。",parent=self);return
        requested_counts=1 if v["capital_scope"]=="计入主动资金" else 0
        existing_holding=self.db.row(
            """SELECT counts_toward_active_capital FROM holdings
                 WHERE fund_code=? AND active=1 ORDER BY id LIMIT 1""",(v["fund_code"],)
        )
        # A sale and an add-on purchase inherit the existing holding's capital
        # bucket.  Letting a transaction independently choose the opposite value
        # used to make active cash appear or disappear.
        counts=(1 if int(existing_holding["counts_toward_active_capital"] or 0) else 0) if existing_holding else requested_counts
        proposed={**row,"trade_date":v["trade_date"],"confirm_date":confirm_text if v["status"]=="已确认" else "","fund_code":v["fund_code"],"action":v["action"],"amount":amount,"units":units,"nav":nav,"fee":fee,"nav_date":v["nav_date"],"status":v["status"],"rationale":v["rationale"],"counts_toward_active_capital":counts}
        try:posted=ledger.finalize_trade(row["id"],proposed)
        except LedgerError as exc:
            messagebox.showerror("未通过入账硬校验",str(exc)+"\n\n本次保存和过账已在同一事务中全部撤回；交易、持仓、成本批次和现金都没有出现半成品。",parent=self);self.app.refresh_all();return
        if v["status"]!="已确认":
            self.app.refresh_all();messagebox.showinfo("记录已更新","这笔记录仍未入账，没有改变持仓、成本批次或现金。",parent=self);return
        if posted.get("posted"):
            effect=("已按实际份额FIFO减少持仓并核算已实现盈亏。" if v["action"] in {"卖出","清仓","转换转出"} else "已自动增加持仓和成本批次。")
        else:
            effect=f"{posted.get('reason','没有重复修改持仓')}。"
        self.app.refresh_all();messagebox.showinfo("已保存并核对持仓","正式成交已保存。"+effect,parent=self)

    def reverse_selected(self):
        selected=self.tree.selection();row=self.trade_map.get(selected[0]) if selected else None
        if not row:messagebox.showinfo("请先选择","请选择一笔已经入账且确实录错的交易。",parent=self);return
        ledger=PortfolioLedgerService(self.db)
        if not ledger.is_posted(row["id"]):
            messagebox.showinfo("尚未入账","这笔记录尚未改变持仓。待确认记录可在“补全/确认选中”中改为已撤销。",parent=self);return
        reason=simpledialog.askstring("撤销入账原因","请输入为什么要撤销这笔已入账交易。撤销后原记录仍会保留为审计轨迹；需要重新新建正确交易。",parent=self)
        if not reason:return
        if not messagebox.askyesno("最后确认",f"将撤销交易#{row['id']}，并同步恢复该笔交易之前的持仓、成本批次和主动现金。\n\n原因：{reason}\n\n继续吗？",parent=self):return
        try:ledger.reverse_posting(row["id"],reason)
        except LedgerError as exc:messagebox.showerror("无法撤销",str(exc),parent=self);return
        self.app.refresh_all();messagebox.showinfo("已审计式撤销","原交易已标记为已撤销，持仓、成本批次和现金均已同步恢复。请新建一笔正确交易，不要修改原记录。",parent=self)

    def correct_posted_fee(self):
        selected=self.tree.selection();row=self.trade_map.get(selected[0]) if selected else None
        if not row:messagebox.showinfo("请先选择","请选择一笔费用确实录错的已入账买入交易。",parent=self);return
        ledger=PortfolioLedgerService(self.db)
        if not ledger.is_posted(row["id"]):
            messagebox.showinfo("尚未入账","待确认交易可直接在“补全/确认选中”表单里修改费用。",parent=self);return
        if str(row.get("action") or "") not in {"买入","定投","转换转入"}:
            messagebox.showinfo("不适用这笔交易","这个入口只更正已入账买入、定投或转换转入的费用；卖出资料会影响到账与盈亏，仍须审计式撤销后重建。",parent=self);return
        d=FormDialog(
            self,"🧾 更正已入账买入费用",
            [
                ("更正后的支付宝实际费用 元","new_fee",row.get("fee") or 0),
                ("更正原因（会永久留在审计记录）","reason","录入费用有误；按支付宝正式成交结果更正"),
            ],
        )
        if not d.result:return
        try:new_fee=float(d.result["new_fee"] or 0)
        except ValueError:messagebox.showerror("格式错误","更正后的费用必须是数字。",parent=self);return
        reason=str(d.result.get("reason") or "").strip()
        if not reason:messagebox.showerror("缺少原因","费用更正必须填写原因，便于以后复核。",parent=self);return
        if not messagebox.askyesno(
            "确认费用更正",
            f"交易#{row['id']}｜{row['fund_code']} {row.get('fund_name') or ''}\n"
            f"原费用：{money(row.get('fee'))}\n更正后：{money(new_fee)}\n\n"
            "金额、份额、净值、持仓和现金不会改变；系统会同步成本批次并永久保留更正记录。\n\n继续吗？",
            parent=self,
        ):return
        try:result=ledger.correct_posted_buy_fee(row["id"],new_fee,reason)
        except LedgerError as exc:messagebox.showerror("无法更正费用",str(exc),parent=self);return
        self.app.refresh_all()
        if result.get("changed"):
            messagebox.showinfo("费用已审计更正",f"费用已从{money(result['old_fee'])}更正为{money(result['new_fee'])}；交易、成本批次和账本快照已保持一致。",parent=self)
        else:
            messagebox.showinfo("费用没有变化","更正后的费用与当前记录相同，未重复写入审计记录。",parent=self)

    def online_fill_selected(self):
        selected=self.tree.selection();row=self.trade_map.get(selected[0]) if selected else None
        if not row:messagebox.showinfo("请先选择","请选择一笔“待确认”交易。",parent=self);return
        if str(row.get("status") or "")!="待确认" or PortfolioLedgerService(self.db).is_posted(row["id"]):
            messagebox.showinfo("不能改已入账数据","联网补全只用于待确认记录；已入账交易必须保持原样。",parent=self);return
        if self._online_fill_active:messagebox.showinfo("正在联网","正在取得正式净值，请稍候。",parent=self);return
        self._online_fill_stop.clear();self._set_online_fill_active(True)
        self.pending_progress.configure(mode="indeterminate");self.pending_progress.start(12)
        def worker():
            try:result=DraftSettlementService(self.db).enrich_trade(row["id"])
            except Exception as exc:self.after(0,lambda error=exc:self._finish_online_fill(None,error))
            else:self.after(0,lambda:self._finish_online_fill(result,None))
        self.app.start_background(worker,label="待确认交易联网补全",cancel=self._online_fill_stop.set)

    def _finish_online_fill(self,result,error):
        self._set_online_fill_active(False)
        if error:messagebox.showwarning("暂未补全",str(error),parent=self);return
        self.refresh()
        if result.get("action") in {"卖出","清仓","转换转出"}:
            units_label="卖出份额（已保留）" if result.get("units_basis")=="recorded" else "卖出份额（估算）"
            gross=result.get("gross_estimate")
            gross_line=f"\n赎回毛额估算：{money(gross)}" if gross is not None else ""
            detail=(
                f"净值日期：{result['nav_date']}\n正式净值：{result['nav']:.4f}\n"
                f"{units_label}：{result['units']:.2f}{gross_line}\n\n"
                "记录仍保持“待确认”；赎回费、最终净到账金额和确认日出来后，再点‘补全/确认选中’核对。"
            )
        else:
            units_label="实际份额（已保留）" if result.get("units_basis")=="recorded" else "估算份额"
            detail=(
                f"净值日期：{result['nav_date']}\n正式净值：{result['nav']:.4f}\n{units_label}：{result['units']:.2f}\n"
                f"估算费用：{money(result['fee'])}\n\n记录仍保持“待确认”；支付宝的最终份额、折扣费率和费用出来后，"
                "再点‘补全/确认选中’核对。"
            )
        messagebox.showinfo("已取得正式净值",detail,parent=self)
    def _set_online_fill_active(self,active):
        self._online_fill_active=bool(active)
        self.online_fill_button.set_enabled(not active)
        self.bulk_fill_button.set_enabled(not active)
        self.online_stop_button.set_enabled(active)
        if not active:
            self.pending_progress.stop();self.pending_progress.configure(mode="determinate")

    def stop_online_fill(self):
        if not self._online_fill_active:return
        self._online_fill_stop.set();self.online_stop_button.set_enabled(False)
        self.pending_status.config(text="已请求停止；当前基金请求结束后不再补全下一笔交易。",fg=COLORS["warning"])
    def online_fill_all_pending(self):
        if self._online_fill_active:messagebox.showinfo("正在联网","另一项待确认交易补全正在运行，请稍候。",parent=self);return
        pending=int((self.db.row("SELECT COUNT(*) AS n FROM trades WHERE status='待确认'") or {}).get("n") or 0)
        if not pending:messagebox.showinfo("没有待确认交易","目前没有需要联网补全的待确认交易。",parent=self);return
        self._online_fill_stop.clear();self._set_online_fill_active(True);self.pending_progress["value"]=0
        self.pending_status.config(text=f"正在后台逐笔检查 {pending} 笔待确认交易；不会自动确认或改变持仓……",fg=COLORS["info"])
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_pending_progress(d,t,value))
        def worker():
            try:result=DraftSettlementService(self.db).enrich_pending_trades(
                stop_requested=self._online_fill_stop.is_set,progress=progress,
            )
            except Exception as exc:self.after(0,lambda error=exc:self._finish_bulk_online_fill(None,error))
            else:self.after(0,lambda:self._finish_bulk_online_fill(result,None))
        self.app.start_background(worker,label="全部待确认交易联网补全",cancel=self._online_fill_stop.set)

    def _update_pending_progress(self,done,total,label):
        self.pending_progress.configure(mode="determinate",maximum=max(1,total),value=done)
        self.pending_status.config(text=f"待确认交易补全 {done}/{total}｜{label}；停止会在当前请求结束后生效。",fg=COLORS["info"])
    def _finish_bulk_online_fill(self,result,error):
        self._set_online_fill_active(False);self.refresh()
        if error:messagebox.showwarning("批量补全未完成",str(error),parent=self);return
        detail=(
            f"检查 {result.checked} 笔｜已取得正式净值 {result.resolved} 笔｜尚未发布 {result.waiting} 笔｜失败 {result.failed} 笔。\n\n"
            "所有记录仍保持“待确认”，没有改变持仓、成本批次或现金。接下来请逐笔选择绿色记录，核对支付宝实际金额、份额、费用和确认日后再确认入账。"
        )
        if result.stopped:detail="已按你的要求停止；已完成记录均已安全保存，未改变持仓。\n"+detail
        if result.errors:detail+="\n\n失败明细：\n"+"\n".join(result.errors[:8])
        show_nonmodal_task_result(self,"待确认交易补全已停止" if result.stopped else "待确认交易补全完成",detail)
    def export(self):
        rows=self.db.rows("SELECT * FROM trades ORDER BY trade_date,id")
        path=filedialog.asksaveasfilename(parent=self,initialdir=EXPORT_DIR,initialfile="板块雷达_交易日志.csv",defaultextension=".csv",filetypes=[("CSV","*.csv")])
        if not path:return
        with open(path,"w",encoding="utf-8-sig",newline="") as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys() if rows else ["trade_date"]);w.writeheader();w.writerows(rows)
        messagebox.showinfo("导出成功",path,parent=self)


class RecurringPage(BasePage):
    title = "🔁 定投管理"
    subtitle = "按计划自动生成逐日流水；当前可跳过扣款确认，只等待正式净值和合法确认日后自动入账。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.service = RecurringInvestmentService(self.db)
        self._nav_sync_active=False
        self._nav_sync_stop=threading.Event()
        self.plan_map = {}; self.occurrence_map = {}
        summary = Card(self.body); summary.pack(fill="x", pady=(0, 10))
        self.summary = tk.Label(summary, text="", justify="left", anchor="w", bg=COLORS["panel"],
                                fg=COLORS["text"], font=(FONT, FONT_SIZE["body"], "bold"), padx=16, pady=12)
        self.summary.pack(fill="x")

        plan_bar = tk.Frame(self.body, bg=COLORS["bg"]); plan_bar.pack(fill="x", pady=(0, 7))
        tk.Label(plan_bar, text="定投计划", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(FONT, FONT_SIZE["section"], "bold")).pack(side="left")
        CuteButton(plan_bar, "📌 新建计划", self.add_plan).pack(side="left", padx=(18, 7))
        CuteButton(plan_bar, "✏️ 编辑计划", self.edit_plan, "secondary").pack(side="left")
        CuteButton(plan_bar, "⏸ 启用/暂停", self.toggle_plan, "secondary").pack(side="left", padx=7)
        self.plan_tree = ttk.Treeview(self.body, columns=("fund","amount","frequency","range","sync","state"), show="headings", height=3)
        for key, label, width in [("fund","基金代码＋全名",310),("amount","每笔金额",100),("frequency","频率",150),("range","有效日期",220),("sync","联网校准",110),("state","状态",90)]:
            self.plan_tree.heading(key, text=label); self.plan_tree.column(key, width=width, anchor="w" if key in {"fund","range"} else "center")
        self.plan_tree.pack(fill="x"); self.plan_tree.bind("<<TreeviewSelect>>", lambda _e: self.refresh_occurrences())

        action = tk.Frame(self.body, bg=COLORS["bg"]); action.pack(fill="x", pady=(11, 7))
        tk.Label(action, text="逐笔流水", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(FONT, FONT_SIZE["section"], "bold")).pack(side="left")
        CuteButton(action, "📅 生成到今天", self.generate_due, "secondary").pack(side="left", padx=(18, 7))
        self.recurring_sync_button=CuteButton(action, "🌐 联网校准净值", self.sync_navs, "success")
        self.recurring_sync_button.pack(side="left")
        self.recurring_stop_button=CuteButton(action,"⏹ 停止校准",self.stop_nav_sync,"danger")
        self.recurring_stop_button.pack(side="left",padx=7);self.recurring_stop_button.set_enabled(False)
        self.recurring_select_button=CuteButton(action, "✅ 选中全部可确认", self.select_ready, "secondary")
        self.recurring_select_button.pack(side="left", padx=7)
        self.recurring_confirm_button=CuteButton(action, "📥 批量确认入账", self.confirm_selected)
        self.recurring_confirm_button.pack(side="left")
        if str(self.db.setting("recurring_assume_all_deducted", "0")).strip().lower() in {"1","true","yes","on"}:
            self.recurring_select_button.pack_forget()
            self.recurring_confirm_button.pack_forget()
        CuteButton(action, "🚫 标记未扣款/跳过", self.skip_selected, "danger").pack(side="right")
        CuteButton(action, "✏️ 校正选中数据", self.edit_occurrence, "secondary").pack(side="right", padx=7)

        table = tk.Frame(self.body, bg=COLORS["bg"]); table.pack(fill="both", expand=True)
        cols = ("date","fund","amount","navdate","nav","fee","units","status","source")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=10, selectmode="extended")
        for key,label,width in [("date","计划/扣款日",120),("fund","基金代码＋全名",290),("amount","金额",85),("navdate","净值日期",110),("nav","净值",90),("fee","手续费",85),("units","确认份额",100),("status","状态",120),("source","校准来源",170)]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width,anchor="w" if key in {"fund","source"} else "center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        self.tree.bind("<MouseWheel>", lambda e: self.tree.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.tree.bind("<Double-1>", lambda _e: self.edit_occurrence())
        self.status = tk.Label(self.body, text="", bg=COLORS["bg"], fg=COLORS["muted"],
                               font=(FONT, FONT_SIZE["small"]), anchor="w")
        self.status.pack(fill="x", pady=(7,0))
        self.recurring_progress=ttk.Progressbar(self.body,orient="horizontal",mode="determinate")
        self.recurring_progress.pack(fill="x",pady=(5,0))
        self.refresh()

    def refresh(self):
        try:
            self.service.generate_due()
            self.service.auto_confirm_ready()
        except Exception as exc:
            self.status.config(text=f"生成计划流水失败：{exc}", fg=COLORS["danger"])
        selected = self.plan_tree.selection() if hasattr(self, "plan_tree") else ()
        self.plan_tree.delete(*self.plan_tree.get_children()); self.plan_map = {}
        for row in self.db.rows("""SELECT p.*,f.fund_name FROM recurring_plans p JOIN funds f USING(fund_code) ORDER BY p.enabled DESC,p.id"""):
            iid = str(row["id"]); self.plan_map[iid] = row
            date_range = f"{row['start_date']} → {row['end_date'] or '持续'}"
            self.plan_tree.insert("","end",iid=iid,values=(f"{row['fund_code']} {row['fund_name']}",money(row["amount"]),row["frequency"],date_range,"自动" if row["auto_sync"] else "手动","启用" if row["enabled"] else "暂停"))
        if selected and self.plan_tree.exists(selected[0]): self.plan_tree.selection_set(selected[0])
        elif self.plan_tree.get_children(): self.plan_tree.selection_set(self.plan_tree.get_children()[0])
        counts = {row["status"]: row["n"] for row in self.db.rows("SELECT status,COUNT(*) n FROM recurring_occurrences GROUP BY status")}
        assume_deducted = str(self.db.setting("recurring_assume_all_deducted", "0")).strip().lower() in {"1","true","yes","on"}
        self.summary.config(text=(
            f"待扣款核对 {counts.get('待扣款核对',0)} 笔　｜　待净值 {counts.get('待净值',0)} 笔　｜　"
            f"可确认 {counts.get('可确认入账',0)} 笔　｜　已入账 {counts.get('已入账',0)} 笔\n"
            + ("已按你的设置把所有定投视为已扣款；正式净值和合法确认日齐备后自动入账。"
               if assume_deducted else "公开净值只能校准价格，不能证明支付宝实际扣款；确认入账前务必核对支付宝。")
        ))
        self.refresh_occurrences()

    def refresh_occurrences(self):
        self.tree.delete(*self.tree.get_children()); self.occurrence_map = {}
        selected = self.plan_tree.selection()
        where = "WHERE o.plan_id=?" if selected else ""
        params = (int(selected[0]),) if selected else ()
        rows = self.db.rows(f"""SELECT o.*,f.fund_name FROM recurring_occurrences o
            JOIN recurring_plans p ON p.id=o.plan_id JOIN funds f ON f.fund_code=o.fund_code
            {where} ORDER BY o.scheduled_date DESC,o.id DESC""", params)
        for row in rows:
            iid=str(row["id"]); self.occurrence_map[iid]=row
            self.tree.insert("","end",iid=iid,values=(row["trade_date"],f"{row['fund_code']} {row['fund_name']}",money(row["amount"]),row["pricing_date"],format_float(row["nav"],4),money(row["fee"]),format_float(row["units"]),row["status"],row["source"] or "尚未联网"),tags=("ready" if row["status"]=="可确认入账" else "done" if row["status"]=="已入账" else "pending",))
        self.tree.tag_configure("ready",foreground=COLORS["primary_dark"]);self.tree.tag_configure("done",foreground=COLORS["accent"]);self.tree.tag_configure("pending",foreground=COLORS["muted"])
        last=self.db.setting("recurring_last_sync_at","");error=self.db.setting("recurring_last_sync_error","")
        attempt_at=self.db.setting("recurring_last_sync_attempt_at","")
        attempt_status=self.db.setting("recurring_last_sync_attempt_status","")
        attempt=(f"　最近尝试：{attempt_status}（{attempt_at}）" if attempt_status and attempt_at else "")
        self.status.config(
            text=(f"最近完成校准：{last or '尚未执行'}{attempt}" + (f"　最近错误：{error.splitlines()[0]}" if error else "")),
            fg=COLORS["danger"] if error else COLORS["warning"] if attempt_status=="已停止" else COLORS["muted"],
        )

    def selected_plan(self):
        items=self.plan_tree.selection(); return self.plan_map.get(items[0]) if items else None

    def add_plan(self): self._plan_form(None)
    def edit_plan(self):
        plan=self.selected_plan()
        if not plan: messagebox.showinfo("请先选择","请先选择一条定投计划。",parent=self);return
        self._plan_form(plan)

    def _plan_form(self, plan):
        plan=plan or {}
        fields=[("基金代码","fund_code",plan.get("fund_code","014978")),("每笔金额 元","amount",plan.get("amount",10)),
                ("频率","frequency",plan.get("frequency","交易日")),("执行星期 0=周一","weekdays",plan.get("weekdays","0,1,2,3,4")),
                ("开始日期","start_date",plan.get("start_date",local_today().isoformat())),("结束日期（可留空）","end_date",plan.get("end_date","")),
                ("买入费率%（留空继承基金库）","purchase_fee_pct",plan.get("purchase_fee_pct","")),
                ("启动时自动联网校准","auto_sync","是" if plan.get("auto_sync",1) else "否"),("备注","notes",plan.get("notes",""))]
        d=FormDialog(self,"定投计划",fields,choices={"frequency":["交易日","每日","每周"],"auto_sync":["是","否"]})
        if not d.result:return
        v=d.result
        if not self.db.row("SELECT 1 FROM funds WHERE fund_code=?",(v["fund_code"],)):messagebox.showerror("基金不存在","请先在基金库添加基金。",parent=self);return
        try:
            amount=float(v["amount"]); start=date.fromisoformat(v["start_date"]); end=date.fromisoformat(v["end_date"]) if v["end_date"] else None
            fee=float(v["purchase_fee_pct"]) if v["purchase_fee_pct"].strip() else None
            weekdays={int(x.strip()) for x in v["weekdays"].split(",") if x.strip()}
            if amount<=0 or (fee is not None and fee<0) or not weekdays.issubset(set(range(7))) or (end and end<start):raise ValueError
        except ValueError:messagebox.showerror("格式错误","请检查金额、日期、费率和星期（只能是0—6）。",parent=self);return
        now=local_now().isoformat(timespec="seconds")
        values=(v["fund_code"],amount,v["frequency"],",".join(str(x) for x in sorted(weekdays)),v["start_date"],v["end_date"],fee,1 if v["auto_sync"]=="是" else 0,v["notes"],now)
        if plan:self.db.execute("""UPDATE recurring_plans SET fund_code=?,amount=?,frequency=?,weekdays=?,start_date=?,end_date=?,purchase_fee_pct=?,auto_sync=?,notes=?,updated_at=? WHERE id=?""",(*values,plan["id"]))
        else:self.db.execute("""INSERT INTO recurring_plans(fund_code,amount,frequency,weekdays,start_date,end_date,purchase_fee_pct,auto_sync,enabled,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)""",(*values[:-1],now,now))
        self.refresh()

    def toggle_plan(self):
        plan=self.selected_plan()
        if not plan:return
        self.db.execute("UPDATE recurring_plans SET enabled=?,updated_at=? WHERE id=?",(0 if plan["enabled"] else 1,local_now().isoformat(timespec="seconds"),plan["id"]));self.refresh()

    def generate_due(self):
        try:n=self.service.generate_due()
        except Exception as exc:messagebox.showerror("生成失败",str(exc),parent=self);return
        self.refresh();messagebox.showinfo("生成完成",f"新增 {n} 条定投流水；重复日期不会重复生成。已按你的设置跳过扣款确认。",parent=self)

    def sync_navs(self):
        if self._nav_sync_active:
            messagebox.showinfo("正在校准","定投净值仍在后台校准，可点击停止。",parent=self);return
        ids=[int(x) for x in self.tree.selection()] or None
        self._nav_sync_active=True;self._nav_sync_stop.clear()
        self.recurring_sync_button.set_enabled(False);self.recurring_stop_button.set_enabled(True);self.recurring_progress["value"]=0
        self.status.config(text="正在联网校准历史净值…",fg=COLORS["info"])
        def progress(done,total,label):
            self.after(0,lambda d=done,t=total,value=label:self._update_recurring_progress(d,t,value))
        def worker():
            try:
                result=self.service.sync_pending_navs(
                    ids,stop_requested=self._nav_sync_stop.is_set,progress=progress,
                )
                self.after(0,lambda:self._finish_recurring_sync(result,None))
            except Exception as exc:
                self.after(0,lambda error=exc:self._finish_recurring_sync(None,error))
        self.app.start_background(worker,label="定投净值校准",cancel=self._nav_sync_stop.set)

    def _update_recurring_progress(self,done,total,label):
        self.recurring_progress["maximum"]=max(1,total);self.recurring_progress["value"]=done
        self.status.config(text=f"定投净值校准 {done}/{total}｜{label}；停止会在当前请求结束后生效。",fg=COLORS["info"])

    def stop_nav_sync(self):
        if not self._nav_sync_active:return
        self._nav_sync_stop.set();self.recurring_stop_button.set_enabled(False)
        self.status.config(text="已请求停止；当前基金请求结束后不再校准下一只。",fg=COLORS["warning"])

    def _finish_recurring_sync(self,result,error):
        self._nav_sync_active=False
        self.recurring_sync_button.set_enabled(True);self.recurring_stop_button.set_enabled(False)
        self.refresh()
        if error:
            messagebox.showerror("校准失败",str(error),parent=self);return
        stopped=bool(result.get("stopped"))
        auto_posted=int(result.get("auto_posted",0))
        text=(f"检查 {result['checked']} 笔；新取得正式数据 {result['ready']} 笔；待净值 {result['missing']} 笔；"
              f"失败 {result['failed']} 笔；本次自动入账 {auto_posted} 笔。\n\n"
              "只有正式净值和合法确认日都齐备的定投才会写入持仓。")
        if stopped:text="已按你的要求停止；已完成基金的校准结果均已保存。\n"+text
        messagebox.showinfo("校准已停止" if stopped else "校准完成",text,parent=self)

    def select_ready(self):
        ready=[iid for iid,row in self.occurrence_map.items() if row["status"]=="可确认入账"]
        self.tree.selection_set(ready)
        if ready:self.tree.see(ready[0])

    def confirm_selected(self):
        ids=[int(x) for x in self.tree.selection() if self.occurrence_map[x]["status"]=="可确认入账"]
        if not ids:messagebox.showinfo("没有可确认项","请先选中已校准且你已在支付宝核对过扣款的流水。",parent=self);return
        total=sum(float(self.occurrence_map[str(i)]["amount"]) for i in ids)
        if not messagebox.askyesno("确认实际扣款",f"将确认 {len(ids)} 笔、合计 {money(total)}。\n\n你是否已经在支付宝逐日核对：确实扣款、基金代码正确、金额正确？",parent=self):return
        try:n=self.service.confirm(ids)
        except RecurringInvestmentError as exc:messagebox.showerror("入账失败",str(exc),parent=self);return
        self.refresh();self.app.refresh_all();messagebox.showinfo("入账完成",f"已写入 {n} 笔交易、{n} 个独立成本批次，并同步更新总持仓。",parent=self)

    def skip_selected(self):
        ids=[int(x) for x in self.tree.selection()]
        if not ids:return
        if messagebox.askyesno("确认跳过",f"把选中的 {len(ids)} 笔标记为未扣款/跳过？已入账流水不会被改变。",parent=self):
            self.service.mark_skipped(ids);self.refresh()

    def edit_occurrence(self):
        selected=self.tree.selection()
        if len(selected)!=1:messagebox.showinfo("请选择一笔","双击或只选中一笔待确认流水。",parent=self);return
        row=self.occurrence_map[selected[0]]
        if row["status"]=="已入账":messagebox.showinfo("已入账","已入账流水请通过交易日志和备份审计，不在此处直接改写。",parent=self);return
        fields=[("扣款/交易日期","trade_date",row["trade_date"]),("净值定价日期","pricing_date",row["pricing_date"]),("确认日期","confirm_date",row["confirm_date"]),
                ("金额 元","amount",row["amount"]),("手续费 元","fee",row["fee"]),("确认净值","nav",row["nav"]),("确认份额","units",row["units"]),
                ("状态","status",row["status"]),("备注","notes",row["notes"])]
        d=FormDialog(self,"校正定投流水",fields,choices={"status":["待扣款核对","待净值","可确认入账","已跳过"]})
        if not d.result:return
        v=d.result
        try:
            date.fromisoformat(v["trade_date"])
            for k in ("pricing_date","confirm_date"):
                if v[k]:date.fromisoformat(v[k])
            amount,fee,nav,units=(float(v[k] or 0) for k in ("amount","fee","nav","units"))
            if amount<=0 or fee<0 or nav<0 or units<0:raise ValueError
            if v["status"]=="可确认入账" and (nav<=0 or units<=0 or not v["pricing_date"]):raise ValueError
        except ValueError:messagebox.showerror("格式错误","日期、金额、手续费、净值、份额或状态不一致。",parent=self);return
        self.db.execute("""UPDATE recurring_occurrences SET trade_date=?,pricing_date=?,confirm_date=?,amount=?,fee=?,nav=?,units=?,status=?,notes=?,updated_at=? WHERE id=?""",
            (v["trade_date"],v["pricing_date"],v["confirm_date"],amount,fee,nav,units,v["status"],v["notes"],local_now().isoformat(timespec="seconds"),row["id"]))
        self.refresh()


class ReviewPage(BasePage):
    title="📊 策略复盘"
    subtitle="分开查看真实交易结果与AI推荐后的表现；推荐观察绝不冒充你的实际买卖。"
    def __init__(self,parent,app):
        super().__init__(parent,app)
        self._recommendation_sync_active=False
        self._recommendation_local_active=False
        self._recommendation_stop=threading.Event()
        self.tabs=ttk.Notebook(self.body);self.tabs.pack(fill="both",expand=True)
        self.trade_tab=tk.Frame(self.tabs,bg=COLORS["bg"])
        self.recommendation_tab=tk.Frame(self.tabs,bg=COLORS["bg"])
        self.tabs.add(self.trade_tab,text="① 我的真实交易复盘")
        self.tabs.add(self.recommendation_tab,text="② AI推荐验证（仅观察）")

        filters=tk.Frame(self.trade_tab,bg=COLORS["bg"]);filters.pack(fill="x",pady=(8,8))
        tk.Label(filters,text="时间范围",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"],"bold")).pack(side="left")
        self.period_var=tk.StringVar(value="全部")
        period=ttk.Combobox(filters,textvariable=self.period_var,state="readonly",values=("全部","近30天","近90天","近365天"),width=11);period.pack(side="left",padx=(8,18));period.bind("<<ComboboxSelected>>",lambda _e:self.refresh())
        tk.Label(filters,text="基金",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"],"bold")).pack(side="left")
        self.fund_var=tk.StringVar(value="全部基金")
        self.fund_box=ttk.Combobox(filters,textvariable=self.fund_var,state="readonly",width=34);self.fund_box.pack(side="left",padx=8);self.fund_box.bind("<<ComboboxSelected>>",lambda _e:self.refresh())
        CuteButton(filters,"🔄 刷新复盘",self.refresh,"secondary").pack(side="right")
        self.cards=tk.Frame(self.trade_tab,bg=COLORS["bg"]);self.cards.pack(fill="x")
        self.labels=[]
        for title in ("完整持有段 已核算/全部","退出净到账","费后盈亏","持有段胜率","平均持有天数","组合最大回撤（有证据）"):
            c=Card(self.cards);c.pack(side="left",fill="x",expand=True,padx=(0,8));tk.Label(c,text=title,bg=COLORS["panel"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"])).pack(anchor="w",padx=12,pady=(10,2));l=tk.Label(c,text="—",bg=COLORS["panel"],fg=COLORS["text"],font=(FONT,FONT_SIZE["section"],"bold"));l.pack(anchor="w",padx=12,pady=(0,10));self.labels.append(l)
        chart_height=max(280,int(round(190*self.app.ui_pixel_scale)))
        self.canvas=tk.Canvas(
            self.trade_tab,bg=COLORS["panel"],highlightbackground=COLORS["border"],
            highlightthickness=1,height=chart_height,
        );self.canvas.pack(fill="x",pady=(10,8))
        self.trade_tables=tk.PanedWindow(
            self.trade_tab,orient="horizontal",sashwidth=8,sashrelief="flat",
            bg=COLORS["border"],bd=0,
        )
        self.trade_tables.pack(fill="both",expand=True)
        fund_panel=tk.Frame(self.trade_tables,bg=COLORS["bg"],width=1350)
        trade_panel=tk.Frame(self.trade_tables,bg=COLORS["bg"])
        self.trade_tables.add(fund_panel,minsize=950)
        self.trade_tables.add(trade_panel,minsize=900)
        self._trade_tables_balanced=False
        self.trade_tab.bind("<Configure>",self._schedule_trade_table_balance,add="+")
        tk.Label(fund_panel,text="完整持有段（从首次开仓到份额归零）",bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["section"],"bold")).pack(anchor="w",pady=(2,4))
        fund_cols=("fund","trades","invested","received","pnl","days","period")
        fund_table=tk.Frame(fund_panel,bg=COLORS["bg"]);fund_table.pack(fill="both",expand=True,padx=(0,8))
        fund_x=ttk.Scrollbar(fund_table,orient="horizontal")
        self.fund_tree=ttk.Treeview(fund_table,columns=fund_cols,show="headings",height=5,xscrollcommand=fund_x.set)
        for key,label,width in [("fund","基金",340),("trades","买/卖",90),("invested","累计投入",140),("received","退出净到账",150),("pnl","费后盈亏",130),("days","持有天数",110),("period","持有期间",250)]:self.fund_tree.heading(key,text=label);self.fund_tree.column(key,width=width,anchor="w" if key in {"fund","period"} else "center")
        fund_x.config(command=self.fund_tree.xview);fund_x.pack(side="bottom",fill="x")
        self.fund_tree.pack(side="top",fill="both",expand=True)
        self.fund_tree.bind("<<TreeviewSelect>>",lambda _e:self._show_selected_segment_exits())
        tk.Label(trade_panel,text="选中持有段内的退出流水（部分卖出不单独算胜负）",bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["section"],"bold")).pack(anchor="w",pady=(2,4))
        trade_cols=("date","fund","action","amount","units","pnl","source")
        trade_table=tk.Frame(trade_panel,bg=COLORS["bg"]);trade_table.pack(fill="both",expand=True)
        trade_x=ttk.Scrollbar(trade_table,orient="horizontal")
        self.trade_tree=ttk.Treeview(trade_table,columns=trade_cols,show="headings",height=5,xscrollcommand=trade_x.set)
        for key,label,width in [("date","交易日",105),("fund","基金",300),("action","操作",80),("amount","实际金额",115),("units","份额",100),("pnl","结果盈亏",115),("source","记录来源",170)]:self.trade_tree.heading(key,text=label);self.trade_tree.column(key,width=width,anchor="w" if key in {"fund","source"} else "center")
        trade_x.config(command=self.trade_tree.xview);trade_x.pack(side="bottom",fill="x")
        self.trade_tree.pack(side="top",fill="both",expand=True)
        self.note=tk.Label(self.trade_tab,text="",justify="left",anchor="w",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]));self.note.pack(fill="x")
        self._build_recommendation_tab()

    def _schedule_trade_table_balance(self,_event=None):
        if not self._trade_tables_balanced:
            self.after_idle(self._balance_trade_tables_once)

    def _balance_trade_tables_once(self):
        if self._trade_tables_balanced:
            return
        self.trade_tables.update_idletasks()
        width=self.trade_tables.winfo_width()
        if width<1400:
            return
        try:
            # The segment summary needs enough room for fund, money and date
            # columns; the old minimum-size split left it as a cramped strip.
            self.trade_tables.sash_place(0,int(width*0.45),1)
            self._trade_tables_balanced=True
        except tk.TclError:
            return

    def _build_recommendation_tab(self):
        controls=tk.Frame(self.recommendation_tab,bg=COLORS["bg"]);controls.pack(fill="x",pady=(8,8))
        tk.Label(controls,text="观察周期",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"],"bold")).pack(side="left")
        self.recommendation_horizon=tk.StringVar(value="20个交易日")
        horizon=ttk.Combobox(
            controls,textvariable=self.recommendation_horizon,state="readonly",width=14,
            values=tuple(f"{value}个交易日" for value in PERFORMANCE_HORIZONS),
        );horizon.pack(side="left",padx=(8,18));horizon.bind("<<ComboboxSelected>>",lambda _e:self._refresh_recommendations())
        self.recommendation_policy=tk.StringVar(value="全部算法版本")
        tk.Label(controls,text="算法版本",bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"],"bold")).pack(side="left")
        self.recommendation_policy_box=ttk.Combobox(
            controls,textvariable=self.recommendation_policy,state="readonly",width=24,
        );self.recommendation_policy_box.pack(side="left",padx=8);self.recommendation_policy_box.bind("<<ComboboxSelected>>",lambda _e:self._refresh_recommendations())
        self.recover_recommendation_button=CuteButton(
            controls,"🧊 恢复可验证V3快照",self.recover_recommendation_snapshots,"secondary",
        );self.recover_recommendation_button.pack(side="right",padx=(8,0))
        self.stop_recommendation_button=CuteButton(
            controls,"⏹ 停止净值更新",self.stop_recommendation_sync,"danger",
        );self.stop_recommendation_button.pack(side="right",padx=(8,0));self.stop_recommendation_button.set_enabled(False)
        self.sync_recommendation_button=CuteButton(
            controls,"🌐 联网补齐推荐基金净值",self.sync_recommendation_navs,"success",
        );self.sync_recommendation_button.pack(side="right",padx=(8,0))
        self.recompute_recommendation_button=CuteButton(
            controls,"🔄 仅重算本地推荐表现",self.refresh_recommendation_cached,"secondary",
        );self.recompute_recommendation_button.pack(side="right")

        self.recommendation_cards=tk.Frame(self.recommendation_tab,bg=COLORS["bg"]);self.recommendation_cards.pack(fill="x")
        self.recommendation_labels=[]
        for title in ("记录快照","指数结果成熟","基金费后成熟","平均费后收益","方向命中率","平均最大回撤"):
            card=Card(self.recommendation_cards);card.pack(side="left",fill="x",expand=True,padx=(0,8))
            tk.Label(card,text=title,bg=COLORS["panel"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"])).pack(anchor="w",padx=12,pady=(9,2))
            label=tk.Label(card,text="—",bg=COLORS["panel"],fg=COLORS["text"],font=(FONT,FONT_SIZE["section"],"bold"));label.pack(anchor="w",padx=12,pady=(0,9))
            self.recommendation_labels.append(label)

        self.recommendation_status=tk.Label(
            self.recommendation_tab,text="",justify="left",anchor="w",bg="#EEF6FA",fg=COLORS["info"],
            font=(FONT,FONT_SIZE["small"]),padx=12,pady=7,
        );self.recommendation_status.pack(fill="x",pady=(10,6))
        self.recommendation_progress=ttk.Progressbar(
            self.recommendation_tab,orient="horizontal",mode="determinate",
        );self.recommendation_progress.pack(fill="x",pady=(0,6))
        tables=tk.PanedWindow(
            self.recommendation_tab,orient="horizontal",sashwidth=8,sashrelief="flat",
            bg=COLORS["border"],bd=0,
        );tables.pack(fill="both",expand=True)
        group_panel=tk.Frame(tables,bg=COLORS["bg"],width=760)
        detail_panel=tk.Frame(tables,bg=COLORS["bg"])
        tables.add(group_panel,minsize=620);tables.add(detail_panel,minsize=720)
        group_tabs=ttk.Notebook(group_panel);group_tabs.pack(fill="both",expand=True,padx=(0,8))
        tier_tab=tk.Frame(group_tabs,bg=COLORS["bg"]);score_tab=tk.Frame(group_tabs,bg=COLORS["bg"])
        group_tabs.add(tier_tab,text="按七档结论")
        group_tabs.add(score_tab,text="按每10分区间")
        group_cols=("group","observations","mature","return","positive","hit","days","drawdown")
        self.recommendation_tier_tree=ttk.Treeview(
            tier_tab,columns=group_cols,show="headings",height=10,
        )
        for key,label,width in [
            ("group","七档结论",105),("observations","快照",62),("mature","成熟",62),
            ("return","费后收益",82),("positive","正收益率",82),("hit","方向命中",82),
            ("days","持有天数",75),("drawdown","平均回撤",82),
        ]:
            self.recommendation_tier_tree.heading(key,text=label);self.recommendation_tier_tree.column(key,width=width,anchor="center")
        self.recommendation_tier_tree.pack(fill="both",expand=True)
        self.recommendation_score_tree=ttk.Treeview(
            score_tab,columns=group_cols,show="headings",height=10,
        )
        for key,label,width in [
            ("group","推荐分区间",105),("observations","快照",62),("mature","成熟",62),
            ("return","费后收益",82),("positive","正收益率",82),("hit","方向命中",82),
            ("days","持有天数",75),("drawdown","平均回撤",82),
        ]:
            self.recommendation_score_tree.heading(key,text=label);self.recommendation_score_tree.column(key,width=width,anchor="center")
        self.recommendation_score_tree.pack(fill="both",expand=True)
        tk.Label(
            detail_panel,text="逐条推荐结果（横向滚动可看完整字段）",anchor="w",
            bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["small"],"bold"),
        ).pack(fill="x",pady=(0,4))
        detail_cols=("time","sector","score","tier","fund","horizon","index_result","fund_result","drawdown","state")
        detail_table=tk.Frame(detail_panel,bg=COLORS["bg"]);detail_table.pack(fill="both",expand=True)
        detail_y=ttk.Scrollbar(detail_table,orient="vertical")
        detail_x=ttk.Scrollbar(detail_table,orient="horizontal")
        self.recommendation_tree=ttk.Treeview(
            detail_table,columns=detail_cols,show="headings",height=10,
            yscrollcommand=detail_y.set,xscrollcommand=detail_x.set,
        )
        for key,label,width in [
            ("time","推荐时刻",145),("sector","行业/指数",180),("score","推荐分",70),("tier","七档结论",100),
            ("fund","执行基金",175),("horizon","周期",65),("index_result","指数参考",95),
            ("fund_result","基金费后",100),("drawdown","基金回撤",90),("state","核算状态",200),
        ]:
            self.recommendation_tree.heading(key,text=label);self.recommendation_tree.column(key,width=width,anchor="w" if key in {"sector","fund","state"} else "center")
        detail_y.config(command=self.recommendation_tree.yview);detail_x.config(command=self.recommendation_tree.xview)
        detail_y.pack(side="right",fill="y");detail_x.pack(side="bottom",fill="x")
        self.recommendation_tree.pack(side="left",fill="both",expand=True)
        self.recommendation_note=tk.Label(
            self.recommendation_tab,
            text="口径：推荐在15:00前以当日为模拟定价日，15:00后或休市顺延；指数和基金分开。只有净值与适用费率都有来源时，基金才进入费后收益与命中率；观察档没有方向命中率。这里不会创建草稿、交易、持仓或现金流水。",
            justify="left",anchor="w",wraplength=1500,bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),
        );self.recommendation_note.pack(fill="x",pady=(6,0))
    def refresh(self):
        review=holding_segment_review(self.db)
        all_segments=review["completed"]
        options={"全部基金":""}
        for row in [*all_segments,*review["open"]]:options[f"{row['fund_code']} {row['fund_name']}"]=row["fund_code"]
        self.fund_box["values"]=list(options)
        if self.fund_var.get() not in options:self.fund_var.set("全部基金")
        days={"近30天":30,"近90天":90,"近365天":365}.get(self.period_var.get())
        cutoff=(local_today()-timedelta(days=days)).isoformat() if days else ""
        fund_code=options.get(self.fund_var.get(),"")
        segments=[row for row in all_segments if (not cutoff or row["end_date"]>=cutoff) and (not fund_code or row["fund_code"]==fund_code)]
        metrics=holding_segment_metrics(segments)
        known_segments=[row for row in segments if row.get("result_pnl") is not None and row.get("pnl_status")=="已核算"]
        drawdown=portfolio_equity_drawdown(self.db)
        drawdown_text=(f"{float(drawdown['max_drawdown_pct']):.2f}%" if drawdown.get("max_drawdown_pct") is not None else f"样本不足({drawdown.get('sample_count',0)})")
        vals=[f"{metrics['known_segment_count']}/{metrics['segment_count']}",money(metrics["exit_amount_total"]),money(metrics["pnl_total"]),f"{metrics['win_rate_pct']:.1f}%" if metrics["win_rate_pct"] is not None else "—",f"{metrics['average_holding_days']:.1f}天" if metrics["average_holding_days"] is not None else "—",drawdown_text]
        for label,value in zip(self.labels,vals):
            pnl_color=(COLORS["profit"] if metrics["pnl_total"]>=0 else COLORS["loss"]) if metrics["known_segment_count"] else COLORS["text"]
            label.config(text=value,fg=pnl_color if label is self.labels[2] else COLORS["text"])
        self._draw_curve(known_segments,metrics["cumulative"])
        self.fund_tree.delete(*self.fund_tree.get_children());self._visible_segments={}
        for row in reversed(segments):
            pnl_text=money(row["result_pnl"]) if row.get("result_pnl") is not None else "待核算（不是0）"
            tag="normal" if row.get("result_pnl") is None else "profit" if float(row["result_pnl"])>=0 else "loss"
            iid=str(row["segment_id"]);self._visible_segments[iid]=row
            self.fund_tree.insert("","end",iid=iid,values=(f"{row['fund_code']} {row['fund_name']}",f"{row['buy_count']}/{row['exit_count']}",money(row["invested_amount"]) if row.get("invested_amount") is not None else "待核算",money(row["exit_amount"]),pnl_text,f"{row['holding_days']}天" if row.get("holding_days") is not None else "—",f"{row['start_date'] or '日期未知'} → {row['end_date'] or '未结束'}"),tags=(tag,))
        self.fund_tree.tag_configure("profit",foreground=COLORS["profit"]);self.fund_tree.tag_configure("loss",foreground=COLORS["loss"])
        self._show_selected_segment_exits(select_first=True)
        period=(
            f"{segments[0]['start_date'] or '起点待核对'} 至 "
            f"{segments[-1]['end_date'] or '终点待核对'}"
            if segments else "当前筛选没有完整结束的持有段"
        )
        unknown_note=(f"其中{metrics['unknown_segment_count']}段盈亏尚未核算，绝不会按0计入胜率。" if metrics["unknown_segment_count"] else "全部完整持有段的盈亏均已核算。")
        visible_open=[row for row in review["open"] if not fund_code or row["fund_code"]==fund_code]
        open_note=f"当前筛选另有{len(visible_open)}段仍在持有，不计入胜率。" if visible_open else "当前筛选没有未结束持有段。"
        drawdown_period=(f"；最大回撤区间 {str(drawdown.get('peak_at') or '')[:10]} → {str(drawdown.get('trough_at') or '')[:10]}" if drawdown.get("max_drawdown_pct") is not None and drawdown.get("peak_at") else "")
        coverage = drawdown.get("coverage_pct")
        coverage_note = (
            f"；正式日线覆盖{int(drawdown.get('complete_days') or 0)}/"
            f"{int(drawdown.get('candidate_days') or 0)}个已收盘交易日"
            + (f"（{float(coverage):.1f}%）" if coverage is not None else "")
            if drawdown.get("candidate_days") else ""
        )
        fallback_reason = str(drawdown.get("reason") or "").strip()
        reason_note = f" 降级原因：{fallback_reason}" if fallback_reason else ""
        self.note.config(text=f"复盘口径：同一基金从份额首次由0变为正，到最终回到0，才算1个完整持有段；加仓和部分卖出留在段内，不会虚增胜率。{period}；{unknown_note}{open_note}\n最大回撤依据：{drawdown.get('basis') or '样本不足'}；当前有效样本{drawdown.get('sample_count',0)}个{coverage_note}{drawdown_period}。正式日线要求每只在持基金都有当日来源净值，缺失日整天跳过且绝不沿用旧净值；额度调整作为外部资金流剔除。债券、纳指定投、货币基金和余额宝不混入。{reason_note}\n规则提示：连续3个完整持有段亏损暂停2个交易日；从组合阶段高点回撤6%暂停开仓5个交易日，回撤10%停止系统并复盘。")
        self._refresh_recommendations()

    def _show_selected_segment_exits(self,select_first=False):
        children=self.fund_tree.get_children()
        if select_first and children and not self.fund_tree.selection():
            self.fund_tree.selection_set(children[0])
        selected=self.fund_tree.selection()
        segment=self._visible_segments.get(str(selected[0])) if selected else None
        self.trade_tree.delete(*self.trade_tree.get_children())
        for row in reversed((segment or {}).get("exit_details",[])):
            pnl=row.get("result_pnl") if row.get("result_pnl_status")=="已核算" else None
            pnl_text=money(pnl) if pnl is not None else "待核算（不是0）"
            tag="normal" if pnl is None else "profit" if float(pnl)>=0 else "loss"
            self.trade_tree.insert("","end",values=(row["trade_date"],f"{segment['fund_code']} {segment['fund_name']}",row["action"],money(row["amount"]),format_float(row["units"]),pnl_text,row.get("source") or "手工记录"),tags=(tag,))
        self.trade_tree.tag_configure("profit",foreground=COLORS["profit"]);self.trade_tree.tag_configure("loss",foreground=COLORS["loss"])

    @staticmethod
    def _metric_text(value, suffix="%"):
        return "—" if value is None else f"{float(value):.2f}{suffix}"

    def _refresh_recommendations(self):
        try:horizon=int(self.recommendation_horizon.get().split("个",1)[0])
        except (ValueError,AttributeError):horizon=20
        versions=[row["ranking_policy_version"] for row in self.db.rows(
            "SELECT DISTINCT ranking_policy_version FROM recommendation_observations ORDER BY ranking_policy_version"
        ) if row.get("ranking_policy_version")]
        options=["全部算法版本",*versions]
        self.recommendation_policy_box["values"]=options
        if self.recommendation_policy.get() not in options:self.recommendation_policy.set("全部算法版本")
        policy="" if self.recommendation_policy.get()=="全部算法版本" else self.recommendation_policy.get()
        summary=recommendation_performance_summary(
            self.db,horizon_sessions=horizon,ranking_policy_version=policy,
        )
        values=[
            str(summary["observations"]),str(summary["index_mature"]),str(summary["fund_mature"]),
            self._metric_text(summary["average_fund_net_return_pct"]),
            self._metric_text(summary["direction_hit_rate_pct"]),
            self._metric_text(summary["average_fund_max_drawdown_pct"]),
        ]
        for label,value in zip(self.recommendation_labels,values):label.config(text=value)
        self.recommendation_tier_tree.delete(*self.recommendation_tier_tree.get_children())
        for row in summary["by_tier"]:
            self.recommendation_tier_tree.insert("","end",values=(
                row["research_tilt"],row["observations"],row["fund_mature"],
                self._metric_text(row["average_fund_net_return_pct"]),
                self._metric_text(row["fund_positive_rate_pct"]),
                self._metric_text(row["direction_hit_rate_pct"]),
                self._metric_text(row["average_holding_calendar_days"],"天"),
                self._metric_text(row["average_fund_max_drawdown_pct"]),
            ))
        self.recommendation_score_tree.delete(*self.recommendation_score_tree.get_children())
        for row in summary["by_score_band"]:
            self.recommendation_score_tree.insert("","end",values=(
                row["group"],row["observations"],row["fund_mature"],
                self._metric_text(row["average_fund_net_return_pct"]),
                self._metric_text(row["fund_positive_rate_pct"]),
                self._metric_text(row["direction_hit_rate_pct"]),
                self._metric_text(row["average_holding_calendar_days"],"天"),
                self._metric_text(row["average_fund_max_drawdown_pct"]),
            ))
        self.recommendation_tree.delete(*self.recommendation_tree.get_children())
        for row in summary["rows"]:
            fund=f"{row['fund_code']} {row['fund_name']}".strip() or "仅指数参考"
            if row.get("fund_selection_kind"):
                fund=f"{fund}｜{row['fund_selection_kind']}"
            self.recommendation_tree.insert("","end",values=(
                str(row["recommendation_at"])[:16].replace("T"," "),
                f"{row['sector_name']} {row['index_code']}".strip(),f"{float(row['recommendation_score']):.1f}",
                row["research_tilt"],fund,f"{row['horizon_sessions']}日",
                self._metric_text(row.get("index_return_pct")),self._metric_text(row.get("fund_net_return_pct")),
                self._metric_text(row.get("fund_max_drawdown_pct")),
                f"{row['outcome_status']}；{row.get('outcome_reason') or ''}".rstrip("；"),
            ))
        if not summary["observations"]:
            status=(
                "尚无推荐快照。下一次完整扫描会自动记录；也可点“恢复可验证V3快照”，"
                "仅从完整原始V3 JSON恢复，旧V2、186项、失败或证据不闭合的内容会自动排除。"
            )
        else:
            status=(
                f"当前周期：{horizon}个交易日；快照{summary['observations']}条，"
                f"指数已成熟{summary['index_mature']}条（其中仅指数研究"
                f"{summary['index_only_mature']}条），基金费后已成熟{summary['fund_mature']}条。"
                "成熟数少于快照数是正常状态，不会把未知结果按0。"
            )
        if not (self._recommendation_sync_active or self._recommendation_local_active):
            self.recommendation_status.config(text=status,fg=COLORS["info"])

    def recover_recommendation_snapshots(self):
        if self._recommendation_sync_active or self._recommendation_local_active:
            messagebox.showinfo(
                "请稍候", "另一项推荐表现任务正在后台运行，请等待完成后再恢复历史快照。", parent=self,
            )
            return
        if not messagebox.askyesno(
            "恢复可验证V3快照",
            "只检查本机已经保存的完整V3原始研究。\n\n"
            "不会联网、不会调用DeepSeek，也不会创建草稿、交易、持仓或现金流水；"
            "旧V2、186项、失败任务和证据不闭合的基金会被排除。\n\n现在开始吗？",
            parent=self,
        ):
            return
        self._recommendation_local_active=True
        self._set_recommendation_task_mode("local")
        self.recommendation_status.config(
            text="正在后台检查本机历史研究、验证原始证据并恢复推荐快照；不联网、不调用DeepSeek……",
            fg=COLORS["info"],
        )
        def worker():
            try:result=RecommendationPerformanceService(self.db).recover_verified_v3_snapshots()
            except Exception as exc:self.after(0,lambda error=exc:self._finish_snapshot_recovery(None,error))
            else:self.after(0,lambda value=result:self._finish_snapshot_recovery(value,None))
        self.app.start_background(worker,label="历史推荐快照恢复")

    def _finish_snapshot_recovery(self,result,error):
        self._recommendation_local_active=False;self._set_recommendation_task_mode(None)
        if error:
            self.recommendation_status.config(text=f"历史快照恢复失败：{error}",fg=COLORS["danger"])
            messagebox.showerror("历史快照恢复失败",str(error),parent=self);return
        self._refresh_recommendations()
        details="\n".join(
            f"{row['task_id']}：{row['status']}；{row['reason']}"
            for row in result.get("details",[])
        )
        headline=(
            f"检查{result.get('tasks_checked',0)}轮，符合严格条件{result.get('tasks_eligible',0)}轮；"
            f"本次恢复{result.get('tasks_restored',0)}轮、{result.get('observations_inserted',0)}条观察，"
            f"生成{result.get('outcomes_inserted',0)}个周期。\n"
            f"历史候选基金证据闭合{result.get('candidate_funds_kept',0)}只，"
            f"证据不足留空{result.get('candidate_funds_dropped',0)}只。"
        )
        show_nonmodal_task_result(
            self,"历史V3推荐快照恢复",headline+("\n\n"+details if details else "")+
            "\n\n本次未联网、未调用DeepSeek，也未改动真实交易账本。",
        )

    def refresh_recommendation_cached(self):
        if self._recommendation_sync_active or self._recommendation_local_active:
            messagebox.showinfo("请稍候","另一项推荐表现任务正在后台运行，请等待完成后再重算。",parent=self);return
        self._recommendation_local_active=True;self._set_recommendation_task_mode("local")
        self.recommendation_status.config(
            text="正在后台使用已缓存行情重算推荐表现；不联网、不调用DeepSeek……",fg=COLORS["info"],
        )
        def worker():
            try:result=RecommendationPerformanceService(self.db).refresh_cached()
            except Exception as exc:self.after(0,lambda error=exc:self._finish_cached_recommendation_refresh(None,error))
            else:self.after(0,lambda value=result:self._finish_cached_recommendation_refresh(value,None))
        self.app.start_background(worker,label="本地推荐表现重算")

    def _finish_cached_recommendation_refresh(self,result,error):
        self._recommendation_local_active=False;self._set_recommendation_task_mode(None)
        if error:
            self.recommendation_status.config(text=f"推荐表现重算失败：{error}",fg=COLORS["danger"])
            messagebox.showerror("推荐表现重算失败",str(error),parent=self);return
        self._refresh_recommendations()
        messagebox.showinfo(
            "本地重算完成",
            f"检查{result.get('total',0)}个周期结果；指数成熟{result.get('index_mature',0)}条，"
            f"其中仅指数研究已完成{result.get('index_only_mature',0)}条，"
            f"基金费后成熟{result.get('fund_mature',0)}条。\n\n本次没有联网，也没有调用DeepSeek。",
            parent=self,
        )

    def sync_recommendation_navs(self):
        if self._recommendation_sync_active or self._recommendation_local_active:
            messagebox.showinfo("正在更新","推荐表现任务正在后台运行；联网净值任务可点‘停止净值更新’。",parent=self);return
        self._recommendation_sync_active=True;self._recommendation_stop.clear()
        self._set_recommendation_task_mode("network")
        self.recommendation_status.config(text="正在后台下载推荐候选的正式历史净值；不会调用DeepSeek，也不会创建交易……",fg=COLORS["info"])
        service=RecommendationPerformanceService(self.db)
        def progress(done,total,code):
            self.after(0,lambda d=done,t=total,value=code:self._update_recommendation_progress(d,t,value))
        def worker():
            try:result=service.sync_candidate_navs(stop_requested=self._recommendation_stop.is_set,progress=progress)
            except Exception as exc:self.after(0,lambda error=exc:self._finish_recommendation_sync(None,error))
            else:self.after(0,lambda:self._finish_recommendation_sync(result,None))
        self.app.start_background(
            worker,label="推荐表现净值更新",cancel=self._recommendation_stop.set,
        )

    def stop_recommendation_sync(self):
        if not self._recommendation_sync_active:return
        self._recommendation_stop.set()
        self.recommendation_status.config(text="已请求停止；当前基金请求结束后不再下载下一只。",fg=COLORS["warning"])

    def _finish_recommendation_sync(self,result,error):
        self._recommendation_sync_active=False;self._set_recommendation_task_mode(None)
        if error:
            self.recommendation_status.config(text=f"推荐净值更新失败：{error}",fg=COLORS["danger"])
            messagebox.showerror("推荐净值更新失败",str(error),parent=self);return
        self._refresh_recommendations()
        stopped=self._recommendation_stop.is_set()
        text=(
            f"{'已停止' if stopped else '已完成'}：候选基金{result['funds_updated']}/{result['funds_total']}只，"
            f"保存净值{result['nav_rows_saved']}条，错误{len(result['errors'])}项；"
            f"基金费后成熟{result['outcomes'].get('fund_mature',0)}条。"
        )
        self.recommendation_status.config(text=text,fg=COLORS["warning"] if result["errors"] or stopped else COLORS["accent"])
        if result["errors"]:show_nonmodal_task_result(self,"推荐净值更新结果",text+"\n\n"+"\n".join(result["errors"][:30]))
        else:messagebox.showinfo("推荐净值更新完成",text+"\n\n未调用DeepSeek，也没有生成交易。",parent=self)

    def _set_recommendation_task_mode(self,mode):
        active=mode is not None
        self.recover_recommendation_button.set_enabled(not active)
        self.recompute_recommendation_button.set_enabled(not active)
        self.sync_recommendation_button.set_enabled(not active)
        self.stop_recommendation_button.set_enabled(mode=="network")
        self.recommendation_progress.stop()
        if mode=="local":
            self.recommendation_progress.configure(mode="indeterminate",maximum=100,value=0)
            self.recommendation_progress.start(12)
        else:
            self.recommendation_progress.configure(mode="determinate",maximum=1,value=0)

    def _update_recommendation_progress(self,done,total,code):
        self.recommendation_progress.configure(mode="determinate",maximum=max(1,total),value=done)
        self.recommendation_status.config(
            text=f"净值更新 {done}/{total}：{code}；界面仍可继续使用，停止按钮会在当前基金完成后生效。",
            fg=COLORS["info"],
        )

    def _draw_curve(self,trades,cumulative):
        self.canvas.delete("all");self.canvas.update_idletasks();w=max(self.canvas.winfo_width(),900);h=max(self.canvas.winfo_height(),260)
        title_item=self.canvas.create_text(18,14,anchor="nw",text="按完整持有段累计的费后已实现盈亏",font=(FONT,FONT_SIZE["section"],"bold"),fill=COLORS["text"])
        # Canvas coordinates are pixels, while Tk font sizes grow with DPI.
        # Fixed y=14/48 overlapped at the user's 225% scaling even though it
        # looked fine at 100%. Place following regions from rendered bounds.
        title_box=self.canvas.bbox(title_item) or (18,14,18,14)
        subtitle_y=title_box[3]+max(6,int(round(4*self.app.ui_pixel_scale)))
        subtitle_item=self.canvas.create_text(18,subtitle_y,anchor="nw",text="每个点代表一个完整持有段；加仓和部分卖出不会单独制造胜负。",font=(FONT,FONT_SIZE["micro"]),fill=COLORS["muted"])
        subtitle_box=self.canvas.bbox(subtitle_item) or (18,subtitle_y,18,subtitle_y)
        top=max(108,subtitle_box[3]+max(20,int(round(10*self.app.ui_pixel_scale))))
        bottom=h-48
        if not trades:self.canvas.create_text(w/2,(top+bottom)/2,text="当前筛选没有已核算的完整持有段。",font=(FONT,FONT_SIZE["body"]),fill=COLORS["muted"]);return
        left,right=108,w-34;ymin=min(0,min(cumulative));ymax=max(0,max(cumulative));span=ymax-ymin
        if span<1:ymin-=1;ymax+=1;span=ymax-ymin
        for i in range(5):
            value=ymax-span*i/4;y=top+(bottom-top)*i/4
            self.canvas.create_line(left,y,right,y,fill="#F0E5DF")
            self.canvas.create_text(left-8,y,text=money(value),anchor="e",font=(FONT,FONT_SIZE["micro"]),fill=COLORS["muted"])
        def xy(index,value):
            x=(left+right)/2 if len(cumulative)==1 else left+(right-left)*index/(len(cumulative)-1)
            y=bottom-(value-ymin)/span*(bottom-top);return x,y
        points=[]
        for index,value in enumerate(cumulative):points.extend(xy(index,value))
        if len(cumulative)>1:self.canvas.create_line(*points,fill=COLORS["accent"],width=3,smooth=True)
        for index,value in enumerate(cumulative):
            x,y=xy(index,value);self.canvas.create_oval(x-4,y-4,x+4,y+4,fill=COLORS["profit"] if float(trades[index]["result_pnl"])>=0 else COLORS["loss"],outline="white")
        if len(cumulative)==1:
            self.canvas.create_text((left+right)/2,bottom+18,text=trades[0]["end_date"],anchor="n",font=(FONT,FONT_SIZE["micro"]),fill=COLORS["muted"])
        else:
            self.canvas.create_text(left,bottom+18,text=trades[0]["end_date"],anchor="w",font=(FONT,FONT_SIZE["micro"]),fill=COLORS["muted"])
            self.canvas.create_text(right,bottom+18,text=trades[-1]["end_date"],anchor="e",font=(FONT,FONT_SIZE["micro"]),fill=COLORS["muted"])
        x,y=xy(len(cumulative)-1,cumulative[-1])
        label_below=y<top+38
        self.canvas.create_text(
            x,y+(14 if label_below else -14),text=money(cumulative[-1]),
            anchor="n" if label_below else "s",font=(FONT,FONT_SIZE["small"],"bold"),
            fill=COLORS["profit"] if cumulative[-1]>=0 else COLORS["loss"],
        )


class HelpPage(BasePage):
    title = "📖 使用指南"
    subtitle = "说明已经放进软件里；像游戏玩法介绍一样，点击章节就能阅读。"

    def __init__(self, parent, app):
        super().__init__(parent, app)
        shell = Card(self.body)
        shell.pack(fill="both", expand=True)

        index = tk.Frame(shell, bg="#FFF3DE", width=310)
        index.pack(side="left", fill="y")
        index.pack_propagate(False)
        tk.Label(
            index, text="教程目录", bg="#FFF3DE", fg=COLORS["primary_dark"],
            font=(FONT, FONT_SIZE["section"], "bold"),
        ).pack(anchor="w", padx=18, pady=(18, 10))
        tk.Label(
            index, text="按顺序阅读，也可以随时回来查。", justify="left",
            bg="#FFF3DE", fg=COLORS["muted"], font=(FONT, FONT_SIZE["small"]),
            wraplength=270,
        ).pack(anchor="w", padx=18, pady=(0, 12))

        self.topic_buttons = []
        self.topic_rows = []
        self.topic_hints = []
        for topic_index, (topic_title, _content) in enumerate(HELP_TOPICS):
            topic_row = tk.Frame(index, bg="#FFF3DE")
            topic_row.pack(fill="x", padx=8, pady=2)
            button = tk.Button(
                topic_row, text=f"{topic_index + 1}. {topic_title}", anchor="w",
                command=lambda selected=topic_index: self.show_topic(selected),
                bg="#FFF3DE", fg=COLORS["text"], activebackground="#F5D9DD",
                activeforeground=COLORS["primary_dark"], relief="flat", bd=0,
                padx=16, pady=5, font=(FONT, FONT_SIZE["body"], "bold"),
                cursor="hand2",
            )
            button.pack(fill="x")
            hint = tk.Label(
                topic_row, text="点击打开这一章", anchor="w", bg="#FFF3DE",
                fg=COLORS["muted"], padx=18, pady=2,
                font=(FONT, FONT_SIZE["hint"]),
            )
            hint.pack(fill="x")
            self.topic_buttons.append(button)
            self.topic_rows.append(topic_row)
            self.topic_hints.append(hint)

        article = tk.Frame(shell, bg=COLORS["panel"])
        article.pack(side="left", fill="both", expand=True)
        self.article_title = tk.Label(
            article, text="", bg=COLORS["panel"], fg=COLORS["text"],
            font=(FONT, FONT_SIZE["title"], "bold"), anchor="w",
        )
        self.article_title.pack(fill="x", padx=28, pady=(22, 10))
        reading = tk.Frame(article, bg=COLORS["panel"])
        reading.pack(fill="both", expand=True, padx=(28, 18), pady=(0, 22))
        scrollbar = ttk.Scrollbar(reading, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.article = tk.Text(
            reading, wrap="word", relief="flat", bd=0, bg=COLORS["panel"],
            fg=COLORS["text"], font=(FONT, FONT_SIZE["body"]), padx=4, pady=4,
            spacing1=4, spacing2=3, spacing3=8, cursor="arrow",
            yscrollcommand=scrollbar.set,
        )
        self.article.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.article.yview)
        self.article.tag_configure(
            "section", foreground=COLORS["primary_dark"],
            font=(FONT, FONT_SIZE["section"], "bold"), spacing1=10, spacing3=8,
        )
        self.show_topic(0)

    def show_topic(self, selected: int):
        title, content = HELP_TOPICS[selected]
        self.article_title.config(text=title)
        self.article.config(state="normal")
        self.article.delete("1.0", "end")
        for line in content.splitlines():
            tag = "section" if line.startswith("【") and line.endswith("】") else None
            self.article.insert("end", line + "\n", tag)
        self.article.config(state="disabled")
        self.article.yview_moveto(0)
        for index, button in enumerate(self.topic_buttons):
            selected_now = index == selected
            bg = "#F5D9DD" if selected_now else "#FFF3DE"
            button.config(
                bg=bg,
                fg=COLORS["primary_dark"] if selected_now else COLORS["text"],
            )
            self.topic_rows[index].config(bg=bg)
            self.topic_hints[index].config(bg=bg)


class SettingsPage(BasePage):
    title="⚙️ 设置与备份"
    subtitle="风险策略与数据安全分开管理；所有目录跟随软件位置，重要资料不会被自动清理。"
    def __init__(self,parent,app):
        super().__init__(parent,app)
        self._inventory_active=False
        self._cleanup_active=False
        self._last_storage_inventory={}
        self.tabs=ttk.Notebook(self.body);self.tabs.pack(fill="both",expand=True)
        self.risk_scroller=ScrollableContent(self.tabs)
        self.storage_scroller=ScrollableContent(self.tabs)
        self.risk_tab=self.risk_scroller.content
        self.storage_tab=self.storage_scroller.content
        self.tabs.add(self.risk_scroller,text="风险与排名")
        self.tabs.add(self.storage_scroller,text="存储与备份")

        self.form=Card(self.risk_tab);self.form.pack(fill="x",pady=(10,0))
        specs=[("主动资金上限 元","active_capital_limit"),("单指数上限 元","single_position_limit"),("相关性桶上限 元","correlation_bucket_limit"),("最多同时主题数","max_active_themes"),("软止损 毛收益%","soft_stop_gross_pct"),("硬止损 毛收益%","hard_stop_gross_pct"),("组合暂停回撤%","portfolio_pause_pct"),("组合停止回撤%","portfolio_stop_pct"),("内部截止时间","internal_cutoff")]
        self.vars={}
        for i,(label,key) in enumerate(specs):
            tk.Label(self.form,text=label,bg=COLORS["panel"],fg=COLORS["text"],font=(FONT,FONT_SIZE["body"])).grid(row=i//3*2,column=i%3,sticky="w",padx=16,pady=(12,3));v=tk.StringVar();self.vars[key]=v;ttk.Entry(self.form,textvariable=v,width=25).grid(row=i//3*2+1,column=i%3,sticky="ew",padx=16,pady=(0,10))
        for c in range(3):self.form.columnconfigure(c,weight=1)
        self.ranking_form=Card(self.risk_tab);self.ranking_form.pack(fill="x",pady=(12,0))
        tk.Label(
            self.ranking_form,text="四维排名策略（只影响下一轮研究）",bg=COLORS["panel"],
            fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["section"],"bold"),
        ).grid(row=0,column=0,columnspan=3,sticky="w",padx=16,pady=(14,2))
        tk.Label(
            self.ranking_form,
            text="五项机会权重之和必须等于1；拥挤度50为中性，低于50适度加分、高于50对称扣分；数据置信度只作为执行门槛。每轮任务冻结当时策略，历史结果不会被改写。",
            bg=COLORS["panel"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["hint"]),
            justify="left",wraplength=1450,
        ).grid(row=1,column=0,columnspan=3,sticky="w",padx=16,pady=(0,8))
        ranking_specs=[
            ("趋势机会权重","ranking_trend_weight"),("入场性价比权重","ranking_entry_weight"),("催化权重","ranking_catalyst_weight"),
            ("相对强弱权重","ranking_relative_strength_weight"),("量价/真实扩散权重","ranking_volume_breadth_weight"),("拥挤调整系数（50为中性）","ranking_crowding_penalty"),
            ("数据置信度最低线","ranking_data_confidence_min"),("主候选绝对合格线","ranking_candidate_min_score"),("池目标下限（不足不补）","ranking_pool_min"),
            ("候选池最多","ranking_pool_max"),("候选池同桶最多","ranking_pool_cluster_max"),("主榜同桶最多","ranking_display_cluster_max"),
            ("同桶获资金最多","ranking_funded_cluster_max"),
        ]
        for i,(label,key) in enumerate(ranking_specs):
            row=2+(i//3)*2;column=i%3
            tk.Label(self.ranking_form,text=label,bg=COLORS["panel"],fg=COLORS["text"],font=(FONT,FONT_SIZE["body"])).grid(row=row,column=column,sticky="w",padx=16,pady=(8,3))
            value=tk.StringVar();self.vars[key]=value
            ttk.Entry(self.ranking_form,textvariable=value,width=25).grid(row=row+1,column=column,sticky="ew",padx=16,pady=(0,8))
        for c in range(3):self.ranking_form.columnconfigure(c,weight=1)
        bar=tk.Frame(self.risk_tab,bg=COLORS["bg"]);bar.pack(fill="x",pady=12)
        CuteButton(bar,"💾 保存设置",self.save).pack(side="left")
        tk.Label(
            bar,text="主动资金上限发生变化时必须填写原因，并写入真实现金流水。",
            bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),
        ).pack(side="left",padx=14)

        self._build_storage_tab()
        self.risk_scroller.bind_mousewheel_tree()
        self.storage_scroller.bind_mousewheel_tree()

    def _build_storage_tab(self):
        location=Card(self.storage_tab);location.pack(fill="x",pady=(10,10))
        tk.Label(
            location,text="当前位置（搬盘时复制整个应用文件夹）",bg=COLORS["panel"],
            fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["section"],"bold"),
        ).grid(row=0,column=0,columnspan=3,sticky="w",padx=16,pady=(13,4))
        self.program_path_label=tk.Label(
            location,text=f"程序：{app_root()}",bg=COLORS["panel"],fg=COLORS["text"],
            font=(FONT,FONT_SIZE["small"]),anchor="w",justify="left",wraplength=700,
        )
        self.program_path_label.grid(row=1,column=0,columnspan=3,sticky="ew",padx=16,pady=(2,2))
        self.data_path_label=tk.Label(
            location,text=f"数据：{DB_PATH.parent}",bg=COLORS["panel"],fg=COLORS["text"],
            font=(FONT,FONT_SIZE["small"]),anchor="w",justify="left",wraplength=700,
        )
        self.data_path_label.grid(row=2,column=0,columnspan=3,sticky="ew",padx=16,pady=(0,8))
        for column,(text_value,path) in enumerate([
            ("📂 打开程序目录",app_root()),
            ("📂 打开数据目录",DB_PATH.parent),
            ("📂 打开备份目录",BACKUP_DIR),
        ]):
            button=CuteButton(location,text_value,lambda p=path:open_path(p),"secondary")
            button.grid(row=3,column=column,sticky="ew",padx=(16 if column==0 else 5,16 if column==2 else 5),pady=(0,13))
            location.columnconfigure(column,weight=1,uniform="storage-path")

        overview=Card(self.storage_tab);overview.pack(fill="x",pady=(0,10))
        head=tk.Frame(overview,bg=COLORS["panel"]);head.pack(fill="x",padx=16,pady=(12,7))
        tk.Label(head,text="空间分类",bg=COLORS["panel"],fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["section"],"bold")).pack(side="left")
        self.storage_status=tk.Label(
            head,text="尚未统计",bg=COLORS["panel"],fg=COLORS["muted"],
            font=(FONT,FONT_SIZE["small"]),anchor="e",
        );self.storage_status.pack(side="right")
        metric_grid=tk.Frame(overview,bg=COLORS["panel"]);metric_grid.pack(fill="x",padx=11,pady=(0,12))
        self.storage_metric_labels={}
        metrics=[
            ("automatic_backups","自动数据库备份","最新N份＋保留期内每天1份"),
            ("deployment_backups","部署前数据库备份","仅点“立即安全清理”后处理"),
            ("protected_backups","人工/历史备份","始终保护，软件绝不删除"),
            ("rollback_directories","可回退旧版本","最新N份＋保留期内每天1份"),
            ("migration_directories","迁移演练副本","只保留近期健康副本"),
            ("diagnostic_exports","脱敏诊断","超过数量和天数才清理"),
            ("protected_exports","CSV/截图等导出","始终保护，不自动删除"),
        ]
        for index,(key,title,note) in enumerate(metrics):
            row,column=divmod(index,3)
            tile=tk.Frame(metric_grid,bg="#FFFCFA",highlightbackground=COLORS["border"],highlightthickness=1)
            tile.grid(row=row,column=column,sticky="nsew",padx=4,pady=4)
            tk.Label(tile,text=title,bg="#FFFCFA",fg=COLORS["text"],font=(FONT,FONT_SIZE["small"],"bold"),anchor="w").pack(fill="x",padx=11,pady=(8,1))
            value=tk.Label(tile,text="—",bg="#FFFCFA",fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["body"],"bold"),anchor="w")
            value.pack(fill="x",padx=11)
            tk.Label(tile,text=note,bg="#FFFCFA",fg=COLORS["muted"],font=(FONT,FONT_SIZE["hint"]),anchor="w").pack(fill="x",padx=11,pady=(1,8))
            self.storage_metric_labels[key]=value
            metric_grid.columnconfigure(column,weight=1,uniform="storage-metric")

        policy=Card(self.storage_tab);policy.pack(fill="x",pady=(0,10))
        tk.Label(policy,text="自动保留策略",bg=COLORS["panel"],fg=COLORS["primary_dark"],font=(FONT,FONT_SIZE["section"],"bold")).grid(row=0,column=0,columnspan=3,sticky="w",padx=16,pady=(12,2))
        tk.Label(
            policy,text="自动备份、部署前备份与回退程序都始终保留最新N份，并在设定天数内每天再留1份；部署前备份只在你点击“立即安全清理”并确认后处理，人工备份等资料永不自动删除。",
            bg=COLORS["panel"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),anchor="w",
        ).grid(row=1,column=0,columnspan=3,sticky="ew",padx=16,pady=(0,5))
        policy_specs=[
            ("自动备份始终保留最新份数","backup_keep_count"),("每日备份点保留天数","backup_keep_days"),
            ("部署前备份始终保留份数","deployment_backup_keep_count"),("部署前每日点保留天数","deployment_backup_keep_days"),
            ("回退版本始终保留最新份数","rollback_keep_count"),("每日回退点保留天数","rollback_keep_days"),
            ("脱敏诊断至少保留份数","diagnostic_keep_count"),("脱敏诊断至少保留天数","diagnostic_keep_days"),
        ]
        self.storage_vars={}
        for index,(label,key) in enumerate(policy_specs):
            row=2+(index//3)*2;column=index%3
            tk.Label(policy,text=label,bg=COLORS["panel"],fg=COLORS["text"],font=(FONT,FONT_SIZE["small"])).grid(row=row,column=column,sticky="w",padx=16,pady=(6,2))
            value=tk.StringVar();self.storage_vars[key]=value
            ttk.Entry(policy,textvariable=value,width=22).grid(row=row+1,column=column,sticky="ew",padx=16,pady=(0,9))
            policy.columnconfigure(column,weight=1)

        actions=tk.Frame(self.storage_tab,bg=COLORS["bg"]);actions.pack(fill="x",pady=(0,8))
        self.save_storage_button=CuteButton(actions,"💾 保存存储策略",self.save_storage);self.save_storage_button.pack(side="left")
        self.inventory_button=CuteButton(actions,"📊 重新统计占用",self.refresh_storage_inventory,"secondary");self.inventory_button.pack(side="left",padx=7)
        self.cleanup_button=CuteButton(actions,"🧹 立即安全清理",self.cleanup_storage,"danger");self.cleanup_button.pack(side="left")
        CuteButton(actions,"📦 创建完整备份",self.backup,"success").pack(side="right")
        CuteButton(actions,"♻️ 恢复数据库",self.restore,"danger").pack(side="right",padx=7)

    def refresh(self):
        s=self.db.settings()
        for k,v in self.vars.items():v.set(s.get(k,""))
        for k,v in self.storage_vars.items():v.set(s.get(k,""))
        self.program_path_label.config(text=f"程序：{app_root()}")
        self.data_path_label.config(text=f"数据：{DB_PATH.parent}")
        self.refresh_storage_inventory()

    def save_storage(self):
        ranges={
            "backup_keep_count":(3,100,"自动备份始终保留的最新份数"),
            "backup_keep_days":(1,3650,"每日备份点保留天数"),
            "deployment_backup_keep_count":(2,20,"部署前备份始终保留的最新份数"),
            "deployment_backup_keep_days":(7,3650,"部署前每日备份点保留天数"),
            "rollback_keep_count":(2,20,"回退版本始终保留的最新份数"),
            "rollback_keep_days":(7,3650,"每日回退点保留天数"),
            "diagnostic_keep_count":(5,200,"脱敏诊断保留份数"),
            "diagnostic_keep_days":(30,3650,"脱敏诊断保留天数"),
        }
        values={}
        for key,(minimum,maximum,label) in ranges.items():
            raw=self.storage_vars[key].get().strip()
            try:value=int(raw)
            except ValueError:
                messagebox.showerror("格式错误",f"{label}必须是整数。",parent=self);return
            if not minimum<=value<=maximum:
                messagebox.showerror("范围错误",f"{label}必须在 {minimum}～{maximum} 之间。",parent=self);return
            values[key]=str(value)
        self.db.save_settings(values)
        messagebox.showinfo(
            "存储策略已保存",
            "新规则已经保存。只有软件能够严格确认来源的自动备份、旧运行版本、迁移演练副本和脱敏诊断会受规则管理；人工资料继续受到保护。",
            parent=self,
        )
        self.refresh_storage_inventory()

    def refresh_storage_inventory(self):
        if self._inventory_active or self._cleanup_active:return
        self._inventory_active=True
        self.storage_status.config(text="正在后台统计…",fg=COLORS["info"])
        self.inventory_button.set_enabled(False)
        self.cleanup_button.set_enabled(False)
        def worker():
            try:
                settings=self.db.settings()
                result=storage_inventory(
                    RUNTIME_DIR,BACKUP_DIR,EXPORT_DIR,program_root=app_root(),
                    settings=settings,
                )
                self.after(0,lambda r=result:self._finish_storage_inventory(r,None))
            except Exception as exc:
                self.after(0,lambda e=str(exc):self._finish_storage_inventory(None,e))
        self.app.start_background(worker,label="统计存储占用")

    def _finish_storage_inventory(self,result,error):
        self._inventory_active=False
        self.inventory_button.set_enabled(True)
        self.cleanup_button.set_enabled(not self._cleanup_active)
        if error:
            self.storage_status.config(text=f"统计失败：{error[:120]}",fg=COLORS["danger"])
            return
        self._last_storage_inventory=dict(result or {})
        for key,label in self.storage_metric_labels.items():
            metric=(result or {}).get(key,{})
            text=f"{int(metric.get('count') or 0)} 项 · {format_bytes(metric.get('bytes'))}"
            if key in {"automatic_backups","deployment_backups","rollback_directories"} and int(metric.get("cleanup_candidates") or 0):
                text+=f"｜可清理 {int(metric['cleanup_candidates'])} 份 · {format_bytes(metric.get('cleanup_bytes'))}"
            if key in {"automatic_backups","deployment_backups"} and int(metric.get("sidecar_cleanup_candidates") or 0):
                text+=(
                    f"｜空边车 {int(metric['sidecar_cleanup_candidates'])} 个 · "
                    f"{format_bytes(metric.get('sidecar_cleanup_bytes'))}"
                )
            label.config(text=text)
        self.storage_status.config(
            text=f"统计完成 · {local_now().strftime('%H:%M:%S')}",fg=COLORS["accent"],
        )

    def cleanup_storage(self):
        if self._cleanup_active or self._inventory_active:
            messagebox.showinfo("维护任务正在运行","请等待当前存储统计或清理完成。",parent=self);return
        automatic=(self._last_storage_inventory or {}).get("automatic_backups",{})
        deployment=(self._last_storage_inventory or {}).get("deployment_backups",{})
        rollback=(self._last_storage_inventory or {}).get("rollback_directories",{})
        if automatic or deployment or rollback:
            rollback_preview=(
                "\n\n当前预览："
                f"自动备份可清理 {int(automatic.get('cleanup_candidates') or 0)} 份（{format_bytes(automatic.get('cleanup_bytes'))}）；"
                f"其中空WAL/SHM边车 {int(automatic.get('sidecar_cleanup_candidates') or 0)} 个（{format_bytes(automatic.get('sidecar_cleanup_bytes'))}）；"
                f"部署前备份可清理 {int(deployment.get('cleanup_candidates') or 0)} 份（{format_bytes(deployment.get('cleanup_bytes'))}）；"
                f"其中空WAL/SHM边车 {int(deployment.get('sidecar_cleanup_candidates') or 0)} 个（{format_bytes(deployment.get('sidecar_cleanup_bytes'))}）；"
                f"已核验旧版本可清理 {int(rollback.get('cleanup_candidates') or 0)} 份（{format_bytes(rollback.get('cleanup_bytes'))}）。"
            )
        else:
            rollback_preview="\n\n尚无最新占用预览；清理前仍会按当前规则重新核验每个目标。"
        if not messagebox.askyesno(
            "确认安全清理",
            "只会处理：超过已保存规则的自动数据库备份、严格识别且完整的部署前数据库备份、已核验旧运行版本、迁移演练副本、脱敏API诊断、残留_MEI目录和DeepSeek临时文件。"
            +rollback_preview+
            "\n\n人工备份、任务包、交易CSV、截图、正式数据库及无法确认来源的文件都不会删除。继续吗？",
            parent=self,
        ):return
        self._cleanup_active=True
        self.storage_status.config(text="正在后台执行安全清理…",fg=COLORS["warning"])
        self.cleanup_button.set_enabled(False);self.inventory_button.set_enabled(False)
        self.save_storage_button.set_enabled(False)
        def worker():
            try:
                settings=self.db.settings()
                backups=self.db.prune_backups(include_deployment=True)
                result=cleanup_managed_storage(
                    RUNTIME_DIR,EXPORT_DIR,settings=settings,program_root=app_root(),
                )
                inventory=storage_inventory(
                    RUNTIME_DIR,BACKUP_DIR,EXPORT_DIR,program_root=app_root(),
                    settings=settings,
                )
                self.db.save_settings({
                    "storage_cleanup_last_at":local_now().isoformat(timespec="seconds"),
                    "storage_cleanup_last_result":json.dumps(
                        {"backups":backups,"managed":result},ensure_ascii=False,
                    ),
                    "storage_cleanup_last_error":"",
                })
                self.after(0,lambda:self._finish_storage_cleanup(backups,result,inventory,None))
            except Exception as exc:
                try:self.db.save_settings({"storage_cleanup_last_error":str(exc)[:2000]})
                except Exception:pass
                self.after(0,lambda e=str(exc):self._finish_storage_cleanup(None,None,None,e))
        self.app.start_background(worker,label="安全清理存储")

    def _finish_storage_cleanup(self,backups,result,inventory,error):
        self._cleanup_active=False
        self.cleanup_button.set_enabled(True);self.inventory_button.set_enabled(True)
        self.save_storage_button.set_enabled(True)
        if error:
            self.storage_status.config(text=f"清理失败：{error[:120]}",fg=COLORS["danger"])
            show_nonmodal_task_result(self,"安全清理失败",error)
            return
        self._finish_storage_inventory(inventory,None)
        auto_removed=int((backups or {}).get("removed") or 0)
        sidecar_removed=int((backups or {}).get("sidecars_removed") or 0)
        deployment_removed=int((backups or {}).get("deployment_removed") or 0)
        deployment_sidecar_removed=int((backups or {}).get("deployment_sidecars_removed") or 0)
        managed_removed=int((result or {}).get("removed") or 0)
        freed=(
            int((result or {}).get("bytes_freed") or 0)
            +int((backups or {}).get("removed_bytes") or 0)
            +int((backups or {}).get("sidecar_bytes_freed") or 0)
            +int((backups or {}).get("deployment_removed_bytes") or 0)
            +int((backups or {}).get("deployment_sidecar_bytes_freed") or 0)
        )
        error_rows=[]
        for key in ("runtime_cache","rollback_directories","migration_directories","diagnostic_exports"):
            error_rows.extend(str(item) for item in ((result or {}).get(key,{}) or {}).get("errors",[]))
        message=(
            f"安全清理完成。\n\n"
            f"自动数据库备份：清理 {auto_removed} 份；空WAL/SHM边车 {sidecar_removed} 个\n"
            f"部署前数据库备份：清理 {deployment_removed} 份；空WAL/SHM边车 {deployment_sidecar_removed} 个\n"
            f"运行缓存、回退版本、迁移副本和脱敏诊断：清理 {managed_removed} 项\n"
            f"本次合计释放：{format_bytes(freed)}\n\n"
            "人工备份、任务包、交易CSV、截图、正式数据库和未识别文件均未处理。"
        )
        if error_rows:message += "\n\n个别文件未能处理：\n"+"\n".join(error_rows[:10])
        show_nonmodal_task_result(self,"安全清理完成",message)
    def save(self):
        try:
            for k,v in self.vars.items():
                if k!="internal_cutoff":float(v.get())
        except ValueError:messagebox.showerror("格式错误","除时间外的设置必须是数字。",parent=self);return
        values={k:v.get().strip() for k,v in self.vars.items()}
        try:
            ResearchOrchestrator._validated_ranking_policy({
                "trend_weight":values["ranking_trend_weight"],
                "entry_weight":values["ranking_entry_weight"],
                "catalyst_weight":values["ranking_catalyst_weight"],
                "relative_strength_weight":values["ranking_relative_strength_weight"],
                "volume_breadth_weight":values["ranking_volume_breadth_weight"],
                "crowding_penalty":values["ranking_crowding_penalty"],
                "data_confidence_min":values["ranking_data_confidence_min"],
                "candidate_min_score":values["ranking_candidate_min_score"],
                "pool_min":values["ranking_pool_min"],
                "pool_max":values["ranking_pool_max"],
                "pool_cluster_max":values["ranking_pool_cluster_max"],
                "display_cluster_max":values["ranking_display_cluster_max"],
                "funded_cluster_max":values["ranking_funded_cluster_max"],
                "version":self.db.setting("ranking_policy_version",RANKING_FORMULA_VERSION),
            })
        except DeepSeekError as exc:
            messagebox.showerror("排名策略无效",str(exc),parent=self);return
        old_limit=float(self.db.setting("active_capital_limit","8000"));new_limit=float(values["active_capital_limit"])
        if abs(new_limit-old_limit)>=0.005:
            reason=simpledialog.askstring("说明额度调整原因",f"主动资金上限将从{money(old_limit)}调整为{money(new_limit)}。\n这会同步增减真实现金账本，而不是改动持仓市值。请简要填写原因：",parent=self)
            if not reason:return
            try:PortfolioLedgerService(self.db).adjust_active_capital_limit(new_limit,reason)
            except LedgerError as exc:messagebox.showerror("额度调整失败",str(exc),parent=self);return
        values.pop("active_capital_limit",None)
        self.db.save_settings(values);messagebox.showinfo("已保存","额度、风险与四维排名策略已经保存。额度变化已写入现金流水；排名策略从下一轮研究生效，历史轮次保持冻结。",parent=self);self.app.refresh_all()
    def backup(self):
        stamp=local_now().strftime("%Y%m%d_%H%M%S");target=filedialog.asksaveasfilename(parent=self,initialdir=BACKUP_DIR,initialfile=f"板块雷达完整备份_{stamp}.sqlite3",defaultextension=".sqlite3",filetypes=[("板块雷达备份","*.sqlite3")])
        if not target:return
        self.db.backup(Path(target));messagebox.showinfo("备份完成",f"备份已保存：\n{target}",parent=self);self.refresh_storage_inventory()
    def restore(self):
        workers=self.app.active_background_workers()
        if workers:
            labels="、".join(sorted({meta.get("label","后台任务") for _thread,meta in workers}))
            messagebox.showwarning(
                "暂不能恢复",
                f"后台任务仍在使用数据库：{labels}。\n请等待任务结束后再恢复备份。",
                parent=self,
            );return
        source=filedialog.askopenfilename(parent=self,title="选择板块雷达数据库备份",initialdir=BACKUP_DIR,filetypes=[("板块雷达备份","*.sqlite3"),("所有文件","*.*")])
        if not source:return
        if not messagebox.askyesno("确认恢复","恢复会替换当前数据；软件会先自动创建安全副本。继续吗？",parent=self):return
        try:safety=self.db.restore(Path(source))
        except Exception as exc:messagebox.showerror("恢复失败",str(exc),parent=self);return
        messagebox.showinfo("恢复成功",f"已恢复。原数据安全副本：\n{safety}",parent=self);self.app.refresh_all()


class FormDialog(tk.Toplevel):
    def __init__(self,parent,title,fields,choices=None):
        super().__init__(parent);self.title(plain_ui_text(title));self.configure(bg=COLORS["bg"]);self.resizable(True,False);self.transient(parent);self.grab_set();self.result=None;self.vars={};choices=choices or {}
        frame=tk.Frame(self,bg=COLORS["bg"]);frame.pack(fill="both",expand=True,padx=20,pady=16)
        for i,(label,key,value) in enumerate(fields):
            tk.Label(frame,text=label,bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["small"],"bold")).grid(row=i,column=0,sticky="w",pady=7,padx=(0,14));var=tk.StringVar(value="" if value is None else str(value));self.vars[key]=var
            widget=ttk.Combobox(frame,textvariable=var,values=choices[key],state="readonly",width=42) if key in choices else ttk.Entry(frame,textvariable=var,width=45)
            widget.grid(row=i,column=1,sticky="ew",pady=5)
        buttons=tk.Frame(frame,bg=COLORS["bg"]);buttons.grid(row=len(fields),column=0,columnspan=2,sticky="e",pady=(14,0));CuteButton(buttons,"🚫 取消",self.destroy,"secondary").pack(side="left",padx=6);CuteButton(buttons,"✅ 保存",self._save).pack(side="left")
        frame.columnconfigure(1,weight=1);self.update_idletasks()
        screen_w,screen_h=self.winfo_screenwidth(),self.winfo_screenheight();req_w=max(760,self.winfo_reqwidth()+50);req_h=max(360,self.winfo_reqheight()+35)
        self.geometry(f"{min(req_w,int(screen_w*.88))}x{min(req_h,int(screen_h*.88))}");self.minsize(min(700,screen_w),min(320,screen_h));self.wait_window()
    def _save(self):self.result={k:v.get() for k,v in self.vars.items()};self.destroy()


class TextDialog(tk.Toplevel):
    def __init__(self,parent,title,instruction,initial=""):
        super().__init__(parent);self.title(plain_ui_text(title));self.configure(bg=COLORS["bg"]);self.transient(parent);self.grab_set();self.result=None
        tk.Label(self,text=instruction,justify="left",bg=COLORS["bg"],fg=COLORS["text"],font=(FONT,FONT_SIZE["body"]),wraplength=900).pack(anchor="w",padx=22,pady=(18,10));self.text=tk.Text(self,width=70,height=15,font=("Consolas",FONT_SIZE["body"]));self.text.pack(fill="both",expand=True,padx=22);self.text.insert("1.0",initial)
        bar=tk.Frame(self,bg=COLORS["bg"]);bar.pack(fill="x",padx=22,pady=16);CuteButton(bar,"🚫 取消",self.destroy,"secondary").pack(side="right",padx=6);CuteButton(bar,"✅ 保存",self._save).pack(side="right");self.geometry("900x620");self.minsize(720,480);self.wait_window()
    def _save(self):self.result=self.text.get("1.0","end").strip();self.destroy()


class SectorRadarApp(tk.Tk):
    def __init__(
        self,
        *,
        start_hidden: bool = False,
        database: Database | None = None,
        eager_pages: bool | None = None,
    ):
        super().__init__()
        if start_hidden:
            self.withdraw()
        self.reported_tk_scaling, self.applied_tk_scaling = normalize_tk_scaling(self)
        # Tk scales fonts automatically, but pixel paddings and Treeview row
        # heights do not. Keep a bounded companion factor for those values.
        self.ui_pixel_scale = min(1.70, max(1.0, self.applied_tk_scaling / 1.333333))
        try:
            print(
                f"TK_SCALING_REPORTED={self.reported_tk_scaling:.3f} "
                f"TK_SCALING_APPLIED={self.applied_tk_scaling:.3f}", flush=True,
            )
        except OSError:
            # The installed GUI is intentionally built without a console.
            pass
        self._normal_title=f"{APP_NAME} {APP_VERSION}"
        self.title(self._normal_title)
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        # Start near the available desktop size. Alpha.5 capped the window at
        # 1500x900 before DPI conversion, which could appear as only a small
        # fraction of a high-DPI screen.
        width = min(max(900, int(screen_w * 0.94)), max(640, screen_w - 24))
        height = min(max(620, int(screen_h * 0.90)), max(480, screen_h - 56))
        x, y = max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(1120, screen_w), min(700, screen_h))
        self.configure(bg=COLORS["bg"]);self.db=database or Database()
        self._recovered_interrupted_jobs=self.db.recover_interrupted_jobs()
        self._worker_lock=threading.Lock();self._background_workers={};self._closing=False
        self._destroying=False
        self._startup_after_ids=[]
        self._close_state=None;self._close_dialog=None;self._close_label=None;self._close_progress=None
        self._style()
        # Large reading-oriented navigation. The item list scrolls, so font and
        # hit targets stay generous even after adding more pages.
        nav_width = max(430, min(520, int(screen_w * 0.23)))
        nav=tk.Frame(self,bg=COLORS["nav"],width=nav_width);nav.pack(side="left",fill="y");nav.pack_propagate(False)
        brand=tk.Frame(nav,bg=COLORS["nav"]);brand.pack(fill="x",pady=(12,4))
        logo=tk.Canvas(brand,width=88,height=72,bg=COLORS["nav"],highlightthickness=0)
        logo.create_polygon(13,27,21,6,38,21,50,21,67,6,75,27,fill="#F4C7D0",outline=COLORS["primary_dark"],width=2)
        logo.create_oval(13,17,75,70,fill="#FFF7F4",outline=COLORS["primary_dark"],width=2)
        logo.create_oval(30,38,36,44,fill=COLORS["text"],outline="")
        logo.create_oval(52,38,58,44,fill=COLORS["text"],outline="")
        logo.create_polygon(41,49,47,49,44,53,fill=COLORS["primary"],outline="")
        logo.create_arc(34,47,44,58,start=250,extent=100,style="arc",outline=COLORS["text"],width=2)
        logo.create_arc(44,47,54,58,start=190,extent=100,style="arc",outline=COLORS["text"],width=2)
        logo.pack(side="left",padx=(22,6))
        brand_text=tk.Frame(brand,bg=COLORS["nav"]);brand_text.pack(side="left",fill="x",expand=True,pady=(8,0))
        tk.Label(brand_text,text="板块雷达",bg=COLORS["nav"],fg=COLORS["text"],font=(FONT,24,"bold")).pack(anchor="w")
        tk.Label(brand_text,text="猫猫研究驾驶舱",bg=COLORS["nav"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"])).pack(anchor="w")
        deco=tk.Frame(nav,bg=COLORS["nav"]);deco.pack(pady=(0,6))
        CuteSticker(deco,"heart",28,COLORS["nav"]).pack(side="left");CuteSticker(deco,"star",28,COLORS["nav"]).pack(side="left",padx=5);CuteSticker(deco,"paw",28,COLORS["nav"]).pack(side="left")
        self.container=tk.Frame(self,bg=COLORS["bg"]);self.container.pack(side="left",fill="both",expand=True)
        nav_area=tk.Frame(nav,bg=COLORS["nav"]);nav_area.pack(fill="both",expand=True,padx=(8,4))
        nav_canvas=tk.Canvas(nav_area,bg=COLORS["nav"],highlightthickness=0,width=nav_width-24)
        nav_scroll=ttk.Scrollbar(nav_area,orient="vertical",command=nav_canvas.yview)
        nav_canvas.configure(yscrollcommand=nav_scroll.set)
        nav_canvas.pack(side="left",fill="both",expand=True);nav_scroll.pack(side="right",fill="y")
        nav_list=tk.Frame(nav_canvas,bg=COLORS["nav"])
        self.nav_canvas=nav_canvas
        self.nav_list=nav_list
        nav_window=nav_canvas.create_window((0,0),window=nav_list,anchor="nw")
        nav_list.bind("<Configure>",lambda _e:nav_canvas.configure(scrollregion=nav_canvas.bbox("all")))
        nav_canvas.bind("<Configure>",lambda e:nav_canvas.itemconfigure(nav_window,width=e.width))
        nav_canvas.bind("<Enter>",lambda _e:nav_canvas.bind_all("<MouseWheel>",lambda e:nav_canvas.yview_scroll(-1 if e.delta>0 else 1,"units")))
        nav_canvas.bind("<Leave>",lambda _e:nav_canvas.unbind_all("<MouseWheel>"))
        page_groups = [
            ("每天先看", [
                ("decision","今日决策",DashboardPage),
                ("api","一键AI扫描",APIRadarPage),
                ("radar","夜间雷达",RadarPage),
            ]),
            ("研究与选择", [
                ("market","行情与映射",MarketDataPage),
                ("audit","中证代表目录",AuditPage),
                ("fundselect","基金优选",FundDiscoveryPage),
                ("ai","备用中转站",AIHubPage),
            ]),
            ("账户与复盘", [
                ("holdings","当前持仓",HoldingPage),
                ("trades","交易日志",TradePage),
                ("recurring","定投管理",RecurringPage),
                ("funds","基金库",FundPage),
                ("review","策略复盘",ReviewPage),
            ]),
            ("帮助与安全", [
                ("help","使用指南",HelpPage),
                ("settings","设置与备份",SettingsPage),
            ]),
        ]
        # A normal launch only constructs the first visible page.  Building all
        # fourteen pages up front performs many repeated catalog/database reads
        # and is especially noticeable while the program lives on a mechanical
        # drive.  Hidden self-tests keep the old eager behavior so every page is
        # still exercised before a release is accepted.
        self._eager_pages = start_hidden if eager_pages is None else bool(eager_pages)
        self.pages={};self.page_classes={};self.nav_buttons={};self.nav_rows={};self.nav_icons={};self.nav_textboxes={};self.nav_hints={};self.nav_indicators={};self._current_page_key=""
        for group_name, group_items in page_groups:
            tk.Label(
                nav_list, text=group_name, anchor="w", bg=COLORS["nav"],
                fg=COLORS["primary_dark"], font=(FONT, FONT_SIZE["micro"], "bold"),
                padx=18, pady=2,
            ).pack(fill="x", pady=(4, 0))
            for key,label,klass in group_items:
                row=tk.Frame(nav_list,bg=COLORS["nav"]);row.pack(fill="x",padx=8,pady=1)
                indicator=tk.Frame(row,bg=COLORS["nav"],width=5);indicator.pack(side="left",fill="y")
                icon_kind,color=PAGE_META[key]
                badge=Pictogram(row,icon_kind,color,36,COLORS["nav"])
                badge.pack(side="left",padx=(7,7),pady=2);badge.bind("<Button-1>",lambda _e,k=key:self.show(k))
                text_box=tk.Frame(row,bg=COLORS["nav"]);text_box.pack(side="left",fill="x",expand=True)
                b=tk.Button(text_box,text=label,anchor="w",command=lambda k=key:self.show(k),bg=COLORS["nav"],fg=COLORS["text"],activebackground="#EADAD3",activeforeground=COLORS["text"],relief="flat",bd=0,padx=8,pady=2,font=(FONT,FONT_SIZE["body"],"bold"),cursor="hand2")
                b.pack(fill="x")
                hint=tk.Label(text_box,text=NAV_HELP[key],anchor="w",bg=COLORS["nav"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["hint"]),padx=10,pady=0,cursor="hand2")
                hint.pack(fill="x");hint.bind("<Button-1>",lambda _e,k=key:self.show(k))
                self.nav_buttons[key]=b;self.nav_rows[key]=row;self.nav_textboxes[key]=text_box;self.nav_hints[key]=hint
                self.nav_icons[key]=badge;self.nav_indicators[key]=indicator
                klass.page_key=key
                self.page_classes[key]=klass
        if self._eager_pages:
            for key in self.page_classes:
                self._create_page(key)
        tk.Label(nav,text="小心求证，慢慢变强\n不自动交易 · 数据留在本机",justify="center",bg=COLORS["nav"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["micro"])).pack(side="bottom",pady=10)
        self.show("decision");self.protocol("WM_DELETE_WINDOW",self.on_close)
        if not start_hidden:
            self._startup_after_ids.extend([
                self.after_idle(self._maximize_initial_window),
                self.after(1800,self._auto_sync_recurring),
                self.after(2600,self._auto_cleanup_runtime),
            ])

    def _maximize_initial_window(self):
        """Open maximized on Windows; the large geometry remains the fallback."""
        try:
            if sys.platform.startswith("win"):
                self.state("zoomed")
        except tk.TclError:
            pass

    def _style(self):
        style=ttk.Style(self)
        try:style.theme_use("clam")
        except tk.TclError:pass
        row_height=max(52,int(round(31*self.applied_tk_scaling)))
        heading_pad=max(12,int(round(7*self.applied_tk_scaling)))
        style.configure("Treeview",background=COLORS["panel"],fieldbackground=COLORS["panel"],foreground=COLORS["text"],rowheight=row_height,font=(FONT,FONT_SIZE["small"]),borderwidth=0)
        style.configure("Treeview.Heading",background="#F3E9E4",foreground=COLORS["text"],font=(FONT,FONT_SIZE["small"],"bold"),relief="flat",padding=heading_pad)
        style.map("Treeview",background=[("selected","#F2CBD4")],foreground=[("selected",COLORS["text"])])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background="#EEE5E1", foreground=COLORS["muted"], padding=(16,8), font=(FONT,FONT_SIZE["small"],"bold"))
        style.map("TNotebook.Tab", background=[("selected",COLORS["panel"])], foreground=[("selected",COLORS["primary_dark"])])
        style.configure("TEntry",padding=8,fieldbackground="white",font=(FONT,FONT_SIZE["body"]));style.configure("TCombobox",padding=7,font=(FONT,FONT_SIZE["body"]));style.configure("TProgressbar",thickness=18)
    def _create_page(self,key):
        """Create one page on first use and return the stable page instance."""
        page=self.pages.get(key)
        if page is not None:return page
        klass=self.page_classes.get(key)
        if klass is None:raise KeyError(f"Unknown page: {key}")
        page=klass(self.container,self)
        self.pages[key]=page
        page.place(relx=0,rely=0,relwidth=1,relheight=1)
        return page

    def show(self,key):
        page=self.pages.get(key)
        previous_cursor=None
        if page is None:
            try:
                previous_cursor=self.cget("cursor")
                self.configure(cursor="watch")
                self.update_idletasks()
            except tk.TclError:pass
            try:page=self._create_page(key)
            finally:
                if previous_cursor is not None and not self._closing:
                    try:self.configure(cursor=previous_cursor)
                    except tk.TclError:pass
        self._current_page_key=key
        page.tkraise();page.refresh()
        for k,b in self.nav_buttons.items():
            selected = k == key
            bg = "#E8D5CE" if selected else COLORS["nav"]
            b.config(bg=bg,fg=COLORS["primary_dark"] if selected else COLORS["text"])
            self.nav_rows[k].config(bg=bg)
            self.nav_icons[k].config(bg=bg)
            self.nav_textboxes[k].config(bg=bg)
            self.nav_hints[k].config(bg=bg)
            self.nav_indicators[k].config(bg=COLORS["primary"] if selected else COLORS["nav"])
        self.after_idle(lambda k=key:self._ensure_nav_visible(k))

    def _ensure_nav_visible(self,key):
        """Scroll the navigation just enough to keep the active page visible."""
        row=self.nav_rows.get(key)
        if row is None:return
        self.update_idletasks()
        total=max(1,self.nav_list.winfo_reqheight())
        viewport=max(1,self.nav_canvas.winfo_height())
        top=float(self.nav_canvas.canvasy(0))
        row_top=float(row.winfo_y())
        row_bottom=row_top+float(row.winfo_height())
        if row_top < top:
            self.nav_canvas.yview_moveto(max(0.0,row_top/total))
        elif row_bottom > top+viewport:
            self.nav_canvas.yview_moveto(max(0.0,min(1.0,(row_bottom-viewport)/total)))
    def _auto_sync_recurring(self):
        if self._closing:return
        enabled=self.db.row("SELECT COUNT(*) n FROM recurring_plans WHERE enabled=1 AND auto_sync=1")["n"]
        if not enabled:return
        stop=threading.Event()
        def progress(done,total,label):
            page=self.pages.get("recurring")
            if page is not None:
                self.after(0,lambda d=done,t=total,value=label:page._update_recurring_progress(d,t,f"启动自动校准｜{value}"))
        def worker():
            try:
                RecurringInvestmentService(self.db).auto_sync_due(
                    stop_requested=stop.is_set,progress=progress,
                )
            except Exception as exc:
                self.db.save_settings({"recurring_last_sync_error":str(exc)[:2000]})
            def finish():
                recurring_page=self.pages.get("recurring")
                if recurring_page is not None:recurring_page.refresh()
                decision_page=self.pages.get("decision")
                if self._current_page_key == "decision" and decision_page is not None:
                    decision_page.refresh()
            if not self._closing:
                try:self.after(0,finish)
                except tk.TclError:pass
        self.start_background(worker,label="自动定投净值同步",cancel=stop.set)
    def _auto_cleanup_runtime(self):
        """Keep app-owned temporary files bounded without blocking Tk."""
        if self._closing:return
        if not runtime_cleanup_due(
            self.db.setting("runtime_cleanup_last_at", ""),
            now=local_now(),
        ):
            return
        stop=threading.Event()
        def worker():
            try:
                backup_result=self.db.prune_backups(stop_requested=stop.is_set)
                if stop.is_set() or backup_result.get("stopped"):
                    return
                result=cleanup_managed_storage(
                    RUNTIME_DIR,EXPORT_DIR,settings=self.db.settings(),
                    program_root=app_root(),
                    stop_requested=stop.is_set,
                )
                if stop.is_set() or result.get("stopped"):
                    return
                self.db.save_settings({
                    "runtime_cleanup_last_at":local_now().isoformat(timespec="seconds"),
                    "runtime_cleanup_last_result":json.dumps(
                        {"backups":backup_result,"managed":result},ensure_ascii=False,
                    ),
                    "runtime_cleanup_last_error":"",
                })
            except Exception as exc:
                # Cache cleanup is maintenance, never a reason to stop the app.
                if stop.is_set():
                    return
                try:self.db.save_settings({"runtime_cleanup_last_error":str(exc)[:2000]})
                except Exception:pass
        self.start_background(worker,label="清理旧运行缓存",cancel=stop.set)
    def start_background(self,target:Callable[[],Any],*,label:str,cancel:Callable[[],Any]|None=None):
        """Track every database/network worker so restore and shutdown stay safe."""
        def wrapped():
            try:
                target()
            finally:
                current=threading.current_thread()
                with self._worker_lock:self._background_workers.pop(current,None)
        thread=threading.Thread(target=wrapped,name=f"SectorRadar:{label}",daemon=True)
        with self._worker_lock:self._background_workers[thread]={"label":label,"cancel":cancel}
        thread.start();return thread
    def active_background_workers(self):
        with self._worker_lock:
            dead=[thread for thread in self._background_workers if not thread.is_alive()]
            for thread in dead:self._background_workers.pop(thread,None)
            return [(thread,dict(meta)) for thread,meta in self._background_workers.items()]
    def _cancel_pending_tk_callbacks(self):
        """Remove every Tcl ``after`` script before destroying this interpreter.

        Tk keeps delayed scripts in the interpreter, not in the Python widget
        hierarchy.  A direct ``destroy()`` (used by tests and emergency host
        shutdowns) used to leave the dashboard's idle fund-name callback and
        navigation ``after_idle`` callback behind.  When a later Tk root
        pumped the shared event queue, Tcl tried to invoke their already
        deleted Python commands and printed ``invalid command name``.  Normal
        WM close already cancels known workers; this final interpreter-level
        sweep also covers short-lived UI callbacks that do not warrant a
        separate token field.
        """
        try:
            tokens=self.tk.splitlist(self.tk.call("after","info"))
        except (tk.TclError,AttributeError):
            return
        for token in tokens:
            try:self.after_cancel(token)
            except tk.TclError:pass
    def destroy(self):
        """Destroy without leaking delayed callbacks into the next Tk root."""
        if getattr(self,"_destroying",False):
            return
        self._destroying=True
        self._closing=True
        try:self._request_close_worker_stop()
        except Exception:pass
        self._cancel_pending_tk_callbacks()
        try:return super().destroy()
        finally:self._close_state=None
    def refresh_all(self):
        # Every page refreshes itself when it becomes visible.  Refreshing all
        # fourteen hidden pages after a single edit blocked Tk for several
        # seconds and repeated the same catalog calculations many times.
        page=self.pages.get(self._current_page_key)
        if page is not None:
            try:page.refresh()
            except Exception as exc:
                error_text=f"{type(exc).__name__}: {exc}"
                try:self.db.save_settings({"last_ui_refresh_error":error_text[:2000]})
                except Exception:pass
                try:messagebox.showerror("界面刷新失败",f"数据操作已经完成，但当前页面未能刷新：\n{error_text}\n\n请不要依据旧画面继续操作；切换页面或重启软件后再核对。",parent=self)
                except tk.TclError:pass
                return False
        return True
    def generate_task(self,task_type):
        if task_type=="repair":
            errors=[r["errors"] for r in self.db.rows("SELECT errors FROM ai_imports WHERE validation_status='失败' ORDER BY id DESC LIMIT 1")]
            content=repair_prompt((errors[0].splitlines() if errors else ["请检查并重新输出完整结果块。"]))
            path=TASK_DIR/f"板块雷达_结果修复_{local_now().strftime('%Y-%m-%d_%H%M')}.md";path.write_text(content,encoding="utf-8")
        else:content,path=build_task_package(self.db,task_type)
        self.clipboard_clear();self.clipboard_append(content);self.update()
        if task_type == "night":
            messagebox.showinfo(
                "网页版备用任务包已生成",
                f"备用任务包已经复制到剪贴板，也保存为：\n{path}\n\n"
                "它不会自动执行研究，也不会让‘夜间雷达’出现候选。"
                "如需正常自动研究，请到‘一键AI扫描’运行‘完整扫描全部CSI行业’；"
                "只有网页版返回完整V2结果并在‘备用中转站’成功导入后，备用流程才会填充夜间雷达。",
                parent=self,
            )
        else:
            messagebox.showinfo("任务包已生成",f"完整任务包已经复制到剪贴板，也保存为：\n{path}\n\n现在打开任意网页版ChatGPT新窗口，直接粘贴并发送即可。",parent=self)
    def _show_close_progress(self,message):
        """Show a non-blocking shutdown status while Tk keeps pumping events."""
        dialog=self._close_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    self._set_close_progress(message);dialog.lift();return
            except tk.TclError:pass
        dialog=tk.Toplevel(self)
        dialog.title("正在安全退出")
        dialog.configure(bg=COLORS["bg"]);dialog.resizable(False,False);dialog.transient(self)
        dialog.protocol("WM_DELETE_WINDOW",lambda:None)
        tk.Label(
            dialog,text="🐾 正在安全退出板块雷达",bg=COLORS["bg"],fg=COLORS["primary_dark"],
            font=(FONT,FONT_SIZE["section"],"bold"),
        ).pack(anchor="w",padx=24,pady=(20,8))
        label=tk.Label(
            dialog,text=message,bg=COLORS["bg"],fg=COLORS["text"],justify="left",
            wraplength=580,font=(FONT,FONT_SIZE["body"]),
        )
        label.pack(fill="x",padx=24,pady=(0,12))
        progress=ttk.Progressbar(dialog,mode="indeterminate",length=580)
        progress.pack(fill="x",padx=24,pady=(0,8));progress.start(12)
        tk.Label(
            dialog,text="无需再次点击关闭；任务收尾和备份完成后窗口会自动退出。",
            bg=COLORS["bg"],fg=COLORS["muted"],font=(FONT,FONT_SIZE["small"]),
        ).pack(anchor="w",padx=24,pady=(0,20))
        self._close_dialog=dialog;self._close_label=label;self._close_progress=progress
        dialog.update_idletasks()
        width=max(640,dialog.winfo_reqwidth());height=max(230,dialog.winfo_reqheight())
        x=max(0,self.winfo_rootx()+(self.winfo_width()-width)//2)
        y=max(0,self.winfo_rooty()+(self.winfo_height()-height)//2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        try:dialog.grab_set()
        except tk.TclError:pass

    def _set_close_progress(self,message):
        try:self.title(f"正在安全退出 · {self._normal_title}")
        except tk.TclError:pass
        label=self._close_label
        if label is not None:
            try:label.config(text=message)
            except tk.TclError:pass

    def _dismiss_close_progress(self):
        progress=self._close_progress
        if progress is not None:
            try:progress.stop()
            except tk.TclError:pass
        dialog=self._close_dialog
        if dialog is not None:
            try:dialog.grab_release()
            except tk.TclError:pass
            try:dialog.destroy()
            except tk.TclError:pass
        self._close_dialog=None;self._close_label=None;self._close_progress=None

    def _request_close_worker_stop(self):
        """Request cooperative cancellation and return the currently active labels."""
        for token in self._startup_after_ids:
            try:self.after_cancel(token)
            except tk.TclError:pass
        self._startup_after_ids.clear()
        decision_page=self.pages.get("decision")
        if decision_page is not None:
            try:decision_page.cancel_delayed_background_work()
            except Exception:pass
        api_page=self.pages.get("api")
        scan_active=bool(api_page is not None and getattr(api_page,"_scan_active",False))
        if scan_active:
            orchestrator=getattr(api_page,"_active_orchestrator",None)
            if orchestrator is not None:
                try:orchestrator.request_stop()
                except Exception:pass
        workers=self.active_background_workers()
        labels={meta.get("label","后台任务") for _thread,meta in workers}
        if scan_active:labels.add("扫描任务收尾")
        for _thread,meta in workers:
            cancel=meta.get("cancel")
            if callable(cancel):
                try:cancel()
                except Exception:pass
        return sorted(labels)

    def on_close(self):
        if self._closing:
            dialog=self._close_dialog
            if dialog is not None:
                try:dialog.lift()
                except tk.TclError:pass
            return
        self._closing=True
        self.configure(cursor="watch")
        labels=self._request_close_worker_stop()
        state={"phase":"stopping","error":None,"thread":None}
        self._close_state=state
        if labels:
            message="正在停止并等待后台任务安全收尾：\n"+"、".join(labels)
        else:
            message="后台任务已经结束，正在准备关闭前备份……"
        self._show_close_progress(message)
        self.after(100,lambda current=state:self._poll_close_workers(current))

    def _poll_close_workers(self,state):
        if not self._closing or state is not self._close_state:return
        labels=self._request_close_worker_stop()
        if labels:
            self._set_close_progress("正在停止并等待后台任务安全收尾：\n"+"、".join(labels))
            self.after(150,lambda current=state:self._poll_close_workers(current));return
        self._begin_close_backup(state)

    def _begin_close_backup(self,state):
        if not self._closing or state is not self._close_state or state.get("phase")=="backup":return
        state["phase"]="backup"
        self._set_close_progress("后台任务已经安全结束。正在创建关闭前数据库备份……")
        close_state = {"done": threading.Event(), "error": None, "thread": None}
        def worker():
            # Shutdown must protect the database promptly.  Retention scans run
            # integrity checks across many old 17MB backups and can take minutes
            # on the user's mechanical G drive; startup maintenance already owns
            # that bounded cleanup.  Create and verify this backup now, but do not
            # make window exit wait for unrelated historical pruning.
            try:self.db.backup(prune_automatic=False)
            except Exception as exc:close_state["error"]=exc
            finally:close_state["done"].set()
        close_state["thread"] = threading.Thread(
            target=worker,name="SectorRadar:关闭前安全备份",daemon=True,
        )
        state["backup_state"]=close_state
        close_state["thread"].start()
        # Tk calls are only legal on the main thread.  Calling ``self.after``
        # from the backup worker can block forever after the SQLite copy has
        # already completed, leaving a responsive-looking window that will
        # never exit.  Poll the thread-owned Event from Tk instead.
        self.after(100, lambda state=close_state:self._poll_close_backup(state))
    def _poll_close_backup(self,state):
        if not self._closing:return
        thread=state.get("thread")
        if not state["done"].is_set() or (thread is not None and thread.is_alive()):
            self.after(100,lambda current=state:self._poll_close_backup(current));return
        self._finish_close(state.get("error"))
    def _finish_close(self,error):
        self._dismiss_close_progress()
        try:self.title(self._normal_title)
        except tk.TclError:pass
        if error is not None and not messagebox.askyesno(
            "自动备份失败",
            f"关闭前备份失败：{error}\n\n是否仍要关闭软件？",
            parent=self,
        ):
            self._closing=False;self._close_state=None;self.configure(cursor="");return
        self.destroy()


def main():
    if not acquire_single_instance():
        return
    try:
        app=SectorRadarApp();app.mainloop()
    finally:
        release_single_instance()
