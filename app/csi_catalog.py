from __future__ import annotations

from dataclasses import dataclass


CSI_METHODOLOGY_URL = (
    "https://oss-ch.csindex.com.cn/industryClassification/"
    "%E4%B8%AD%E8%AF%81%E8%A1%8C%E4%B8%9A%E5%88%86%E7%B1%BB%E7%9A%84%E8%AF%B4%E6%98%8E.pdf"
)
CSI_METHODOLOGY_VERSION = "中证行业分类标准说明（2025年11月版；2026-08-08核验）"
CSI_REPRESENTATIVE_VERSION = "CSI-VERIFIED-SCANNABLE-100-V5"


@dataclass(frozen=True)
class CsiRepresentative:
    code: str
    name: str
    short_name: str
    industry: str
    order: int
    hierarchy_level: int = 4
    parent_code: str = ""
    official_tracked: bool = True
    selection_reason: str = ""

    @property
    def universe_id(self) -> str:
        return f"C{self.code}"

    @property
    def display_code(self) -> str:
        return f"{self.code}.CSI"

    @property
    def security_id(self) -> str:
        # Preserve the official CSI identity through the market-data pipeline.
        # Converting 930/931/932/H codes to a vendor market id loses that
        # authoritative identity, and H-prefixed indices cannot be represented
        # by the old numeric-only fallback scheme at all.
        return f"CSI.{self.code}"

    @property
    def allocation_cluster(self) -> str:
        # Siblings share a risk cluster even though none of their parents is scanned.
        return self.parent_code or self.industry

    @property
    def factsheet_url(self) -> str:
        return (
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
            f"detail/files/zh_CN/{self.code}factsheet.pdf"
        )

    @property
    def reason(self) -> str:
        if self.selection_reason:
            return self.selection_reason
        tracking = (
            "中证官网目录标记已有跟踪产品；仍需在基金优选页核对场外可买份额。"
            if self.official_tracked
            else "中证官网目录未标记跟踪产品，只研究不自动分配买入金额。"
        )
        return f"叶子行业目标；父级指数不参与日扫，防止父子仓位重复。{tracking}"


@dataclass(frozen=True)
class CsiFundSeed:
    index_code: str
    fund_code: str
    fund_name: str
    share_class: str
    strategy_kind: str = "被动指数"

    @property
    def source_url(self) -> str:
        return f"https://fundf10.eastmoney.com/jbgk_{self.fund_code}.html"


def _leaf(
    code: str,
    name: str,
    industry: str,
    parent: str,
    *,
    tracked: bool = True,
) -> CsiRepresentative:
    return CsiRepresentative(code, name, name, industry, 0, 4, parent, tracked)


# v16 used ten broad level-one representatives.  Fresh databases still execute that
# migration before v17 replaces them with the leaf-only catalog, so keep the old
# identities as migration references only.  They are never exposed by the live radar.
CSI_PARENT_REFERENCES: tuple[CsiRepresentative, ...] = (
    CsiRepresentative("000986", "中证全指能源指数", "全指能源", "能源", 1, 1),
    CsiRepresentative("000987", "中证全指原材料指数", "全指材料", "原材料", 2, 1),
    CsiRepresentative("000988", "中证全指工业指数", "全指工业", "工业", 3, 1),
    CsiRepresentative("000989", "中证全指可选消费指数", "全指可选", "可选消费", 4, 1),
    CsiRepresentative("000932", "中证主要消费指数", "中证消费", "主要消费", 5, 1),
    CsiRepresentative("000991", "中证全指医药卫生指数", "全指医药", "医药卫生", 6, 1),
    CsiRepresentative("000992", "中证全指金融地产指数", "金融地产", "金融地产", 7, 1),
    CsiRepresentative("000993", "中证全指信息技术指数", "全指信息", "信息技术", 8, 1),
    CsiRepresentative("000936", "中证通信服务指数", "中证通信", "通信服务", 9, 1),
    CsiRepresentative("000995", "中证全指公用事业指数", "全指公用", "公用事业", 10, 1),
)


