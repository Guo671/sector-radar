from __future__ import annotations

import json
from typing import Any

from .config import local_now
from .platform_catalog import PlatformCatalogService
from .services import latest_current_radar_run


class DetailedResearchPromptService:
    """Build self-contained prompts for consumer DeepSeek and ChatGPT web pages."""

    def __init__(self, db):
        self.db = db
        self.catalog = PlatformCatalogService(db)

    def build(self, index_id: int, target: str) -> str:
        index = self.catalog.index(index_id)
        if not index:
            raise ValueError("没有找到选中的指数目录条目。")
        if not int(index.get("active") or 0) or not int(index.get("radar_enabled") or 0):
            raise ValueError("该指数只保留在历史审计中，不扫描，也不能生成AI研究提示。")
        funds = self.catalog.strict_catalog_funds(index_id)
        if not funds:
            raise ValueError("该指数没有通过严格条件的场外被动C类基金，不能生成AI研究提示。")
        official_products = self.catalog.official_products(index_id)
        current_run = latest_current_radar_run(self.db)
        latest_audit = (
            self.db.row(
                "SELECT * FROM radar_audit WHERE task_id=? AND universe_id=?",
                (current_run["task_id"], index["universe_id"]),
            )
            if current_run else None
        ) or {}
        holdings = self.db.rows(
            """SELECT h.fund_code,f.fund_name,h.units,h.cost,h.latest_nav,h.nav_date,h.sleeve
                 FROM holdings h JOIN funds f USING(fund_code)
                WHERE h.active=1 AND (f.index_code=? OR f.index_name=? OR f.correlation_bucket=?)
                ORDER BY h.fund_code""",
            (
                index.get("official_index_code") or "__none__",
                index.get("official_index_name") or "__none__",
                index.get("industry_group") or "__none__",
            ),
        )
        facts: dict[str, Any] = {
            "生成时间_北京时间": local_now().isoformat(timespec="minutes"),
            "行业分类": index.get("industry_group") or index.get("historical_category"),
            "CSI代表指数": {
                "代码": index.get("ant_index_code"),
                "名称": index.get("official_index_name"),
                "代表理由": index.get("representative_reason"),
                "方法版本": index.get("csi_methodology_version"),
                "行情历史状态": index.get("quote_history_status"),
            },
            "正式指数": {
                "代码": index.get("official_index_code"),
                "名称": index.get("official_index_name"),
                "编制方资料": index.get("official_source_url"),
            },
            "行情代理_只用于取行情": {
                "代码": index.get("proxy_code"),
                "名称": index.get("proxy_name"),
                "证券ID": index.get("security_id"),
                "映射类型": index.get("mapping_kind"),
            },
            "雷达准入": {
                "状态": index.get("admission_status"),
                "排除原因": index.get("exclusion_reason"),
            },
            "本地严格C类基金白名单": [
                {
                    "代码": row.get("fund_code"),
                    "名称": row.get("fund_name"),
                    "策略": row.get("strategy_kind"),
                    "份额类别": row.get("share_class"),
                    "公开申购状态": row.get("public_buyable_status"),
                    "与当前指数关系": row.get("relation_kind"),
                    "同指数被动属性已核验": bool(row.get("passive_verified")),
                    "结构准入": True,
                    "当日下单前仍需复核": not self.catalog.evaluate_fund_execution(row).executable,
                    "资料核验时间": row.get("verified_at"),
                    "公开资料": row.get("source_url"),
                    "备注": row.get("source_note"),
                }
                for row in funds
            ],
            "中证官网全部关联产品_只作关联证据": [
                {
                    "代码": row.get("product_code"),
                    "名称": row.get("fund_name"),
                    "类别": row.get("strategy_kind"),
                    "管理人": row.get("fund_manager"),
                    "成立日": row.get("inception_date"),
                    "官网资料": row.get("source_url"),
                }
                for row in official_products
            ],
            "本地最近一次雷达数据": {
                key: latest_audit.get(key)
                for key in (
                    "d1", "d3", "d5", "d10", "d20", "volume_signal", "breadth_signal",
                    "position_20d", "crowding", "catalyst", "note",
                )
            },
            "相关现有持仓": holdings,
        }
        if target.lower() == "deepseek":
            platform_intro = (
                "你正在 DeepSeek 网页版中工作。请开启并实际使用联网搜索；先检索、后交叉核验、再下结论。"
                "不要声称调用了未实际使用的工具，也不要把模型记忆当作今天的事实。"
            )
            citation_rule = "每一项时效性事实后给出可点击来源链接，并在末尾列出“来源、发布日期、访问日期、支持的结论”。"
            title = "DeepSeek 网页版详细研究任务"
        elif target.lower() == "chatgpt":
            platform_intro = (
                "你正在 ChatGPT 网页版中工作。请使用网页浏览搜索当前资料，以直接引用支撑结论；"
                "优先指数公司、基金公司、交易所、监管披露和基金定期报告。"
            )
            citation_rule = "对新闻、规模、费率、基金经理、持仓、申购状态等会变化的事实逐段引用来源，并提供可点击链接。"
            title = "ChatGPT 网页版详细研究任务"
        else:
            raise ValueError("目标网页只能是 DeepSeek 或 ChatGPT。")

        return f"""# {title}

{platform_intro}

你是一名谨慎的中国公募指数基金研究员。请围绕下列唯一目标完成一次可复核的深度研究：判断这个中证官方.CSI代码（六位数字或H加五位数字）的代表行业指数是否值得在当前日期配置，并在公开资料显示可申购的普通场外被动指数基金或ETF联接基金中选出最合适的一只。不要代替用户下单，不保证收益。

## 本地已知事实

```json
{json.dumps(facts, ensure_ascii=False, indent=2)}
```

本地事实只是检索起点，不是免核验结论。尤其要严格区分：研究ID、中证.CSI展示代码、正式指数代码、行情证券ID、基金代码；任何两者都不得因名称相似而互相冒充。中证正式代码既可能是六位数字，也可能是H加五位数字。最终短线首选只能从“本地严格C类基金白名单”中选择；如果联网发现白名单基金已经暂停申购或关系发生变化，结论必须改为等待，不得临时换入目录外基金。

## 必须依次完成的研究

1. **身份复核**：从中证指数公司核对中证.CSI代码（六位数字或H加五位数字）、正式指数名称、指数基日、选样规则、调样周期、成分数量、行业与前十大权重。不得用名称相近的主题指数替代，也不得猜测后缀或代码。
2. **子行业结构**：把指数拆成真实子行业，说明每个子行业的权重、盈利驱动、景气位置和主要风险；不要用泛泛的板块故事代替成分结构。
3. **新闻与催化**：整理最近7天、30天、90天的重要政策、产业、公司、价格、供需和资金事件，标明事件发生日期与信息发布日期，区分已发生、市场预期和传闻。
4. **行业前景**：分别给出未来1—4周、1—3个月、6—12个月的基准/乐观/悲观情景，列出可观察验证指标和会推翻判断的条件。
5. **估值与交易位置**：结合指数估值分位、盈利趋势、1/3/5/10/20日走势、量能、扩散度、拥挤度、回撤和波动；若没有可靠数据就写缺失，不得编数。
6. **基金全量发现**：搜索所有跟踪同一正式指数的A/C/E/I类、场内ETF、场外普通指数与ETF联接产品，完整留档比较。场内ETF只能作为联接关系证据，不能直接推荐用户在场内买入。指数增强、主动基金、名称相似但跟踪不同指数的基金必须单列并排除出“被动首选”。
7. **基金横向比较**：至少比较跟踪指数一致性、成立时间、最新规模、近1年跟踪误差/跟踪偏离、管理费、托管费、销售服务费、实际申购费、赎回费、限购、暂停申购、基金公司、基金经理、ETF母基金流动性、持仓结构和历史异常。A/C类要按30天、90天、1年持有期分别计算成本，不得把同一基金不同份额当成独立策略。
8. **可执行性核验**：本软件用于A股短线，最终首选只能是明确的C类份额，并且必须精确跟踪同一指数、属于场外被动基金、基金公司公开资料显示正常申购。A/E/I类、场内ETF和增强基金可以比较但不得成为短线首选。最后另列“下单前核对清单”，提醒用户在自己实际使用的平台核对折扣费率、限购和当前可买状态；不得把平台实时状态说成已经核验。
9. **投资结论**：给出“买入/小额观察/等待/回避”之一；若建议买入，给出占8000元主动资金的建议百分比和金额区间、分批条件、止损/退出观察条件；若证据不足，结论必须是等待核验。

## 输出格式

按以下标题输出：

1. 一页结论
2. 指数身份核验表
3. 子行业与成分结构
4. 近期新闻、催化与风险时间线
5. 三周期情景分析
6. 同指数被动基金完整候选表
7. 基金淘汰过程与最终首选
8. 今日投资建议（百分比、金额、条件）
9. 下单前仍需用户在实际购买平台核对的项目
10. 来源与数据截止时间

{citation_rule}

最后必须附一段“防错检查”：确认最终短线首选是C类、没有把A/E/I类选成短线首选、没有把指数增强当被动、没有把场内ETF当场外可申购基金、没有把相似指数当同一指数、没有把行情证券ID当正式指数、没有把公开申购状态当用户购买平台的实时状态、没有虚构代码或数据。
"""
