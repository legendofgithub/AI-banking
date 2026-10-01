# 理财产品真实行情接入方案（未实施 · 仅方案与仿真数据）

> 目标：演示用仿真数据（结构与真实接口逐字一致）→ 生产切真实数据源时**零代码改动、只换配置**。
> 现状：wealth_products 表数据来自 bank_core/seed.py 的 PRODUCTS 写死列表，纯虚构。

## 一、检索结论：真实数据长什么样、从哪来

| 数据域 | 真实来源 | 接口形态 | 关键字段 | 生产可用性 |
|---|---|---|---|---|
| 公募基金实时估值 | 天天基金 `fundgz.1234567.com.cn/js/{code}.js` | JSONP（去壳即 JSON），免费无鉴权 | fundcode/name/jzrq/dwjz/gsz/gszzl/gztime | ✅ 可直接用（仅股/混基金） |
| 货币基金收益 | 天天基金货基页面接口 | JSON | 七日年化/万份收益/日期 | ✅ 可直接用 |
| 基金历史净值 | `api.fund.eastmoney.com/f10/lsjz` | JSON | FSRQ/DWJZ/LJJZ | ✅ 可直接用 |
| 银行理财明细 | 中国理财网（银行业理财登记托管中心信披平台） | **无公开 API，反爬** | 登记编码(C开头)/发行机构/产品类型/风险等级/业绩比较基准(区间型)/起购/募集期/期限 | ⚠️ 需合规采购或机构级对接；akshare 仅有发行量宏观数据 |
| 上述封装库 | akshare / Tushare | Python SDK | 以上字段的封装 | ✅ 开发期方便，生产建议直连 |

合规红线：理财产品展示规范要求**业绩比较基准≠预期收益率**声明（2025-26 年新规：大量产品改为区间型/挂钩指数型基准）。

## 二、架构：Provider 抽象 + 归一化中间层（无缝衔接的机制）

```
真实源(生产)                          仿真源(演示)
fundgz JSONP ─┐                       data_samples/*.sample.json
货基接口 ──────┤ EastmoneyProvider     ┌─ SeedFileProvider(读同一目录)
中国理财网 ────┤ BankWPProvider(合规)  │
              ▼                       ▼
        ┌─────────────────────────────────┐
        │ MarketDataProvider 接口           │
        │  fetch_products() / fetch_quotes()│   ← 三个实现产出同一结构
        └──────────────┬──────────────────┘
                       ▼
        归一化层 normalized_products（统一 schema，见
        data_samples/normalized_products.sample.json；
        每条带 source/source_ref/as_of/is_simulated）
                       ▼ upsert(按 code，不删历史)
        wealth_products（现有表 + 建议新增列：
        source, as_of, benchmark_range, nav_json, is_simulated）
                       ▼
        agent 理财场景 / 管理台理财页签（下游零改动）
```

**切换 = 环境变量**：`MARKET_PROVIDER=seed|live`。演示机用 seed 读仿真文件；生产机配 live 走真实接口。同步器（定时拉取→归一化→upsert）只在 live 侧启用。

## 三、字段映射表（真实 → 归一化 → 现有表）

| 归一化字段 | 天天基金 | 中国理财网 | wealth_products 现列 |
|---|---|---|---|
| code | fundcode | registration_code | code |
| name | name | product_name | name |
| p_type | 按基金类型推断 | 产品类型映射(固收→bond/混合类→mixed/…) | p_type |
| risk_level | 基金波动等级 | 一~五级 → 1-5 | risk_level |
| expected_return_bps | 货基七日年化 | 基准下限 | expected_return_bps |
| benchmark_range | — | 基准区间 | (新增列) |
| nav | dwjz/gsz/gszzl | — | (新增列 nav_json) |
| min_subscribe_cents | 起购(分) | min_subscribe_yuan | min_subscribe_cents |
| lock_days | — | 期限天数 | lock_days |
| 申赎费 | 费率接口 | 多为 0 | *_fee_bps |

两类收益形态的差异在归一化层吸收：**净值守恒型**（股票/混合基金，无收益率，估值随净值）与**基准型**（理财/货基，有收益率或区间）——现有 agent 话术只需在播报时按 benchmark_type 分支（净值守恒型播报净值，基准型播报区间并附合规声明）。

## 四、演示数据清单（已生成，结构与真实接口逐字一致）

- `data_samples/eastmoney_fundgz.sample.json` —— fundgz JSONP 去壳结构 ×4
- `data_samples/eastmoney_moneyfund.sample.json` —— 货基七日年化 ×2
- `data_samples/chinawealth_wp.sample.json` —— 银行理财披露字段 ×4（含登记编码/区间基准/合规注记）
- `data_samples/normalized_products.sample.json` —— 归一化后 10 条（= 上面三份的合并形态，is_simulated=true）

演示路径（将来实施时）：SeedFileProvider 读 normalized 样本 → upsert wealth_products → 前端/管理台照常展示，界面出现"天弘余额宝/招银理财"等仿真产品；`is_simulated=true` 让前端可打"仿真数据"角标。

## 五、实施步骤（真正要做时的顺序，估 2-3 天）

1. wealth_products 加列（source/as_of/benchmark_range_bps/nav_json/is_simulated），老数据 is_simulated=true 标记；
2. MarketDataProvider 接口 + SeedFileProvider + 归一化 upsert（含风险等级/类型映射表单元测试）；
3. EastmoneyProvider（fundgz+货基，缓存 60s，失败保旧数据）；
4. 管理台加"产品库同步"页签（手动触发 + 显示 as_of/来源/仿真标记）；
5. agent 播报按 benchmark_type 分支 + 合规声明文案；
6. BankWPProvider 留接口桩，等合规数据源到位再实现。