# 选择规则：只保留叶子行业。只要某父级已被一个或多个子行业替代，父级就不进入
# CSI_LEAF_TARGETS。parent_code可以指向未激活的分类节点，用于审计与去重。
_LEAVES = (
    # 能源与资源
    _leaf("399998", "中证煤炭", "能源资源", "932077"),
    _leaf("931248", "油气资源", "能源资源", "932077"),
    _leaf("H11057", "石化产业", "能源资源", "932077"),

    # 原材料
    _leaf("930598", "稀土产业", "原材料", "931936"),
    _leaf("930632", "CS稀金属", "原材料", "931936"),
    _leaf("930606", "中证钢铁", "原材料", "931935"),
    _leaf("931009", "建筑材料", "原材料", "931934"),
    _leaf("H30597", "新材料", "原材料", "932078"),
    _leaf("000813", "细分化工", "原材料", "H30187"),
    _leaf("931238", "沪深港黄金股票", "原材料", "931936"),
    _leaf("000811", "细分有色金属", "原材料", "931936"),
    _leaf("931892", "有色矿业", "原材料", "931936"),

    # 工业、装备与基础设施
    _leaf("H30590", "机器人", "工业装备", "H30166"),
    _leaf("931866", "中证机床", "工业装备", "H30166"),
    _leaf("931752", "工程机械主题", "工业装备", "H30166"),
    _leaf("000693", "科创工业机械", "工业装备", "H30166"),
    _leaf("399807", "高铁产业", "工业装备", "H30171"),
    _leaf("399995", "基建工程", "工业装备", "931931"),
    _leaf("932420", "智选船舶产业", "工业装备", "932143"),
    _leaf("931855", "通用航空", "工业装备", "932143"),
    _leaf("931594", "卫星产业", "工业装备", "932143"),
    _leaf("931994", "电网设备主题", "工业装备", "931932"),
    _leaf("931151", "光伏产业", "工业装备", "931932"),
    _leaf("931719", "CS电池", "工业装备", "931932"),
    _leaf("931521", "高装细分50", "工业装备", "932079"),
    _leaf("931495", "工业互联", "工业装备", "932079"),
    _leaf("930850", "智能制造", "科技制造", "932079"),
    _leaf("000812", "细分机械设备", "工业装备", "H30166"),

    # 汽车与运输
    _leaf("931230", "汽车零部件", "汽车运输", "H30164"),
    _leaf("930997", "新能源车", "汽车运输", "H30164"),
    _leaf("930725", "CS车联网", "汽车运输", "H30164"),
    _leaf("930599", "高端装备制造", "科技制造", "932079"),
    _leaf("399967", "中证军工", "国防科技", "932143"),
    _leaf("930875", "空天一体军工", "国防科技", "932143"),
    _leaf("931066", "军工龙头", "国防科技", "932143"),
    _leaf("399973", "中证国防", "国防科技", "932143"),
    _leaf("930743", "生物科技", "医药科技", "H30179"),
    _leaf("930716", "CS物流", "汽车运输", "H30171"),

    # 半导体、计算机与数字产业。宽泛的H30184“半导体”明确不在本表中。
    _leaf("931743", "半导体材料设备", "信息技术", "H30184"),
    _leaf("930721", "智能汽车", "汽车科技", "H30164"),
    _leaf("950162", "科创芯片设计", "信息技术", "H30184"),
    _leaf("930601", "软件服务", "信息技术", "H30203"),
    _leaf("931470", "沪深港云计算", "信息技术", "H30203"),
    _leaf("931494", "消费电子", "信息技术", "H30183"),
    _leaf("931529", "国企数字经济", "信息技术", "H30203"),
    _leaf("932094", "软件开发", "信息技术", "H30203"),
    _leaf("931247", "中证信创", "信息技术", "H30203"),
    _leaf("399994", "信息安全", "信息技术", "H30203"),
    _leaf("931583", "沪深港数字经济", "信息技术", "H30203"),
    _leaf("931469", "云计算50", "信息技术", "H30203"),
    _leaf("930851", "云计算与大数据", "信息技术", "H30203"),
    _leaf("931582", "数字经济", "信息技术", "H30203"),
    _leaf("931461", "电子50", "信息技术", "H30183"),
    _leaf("930902", "中证数据", "信息技术", "H30203"),
    _leaf("930712", "CS物联网", "信息技术", "H30203"),
    _leaf("931071", "人工智能", "信息技术", "H30203"),
    _leaf("932456", "科创创业AI", "信息技术", "H30203"),
    _leaf("931079", "5G通信", "通信服务", "932085"),
    _leaf("000683", "科创生物医药", "医药科技", "H30179"),
    _leaf("931160", "通信设备", "通信服务", "932145"),
    _leaf("930726", "生物医药", "医药科技", "H30179"),
    _leaf("399989", "中证医疗", "医药科技", "931940"),
    _leaf("931235", "中证电信", "通信服务", "932144"),
    _leaf("000814", "细分医药", "医药科技", "H30179"),
    _leaf("930821", "中证VR", "信息技术", "H30183"),
    _leaf("399970", "移动互联", "信息技术", "H30203"),
    _leaf("000808", "医药生物", "医药科技", "H30179"),

    # 医药卫生
    _leaf("931152", "CS创新药", "医药卫生", "H30179"),
    _leaf("930641", "中证中药", "医药卫生", "H30179"),
    _leaf("931992", "疫苗生物", "医药卫生", "H30179"),
    _leaf("H30217", "医疗器械", "医药卫生", "931940"),
    _leaf("930719", "CS精准医", "医药卫生", "H30179"),
    _leaf("930720", "CS互医疗", "医药卫生", "931940"),

    # 主要消费、可选消费与传媒
    _leaf("H30192", "食品", "消费传媒", "H30177"),
    _leaf("399997", "中证白酒", "消费传媒", "H30205"),
    _leaf("931946", "畜牧养殖", "消费传媒", "930910"),
    _leaf("399814", "大农业", "农业消费", "930910"),
    _leaf("930633", "中证旅游", "消费传媒", "H30204"),
    _leaf("930901", "动漫游戏", "消费传媒", "H30220"),
    _leaf("930781", "中证影视", "消费传媒", "H30220"),
    _leaf("399804", "中证体育", "消费传媒", "H30204"),
    _leaf("931021", "家电龙头", "消费传媒", "930697"),
    _leaf("931480", "线上消费", "消费传媒", "H30175"),
    _leaf("399996", "智能家居", "消费传媒", "H30196"),
    _leaf("930662", "现代农业", "农业消费", "930910"),
    _leaf("000949", "中证农业", "农业消费", "930910"),
    _leaf("399987", "中证酒", "消费传媒", "H30205"),
    _leaf("000815", "细分食品饮料", "消费传媒", "H30177"),

    # 金融与地产
    _leaf("399986", "中证银行", "金融地产", "932083"),
    _leaf("399975", "证券公司", "金融地产", "H30211"),
    _leaf("399809", "保险主题", "金融地产", "932083"),
    _leaf("930986", "金融科技", "金融地产", "932083"),
    _leaf("930653", "食品饮料", "消费传媒", "H30177"),
    _leaf("931775", "房地产", "金融地产", "932083"),

    # 公用事业与环境
    _leaf("931897", "绿色电力", "公用环境", "H30199"),
    _leaf("931241", "家居家电", "消费传媒", "930697"),
    _leaf("930614", "环保50", "公用环境", "931933"),
    _leaf("000806", "消费服务", "消费传媒", "H30204"),
    _leaf("399806", "环境治理", "公用环境", "931933"),
    _leaf("399812", "养老产业", "医药科技", "H30178"),
)


CSI_REPRESENTATIVES = tuple(
    CsiRepresentative(
        item.code,
        item.name,
        item.short_name,
        item.industry,
        order,
        item.hierarchy_level,
        item.parent_code,
        item.official_tracked,
        item.selection_reason,
    )
    for order, item in enumerate(_LEAVES, start=1)
)

# Every directory row has passed both hard gates: CSI lists at least one
# tracking product, and the official performance endpoint returns well over
# 20 usable daily bars.  ``official_tracked`` remains a defensive field for
# future catalog revisions; the released V5 catalog intentionally has no
# research-only placeholders.
CSI_DAILY_TARGETS = tuple(
    item for item in CSI_REPRESENTATIVES if item.official_tracked
)

# Hard invariant: an active parent and active child can never coexist.
_ACTIVE_CODES = {item.code for item in CSI_REPRESENTATIVES}
_PARENT_CHILD_CONFLICTS = tuple(
    item for item in CSI_REPRESENTATIVES if item.parent_code in _ACTIVE_CODES
)
if _PARENT_CHILD_CONFLICTS:
    details = "、".join(f"{item.parent_code}>{item.code}" for item in _PARENT_CHILD_CONFLICTS)
    raise RuntimeError(f"CSI叶子目录存在父子重复：{details}")


# v3.1的宽行业基金证据继续留在数据库历史记录中，但宽行业已退出日扫；因此
# v3.2不再把这些旧基金种子挂到新的叶子目标上，避免为了可买而错误改写跟踪指数。
CSI_FUND_SEEDS: tuple[CsiFundSeed, ...] = ()


CSI_BY_CODE = {item.code: item for item in CSI_REPRESENTATIVES}
CSI_BY_UNIVERSE_ID = {item.universe_id: item for item in CSI_REPRESENTATIVES}
