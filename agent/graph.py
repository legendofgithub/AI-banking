"""LangGraph 编排骨架(M1):Router + 转账子图(含定时/AA/同名消歧)+ 跨场景联动 + 人工确认闸门。

结构(节点名前缀 t_=转账子图,sb_=AA 发起,ss_=AA 结算,l_=跨场景联动):

    START → router ─┬─ transfer → t_extract ⇄ t_clarify(缺槽反问) → t_resolve
                    │      (≥2 同名) → t_disambiguate(反问选人) ─┐(唯一/选定)
                    │                                             ▼
                    │       t_policy(账户选择+风控预检) → t_create(建单,不动钱)
                    │                                             ▼
                    │       t_gate(interrupt 人工确认)
                    │         ├ 确认 → t_confirm(立即单扣款/定时单保留)
                    │         └ 取消 → t_cancel(撤销订单)
                    │                                             ▼
                    │                                       t_report(播报) → END
                    ├─ split_bill → sb_extract ⇄ sb_clarify → sb_gate(interrupt)
                    │         ├ 确认 → sb_create → sb_report → END
                    │         └ 取消 → sb_report → END
                    ├─ split_settle → ss_extract → ss_gate(interrupt 结算确认)
                    │         ├ 确认 → ss_settle → ss_report → END
                    │         └ 取消 → ss_report → END
                    ├─ linkage(建计划) → l_extract ⇄ l_clarify → l_plan(事件匹配+
                    │         建计划,锁定单只建单) → l_gate(interrupt)
                    │         ├ 确认 → l_confirm(confirm_transfer_order 真正锁定) → l_report
                    │         └ 取消 → l_cancel(cancel_linkage_plan 撤计划) → l_report
    ├─ linkage(到期处理) → l_due(按标题模糊匹配最近 active 计划)
                    │         → l_due_gate(逐动作闸门) ─┬ 确认 → l_due_exec(真实扣款购买)
                    │         │                        └ 取消/两问不可识别 → l_due_skip(跳过)
                    │         │   (队列未空则回到 l_due_gate 继续下一项)
                    │         └ 全部处理完 → l_report(播报进度/计划完成) → END
    ├─ bill_analysis → b_extract ⇄ b_clarify(缺槽反问) → b_run(调只读分析工具)
    │         → b_report(播报,严格转述数字) → END(只读场景,无闸门)
    ├─ wealth ─┬─ query → w_query(测评→按风险等级推荐/对比/持仓) → w_report → END
    │          ├─ assess → 五道题逐题反问(经 w_clarify 工厂) → w_assess 落库 → w_report
    │          ├─ subscribe → w_subscribe(定位产品+建单 confirmed=False) → w_gate(interrupt)
    │          │         ├ 确认 → w_confirm(confirmed=True 真扣款) → w_report
    │          │         └ 取消/超限 → notice wealth_cancelled → w_report
    │          └─ redeem → w_redeem(定位持仓+建单) → w_redeem_gate(interrupt)
    │                    ├ 确认 → w_redeem_exec(confirmed=True 到账) → w_report
    │                    └ 取消/超限 → notice wealth_cancelled → w_report
    ├─ card ─┬─ list → k_list(list_cards+播报) → k_report → END
    │        ├─ apply → k_apply(组装卡片视图) → k_gate(interrupt)
    │        ├─ limits → k_limits(复述当前/新限额) → k_gate(interrupt)
    │        └─ status → k_status(复述当前状态/目标) → k_gate(interrupt)
    │                 ├ 确认(lost) → k_lost_gate(第二道闸:明示不可逆) ─┬ 再确认 → k_exec
    │                 │                                              └ 取消 → k_report
    │                 ├ 确认(locked/active) → k_exec(执行) → k_report
    │                 └ 取消/超限 → notice card_cancelled → k_report
    └─ chat → chat(闲聊兜底,不碰银行工具) → END

反问循环(t_clarify/sb_clarify/l_clarify)有 MAX_CLARIFY_ROUNDS 轮上限,
超限走兜底话术收尾(interrupt-resume 不受递归上限约束,须显式封顶)。

铁律落点(研发计划 §4):
- LLM 只做理解/消歧/播报;金额换算、均摊、账户选择、确认语义解析全是确定性代码;
- create_transfer_order 只建单,t_confirm 才调 confirm_transfer_order(动钱必过闸门);
- 联动同理:create_linkage_plan 只建锁定单(pending_confirm 不动钱),
  l_gate 确认后才 confirm_transfer_order 划转预算;到期购买(execute_linkage_action
  真实扣款)必须逐动作过 l_due_gate 闸门,一次确认只买一样;
- 闸门两问仍无法确认 → 一律按取消处理(模糊指令绝不执行资金操作);
- 理财申购/赎回的"确认" = 同参数再调一次 confirmed=True(bank_core 两步语义,
  幂等靠此约定,不是订单号);取消/超限绝不二次调用,资金零变动;
- 卡片写操作(办卡/限额/状态)一律过 confirm_card 闸门;挂失(lost)不可逆,
  必须两道闸都明确 yes 才执行,任一犹豫/取消即放弃;
- 账单分析只读无闸门;时间范围换算(上个月/最近三个月→具体日期)全是
  确定性代码,LLM 只摘录原文短语;
- 每次银行工具调用在 agent 侧记 bank_calls 轨迹(bank 侧另有 audit_log 全量留痕)。

驱动约定(外层聊天循环):
- 新用户消息:graph.ainvoke({"messages": [HumanMessage(text)]}, config)
- 图被 interrupt 暂停后:graph.ainvoke(Command(resume=<用户答复文本或bool>), config)
  同一 thread_id 由 checkpointer 恢复现场;答复由发起 interrupt 的节点解析。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from bank_core.money import cents_to_yuan, yuan_to_cents
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent.bank import BankTools, load_bank_tools
from agent.state import AgentState

# 缺槽/纠偏反问的最大轮次(评审修复:interrupt-resume 循环不受 LangGraph 递归上限
# 约束——每次 resume 重新计数,必须在状态里显式封顶)
MAX_CLARIFY_ROUNDS = 3
# 人工闸门(确认/消歧)的最大追问轮次。实现约束:同一节点里写两个连续
# interrupt() 调用点是错的——resume 值永远喂给第一个调用点,第二个调用点会
# 反复重新中断,形成无限重问;正确写法是"单调用点 + 状态计数 + 自环边"。
MAX_GATE_ASKS = 2

# ===================================================================== 提示词

ROUTER_SYS = """你是银行智能助手的意图分类器。只输出一个 JSON 对象,不要任何多余文字:
{"intent": "transfer | split_bill | split_settle | contact_add | linkage | bill_analysis | wealth | card | chat"}

判定规则:
- 转账/汇款/付钱/打款/定时转 → transfer
- 发起 AA/均摊/拼单收款/向大家收钱 → split_bill
- 某人已把 AA 的钱付了/转来了 → split_settle
- 添加/新增/录入/保存 联系人或收款人(报名字、手机号、备注) → contact_add
- 生日/纪念日/联动/预留/锁定/到期/提醒处理(帮我准备生日、准备礼物、到期了该处理) → linkage
- 看账单/消费统计/分类占比/月报/收支总结/异常可疑交易 → bill_analysis
- 理财/产品/申购/赎回/持仓/风险测评 → wealth
- 卡片/银行卡/挂失/锁卡/解锁/限额/办卡 → card
- 其他(问候、闲聊、订阅代扣等) → chat"""

EXTRACT_SYS = """你是转账信息抽取器。结合【已有槽位】与【对话】,输出完整转账槽位 JSON,不要任何多余文字:
{"payee": "收款人姓名或称呼|null", "payee_phone": "11位手机号|null",
 "amount_yuan": "金额,纯数字字符串,单位元|null", "when": "now|scheduled",
 "scheduled_at": "YYYY-MM-DDTHH:MM:SS|null", "memo": "备注|null"}

规则:
- 中文数字换算成阿拉伯数字:"五百"→"500","五千五"→"5500";金额只允许纯数字(可带小数点);
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null;
- 用户要求"明天/下周三/几点"等定时转时 when="scheduled" 并给出具体 scheduled_at;立即转 when="now"。"""

SB_EXTRACT_SYS = """你是 AA 收款信息抽取器。结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"title": "账单标题|null", "total_yuan": "总金额,纯数字字符串,单位元|null",
 "participants": [{"name": "参与人姓名"}], "even": true|false}

规则:
- 用户说"均摊/AA"且没给每人份额 → even=true,participants 只填姓名,不要自己算份额;
- 用户给了每人份额 → even=false,participants 每项加 "share_yuan": "纯数字字符串";
- 最新消息覆盖已有槽位;未知填 null。"""

SS_EXTRACT_SYS = """你是 AA 收款结算抽取器。从【对话】最新一条用户消息中抽取,输出 JSON,不要任何多余文字:
{"contact_name": "刚付款的参与人姓名|null", "bill_id": 整数|null}
只看"某人付了钱"这件事;用户提到是哪张账单(如"火锅那单")时 bill_id 若不知道具体数值就填 null,由系统按最近账单匹配。"""

CONTACT_EXTRACT_SYS = """你是收款人信息抽取器。结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"name": "联系人姓名|null", "phone": "11位手机号|null", "note": "备注|null"}

规则:
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null;
- 用户明确表示"无/没有/跳过"备注时,note 填空字符串 "";
- 手机号只保留数字(用户报 138-0013-8000 时去掉连字符)。"""

LINKAGE_EXTRACT_SYS = """你是跨场景联动计划抽取器(生日/纪念日剧本:预留预算+提前准备)。
结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"event_title": "事件名,如'林悦的生日'|null", "event_date": "YYYY-MM-DD|null",
 "budget_yuan": "预算金额,纯数字字符串,单位元|null",
 "actions": [{"what": "要准备什么", "merchant": "商户名|null",
              "days_before": 提前几天整数, "amount_yuan": "纯数字字符串,单位元"}]}

规则:
- 用户说"帮我准备生日/纪念日"而没点名人时,event_title 填"生日";
- 中文数字换算成阿拉伯数字("一千"→"1000");金额只允许纯数字(可带小数点);
- "10月1日/下周五"等相对日期换算成具体 YYYY-MM-DD(以今天为基准);
- days_before 未说明时填 2;merchant 未说明填 null(由系统默认);
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null。"""

BILL_EXTRACT_SYS = """你是账单分析抽取器。结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"ask": "monthly_report|category_summary|top_merchants|anomaly_scan|null",
 "period": "时间范围的原文短语,如'上个月'/'最近三个月'/'2026-08'|null",
 "category": "用户点名的关注分类|null"}

规则:
- 月报/月度/收支/结余总结 → monthly_report;分类/类别/结构占比 → category_summary;
  消费排行/花得最多/Top商户 → top_merchants;异常/可疑/重复扣款/盗刷 → anomaly_scan;
- period 原样摘录用户说法即可,不要换算日期(具体起止日期由系统确定性计算);
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null。"""

WEALTH_EXTRACT_SYS = """你是理财业务抽取器。结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"action": "query|assess|subscribe|redeem|null",
 "keyword": "产品名或关键词|null", "product_id": 整数|null,
 "amount_yuan": "金额,纯数字字符串,单位元|null", "holding_id": 整数|null,
 "p_type": "money_fund|bond|mixed|gold|deposit|null",
 "assess_answer": 整数1-5|null}

规则:
- 推荐/有什么产品/看看理财/对比产品/查持仓 → query;风险测评/做测评 → assess;
  买/申购/购入/存入 → subscribe;赎回/取出/变现 → redeem;
- 中文数字换算成阿拉伯数字("五千"→"5000");金额只允许纯数字(可带小数点);
- assess_answer 只在系统刚问了测评题、用户回复了选项分值时填 1-5,其余填 null;
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null。"""

CARD_EXTRACT_SYS = """你是卡片业务抽取器。结合【已有槽位】与【对话】,输出 JSON,不要任何多余文字:
{"action": "list|apply|limits|status|null",
 "card_id": 整数|null, "card_hint": "卡号线索原文,如'尾号8821'/'信用卡'/'第2张'|null",
 "card_type": "debit|credit|null",
 "daily_limit_yuan": "新日限额,纯数字字符串|null",
 "per_tx_limit_yuan": "新单笔限额,纯数字字符串|null",
 "status_target": "locked|active|lost|null"}

规则:
- 看卡/我的卡/名下卡片 → list;办卡/开卡/申请卡 → apply;
  调限额/改限额/设置限额 → limits;锁卡/解锁/挂失/冻结 → status;
- 挂失 → lost;锁定/锁卡/冻结 → locked;解锁/恢复 → active;
- 用户说'尾号8821那张/信用卡/第2张'时把原话摘进 card_hint(系统去库里定位,你不要猜 card_id);
- 中文数字换算;金额只允许纯数字(可带小数点);
- 最新一条用户消息里给出的信息覆盖已有槽位;未提及的沿用已有值;确实未知填 null。"""

REPORT_SYS = """你是银行助手播报员。严格依据【事实】用 1-3 句中文向用户播报结果。
禁止编造、修改、推算任何数字与状态;事实里有错误或拦截原因就如实转述;事实里没有的信息不要补充。"""

CHAT_SYS = """你是"练功假银行"的智能助手(大赛演示沙箱)。当前版本(M2)支持:
智能转账(含定时)、AA 收款、收款人录入(引导用户提供姓名+手机号+备注,每组存一行)、
跨场景联动(生日/纪念日准备:预留预算、提前订购提醒、到期逐项确认)、
账单分析(月报/分类统计/消费Top/异常检测)、理财(推荐/风险测评/申购/赎回,动钱必经确认)、
卡片管理(查询/办卡/限额/锁定解锁挂失,挂失需双重确认)。
订阅/代扣管理即将上线,请礼貌说明并引导用户使用已支持功能;
想体验联动可以说「帮我准备生日」。你不得执行或承诺任何未经确认的资金操作。"""

# ===================================================================== 播报兜底文案(确定性,LLM 失效时用)

_FALLBACK_TEXT = {
    "executed": "转账已完成。",
    "cancelled": "转账已取消,资金未变动。",
    "scheduled_created": "定时转账单已建立,到期后会提醒你确认,确认前不会扣款。",
    "blocked": "这笔转账被风控拦截了。",
    "contact_missing": "没有找到这位收款人。想新增的话,对我说「添加收款人」,我会引导你录入姓名、手机号和备注。",
    "contact_unresolved": "还是没能确定收款人,请稍后再试或提供手机号。",
    "bank_error": "银行系统返回了错误,请稍后再试。",
    "split_created": "AA 收款已发起。",
    "split_cancelled": "已取消,本次未发起 AA 收款。",
    "split_progress": "AA 收款进度已更新。",
    "no_bill_context": "当前会话里没有可结算的 AA 账单,或没听清是谁付的款。",
    "duplicate_order": "这笔转账之前已经处理过了,本次没有重复建单、没有重复扣款。",
    "clarify_gaveup": "问了几轮还是没集齐必要信息,这次先不办理,请想好收款人、金额和时间再说。",
    "settle_cancelled": "好的,没有标记该成员已付。",
    "contact_added": "收款人已保存,之后可以直接按姓名给 ta 转账。",
    "contact_dup": "这个手机号已经是联系人了,没有重复保存。",
    "contact_cancelled": "好的,没有保存这位联系人。",
    "linkage_locked": "联动计划已生效,预留预算已按锁定单划转。",
    "linkage_cancelled": "联动计划已取消,未执行的锁定单与提醒任务已一并撤销。",
    "linkage_plan_done": "联动计划的所有准备事项都完成了。",
    "linkage_progress": "联动计划的到期事项已处理完本轮。",
    "no_due_plan": "没有找到可处理的到期联动计划。",
    # ---- 账单分析(只读) ----
    "bill_report": "账单分析已完成,数字均来自银行工具返回。",
    # ---- 理财 ----
    "no_risk_profile": "还没有风险测评记录,暂时无法推荐理财产品。对我说「做风险测评」,我先带你过五道题。",
    "wealth_products": "已按你的风险等级筛出可购产品,费率与业绩基准均来自产品库。",
    "wealth_compare": "产品对比结果已生成,数据来自产品库原样引用。",
    "wealth_holdings": "持仓与收益已查出,数字均来自银行工具返回。",
    "wealth_assessed": "风险测评已完成,等级已更新。",
    "wealth_subscribed": "申购已确认执行,资金已从付款账户扣减。",
    "wealth_redeemed": "赎回已确认执行,资金已到账。",
    "wealth_cancelled": "好的,本次理财操作已取消,资金未变动。",
    "wealth_duplicate": "这笔申购此前已经执行过了,本次没有重复扣款。",
    "wealth_need_assess": ("你还没有风险测评记录。按适当性要求,未测评暂时只能申购 R1 "
                           "现金管理类产品;对我说「做风险测评」,我先带你过五道题,"
                           "通过后就能按你的风险等级选品了。"),
    "no_product": "没有找到匹配的理财产品,换个说法或说全产品名试试。",
    "no_holding": "没有找到可赎回的持仓。",
    # ---- 卡片管理 ----
    "card_listed": "名下卡片清单已列出。",
    "card_applied": "新卡已办好(演示环境秒批)。",
    "card_limits_set": "卡片限额已按确认值调整。",
    "card_status_set": "卡片状态已变更。",
    "card_cancelled": "好的,没有执行这次卡片操作。",
    "no_card": "没有找到这张卡,请说明卡号尾号后再试。",
}


def _idempotency_key(*, thread_id: str, turn_text: str, account_id: Any,
                     contact_id: Any, to_name: str, amount_cents: int,
                     scheduled_at: str) -> str:
    """转账建单幂等键(评审修复:启用 bank_core 的防重放机制)。

    组成 = 会话 thread + 本轮指令原文 + 业务要素(账户/收款人/金额分/定时时刻)。
    - HTTP 重试/崩溃重放同一条指令 → 同键 → bank 返回既有订单(幂等命中);
    - 用户换说法再转一笔(「再转500」) → 指令原文不同 → 新键,互不误伤。
    """
    raw = "|".join(str(x) for x in (thread_id, turn_text, account_id, contact_id,
                                    to_name, amount_cents, scheduled_at))
    return "agent-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _wealth_idempotency_key(*, kind: str, thread_id: str, turn_text: str,
                            target_id: Any, amount_cents: int,
                            from_account_id: Any) -> str:
    """理财申赎幂等键(评审修复 2026-10-03:补上 bank_core 早已支持、编排层从未启用的防重放)。

    与转账 _idempotency_key 同构:同一会话 + 同一句指令原文 + 业务要素 → 同键,
    重放(HTTP 重试/崩溃恢复/用户原样重发)命中既有流水并拒绝重复扣款;
    用户换一种说法(「再买1000元」)指令原文不同 → 新键,视为新一笔,互不误伤。
    """
    raw = "|".join(str(x) for x in ("wealth", kind, thread_id, turn_text,
                                    target_id, amount_cents, from_account_id))
    return "agent-wealth-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _fallback_announcement(notice: dict | None) -> str:
    return _FALLBACK_TEXT.get(str((notice or {}).get("kind")), "处理完成。")


# ===================================================================== 确定性工具函数(不走 LLM)

# 闸门确认语义解析(评审修复:从"子串包含"改为"整句等价")。
# 资金闸门只接受无保留、无疑问的明确肯定;带犹豫/疑问/修改意图的回答一律视为
# 不可识别(→ 再问一次 → 仍不可识别则按取消处理),绝不猜着扣款。
_NO_WORDS = ("取消", "不转", "不了", "不用", "算了", "否", "no", "n", "false", "别转", "先不转")
# 保留/犹豫/想改主意的信号:出现即不可识别,进入再问
_HEDGE_WORDS = ("稍等", "等等", "等一", "先别", "别急", "改", "换成", "再想", "考虑",
                "缓一缓", "等等看", "先看看", "再看看", "我问问", "wait", "hold",
                "change", "later", "maybe", "delay")
# 疑问信号:出现即不可识别(同时覆盖半角/全角问号)
_QUESTION_WORDS = ("?", "\uff1f", "吗", "怎么", "什么", "为什么", "多少", "哪个",
                   "是不是", "能不能", "可不可以", "行不行")
# 整句等价的肯定词(归一化后精确匹配;句尾语气词会被剥掉)
_AFFIRM_WORDS = frozenset({
    "确认", "确定", "同意", "执行", "好的", "好", "是", "嗯", "行", "可以", "ok",
    "yes", "y", "true", "转吧", "确认执行", "确认转账", "同意执行", "确定执行",
    "确定转", "确认转", "就这么办", "就这么转", "办吧", "转吧确认",
    "确认一下", "确定一下", "就这么确认",
})
_TAIL_PARTICLES = "吧呀啊哈呢了哟哦嘞哒"


def _normalize_affirm(text: str) -> str:
    """小写、去空白与标点,用于整句等价比较。

    注意:字符类里不能出现连续 `~~`(re 未来语法的集合对称差操作符,会告警)。
    句尾语气词不在这里剥——"转吧"这类短语本身就在词表里,先剥反而匹配不上;
    语气词变体(如"好的了")由调用方做第二次"剥尾再比对"兜底。
    """
    low = text.lower()
    return re.sub(
        r"[\s,。.!,、;:~\u003f\uff1f\u201c\u201d\u2018\u2019「」『』()\[\]{}]+", "", low)


def parse_confirmation(value: Any) -> str | None:
    """把闸门 resume 值解析为 "yes"/"no";带保留/疑问/无法识别返回 None。

    规则(评审修复后):
    1. 布尔值直接映射;
    2. 含取消词 → "no";
    3. 含犹豫/疑问/想改主意的信号 → None(再问一次,两次仍 None 按取消);
    4. 归一化后的整句(剥句尾语气词)必须精确命中肯定词表 → "yes";
    5. 其余一律 None。
    典型判例:「好的,但稍等,我要改金额」「怎么确认?」「是500吗」
    「May I see the details」均 → None,绝不判为 yes。
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    if any(w in low for w in _NO_WORDS):
        return "no"
    if any(w in low for w in _HEDGE_WORDS) or any(w in low for w in _QUESTION_WORDS):
        return None
    norm = _normalize_affirm(text)
    if norm in _AFFIRM_WORDS:
        return "yes"
    # 剥掉句尾语气词后再比对一次(如「好的了」「行吧」)
    trimmed = norm.rstrip(_TAIL_PARTICLES)
    return "yes" if trimmed in _AFFIRM_WORDS and trimmed else None


def pick_candidate(answer: Any, candidates: list[dict]) -> dict | None:
    """从用户对"同名联系人"反问的答复中确定选了谁。

    优先级:手机尾号/备注尾号(≥3位数字)> 关系称谓 > 序号("第2个"/"2")。
    """
    text = str(answer)
    digits = re.findall(r"\d+", text)
    for d in digits:  # 1) 尾号
        if len(d) >= 3:
            for c in candidates:
                if str(c.get("phone", "")).endswith(d) or d in str(c.get("note", "")):
                    return c
    rel_words = {"spouse": ("配偶", "爱人", "老婆", "老公", "妻子", "丈夫", "家属"),
                 "friend": ("朋友", "好友"), "colleague": ("同事",),
                 "family": ("家人", "亲戚")}
    for c in candidates:  # 2) 关系称谓
        for w in rel_words.get(str(c.get("relation", "")), ()):
            if w in text:
                return c
    for d in digits:  # 3) 序号(1 起)
        if 1 <= int(d) <= len(candidates):
            return candidates[int(d) - 1]
    for w, idx in (("第一", 1), ("第二", 2), ("第三", 3), ("第四", 4), ("第五", 5)):
        if w in text and idx <= len(candidates):
            return candidates[idx - 1]
    return None


def pick_account(accounts: list[dict]) -> dict | None:
    """确定性选定付款账户:checking 优先,再余额高者,再 id 小者。"""
    if not accounts:
        return None
    pool = [a for a in accounts if a.get("type") == "checking"] or list(accounts)
    return max(pool, key=lambda a: (int(a.get("balance_cents", 0)), -int(a.get("id", 0))))


def even_split(total_cents: int, names: list[str]) -> list[dict]:
    """均摊:整数分运算,余数给最后一人,绝不产生小数分。"""
    n = len(names)
    if n <= 0 or total_cents <= 0:
        return []
    base, rest = divmod(total_cents, n)
    return [{"name": name, "share_cents": base + (rest if i == n - 1 else 0)}
            for i, name in enumerate(names)]


def json_from_content(content: Any) -> dict | None:
    """从 LLM 文本输出解析 JSON 对象;容忍 ```json 围栏与前后缀噪声。"""
    if not isinstance(content, str):
        return None
    s = content.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if fence:
        s = fence.group(1)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
        return None


def _cents(yuan: Any) -> int | None:
    """元的字符串 → 分(bank_core 确定性换算);非法/缺失返回 None。"""
    if yuan is None or yuan == "":
        return None
    try:
        return yuan_to_cents(str(yuan))
    except ValueError:
        return None


def _valid_date(s: Any) -> bool:
    """事件日期格式由确定性代码把关(铁律 1:LLM 不做校验)。"""
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ===================================================================== 反问文案(确定性,同一问题在 interrupt 与消息里保持一致)

def _slot_question(missing: list[str]) -> str:
    if "amount_yuan" in missing:
        return "请问转多少钱?请用数字说明(如:500 或 99.9)。"
    if "payee" in missing:
        return "请问转给谁?请提供收款人姓名或手机号。"
    if "scheduled_at" in missing:
        return "请问具体什么时间转?请说明日期和时刻(如:明天早上9点)。"
    return "信息还差一点,请补充收款人和金额。"


def _sb_slot_question(missing: list[str]) -> str:
    # AA 的缺槽/份额纠偏反问(原 sb_clarify 内联分支原样搬出,文案逐字不变)
    if "shares" in missing:
        return "每人分摊的份额加起来不等于总金额,请重新说明各人份额,或回复「均摊」由系统平分。"
    elif "total_yuan" in missing:
        return "AA 账单总金额是多少?请用数字说明(如:300)。"
    elif "title" in missing:
        return "这份 AA 账单叫什么?(如:火锅)"
    else:
        return "请说明参与人(至少两人,含你自己)。"


# 录入备注的"跳过"答复(确定性识别,占位词不写入备注栏)
_NOTE_SKIP_WORDS = frozenset({
    "无", "无备注", "没有", "没有备注", "不要", "不用", "不需要", "略", "跳过",
    "跳过吧", "算了", "none", "skip",
})


def _contact_slot_question(missing: list[str]) -> str:
    if "name" in missing:
        return "请提供收款人姓名。"
    if "phone" in missing:
        return "请提供这位收款人的手机号(1 开头的 11 位数字)。"
    if "note" in missing:
        return "请为这位联系人填写备注(回复「无」可跳过)。"
    return "请补充收款人姓名和手机号。"


def _valid_phone(phone: Any) -> bool:
    """手机号格式由确定性代码把关(铁律 1:LLM 不做校验)。"""
    s = str(phone or "").strip()
    return len(s) == 11 and s.isdigit() and s.startswith("1")


def _pick_question(cands: list[dict]) -> str:
    lines = ["找到多位同名联系人,请回复序号或手机尾号选择:"]
    for i, c in enumerate(cands, 1):
        tail = str(c.get("phone", ""))[-4:]
        lines.append(f"{i}. {c.get('name')}({c.get('relation', '')} {c.get('note') or ''} 尾号{tail})")
    return "\n".join(lines)


def _confirm_question(order: dict) -> str:
    when = order.get("scheduled_at")
    if when:
        return (f"请确认定时转账单:向 {order.get('to_name')} 转 {order.get('amount_yuan')} 元,"
                f"执行时间 {when};到期后仍需你确认才会扣款。确认建立吗?")
    return f"请确认转账:向 {order.get('to_name')} 转 {order.get('amount_yuan')} 元。确认执行吗?"


def _candidate_view(c: dict) -> dict:
    return {"id": c.get("id"), "name": c.get("name"), "phone": c.get("phone"),
            "relation": c.get("relation"), "note": c.get("note")}


def _order_view(o: dict) -> dict:
    return {"id": o.get("id"), "to_name": o.get("to_name"),
            "amount_yuan": o.get("amount_yuan"), "status": o.get("status"),
            "scheduled_at": o.get("scheduled_at"), "memo": o.get("memo")}


# 各管线闸门的问题文案(原各 gate 内联构造逐字搬出;载荷卡片视图在调用点内联)


def _sb_confirm_question(split: dict) -> str:
    parts = ", ".join(f"{p['name']} {p['share_yuan']}元"
                      for p in split.get("participants", []))
    return (f"请确认发起 AA 收款:《{split.get('title')}》总额 {split.get('total_yuan')} 元,"
            f"分摊:{parts}。确认发起吗?")


def _ss_confirm_question(settle: dict) -> str:
    share = settle.get("share_yuan") or "?"  # 份额未知时闸门显示 ?
    return (f"请确认把《{settle.get('title') or settle['bill_id']}》里 "
            f"{settle['contact_name']} 应付的 {share} 元标记为已付。确认吗?")


def _contact_confirm_question(draft: dict) -> str:
    return (f"请确认添加收款人:姓名 {draft.get('name')},"
            f"手机号 {draft.get('phone')},"
            f"备注「{draft.get('note') or '无'}」。确认保存吗?")


# ---------------------------------------------------------------- 联动文案/视图(确定性)

def _linkage_slot_question(missing: list[str]) -> str:
    # 联动缺槽反问:预算 → 日期 → 动作(与任务书文案顺序一致)
    if "budget_yuan" in missing:
        return "这次准备预留多少预算?请用数字说明(如:1000)。"
    if "event_date" in missing:
        return "是哪一天?请说明具体日期(如:10月1日 或 2026-10-01)。"
    if "actions" in missing:
        return "想准备些什么?请说明每样东西和金额(如:鲜花 300,蛋糕 200)。"
    return "请补充事件日期、预算和要准备的东西。"


def _linkage_confirm_question(plan: dict) -> str:
    # 照 _confirm_question 风格:复述要素 + 确认句式;金额全部来自建计划回执
    ev = plan.get("event") or {}
    lock = plan.get("lock_order") or {}
    acts = "、".join(f"{a.get('what')}{a.get('amount_yuan')}元(提前{a.get('days_before')}天)"
                     for a in plan.get("actions") or [])
    return (f"请确认联动计划《{plan.get('title')}》:事件日 {ev.get('date')},"
            f"预算 {plan.get('budget_yuan')} 元,准备:{acts};"
            f"确认后将执行预算锁定单(订单{lock.get('order_id')}),"
            f"从{lock.get('from_account') or '活期'}预留 {lock.get('amount_yuan')} 元。确认执行吗?")


def _plan_view(p: dict) -> dict:
    """联动闸门卡片视图(标题/日期/预算/各动作金额与提前天数/锁定单摘要)。"""
    return {
        "plan_id": p.get("plan_id"), "title": p.get("title"),
        "status": p.get("status"), "event": p.get("event") or {},
        "budget_yuan": p.get("budget_yuan"),
        "actions": [{"idx": a.get("idx"), "what": a.get("what"),
                     "merchant": a.get("merchant"),
                     "amount_yuan": a.get("amount_yuan"),
                     "days_before": a.get("days_before"),
                     "run_at": a.get("run_at")} for a in p.get("actions") or []],
        "lock_order": p.get("lock_order") or {},
        "progress": p.get("progress"),
    }


def _due_action_question(due: dict) -> str:
    a = (due or {}).get("action") or {}
    return (f"计划《{due.get('title')}》的【{a.get('what')}】到期了:"
            f"向{a.get('merchant') or '商户'}购买 {a.get('amount_yuan')} 元。确认购买吗?")


def _due_action_view(idx: int, a: dict, plan: dict) -> dict:
    """到期闸门动作视图(what/merchant/amount + 计划定位字段)。"""
    return {"idx": idx, "what": a.get("what"), "merchant": a.get("merchant"),
            "amount_yuan": a.get("amount_yuan"), "days_before": a.get("days_before"),
            "done": bool(a.get("done")),
            "plan_id": plan.get("plan_id"), "plan_title": plan.get("title")}


# 到期处理触发词(确定性代码判,不靠 LLM):任务书口径 = 到期/提醒/该准备了,
# 外加系统提醒文本前缀 '(到期提醒)'(api /api/reminders 生成的横幅文本)。
_DUE_TRIGGER_WORDS = ("到期", "提醒", "该准备")


def _is_due_message(text: str) -> bool:
    t = str(text or "")
    return t.startswith("(到期提醒)") or any(w in t for w in _DUE_TRIGGER_WORDS)


def _plan_title_from_text(text: str) -> str | None:
    m = re.search(r"《(.+?)》", str(text or ""))
    return m.group(1) if m else None


def _latest_human(state: AgentState) -> str:
    return str(next((m.content for m in reversed(state["messages"])
                     if isinstance(m, HumanMessage)), ""))


# ---------------------------------------------------------------- 账单分析:周期确定性解析
# 铁律 1:日期计算绝不给 LLM——"上个月/最近三个月/2026-08"→具体 start/end/month
# 全在这里算;解析不出来就当缺槽反问,绝不猜一个日期去查。

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """中文数字(一~十二,含'十'组合)→整数;解析不出返回 None。"""
    if not text:
        return None
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        if (not left or left in _CN_DIGITS) and (not right or right in _CN_DIGITS):
            return tens * 10 + ones
        return None
    if len(text) == 1 and text in _CN_DIGITS:
        return _CN_DIGITS[text]
    return None


def _shift_month(day: date, delta: int) -> date:
    """月份平移(结果取该月 1 号);跨年/进位由整数除法兜住。"""
    total = day.year * 12 + day.month - 1 + delta
    return date(total // 12, total % 12 + 1, 1)


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """某月首尾日期字符串(含首尾)。"""
    start = date(year, month, 1)
    end = _shift_month(start, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def parse_period(phrase: Any, today: date | None = None) -> dict | None:
    """把用户口述的时间范围原文解析为具体区间(纯确定性)。

    Returns:
        {"month": "YYYY-MM", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
         "days": 覆盖天数} ;解析失败返回 None(→ 缺槽反问)。
        month 恒有值:区间类短语取 end 所在月(月报兜底用)。
    """
    t = str(phrase or "").strip()
    if not t:
        return None
    ref = today or date.today()

    def _mk(start: date, end: date) -> dict:
        return {"month": f"{end.year:04d}-{end.month:02d}",
                "start": start.isoformat(), "end": end.isoformat(),
                "days": (end - start).days + 1}

    def _mk_month(year: int, month: int) -> dict:
        s, e = _month_bounds(year, month)
        return {"month": f"{year:04d}-{month:02d}", "start": s, "end": e,
                "days": (date.fromisoformat(e) - date.fromisoformat(s)).days + 1}

    # 1) 显式月份 'YYYY-MM'
    m = re.search(r"(20\d{2})[-/年.](\d{1,2})", t)
    if m:
        y, mon = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12:
            return _mk_month(y, mon)
        return None
    # 2) 本月/当月;上月(先判"上",避免"本月"被"月"误吞)
    if "本月" in t or "这个月" in t or "当月" in t:
        return _mk_month(ref.year, ref.month)
    if "上月" in t or "上个月" in t or "前一个月" in t or "前月" in t:
        prev = _shift_month(ref, -1)
        return _mk_month(prev.year, prev.month)
    # 3) 半年=6 个月(说法里是"年"不是"月",单列);最近/近/过去 N 个月
    if "半年" in t:
        return _mk(_shift_month(ref, -5), ref)
    m = re.search(r"(?:最近|近|过去|前)?\s*([0-9一二两三四五六七八九十]+)\s*个?月", t)
    if m:
        n = 6 if m.group(1) == "半" else (
            int(m.group(1)) if m.group(1).isdigit() else _cn_to_int(m.group(1)))
        if n and 1 <= n <= 24:
            start = _shift_month(ref, -(n - 1))
            return _mk(start, ref)
    # 4) 最近/近/过去 N 天
    m = re.search(r"(?:最近|近|过去)\s*([0-9一二两三四五六七八九十]+)\s*天", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _cn_to_int(m.group(1))
        if n and 1 <= n <= 365:
            return _mk(ref - timedelta(days=n - 1), ref)
    # 5) 今年/本年
    if "今年" in t or "本年" in t:
        return _mk(date(ref.year, 1, 1), ref)
    return None


# ---------------------------------------------------------------- 理财:测评题/等级口径(固定写在代码里)

# C1-C5 → 可购产品风险上限 R(bank_core.money 同口径;agent 侧重述用)
_LEVEL_MAX_RISK = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}
_LEVEL_MEANING = {
    "C1": "保守型:优先保证本金安全,只适合低风险产品",
    "C2": "稳健型:可接受小幅波动,以低风险产品为主",
    "C3": "平衡型:可接受一定波动,追求稳健增值",
    "C4": "成长型:可接受较大波动,博取较高收益",
    "C5": "进取型:追求高收益,能承受较大亏损",
}

# 五道测评题(题目与选项固定,答案 1-5;题目名与 bank_core.seed 的
# risk_profiles.answers_json 键一致,方便与种子数据对账)
_RISK_QUESTIONS = [
    ("稳健偏好", "风险测评第 1 题(共 5 题):您更能接受哪种结果?\n"
                 "1=不能接受本金亏损  2=可接受小幅亏损  3=可接受一定波动  "
                 "4=可接受较大波动  5=追求高收益不怕大亏\n请回复 1-5。"),
    ("投资经验", "风险测评第 2 题(共 5 题):您的投资理财经验有多久?\n"
                 "1=没有  2=不到 1 年  3=1-3 年  4=3-5 年  5=5 年以上\n请回复 1-5。"),
    ("亏损容忍", "风险测评第 3 题(共 5 题):若投资一年后亏损 20%,您会怎么做?\n"
                 "1=全部赎回,不再投资  2=赎回大部分  3=持观望望  "
                 "4=逢低补仓  5=大幅补仓\n请回复 1-5。"),
    ("投资期限", "风险测评第 4 题(共 5 题):这笔钱计划投资多久?\n"
                 "1=3 个月内  2=3-12 个月  3=1-3 年  4=3-5 年  5=5 年以上\n请回复 1-5。"),
    ("收入稳定", "风险测评第 5 题(共 5 题):您的收入状况更接近哪种?\n"
                 "1=不稳定  2=偶尔波动  3=基本稳定  4=稳定且有结余  5=很高且持续增长\n请回复 1-5。"),
]
_RISK_TITLES = [q[0] for q in _RISK_QUESTIONS]


def _bps_from_pct(pct: Any) -> int:
    """产品费率百分数(如 0.15)→ 万分比整数;浮点尾差用 round 兜住。"""
    try:
        return round(float(pct or 0) * 100)
    except (TypeError, ValueError):
        return 0


def _fee_yuan(amount_yuan: Any, fee_pct: Any) -> str:
    """预计费用(元字符串)= 金额分 × 费率万分比 // 10000,整数分运算。"""
    cents = _cents(amount_yuan) or 0
    return cents_to_yuan(cents * _bps_from_pct(fee_pct) // 10000)


def _norm_yuan(x: Any) -> str | None:
    """限额等"元"输入规范化:校验合法并统一成库内同款两位小数字符串。

    "3000" → "3000.00";非法/缺失返回 None(由缺槽反问兜住)。
    """
    cents = _cents(x)
    return cents_to_yuan(cents) if cents is not None else None


# ---------------------------------------------------------------- 卡片:线索确定性解析

def _card_tail(card_no_masked: Any) -> str:
    """'6222 **** **** 8821' → '8821'(展示用尾号)。"""
    digits = re.findall(r"\d+", str(card_no_masked or ""))
    return digits[-1] if digits else ""


def _resolve_card(hint: Any, cards: list[dict]) -> int | None:
    """从卡号线索(尾号/类型/序号)在 list_cards 结果里确定性定位 card_id。

    优先级:尾号(≥3 位数字)> 卡类型 > 序号(第 N 张/N)。命中不唯一返回 None
    (交回 k_clarify 反问);cards 为空同样 None(→ notice no_card 由调用层判)。
    """
    t = str(hint or "").strip()
    if not t or not cards:
        return None
    for d in re.findall(r"\d+", t):  # 1) 尾号
        if len(d) >= 3:
            m = [c for c in cards
                 if str(c.get("card_no_masked", "")).endswith(d)]
            if len(m) == 1:
                return int(m[0]["id"])
    if "信用" in t or "credit" in t.lower():  # 2) 类型
        m = [c for c in cards if c.get("card_type") == "credit"]
        if len(m) == 1:
            return int(m[0]["id"])
    if "借记" in t or "储蓄" in t or "debit" in t.lower():
        m = [c for c in cards if c.get("card_type") == "debit"]
        if len(m) == 1:
            return int(m[0]["id"])
    for d in re.findall(r"\d+", t):  # 3) 序号(1 起)
        n = int(d)
        if 1 <= n <= len(cards):
            return int(cards[n - 1]["id"])
    for w, idx in (("第一", 1), ("第二", 2), ("第三", 3)):
        if w in t and idx <= len(cards):
            return int(cards[idx - 1]["id"])
    return None


# ---------------------------------------------------------------- 新管线反问文案(确定性)

def _bill_slot_question(missing: list[str]) -> str:
    if "ask" in missing:
        return "想看哪种账单报告?可以说:月报 / 分类统计 / 消费Top商户 / 异常检测。"
    if "period" in missing:
        return "想看哪个时间范围?如:上个月 / 最近三个月 / 2026-08。"
    return "想看哪种账单报告、哪个时间范围?"


def _wealth_slot_question(missing: list[str]) -> str:
    m = str(missing[0]) if missing else ""
    if m.startswith("assess_q"):  # 测评题号编码在缺槽令牌里(见 w_extract 注释)
        idx = int(m[len("assess_q"):])
        if 1 <= idx <= len(_RISK_QUESTIONS):
            return _RISK_QUESTIONS[idx - 1][1]
        return _RISK_QUESTIONS[-1][1]
    if "amount_yuan" in missing:
        return "请问申购多少钱?请用数字说明(如:5000)。"
    if "product" in missing:
        return "请问要申购哪款产品?请说产品名称(如:余额+货币基金)。"
    if "holding" in missing:
        return "请问要赎回哪笔持仓?请说产品名称(如:稳健纯债基金)。"
    if "action" in missing:
        return "想办理哪类理财业务?可以说:推荐产品 / 风险测评 / 申购 / 赎回。"
    return "请补充理财业务信息。"


def _card_slot_question(missing: list[str]) -> str:
    if "card" in missing:
        # 工厂问句只拿得到缺槽键名,带不出库内清单;改为线索式反问,
        # 答复由 k_extract 调 list_cards 确定性解析(尾号/类型/序号)。
        return ("名下有多张卡,请问要操作哪一张?"
                "请回复卡号尾号(如:8821)、卡类型(借记卡/信用卡)或序号(如:第2张)。")
    if "card_type" in missing:
        return "想办哪种卡?请回复 借记卡 或 信用卡。"
    if "limits" in missing:
        return "想把限额调整到多少?请说明日限额和/或单笔限额(如:日限额3000,单笔1000)。"
    if "status_target" in missing:
        return "要把这张卡怎么样?请回复 锁定 / 解锁 / 挂失。"
    if "action" in missing:
        return "想办理哪类卡片业务?可以说:看卡 / 办卡 / 调限额 / 锁卡解锁挂失。"
    return "请补充卡片业务信息。"


# ---------------------------------------------------------------- 新管线闸门文案/视图(确定性)

def _w_confirm_question(w: dict) -> str:
    o = w.get("order") or {}
    return (f"请确认申购理财产品:{o.get('product')}({o.get('code')}),"
            f"金额 {o.get('amount_yuan')} 元,预计费用 {o.get('fee_yuan')} 元,"
            f"锁定期 {o.get('lock_days')} 天,风险等级 {o.get('risk_level')},"
            f"从「{o.get('from_account_name')}」扣款。确认执行吗?")


def _w_redeem_question(w: dict) -> str:
    o = w.get("order") or {}
    return (f"请确认赎回持仓:{o.get('product')}({o.get('code')}),"
            f"当前估值 {o.get('est_value_yuan')} 元,预计费用 {o.get('fee_yuan')} 元,"
            f"预计到手 {o.get('redeem_net_yuan')} 元。确认执行吗?")


def _w_order_view(w: dict) -> dict:
    """confirm_wealth 闸门载荷的 order 视图(kind=subscribe|redeem + 复述要素)。

    下划线开头的键是本层内部状态(如 _idempotency_key),只供图内节点复用,
    不进前端卡片载荷——避免内部字段泄漏到 data-wealth-confirmation。
    """
    o = {k: v for k, v in (w.get("order") or {}).items()
         if not str(k).startswith("_")}
    o["kind"] = w.get("kind")
    return o


_CARD_STATUS_CN = {"locked": "锁定", "active": "正常", "lost": "挂失", "frozen": "冻结"}


def _card_confirm_question(c: dict) -> str:
    v = c.get("card_view") or {}
    kind = v.get("kind")
    if kind == "apply":
        ctype = "借记卡" if v.get("card_type") == "debit" else "信用卡"
        return f"请确认申请一张{ctype}(演示环境秒批,即时生效)。确认办理吗?"
    if kind == "limits":
        parts = []
        if v.get("new_daily_limit_yuan"):
            parts.append(f"日限额 {v.get('current_daily_limit_yuan')} → {v.get('new_daily_limit_yuan')} 元")
        if v.get("new_per_tx_limit_yuan"):
            parts.append(f"单笔限额 {v.get('current_per_tx_limit_yuan')} → {v.get('new_per_tx_limit_yuan')} 元")
        return (f"请确认调整卡尾号{v.get('tail')}的限额:{';'.join(parts)}。确认调整吗?")
    if kind == "status":
        cur = _CARD_STATUS_CN.get(str(v.get("current_status")), str(v.get("current_status")))
        tgt = str(v.get("target"))
        if tgt == "lost":
            return (f"请确认挂失卡尾号{v.get('tail')}(当前状态:{cur})。"
                    f"挂失不可逆,确认后需联系客服补卡。确认挂失吗?")
        verb = "锁定" if tgt == "locked" else "解锁"
        return f"请确认把卡尾号{v.get('tail')}{verb}(当前状态:{cur})。确认执行吗?"
    return "请确认这次卡片操作。确认执行吗?"


def _lost_confirm_question(c: dict) -> str:
    """挂失第二道闸:明示不可逆,再确认一次。"""
    v = c.get("card_view") or {}
    return (f"⚠️ 最后确认:挂失不可逆。卡尾号{v.get('tail')}挂失后将无法消费/转账,"
            f"补卡需联系客服。真的要挂失吗?回复「确认」执行,回复「取消」放弃。")


def _card_view_of(c: dict) -> dict:
    """confirm_card 闸门载荷的 card 视图(kind/card_id/尾号/类型/当前值/目标值)。"""
    return dict(c.get("card_view") or {})


# ===================================================================== 节点工厂
# 四条管线的 gate/clarify 控制流逐行同构,差异(问题文案/载荷/取消 notice)走参数。


def _make_clarify_node(missing_key: str, question_of):
    """缺槽反问节点工厂(t/sb/c 三份 clarify 共用),控制流与原三份逐行等价。

    踩坑(评审修复):递归上限对 interrupt-resume 循环无效,每次 resume 都
    重新计数,必须状态里显式封顶,超限放弃并兜底话术收尾;载荷
    {"type": "ask_slot", question, missing} 是 data-ask-slot 部件的前端契约。
    """

    async def clarify(state: AgentState) -> dict:
        round_no = int(state.get("clarify_round") or 0)
        if round_no >= MAX_CLARIFY_ROUNDS:  # 反问封顶:超限放弃,兜底话术收尾
            return {"notice": {"kind": "clarify_gaveup", "asked": round_no}}
        missing = state.get(missing_key) or []
        question = question_of(missing)
        answer = interrupt({"type": "ask_slot", "question": question,
                            "missing": missing})
        return {"messages": [AIMessage(content=question),
                             HumanMessage(content=str(answer))],
                missing_key: [], "clarify_round": round_no + 1}

    return clarify


def _make_gate_node(payload_type: str, subject_key: str, view_key: str,
                    question_of, view_of, notice_of_no=None, notice_of_giveup=None,
                    pay_required=False, tools=None):
    """人工确认闸门节点工厂(各业务 gate 共用),控制流与原实现逐行等价:
    两问仍不可识别 → 一律按取消(铁律,见函数体注释);单一 interrupt() 调用点
    (两个连续调用点会死循环,踩坑见 MAX_GATE_ASKS 注释,须"单调用点+状态
    计数+自环边");识别成功 → decision+gate_round=0,不可识别 → 自环重问。

    参数:payload_type=载荷 type(api._interrupt_frames 按它映射 SSE data-*
    部件,前端契约);subject_key=业务对象状态键(order/split/settle/
    contact_draft);view_key=载荷卡片字段名(order/bill/settle/contact);
    question_of/view_of=对象→问题文案/卡片视图;notice_of_no(subject, view)=
    显式拒绝的取消 notice、notice_of_giveup(subject)=追问超限的取消 notice,
    None 均为不设——ss_gate 拒绝带裁剪视图、超限带原始 settle,勿"顺手统一"。

    pay_required(动钱/敏感闸):True 或 subject→bool。为真时确认方式从
    "回复确认"改为"输入 6 位支付密码"——取消词仍有效;其余输入(含光回
    "确认")都当密码尝试走 verify_pay_password 工具核验,错/无效→自环重问
    (带"支付密码不正确"前缀),两问封顶按取消,绝不误动钱。tools 为当前
    用户的 BankTools(build_agent_graph 闭包注入,核验天然对准登录用户)。
    """

    def _need_pay(subject) -> bool:
        return bool(pay_required(subject) if callable(pay_required) else pay_required)

    async def gate(state: AgentState) -> dict:
        subject = state.get(subject_key) or {}
        gate_round = int(state.get("gate_round") or 0)
        if gate_round >= MAX_GATE_ASKS:  # 两问仍不可识别/密码错 → 一律按取消(铁律)
            if notice_of_giveup is None:
                return {"decision": "no"}
            return {"decision": "no", "notice": notice_of_giveup(subject)}
        question = question_of(subject)
        view = view_of(subject)
        need_pay = _need_pay(subject)
        if need_pay:
            prefix = ("支付密码不正确或输入无效,请重试。" if gate_round else "")
            question = (f"{prefix}{question}"
                        "\n请输入 6 位支付密码确认(回复「取消」放弃)。")
        value = interrupt({"type": payload_type, "question": question,
                           view_key: view, "pay_required": need_pay})
        update: dict = {"messages": [AIMessage(content=question),
                                     HumanMessage(content=str(value))]}
        if need_pay:
            # 取消词仍然有效;其余一律按支付密码尝试核验(光回"确认"不算数)
            if parse_confirmation(value) == "no":
                cancelled = {**update, "decision": "no", "gate_round": 0}
                if notice_of_no is not None:
                    cancelled["notice"] = notice_of_no(subject, view)
                return cancelled
            ok = False
            if tools is not None:
                res = await tools.call("verify_pay_password",
                                       pay_password=str(value).strip())
                ok = isinstance(res, dict) and res.get("verified") is True
            # bank_calls 留痕(密码脱敏,与 MCP 侧审计同红线)
            update["bank_calls"] = [{"tool": "verify_pay_password",
                                     "args": {"pay_password": "***"},
                                     "ok": ok, "error": None}]
            if ok:
                return {**update, "decision": "yes", "gate_round": 0}
            return {**update, "decision": "", "gate_round": gate_round + 1}  # 自环重问
        decision = parse_confirmation(value)
        if decision is not None:
            confirmed = {**update, "decision": decision, "gate_round": 0}
            if decision == "no" and notice_of_no is not None:
                confirmed["notice"] = notice_of_no(subject, view)
            return confirmed
        return {**update, "decision": "", "gate_round": gate_round + 1}  # 自环重问

    return gate


# ===================================================================== 建图

def build_agent_graph(llm: Any, tools: BankTools, checkpointer: Any = None):
    """组装并编译 M1 编排图。

    Args:
        llm: 具备 .ainvoke 的聊天模型(生产 agent.llm.get_llm();测试注入假模型)。
        tools: agent.bank.load_bank_tools() 返回的银行工具集。
        checkpointer: 异步检查点器(见 make_sqlite_checkpointer);None=不持久化。

    Returns:
        编译后的 LangGraph(ainvoke / astream / aget_state)。
    """

    async def ask_llm(system: str, history: list[BaseMessage], extra: str = "",
                      n: int = 12) -> str:
        msgs = [SystemMessage(content=system), *history[-n:]]
        if extra:
            msgs.append(HumanMessage(content=extra))
        ai = await llm.ainvoke(msgs)
        return ai.content if isinstance(ai.content, str) else str(ai.content)

    def _jr(name: str, args: dict, result: Any) -> dict:
        """单条银行调用记录(评审修复:不再用 {**base, **journal(...)} 合并——
        那会让后一个 bank_calls 键整体覆盖前一个,丢失同节点内的早期调用)。"""
        err = result.get("error") if isinstance(result, dict) else None
        return {"tool": name, "args": args, "ok": err is None, "error": err}

    def _has_error(payload: Any) -> str | None:
        return payload.get("error") if isinstance(payload, dict) else None

    # ---------------------------------------------------------------- router

    async def router(state: AgentState) -> dict:
        parsed = json_from_content(await ask_llm(ROUTER_SYS, state["messages"]))
        intent = (parsed or {}).get("intent")
        if intent not in ("transfer", "split_bill", "split_settle",
                          "contact_add", "linkage", "bill_analysis", "wealth",
                          "card", "chat"):
            intent = "chat"  # 解析失败/未知 → 安全兜底,不碰银行
        # 本轮触发指令原文:幂等键的组成部分(同文本重放=同一笔;换说法=新一笔)
        turn_text = next((m.content for m in reversed(state["messages"])
                          if isinstance(m, HumanMessage)), "")
        # 新一轮对话:清掉上一轮的控制态(notice/order/decision 会误导下游路由),
        # 但保留 slots/split/contact_draft/linkage/bill_slots/wealth/card 等业务记忆,
        # 支持"改成1000""换成上个月"这类跨轮补充。
        return {"intent": intent, "notice": None, "order": None, "decision": "",
                "candidates": None, "contact": None, "missing": [], "settle": None,
                "split_missing": [], "contact_missing": [], "turn_text": str(turn_text),
                "plan": None, "due": None, "linkage_missing": [],
                "bill_missing": [], "wealth_missing": [], "card_missing": [],
                "clarify_round": 0, "gate_round": 0, "pick_round": 0}

    # ---------------------------------------------------------------- 转账子图

    async def t_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("slots") or {}, ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(await ask_llm(EXTRACT_SYS, history, extra)) or {}
        slots: dict = dict(state.get("slots") or {})
        for key in ("payee", "payee_phone", "amount_yuan", "when", "scheduled_at", "memo"):
            if parsed.get(key) is not None:
                slots[key] = parsed[key]
        slots.setdefault("when", "now")

        # 确定性校验(铁律 1:金额合法性由代码判)
        missing: list[str] = []
        if not slots.get("payee") and not slots.get("payee_phone"):
            missing.append("payee")
        if _cents(slots.get("amount_yuan")) is None:
            missing.append("amount_yuan")
        if slots.get("when") == "scheduled" and not slots.get("scheduled_at"):
            missing.append("scheduled_at")
        return {"slots": slots, "missing": missing}

    t_clarify = _make_clarify_node("missing", _slot_question)
    async def t_resolve(state: AgentState) -> dict:
        slots = state.get("slots") or {}
        name = slots.get("payee")
        phone = slots.get("payee_phone")
        if phone:
            found = await tools.call("resolve_contact", phone=str(phone))
        else:
            found = await tools.call("resolve_contact", name=str(name))
        calls = [_jr("resolve_contact", {"name": name, "phone": phone}, found)]
        err = _has_error(found)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "resolve_contact",
                               "error": err}}
        if not isinstance(found, list) or len(found) == 0:
            return {"bank_calls": calls,
                    "notice": {"kind": "contact_missing", "payee": name}}
        if len(found) == 1:
            return {"bank_calls": calls, "contact": found[0], "candidates": None}
        # 同名 ≥2:按 id 升序,保证反问展示与序号选择顺序确定(库里无 ORDER BY)
        return {"bank_calls": calls,
                "candidates": sorted(found, key=lambda c: int(c.get("id", 0)))}

    async def t_disambiguate(state: AgentState) -> dict:
        cands = state.get("candidates") or []
        pick_round = int(state.get("pick_round") or 0)
        if pick_round >= MAX_GATE_ASKS:  # 追问封顶仍无法消歧 → 放弃本次转账
            return {"candidates": None,
                    "notice": {"kind": "contact_unresolved"}}
        question = _pick_question(cands)
        view = [_candidate_view(c) for c in cands]
        answer = interrupt({"type": "pick_contact", "question": question, "candidates": view})
        picked = pick_candidate(answer, cands)
        msgs = [AIMessage(content=question), HumanMessage(content=str(answer))]
        if picked is not None:
            return {"messages": msgs, "contact": picked, "candidates": None,
                    "pick_round": 0}
        return {"messages": msgs, "pick_round": pick_round + 1}  # 自环重问一次

    async def t_policy(state: AgentState) -> dict:
        slots = state.get("slots") or {}
        contact = state.get("contact") or {}
        accounts = await tools.call("get_accounts")
        calls = [_jr("get_accounts", {}, accounts)]  # 评审修复:get_accounts 不再被覆盖
        err = _has_error(accounts)
        if err or not isinstance(accounts, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_accounts",
                               "error": err or "账户查询失败"}}
        account = pick_account(accounts)
        if account is None:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_accounts",
                               "error": "没有可用付款账户"}}
        check_args = {"amount_yuan": str(slots["amount_yuan"]),
                      "to_contact_id": contact.get("id"),
                      "from_account_id": account.get("id")}
        check = await tools.call("policy_check", **check_args)
        calls.append(_jr("policy_check", check_args, check))
        err = _has_error(check)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "policy_check",
                               "error": err}}
        if not check.get("approved"):
            return {"bank_calls": calls, "policy": check,
                    "notice": {"kind": "blocked", "reasons": check.get("reasons", []),
                               "amount_yuan": slots.get("amount_yuan"),
                               "payee": slots.get("payee")}}
        return {"bank_calls": calls, "policy": check,
                "account_id": int(account["id"])}

    async def t_create(state: AgentState) -> dict:
        slots = state.get("slots") or {}
        contact = state.get("contact") or {}
        args = {"from_account_id": state["account_id"],
                "amount_yuan": str(slots["amount_yuan"]),
                "to_contact_id": contact.get("id"),
                "to_name": contact.get("name") or slots.get("payee") or "",
                "memo": slots.get("memo") or ""}
        if slots.get("when") == "scheduled":
            args["scheduled_at"] = str(slots.get("scheduled_at"))
        # 评审修复(防重放):bank_core 的 idempotency_key 此前从未传过,同指令重放
        # 会建第二张单、再确认一次即重复扣款。键 = thread + 本轮指令原文 + 业务要素,
        # 同文本重放命中既有单;用户换一种说法(「再转500」)则视为新一笔,互不误伤。
        # 崩溃恢复场景(建单后、闸门前宕机):重放同指令会幂等命中 pending_confirm
        # 孤儿单并重新走闸门,不会叠加新单。
        # 注:langgraph 1.2.11 异步节点不注入 config 形参,thread_id 从 get_config() 取。
        key = _idempotency_key(
            thread_id=str((get_config().get("configurable") or {}).get("thread_id", "")),
            turn_text=str(state.get("turn_text") or ""),
            account_id=args["from_account_id"],
            contact_id=args["to_contact_id"],
            to_name=args["to_name"],
            amount_cents=_cents(args["amount_yuan"]) or 0,
            scheduled_at=str(args.get("scheduled_at") or ""))
        args["idempotency_key"] = key
        order = await tools.call("create_transfer_order", **args)
        calls = [_jr("create_transfer_order", args, order)]
        err = _has_error(order)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "create_transfer_order",
                               "error": err}}
        # 幂等命中且原单已终态:不进闸门,直接播报"已处理过",杜绝二次确认二次扣款
        if ("幂等命中" in str(order.get("note") or "")
                and order.get("status") in ("executed", "cancelled", "failed")):
            return {"bank_calls": calls,
                    "notice": {"kind": "duplicate_order", "order": _order_view(order)}}
        return {"bank_calls": calls, "order": order}

    # 闸门:取消 notice 不在此设——显式拒绝/追问超限都流转到 t_cancel 撤单产生
    t_gate = _make_gate_node("confirm_transfer", "order", "order",
                             _confirm_question, _order_view,
                             pay_required=True, tools=tools)

    async def t_confirm(state: AgentState) -> dict:
        order = state.get("order") or {}
        if order.get("status") == "scheduled":
            # 定时单的"确认"=保留订单;到期由提醒再次确认才扣款(bank_core 语义)
            return {"notice": {"kind": "scheduled_created", "order": _order_view(order)}}
        res = await tools.call("confirm_transfer_order", order_id=order["id"])
        calls = [_jr("confirm_transfer_order", {"order_id": order.get("id")}, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "confirm_transfer_order",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "executed", "result": res, "order": _order_view(order)}}

    async def t_cancel(state: AgentState) -> dict:
        order = state.get("order") or {}
        res = await tools.call("cancel_transfer_order", order_id=order["id"])
        calls = [_jr("cancel_transfer_order", {"order_id": order.get("id")}, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "cancel_transfer_order",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "cancelled", "result": res, "order": _order_view(order)}}

    async def t_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- AA 发起

    async def sb_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("split") or {}, ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(await ask_llm(SB_EXTRACT_SYS, history, extra)) or {}
        split: dict = dict(state.get("split") or {})
        for key in ("title", "total_yuan", "participants", "even"):
            if parsed.get(key) is not None:
                split[key] = parsed[key]

        missing: list[str] = []
        if not split.get("title"):
            missing.append("title")
        total_cents = _cents(split.get("total_yuan"))
        if total_cents is None:
            missing.append("total_yuan")
        parts = split.get("participants") or []
        if len(parts) < 2:
            missing.append("participants")

        # 份额计算/校验:确定性代码,LLM 不做算术
        if not missing:
            names = [str(p.get("name", "")).strip() for p in parts]
            if split.get("even") or all(not p.get("share_yuan") for p in parts):
                shares = even_split(total_cents, names)
            else:
                shares = []
                for p in parts:
                    c = _cents(p.get("share_yuan"))
                    if c is None:
                        shares = []
                        missing.append("shares")
                        break
                    shares.append({"name": str(p.get("name")), "share_cents": c})
                if "shares" not in missing and sum(s["share_cents"] for s in shares) != total_cents:
                    missing.append("shares")  # 合计≠总额,交 sb_clarify 纠正
            if not missing and shares:
                split["participants"] = [{"name": s["name"],
                                          "share_yuan": cents_to_yuan(s["share_cents"])}
                                         for s in shares]
        return {"split": split, "split_missing": missing}

    sb_clarify = _make_clarify_node("split_missing", _sb_slot_question)

    # AA 闸门:取消即终态、无撤单节点,拒绝/超限的 split_cancelled 由闸门自给
    sb_gate = _make_gate_node(
        "confirm_split", "split", "bill", _sb_confirm_question,
        view_of=lambda split: {"title": split.get("title"),
                               "total_yuan": split.get("total_yuan"),
                               "participants": split.get("participants", [])},
        notice_of_no=lambda subject, view: {"kind": "split_cancelled"},
        notice_of_giveup=lambda subject: {"kind": "split_cancelled"})

    async def sb_create(state: AgentState) -> dict:
        split = state.get("split") or {}
        args = {"title": str(split.get("title")),
                "total_yuan": str(split.get("total_yuan")),
                "participants": [{"name": p["name"], "share_yuan": str(p["share_yuan"])}
                                 for p in split.get("participants", [])]}
        bill = await tools.call("create_split_bill", **args)
        calls = [_jr("create_split_bill", args, bill)]
        err = _has_error(bill)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "create_split_bill",
                               "error": err}}
        return {"bank_calls": calls, "bill": bill,
                "notice": {"kind": "split_created", "bill": bill}}

    async def sb_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- AA 结算

    async def ss_extract(state: AgentState) -> dict:
        parsed = json_from_content(await ask_llm(SS_EXTRACT_SYS, state["messages"])) or {}
        bill_id = parsed.get("bill_id")
        bill = state.get("bill") if isinstance(state.get("bill"), dict) else None
        if bill_id is None and bill:
            bill_id = bill.get("bill_id") or bill.get("id")
        contact_name = parsed.get("contact_name")
        if bill_id is None or not contact_name:
            return {"settle": None, "notice": {"kind": "no_bill_context"}}
        # 顺手带上该成员的应付金额,供闸门复述(信息不足时闸门显示 ?)
        share = None
        title = bill.get("title") if bill else None
        if bill:
            for p in bill.get("participants") or []:
                if str(p.get("name")) == str(contact_name):
                    share = p.get("share_yuan")
                    break
        return {"settle": {"bill_id": int(bill_id), "contact_name": str(contact_name),
                           "share_yuan": share, "title": title},
                "notice": None}

    # 评审修复:settle_split_bill_item 是银行写操作,补上人工确认闸门
    # (bank 侧该方法无 audit 留痕,更不能无人确认就改结算状态)。
    # 取消 notice 两种形态(现状如此,勿统一):拒绝带裁剪视图、超限带原始 settle。
    ss_gate = _make_gate_node(
        "confirm_settle", "settle", "settle", _ss_confirm_question,
        view_of=lambda settle: {"bill_id": settle["bill_id"],
                                "contact_name": settle["contact_name"],
                                "share_yuan": settle.get("share_yuan"),
                                "title": settle.get("title")},
        notice_of_no=lambda subject, view: {"kind": "settle_cancelled", "settle": view},
        notice_of_giveup=lambda subject: {"kind": "settle_cancelled", "settle": subject})

    async def ss_settle(state: AgentState) -> dict:
        settle = state.get("settle")
        if not settle:
            return {"notice": {"kind": "no_bill_context"}}
        args = {"bill_id": settle["bill_id"], "contact_name": settle["contact_name"]}
        res = await tools.call("settle_split_bill_item", **args)
        calls = [_jr("settle_split_bill_item", args, res)]  # bank 侧无审计,agent 侧轨迹必须留
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "settle_split_bill_item",
                               "error": err}}
        progress = await tools.call("get_split_bill", bill_id=settle["bill_id"])
        calls.append(_jr("get_split_bill", {"bill_id": settle["bill_id"]}, progress))
        if _has_error(progress):
            progress = {}
        return {"bank_calls": calls,
                "notice": {"kind": "split_progress", "settled": res, "bill": progress}}

    async def ss_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- 收款人录入

    async def c_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】"
                 + json.dumps(state.get("contact_draft") or {}, ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(
            await ask_llm(CONTACT_EXTRACT_SYS, history, extra)) or {}
        draft: dict = dict(state.get("contact_draft") or {})
        for key in ("name", "phone", "note"):
            if parsed.get(key) is not None:
                draft[key] = str(parsed[key]).strip()
        # 确定性规范化:占位答复不写入备注;手机号格式不合法视同未提供
        if str(draft.get("note") or "").lower() in _NOTE_SKIP_WORDS:
            draft["note"] = ""
        missing: list[str] = []
        if not draft.get("name"):
            missing.append("name")
        if not _valid_phone(draft.get("phone")):
            missing.append("phone")
        if "note" not in draft:
            missing.append("note")
        return {"contact_draft": draft, "contact_missing": missing}

    c_clarify = _make_clarify_node("contact_missing", _contact_slot_question)

    # 收款人闸门:仅追问超限设 contact_cancelled;显式拒绝现状不设 notice
    # (与 sb/ss 不同,播报由 c_report 兜底收尾)——零行为变化,保持原样
    c_gate = _make_gate_node(
        "confirm_contact", "contact_draft", "contact", _contact_confirm_question,
        view_of=lambda draft: {"name": draft.get("name"), "phone": draft.get("phone"),
                               "note": draft.get("note") or ""},
        notice_of_giveup=lambda subject: {"kind": "contact_cancelled"})

    async def c_create(state: AgentState) -> dict:
        draft = state.get("contact_draft") or {}
        args = {"name": str(draft.get("name") or ""),
                "phone": str(draft.get("phone") or ""),
                "note": str(draft.get("note") or "")}
        res = await tools.call("add_contact", **args)
        calls = [_jr("add_contact", args, res)]
        err = _has_error(res)
        if err:
            kind = "contact_dup" if "已是联系人" in str(err) else "bank_error"
            return {"bank_calls": calls,
                    "notice": {"kind": kind, "where": "add_contact",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "contact_added", "contact": res}}

    async def c_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- 跨场景联动
    # 两条支线共用 intent="linkage":建计划(l_extract→…→l_gate)与到期处理(l_due→…)。
    # 分流由确定性代码完成(_is_due_message),LLM 只负责把消息归入 linkage 意图。

    async def l_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("linkage") or {},
                                            ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(
            await ask_llm(LINKAGE_EXTRACT_SYS, history, extra)) or {}
        draft: dict = dict(state.get("linkage") or {})
        for key in ("event_title", "event_date", "budget_yuan", "actions"):
            if parsed.get(key) is not None:
                draft[key] = parsed[key]

        # 确定性校验(铁律 1):日期格式/预算金额/动作数量与金额全由代码判
        missing: list[str] = []
        if not _valid_date(draft.get("event_date")):
            missing.append("event_date")
        if _cents(draft.get("budget_yuan")) is None:
            missing.append("budget_yuan")
        acts = draft.get("actions") or []
        if (not isinstance(acts, list) or not acts
                or not all(isinstance(a, dict) and a.get("what")
                           and _cents(a.get("amount_yuan")) is not None
                           for a in acts)):
            missing.append("actions")
        return {"linkage": draft, "linkage_missing": missing}

    l_clarify = _make_clarify_node("linkage_missing", _linkage_slot_question)

    async def l_plan(state: AgentState) -> dict:
        draft = state.get("linkage") or {}
        title = str(draft.get("event_title") or "生日")
        date = str(draft.get("event_date"))
        calls: list[dict] = []
        # 1) 事件匹配:list_events 找同名/同日事件(优先同名同日 → 同名 → 标题互含 → 同日)
        events = await tools.call("list_events")
        calls.append(_jr("list_events", {}, events))
        if _has_error(events) or not isinstance(events, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "list_events",
                               "error": _has_error(events) or "事件查询失败"}}
        ev = next((e for e in events
                   if str(e.get("date")) == date and title == str(e.get("title"))), None)
        if ev is None:
            ev = next((e for e in events if title == str(e.get("title"))), None)
        if ev is None:
            ev = next((e for e in events if title in str(e.get("title", ""))
                       or str(e.get("title", "")) in title), None)
        if ev is None:
            ev = next((e for e in events if str(e.get("date")) == date), None)
        if ev is None:
            # 2) 找不到则 add_event 建一条(生日语义按年重复,其余 custom)
            is_birthday = "生日" in title
            add_args = {"event_type": "birthday" if is_birthday else "custom",
                        "title": title, "event_date": date,
                        "repeat_yearly": is_birthday}
            res = await tools.call("add_event", **add_args)
            calls.append(_jr("add_event", add_args, res))
            err = _has_error(res)
            if err or not res.get("event_id"):
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "add_event",
                                   "error": err or "事件创建失败"}}
            ev = {"id": res["event_id"], "title": res.get("title") or title,
                  "date": res.get("date") or date}
        # 3) 建联动计划:预算锁定单只建单 pending_confirm,不动钱(铁律)
        plan_args = {
            "event_id": int(ev["id"]),
            "title": f"{ev.get('title') or title}联动",
            "budget_yuan": str(draft["budget_yuan"]),
            "actions": [{"what": str(a.get("what")),
                         "merchant": str(a.get("merchant") or "商户"),
                         "amount_yuan": str(a.get("amount_yuan")),
                         "days_before": int(a.get("days_before") or 2)}
                        for a in draft["actions"] or []],
        }
        plan = await tools.call("create_linkage_plan", **plan_args)
        calls.append(_jr("create_linkage_plan", plan_args, plan))
        err = _has_error(plan)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "create_linkage_plan",
                               "error": err}}
        return {"bank_calls": calls, "plan": plan, "notice": None}

    # 联动闸门:确认 = confirm_transfer_order 划转预算(动钱);取消 = 撤销整个计划。
    # 两种否决的 notice 由 l_cancel 统一产生(与 t_gate→t_cancel 同构),工厂参数留 None。
    l_gate = _make_gate_node("confirm_linkage", "plan", "plan",
                             _linkage_confirm_question, _plan_view,
                             pay_required=True, tools=tools)

    async def l_confirm(state: AgentState) -> dict:
        plan = state.get("plan") or {}
        lock = plan.get("lock_order") or {}
        args = {"order_id": lock.get("order_id")}
        res = await tools.call("confirm_transfer_order", **args)
        calls = [_jr("confirm_transfer_order", args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "confirm_transfer_order",
                               "error": err}}
        # 刷新计划全景(锁定单状态 pending_confirm → executed),闸门后的播报用新事实
        fresh = await tools.call("get_linkage_plan", plan_id=plan.get("plan_id"))
        calls.append(_jr("get_linkage_plan", {"plan_id": plan.get("plan_id")}, fresh))
        if not _has_error(fresh) and isinstance(fresh, dict):
            plan = fresh
        return {"bank_calls": calls, "plan": plan,
                "notice": {"kind": "linkage_locked", "result": res,
                           "plan": _plan_view(plan)}}

    async def l_cancel(state: AgentState) -> dict:
        plan = state.get("plan") or {}
        args = {"plan_id": plan.get("plan_id")}
        res = await tools.call("cancel_linkage_plan", **args)
        calls = [_jr("cancel_linkage_plan", args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "cancel_linkage_plan",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "linkage_cancelled", "result": res,
                           "plan": _plan_view(plan)}}

    async def l_report(state: AgentState) -> dict:
        return await _announce(state)

    # ------------------------------------------------------------ 到期处理
    # 计划发现(没有 list_linkage_plans 工具):本会话 plan + 提醒任务 payload 里的
    # plan_id(list_scheduled_tasks 的 pending/done 两态覆盖:未触发/已触发未购买),
    # 再逐个 get_linkage_plan 过滤 active、按标题模糊匹配,取 plan_id 最大(最新)者。
    async def l_due(state: AgentState) -> dict:
        text = _latest_human(state)
        title = _plan_title_from_text(text)
        calls: list[dict] = []
        cand_ids: list[int] = []
        plan0 = state.get("plan")
        if isinstance(plan0, dict) and plan0.get("plan_id") is not None:
            cand_ids.append(int(plan0["plan_id"]))
        for st in ("pending", "done"):
            tasks = await tools.call("list_scheduled_tasks", status=st)
            calls.append(_jr("list_scheduled_tasks", {"status": st}, tasks))
            if _has_error(tasks) or not isinstance(tasks, list):
                continue
            for t in tasks:
                pid = (t.get("payload") or {}).get("plan_id")
                if pid is not None and int(pid) not in cand_ids:
                    cand_ids.append(int(pid))
        actives: list[dict] = []
        for pid in cand_ids[:12]:  # 防御上限:演示库计划数有限,防误拖垮一轮
            d = await tools.call("get_linkage_plan", plan_id=pid)
            calls.append(_jr("get_linkage_plan", {"plan_id": pid}, d))
            if isinstance(d, dict) and not _has_error(d) and d.get("status") == "active":
                actives.append(d)
        matched = None
        if title:
            matched = next((p for p in actives
                            if title in str(p.get("title"))
                            or str(p.get("title")) in title), None)
        if matched is None and len(actives) == 1:
            matched = actives[0]  # 唯一 active 计划兜底(消息没点名/标题对不上)
        if matched is None:
            return {"bank_calls": calls, "due": None,
                    "notice": {"kind": "no_due_plan", "title": title}}
        actions = matched.get("actions") or []
        pending = [int(a["idx"]) for a in actions if not a.get("done")]
        if not pending:
            return {"bank_calls": calls, "due": None,
                    "notice": {"kind": "linkage_progress", "plan": _plan_view(matched),
                               "all_done": True}}
        amap = {int(a["idx"]): a for a in actions}
        due = {"plan_id": matched.get("plan_id"), "title": matched.get("title"),
               "actions": [_due_action_view(int(a["idx"]), a, matched) for a in actions],
               "pending": pending, "skipped": [], "executed": [],
               "action": _due_action_view(pending[0], amap[pending[0]], matched)}
        return {"bank_calls": calls, "due": due, "notice": None}

    # 到期动作闸门:每次只问当前一个购买;两问不可识别 → 按取消跳过该动作(铁律),
    # 跳过/拒绝的 notice 由 l_due_skip 产生(队列可能还有下一项,不能就此收尾)。
    l_due_gate = _make_gate_node(
        "confirm_linkage_action", "due", "action", _due_action_question,
        view_of=lambda due: dict((due or {}).get("action") or {}),
        pay_required=True, tools=tools)

    async def l_due_exec(state: AgentState) -> dict:
        due = state.get("due") or {}
        pending = list(due.get("pending") or [])
        if not pending:
            return {"notice": {"kind": "no_due_plan"}}
        idx = pending[0]
        args = {"plan_id": due.get("plan_id"), "action_idx": idx}
        res = await tools.call("execute_linkage_action", **args)
        calls = [_jr("execute_linkage_action", args, res)]
        err = _has_error(res)
        if err:  # 动钱失败宁可中止本轮,绝不带病继续买下一项
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "execute_linkage_action",
                               "error": err}}
        pending.pop(0)
        executed = list(due.get("executed") or []) + [idx]
        if not pending:  # 全部完成 → 播报计划完成
            return {"bank_calls": calls,
                    "due": {**due, "pending": [], "executed": executed},
                    "notice": {"kind": "linkage_plan_done", "executed": res,
                               "plan_id": due.get("plan_id"),
                               "title": due.get("title")}}
        views = {int(v.get("idx")): v for v in due.get("actions") or []}
        return {"bank_calls": calls,
                "due": {**due, "pending": pending, "executed": executed,
                        "action": views.get(pending[0], {})},
                "notice": None}

    async def l_due_skip(state: AgentState) -> dict:
        due = state.get("due") or {}
        pending = list(due.get("pending") or [])
        skipped = list(due.get("skipped") or [])
        if pending:
            skipped.append(pending.pop(0))
        if not pending:
            return {"due": {**due, "pending": [], "skipped": skipped},
                    "notice": {"kind": "linkage_progress",
                               "skipped": skipped,
                               "executed": list(due.get("executed") or []),
                               "plan_id": due.get("plan_id"),
                               "title": due.get("title")}}
        views = {int(v.get("idx")): v for v in due.get("actions") or []}
        return {"due": {**due, "pending": pending, "skipped": skipped,
                        "action": views.get(pending[0], {})},
                "notice": None}

    # ---------------------------------------------------------------- 账单分析(只读,无闸门)
    # b_ 前缀。时间范围换算是确定性代码(parse_period);LLM 只摘录原文短语。
    # 结果原样塞进 notice.result 交播报(REPORT_SYS:严格转述,禁止编造/推算/补齐)。

    async def b_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("bill_slots") or {},
                                            ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(await ask_llm(BILL_EXTRACT_SYS, history, extra)) or {}
        draft: dict = dict(state.get("bill_slots") or {})
        for key in ("ask", "period", "category"):
            if parsed.get(key) is not None:
                draft[key] = parsed[key]

        # 确定性校验(铁律 1):ask 枚举与日期解析全由代码判,解析失败 → 缺槽
        missing: list[str] = []
        if draft.get("ask") not in ("monthly_report", "category_summary",
                                    "top_merchants", "anomaly_scan"):
            missing.append("ask")
        parsed_period = parse_period(draft.get("period"))
        if parsed_period is None:
            missing.append("period")
            draft.pop("period_parsed", None)
        else:
            draft["period_parsed"] = parsed_period
        return {"bill_slots": draft, "bill_missing": missing}

    b_clarify = _make_clarify_node("bill_missing", _bill_slot_question)

    async def b_run(state: AgentState) -> dict:
        draft = state.get("bill_slots") or {}
        ask = draft.get("ask")
        p = draft.get("period_parsed") or {}
        # 按 ask 分派到对应只读工具;top_n 取 5(1-3 句播报装不下 10 家)
        if ask == "monthly_report":
            name, args = "monthly_report", {"month": p.get("month")}
        elif ask == "category_summary":
            name, args = "category_summary", {"start": p.get("start"),
                                              "end": p.get("end")}
        elif ask == "top_merchants":
            name, args = "top_merchants", {"start": p.get("start"),
                                           "end": p.get("end"), "top_n": 5}
        else:
            name, args = "detect_anomalies", {"days": int(p.get("days") or 90)}
        res = await tools.call(name, **args)
        calls = [_jr(name, args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": name, "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "bill_report", "ask": ask,
                           "period": draft.get("period"), "result": res}}

    async def b_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- 理财(查询/测评/申购/赎回)
    # w_ 前缀。申购/赎回动钱必过 confirm_wealth 闸门:"确认"= 同参数 confirmed=True
    # 二次调用(bank_core 两步语义,幂等靠此约定,不是订单号)。

    async def w_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("wealth") or {},
                                            ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(await ask_llm(WEALTH_EXTRACT_SYS, history, extra)) or {}
        draft: dict = dict(state.get("wealth") or {})
        prev_action = draft.get("action")
        for key in ("action", "keyword", "p_type"):
            if parsed.get(key) is not None:
                draft[key] = parsed[key]
        for key in ("product_id", "holding_id"):
            if parsed.get(key) is not None:
                try:
                    draft[key] = int(parsed[key])
                except (TypeError, ValueError):
                    draft[key] = None
        if parsed.get("amount_yuan") is not None:
            draft["amount_yuan"] = str(parsed["amount_yuan"])
        # 换了业务动作:清掉上一动作的定位/闸门阶段态,防跨动作串槽
        if parsed.get("action") and parsed["action"] != prev_action:
            for k in ("kind", "product", "order", "product_options"):
                draft.pop(k, None)
        action = draft.get("action")
        if action != "assess":
            draft.pop("assess_answers", None)

        # 确定性校验:query 可裸跑(=推荐);subscribe 需产品定位+金额;redeem 需持仓定位
        missing: list[str] = []
        if action not in ("query", "assess", "subscribe", "redeem"):
            missing.append("action")
        if action == "subscribe":
            if draft.get("product_id") is None and not draft.get("keyword"):
                missing.append("product")
            if _cents(draft.get("amount_yuan")) is None:
                missing.append("amount_yuan")
        if action == "redeem" and draft.get("holding_id") is None \
                and not draft.get("keyword"):
            missing.append("holding")

        update: dict = {"wealth": draft, "wealth_missing": missing}
        if action == "assess":
            # 测评逐题收集:答案只认 1-5(确定性校验)。缺槽令牌带题号
            # (assess_qN)——反问工厂的问句签名只收 missing 列表,题号只能
            # 编码进令牌由 _wealth_slot_question 解析;题库固定在代码里。
            # 每答上一题就把 clarify_round 归零:五题流程天然超过
            # MAX_CLARIFY_ROUNDS=3,封顶语义是"同一问题反复问不答才放弃",
            # 有进展的追问不该被误杀。
            ans = {str(k): int(v) for k, v in
                   (draft.get("assess_answers") or {}).items()}
            raw = parsed.get("assess_answer")
            try:
                val = int(str(raw).strip())
            except (TypeError, ValueError):
                val = None
            if val is not None and 1 <= val <= 5:
                nxt = f"q{len(ans) + 1}"
                if nxt not in ans:
                    ans[nxt] = val
                    update["clarify_round"] = 0
            draft["assess_answers"] = ans
            if len(ans) < 5 and not missing:
                update["wealth_missing"] = [f"assess_q{len(ans) + 1}"]
        return update

    w_clarify = _make_clarify_node("wealth_missing", _wealth_slot_question)

    async def w_query(state: AgentState) -> dict:
        draft = state.get("wealth") or {}
        calls: list[dict] = []
        text = _latest_human(state)
        if "持仓" in text:  # 问持仓:直接查,不依赖风险测评
            res = await tools.call("get_holdings")
            calls.append(_jr("get_holdings", {}, res))
            err = _has_error(res)
            if err or not isinstance(res, list):
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "get_holdings",
                                   "error": err or "持仓查询失败"}}
            return {"bank_calls": calls,
                    "notice": {"kind": "wealth_holdings", "holdings": res}}
        profile = await tools.call("get_risk_profile")
        calls.append(_jr("get_risk_profile", {}, profile))
        err = _has_error(profile)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_risk_profile",
                               "error": err}}
        # 踩坑(实测):无测评时 MCP 返回 None → 适配器给零个内容块
        # → unwrap 短路成 [];非 dict 一律按"无测评"引导,先测评再推荐
        if not (isinstance(profile, dict) and profile.get("level")):
            return {"bank_calls": calls, "notice": {"kind": "no_risk_profile"}}
        level = str(profile.get("level"))
        max_risk = _LEVEL_MAX_RISK.get(level)
        args: dict = {"max_risk_level": max_risk}
        if draft.get("keyword"):
            args["keyword"] = str(draft["keyword"])
        if draft.get("p_type"):
            args["p_type"] = str(draft["p_type"])
        res = await tools.call("list_wealth_products", **args)
        calls.append(_jr("list_wealth_products", args, res))
        err = _has_error(res)
        if err or not isinstance(res, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "list_wealth_products",
                               "error": err or "产品查询失败"}}
        if ("对比" in text or "比较" in text) and len(res) >= 2:
            ids = [int(p["id"]) for p in res[:3]]
            # 评审修复(2026-10-03):参数名原本写成 {"ids": ...},而工具签名是
            # compare_products(product_ids)——pydantic 直接报 missing/unexpected,
            # 异常被 BankTools.call 兜成 {"error": ...},又被下面的 _has_error 静默
            # 吞成空列表,于是"对比结果已生成"照播、数据全空。「对比理财产品」
            # 因此一直是坏的却不报错,测试也没覆盖对比路径。现改为:参数名对齐 +
            # 失败走可见的 bank_error(与 w_query 其它工具失败同构),不再假装成功。
            cmp_args = {"product_ids": ids}
            cmp = await tools.call("compare_products", **cmp_args)
            calls.append(_jr("compare_products", cmp_args, cmp))
            err = _has_error(cmp)
            if err or not isinstance(cmp, list):
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "compare_products",
                                   "error": err or "产品对比失败"}}
            return {"bank_calls": calls,
                    "notice": {"kind": "wealth_compare", "level": level,
                               "products": cmp}}
        return {"bank_calls": calls,
                "notice": {"kind": "wealth_products", "level": level,
                           "max_risk_level": max_risk, "products": res}}

    async def w_assess(state: AgentState) -> dict:
        draft = state.get("wealth") or {}
        ans = draft.get("assess_answers") or {}
        answers: dict[str, int] = {}
        for i, title in enumerate(_RISK_TITLES):
            v = ans.get(f"q{i + 1}")
            if v is None:  # 防御:没集齐不该到这,缺哪题回去补问哪题
                return {"wealth_missing": [f"assess_q{i + 1}"]}
            answers[title] = int(v)
        args = {"answers": answers}
        res = await tools.call("set_risk_profile", **args)
        calls = [_jr("set_risk_profile", args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "set_risk_profile",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "wealth_assessed", "result": res,
                           "meaning": _LEVEL_MEANING.get(str(res.get("level")), "")}}

    async def w_subscribe(state: AgentState) -> dict:
        draft = state.get("wealth") or {}
        calls: list[dict] = []
        # 1) 产品定位:显式 id 直查;关键词匹配(0→no_product;多命中→澄清反问)
        product = None
        if draft.get("product_id") is not None:
            pargs = {"product_id": int(draft["product_id"])}
            res = await tools.call("get_product", **pargs)
            calls.append(_jr("get_product", pargs, res))
            err = _has_error(res)
            if err:
                return {"bank_calls": calls,
                        "notice": {"kind": "no_product", "error": err}}
            product = res
        else:
            largs = {"keyword": str(draft.get("keyword") or "")}
            res = await tools.call("list_wealth_products", **largs)
            calls.append(_jr("list_wealth_products", largs, res))
            err = _has_error(res)
            if err:
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "list_wealth_products",
                                   "error": err}}
            rows = res if isinstance(res, list) else []
            if not rows:
                return {"bank_calls": calls,
                        "notice": {"kind": "no_product", "keyword": draft.get("keyword")}}
            if len(rows) > 1:
                return {"bank_calls": calls,
                        "wealth": {**draft, "product_options": [
                            {"id": p.get("id"), "name": p.get("name"),
                             "code": p.get("code")} for p in rows]},
                        "wealth_missing": ["product"]}
            product = rows[0]
        # 2) 付款账户:确定性选定(复用 pick_account:checking 优先)
        accounts = await tools.call("get_accounts")
        calls.append(_jr("get_accounts", {}, accounts))
        err = _has_error(accounts)
        if err or not isinstance(accounts, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_accounts",
                               "error": err or "账户查询失败"}}
        account = pick_account(accounts)
        if account is None:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_accounts",
                               "error": "没有可用付款账户"}}
        # 3) 建单(confirmed=False,不动钱):起购/风险超限/未测评在此就被 bank 拒掉。
        # 金额先规范化成两位小数:确认时 w_confirm 原样复用 order 里的同一字符串。
        # 幂等键在此生成、存进 order,确认节点原样复用(评审修复 2026-10-03:
        # 此前理财只靠"同参数二次调用"的字面约定,bank 侧 external_ref 带秒级 ts,
        # 同一条指令重放会再扣一次款)。
        amount_yuan = _norm_yuan(draft["amount_yuan"]) or str(draft["amount_yuan"])
        key = _wealth_idempotency_key(
            kind="sub",
            thread_id=str((get_config().get("configurable") or {}).get("thread_id", "")),
            turn_text=str(state.get("turn_text") or ""),
            target_id=int(product["id"]),
            amount_cents=_cents(amount_yuan) or 0,
            from_account_id=int(account["id"]))
        sargs = {"product_id": int(product["id"]), "amount_yuan": amount_yuan,
                 "from_account_id": int(account["id"]), "confirmed": False,
                 "idempotency_key": key}
        pending = await tools.call("subscribe_product", **sargs)
        calls.append(_jr("subscribe_product", sargs, pending))
        err = _has_error(pending)
        if err:
            # 适当性拦下来的走确定性话术(未测评/超等级),其余才是泛化银行错误
            kind = "wealth_need_assess" if "风险测评" in str(err) else "bank_error"
            return {"bank_calls": calls,
                    "notice": {"kind": kind, "where": "subscribe_product",
                               "error": err}}
        # 4) 组装闸门复述要素(费率/锁定期/风险等级原样引用产品库返回)
        order = {"product_id": int(product["id"]),
                 "product": product.get("name"), "code": product.get("code"),
                 "amount_yuan": pending.get("amount_yuan") or amount_yuan,
                 "fee_yuan": _fee_yuan(amount_yuan,
                                       product.get("subscription_fee_pct")),
                 "lock_days": product.get("lock_days"),
                 "risk_level": product.get("risk_level"),
                 "from_account_id": int(account["id"]),
                 "from_account_name": account.get("name") or account.get("type"),
                 "status": pending.get("status"),
                 "_idempotency_key": key}
        return {"bank_calls": calls,
                "wealth": {**draft, "kind": "subscribe", "product": product,
                           "order": order}}

    # 申购闸门:确认=w_confirm 同参数 confirmed=True 真扣款;取消/超限=wealth_cancelled
    w_gate = _make_gate_node(
        "confirm_wealth", "wealth", "order", _w_confirm_question, _w_order_view,
        notice_of_no=lambda subject, view: {"kind": "wealth_cancelled", "order": view},
        notice_of_giveup=lambda subject: {"kind": "wealth_cancelled"},
        pay_required=True, tools=tools)

    async def w_confirm(state: AgentState) -> dict:
        o = (state.get("wealth") or {}).get("order") or {}
        args = {"product_id": int(o["product_id"]),
                "amount_yuan": str(o["amount_yuan"]),
                "from_account_id": int(o["from_account_id"]),
                "confirmed": True}
        # 幂等键在 w_subscribe 建单时生成并存入 order,此处原样复用:同一条指令
        # 重放 → 同键 → bank 侧撞上已有流水并拒绝,不会二次扣款(评审修复)。
        if o.get("_idempotency_key"):
            args["idempotency_key"] = str(o["_idempotency_key"])
        res = await tools.call("subscribe_product", **args)
        calls = [_jr("subscribe_product", args, res)]
        err = _has_error(res)
        if err:
            kind = ("wealth_duplicate" if "幂等命中" in str(err)
                    else "wealth_need_assess" if "风险测评" in str(err)
                    else "bank_error")
            return {"bank_calls": calls,
                    "notice": {"kind": kind, "where": "subscribe_product",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "wealth_subscribed", "result": res,
                           "order": _w_order_view(state.get("wealth") or {})}}

    async def w_redeem(state: AgentState) -> dict:
        draft = state.get("wealth") or {}
        calls: list[dict] = []
        res = await tools.call("get_holdings")
        calls.append(_jr("get_holdings", {}, res))
        err = _has_error(res)
        if err or not isinstance(res, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "get_holdings",
                               "error": err or "持仓查询失败"}}
        # 持仓定位:显式 id > 关键词匹配 > 唯一持仓自动选;多笔→澄清反问;零→no_holding
        sel = None
        if draft.get("holding_id") is not None:
            sel = next((h for h in res
                        if int(h.get("holding_id", -1)) == int(draft["holding_id"])),
                       None)
            if sel is None:
                return {"bank_calls": calls,
                        "notice": {"kind": "no_holding",
                                   "holding_id": draft.get("holding_id")}}
        else:
            kw = str(draft.get("keyword") or "").strip()
            matched = ([h for h in res if kw and kw in str(h.get("product", ""))]
                       or res)
            if not matched:
                return {"bank_calls": calls,
                        "notice": {"kind": "no_holding", "keyword": draft.get("keyword")}}
            if len(matched) == 1:
                sel = matched[0]
            else:
                return {"bank_calls": calls,
                        "wealth": {**draft, "product_options": [
                            {"holding_id": h.get("holding_id"),
                             "product": h.get("product"), "code": h.get("code"),
                             "est_value_yuan": h.get("est_value_yuan")}
                            for h in matched]},
                        "wealth_missing": ["holding"]}
        hargs = {"holding_id": int(sel["holding_id"]), "confirmed": False}
        pending = await tools.call("redeem_product", **hargs)
        calls.append(_jr("redeem_product", hargs, pending))
        err = _has_error(pending)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "redeem_product",
                               "error": err}}
        # 预计费用 = 估值 - 到手(整数分确定性差,原样来自工具返回)
        est = _cents(sel.get("est_value_yuan")) or 0
        net = _cents(pending.get("redeem_net_yuan")) or 0
        order = {"holding_id": int(sel["holding_id"]),
                 "product": sel.get("product"), "code": sel.get("code"),
                 "est_value_yuan": sel.get("est_value_yuan"),
                 "fee_yuan": cents_to_yuan(max(est - net, 0)),
                 "redeem_net_yuan": pending.get("redeem_net_yuan"),
                 "risk_level": (f"R{sel['risk_level']}"
                                if sel.get("risk_level") is not None else None),
                 "status": pending.get("status")}
        return {"bank_calls": calls,
                "wealth": {**draft, "kind": "redeem", "order": order}}

    # 赎回闸门(与申购同 payload type=confirm_wealth,kind 区分;独立节点独立文案)
    w_redeem_gate = _make_gate_node(
        "confirm_wealth", "wealth", "order", _w_redeem_question, _w_order_view,
        notice_of_no=lambda subject, view: {"kind": "wealth_cancelled", "order": view},
        notice_of_giveup=lambda subject: {"kind": "wealth_cancelled"},
        pay_required=True, tools=tools)

    async def w_redeem_exec(state: AgentState) -> dict:
        o = (state.get("wealth") or {}).get("order") or {}
        args = {"holding_id": int(o["holding_id"]), "confirmed": True}
        res = await tools.call("redeem_product", **args)
        calls = [_jr("redeem_product", args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "redeem_product",
                               "error": err}}
        return {"bank_calls": calls,
                "notice": {"kind": "wealth_redeemed", "result": res,
                           "order": _w_order_view(state.get("wealth") or {})}}

    async def w_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- 卡片(查询/办卡/限额/状态)
    # k_ 前缀。写操作(apply/limits/status)一律过 confirm_card 闸门;
    # 挂失(lost)不可逆 → k_gate 之后还要过 k_lost_gate 第二道闸。

    async def k_extract(state: AgentState) -> dict:
        history = state["messages"]
        extra = ("【已有槽位】" + json.dumps(state.get("card") or {},
                                            ensure_ascii=False)
                 + "\n请输出抽取结果。")
        parsed = json_from_content(await ask_llm(CARD_EXTRACT_SYS, history, extra)) or {}
        draft: dict = dict(state.get("card") or {})
        prev_action = draft.get("action")
        if parsed.get("action") is not None:
            draft["action"] = parsed["action"]
        if parsed.get("card_id") is not None:
            try:
                draft["card_id"] = int(parsed["card_id"])
            except (TypeError, ValueError):
                pass
        for key in ("card_hint", "daily_limit_yuan", "per_tx_limit_yuan"):
            if parsed.get(key) is not None:
                draft[key] = str(parsed[key])
        if parsed.get("card_type") is not None:
            ct = str(parsed["card_type"])
            if "信用" in ct or ct == "credit":       # 中文说法归一(确定性)
                draft["card_type"] = "credit"
            elif "借记" in ct or "储蓄" in ct or ct == "debit":
                draft["card_type"] = "debit"
            else:
                draft["card_type"] = None
        if parsed.get("status_target") is not None:
            st = str(parsed["status_target"])
            if st not in ("locked", "active", "lost"):  # 中文兜底归一
                if "挂失" in st:
                    st = "lost"
                elif "锁" in st:
                    st = "locked"
                elif "解" in st or "恢复" in st:
                    st = "active"
                else:
                    st = None
            draft["status_target"] = st
        if parsed.get("action") and parsed["action"] != prev_action:
            draft.pop("card_view", None)  # 换动作清上一动作的闸门视图
        action = draft.get("action")

        calls: list[dict] = []
        missing: list[str] = []
        if action not in ("list", "apply", "limits", "status"):
            missing.append("action")
        # 先 list_cards(READ):唯一卡自动选定;多卡用线索(尾号/类型/序号)确定性解析
        if action in ("limits", "status") and draft.get("card_id") is None:
            cards = await tools.call("list_cards")
            calls.append(_jr("list_cards", {}, cards))
            err = _has_error(cards)
            if err or not isinstance(cards, list):
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "list_cards",
                                   "error": err or "卡片查询失败"}}
            if not cards:
                return {"bank_calls": calls, "notice": {"kind": "no_card"}}
            if len(cards) == 1:
                draft["card_id"] = int(cards[0]["id"])
            else:
                draft["card_id"] = _resolve_card(draft.get("card_hint"), cards)
        if action in ("limits", "status") and draft.get("card_id") is None:
            missing.append("card")
        if action == "apply" and draft.get("card_type") not in ("debit", "credit"):
            missing.append("card_type")
        if action == "limits" and _cents(draft.get("daily_limit_yuan")) is None \
                and _cents(draft.get("per_tx_limit_yuan")) is None:
            missing.append("limits")
        if action == "status" and draft.get("status_target") not in (
                "locked", "active", "lost"):
            missing.append("status_target")
        return {"card": draft, "card_missing": missing, "bank_calls": calls}

    k_clarify = _make_clarify_node("card_missing", _card_slot_question)

    async def k_list(state: AgentState) -> dict:
        res = await tools.call("list_cards")
        calls = [_jr("list_cards", {}, res)]
        err = _has_error(res)
        if err or not isinstance(res, list):
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": "list_cards",
                               "error": err or "卡片查询失败"}}
        return {"bank_calls": calls, "notice": {"kind": "card_listed", "cards": res}}

    async def k_apply(state: AgentState) -> dict:
        # 办卡prep:组装闸门视图(借记/信用说明在闸门问句里)
        draft = state.get("card") or {}
        return {"card": {**draft, "card_view": {"kind": "apply",
                                                "card_type": draft.get("card_type")}}}

    async def _card_row(draft: dict, calls: list) -> tuple[dict | None, list]:
        """按 card_id 查目标卡行(带 agent 侧轨迹);查无返回 None。"""
        cards = await tools.call("list_cards")
        calls.append(_jr("list_cards", {}, cards))
        if _has_error(cards) or not isinstance(cards, list):
            return None, calls
        row = next((c for c in cards
                    if int(c.get("id", -1)) == int(draft.get("card_id", -1))), None)
        return row, calls

    async def k_limits(state: AgentState) -> dict:
        # 限额prep:复述当前限额 + 新限额对照(调整前必须复述,bank 工具契约)
        draft = state.get("card") or {}
        calls: list[dict] = []
        row, calls = await _card_row(draft, calls)
        if row is None:
            # list_cards 本身失败与"查无此卡"分开报(轨迹里 ok=False 即工具失败)
            if calls and calls[-1]["ok"] is False:
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "list_cards",
                                   "error": calls[-1].get("error")}}
            return {"bank_calls": calls,
                    "notice": {"kind": "no_card", "card_id": draft.get("card_id")}}
        new_daily = _norm_yuan(draft.get("daily_limit_yuan"))
        new_per = _norm_yuan(draft.get("per_tx_limit_yuan"))
        card_view = {"kind": "limits", "card_id": int(row["id"]),
                     "tail": _card_tail(row.get("card_no_masked")),
                     "card_type": row.get("card_type"),
                     "current_status": row.get("status"),
                     "current_daily_limit_yuan": row.get("daily_limit_yuan"),
                     "current_per_tx_limit_yuan": row.get("per_tx_limit_yuan"),
                     "new_daily_limit_yuan": new_daily,
                     "new_per_tx_limit_yuan": new_per}
        return {"bank_calls": calls, "card": {**draft, "card_view": card_view}}

    async def k_status(state: AgentState) -> dict:
        # 状态prep:复述当前状态与目标(挂失的"不可逆"警示在两道闸的问句里)
        draft = state.get("card") or {}
        calls: list[dict] = []
        row, calls = await _card_row(draft, calls)
        if row is None:
            if calls and calls[-1]["ok"] is False:
                return {"bank_calls": calls,
                        "notice": {"kind": "bank_error", "where": "list_cards",
                                   "error": calls[-1].get("error")}}
            return {"bank_calls": calls,
                    "notice": {"kind": "no_card", "card_id": draft.get("card_id")}}
        card_view = {"kind": "status", "card_id": int(row["id"]),
                     "tail": _card_tail(row.get("card_no_masked")),
                     "card_type": row.get("card_type"),
                     "current_status": row.get("status"),
                     "target": draft.get("status_target")}
        return {"bank_calls": calls, "card": {**draft, "card_view": card_view}}

    # 卡片闸门:同一节点承接 apply/limits/status 三种 kind(视图分流);
    # 拒绝/超限 → card_cancelled,未执行任何写操作
    k_gate = _make_gate_node(
        "confirm_card", "card", "card", _card_confirm_question, _card_view_of,
        notice_of_no=lambda subject, view: {"kind": "card_cancelled", "card": view},
        notice_of_giveup=lambda subject: {"kind": "card_cancelled"},
        pay_required=lambda s: s.get("action") in ("limits", "status"),
        tools=tools)

    # 挂失第二道闸(不可逆明示):两道都明确 yes 才会走到 k_exec 的 set_card_status
    k_lost_gate = _make_gate_node(
        "confirm_card", "card", "card", _lost_confirm_question, _card_view_of,
        notice_of_no=lambda subject, view: {"kind": "card_cancelled", "card": view},
        notice_of_giveup=lambda subject: {"kind": "card_cancelled"})

    async def k_exec(state: AgentState) -> dict:
        v = dict((state.get("card") or {}).get("card_view") or {})
        kind = v.get("kind")
        if kind == "apply":
            name, args = "apply_card", {"card_type": v.get("card_type") or "debit"}
        elif kind == "limits":
            name = "set_card_limits"
            args = {"card_id": int(v["card_id"])}
            if v.get("new_daily_limit_yuan"):
                args["daily_limit_yuan"] = str(v["new_daily_limit_yuan"])
            if v.get("new_per_tx_limit_yuan"):
                args["per_tx_limit_yuan"] = str(v["new_per_tx_limit_yuan"])
        elif kind == "status":
            name, args = "set_card_status", {"card_id": int(v["card_id"]),
                                             "status": str(v["target"])}
        else:
            return {"notice": {"kind": "bank_error", "where": "k_exec",
                               "error": "未知卡片操作"}}
        res = await tools.call(name, **args)
        calls = [_jr(name, args, res)]
        err = _has_error(res)
        if err:
            return {"bank_calls": calls,
                    "notice": {"kind": "bank_error", "where": name, "error": err}}
        kind_notice = {"apply_card": "card_applied",
                       "set_card_limits": "card_limits_set",
                       "set_card_status": "card_status_set"}[name]
        return {"bank_calls": calls,
                "notice": {"kind": kind_notice, "result": res, "card": v}}

    async def k_report(state: AgentState) -> dict:
        return await _announce(state)

    # ---------------------------------------------------------------- 闲聊兜底

    async def auth_guard(state: AgentState) -> dict:
        """观光模式守卫:未登录说到任何业务意图,统一引导登录。

        零工具调用、零资金风险;notice 走 t_report 通用播报(兜底文案直说
        "右上角登录"),登录后同一条指令再说一遍即可办理。
        """
        return {"notice": {"kind": "login_required"}}

    async def chat(state: AgentState) -> dict:
        text = await ask_llm(CHAT_SYS, state["messages"])
        return {"messages": [AIMessage(content=text or "我在。")]}

    # ---------------------------------------------------------------- 通用播报

    async def _announce(state: AgentState) -> dict:
        facts = {"notice": state.get("notice"), "order": state.get("order"),
                 "bill": state.get("bill"), "plan": state.get("plan"),
                 "due": state.get("due"),
                 "bank_calls": state.get("bank_calls", [])[-6:]}
        text = await ask_llm(REPORT_SYS, state["messages"],
                             "【事实】" + json.dumps(facts, ensure_ascii=False, default=str))
        if not text or not text.strip():
            text = _fallback_announcement(state.get("notice"))
        return {"messages": [AIMessage(content=text)]}

    # ---------------------------------------------------------------- 路由函数

    def route_intent(state: AgentState) -> str:
        intent = state.get("intent") or "chat"
        # 观光模式:除闲聊外的业务意图,未登录一律进 auth_guard 引导登录
        if intent != "chat" and not state.get("auth_user_id"):
            return "auth_guard"
        if intent == "linkage":
            # linkage 的两条支线由确定性代码分流(不靠 LLM 二次判断):
            # 消息含 到期/提醒/该准备(或系统提醒前缀 '(到期提醒)')→ 到期处理,
            # 否则视为建计划。"提前2天提醒我"这类说法会被误送 l_due,匹配不到
            # active 计划时安全收尾(no_due_plan),不会误动钱。
            return "l_due" if _is_due_message(_latest_human(state)) else "l_extract"
        return intent

    def after_extract(state: AgentState) -> str:
        return "t_clarify" if state.get("missing") else "t_resolve"

    def after_clarify(state: AgentState) -> str:
        # 反问超限(notice=clarify_gaveup)→ 直接兜底收尾,不再循环(评审修复)
        return "t_report" if state.get("notice") else "t_extract"

    def after_sb_clarify(state: AgentState) -> str:
        return "sb_report" if state.get("notice") else "sb_extract"

    def after_resolve(state: AgentState) -> str:
        if state.get("notice"):
            return "t_report"
        if state.get("candidates"):
            return "t_disambiguate"
        return "t_policy"

    def after_disambiguate(state: AgentState) -> str:
        if state.get("contact"):
            return "t_policy"
        if state.get("notice"):
            return "t_report"
        return "t_disambiguate"  # 追问重选(单 interrupt 调用点 + 计数自环)

    def after_policy(state: AgentState) -> str:
        return "t_report" if state.get("notice") else "t_create"

    def after_create(state: AgentState) -> str:
        return "t_report" if state.get("notice") else "t_gate"

    def after_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "t_confirm"
        if decision == "no":
            return "t_cancel"
        return "t_gate"  # 回答不可识别 → 重问(计数封顶后自动按取消)

    def after_sb_extract(state: AgentState) -> str:
        return "sb_clarify" if state.get("split_missing") else "sb_gate"

    def after_sb_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "sb_create"
        if decision == "no":
            return "sb_report"
        return "sb_gate"

    def after_ss_extract(state: AgentState) -> str:
        return "ss_report" if state.get("notice") else "ss_gate"

    def after_ss_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "ss_settle"
        if decision == "no":
            return "ss_report"
        return "ss_gate"

    def after_c_extract(state: AgentState) -> str:
        return "c_clarify" if state.get("contact_missing") else "c_gate"

    def after_c_clarify(state: AgentState) -> str:
        return "c_report" if state.get("notice") else "c_extract"

    def after_c_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "c_create"
        if decision == "no":
            return "c_report"
        return "c_gate"

    def after_l_extract(state: AgentState) -> str:
        return "l_clarify" if state.get("linkage_missing") else "l_plan"

    def after_l_clarify(state: AgentState) -> str:
        # 反问超限(notice=clarify_gaveup)→ 直接兜底收尾,不再循环
        return "l_report" if state.get("notice") else "l_extract"

    def after_l_plan(state: AgentState) -> str:
        return "l_report" if state.get("notice") else "l_gate"

    def after_l_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "l_confirm"
        if decision == "no":
            return "l_cancel"
        return "l_gate"

    def after_l_due(state: AgentState) -> str:
        return "l_report" if state.get("notice") else "l_due_gate"

    def after_l_due_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "l_due_exec"
        if decision == "no":
            return "l_due_skip"
        return "l_due_gate"

    def after_l_due_step(state: AgentState) -> str:
        # 队列未空(notice=None)→ 回到闸门问下一项;有 notice(完成/进度/错误)→ 播报收尾
        return "l_report" if state.get("notice") else "l_due_gate"

    # ---- 账单分析路由 ----

    def after_b_extract(state: AgentState) -> str:
        return "b_clarify" if state.get("bill_missing") else "b_run"

    def after_b_clarify(state: AgentState) -> str:
        return "b_report" if state.get("notice") else "b_extract"

    # ---- 理财路由 ----

    def after_w_extract(state: AgentState) -> str:
        if state.get("wealth_missing"):
            return "w_clarify"
        return {"query": "w_query", "assess": "w_assess",
                "subscribe": "w_subscribe",
                "redeem": "w_redeem"}.get((state.get("wealth") or {}).get("action"),
                                          "w_report")

    def after_w_clarify(state: AgentState) -> str:
        return "w_report" if state.get("notice") else "w_extract"

    def after_w_assess(state: AgentState) -> str:
        # 防御路径:答案没集齐(不该发生)→ 回去补问,否则播报测评结果
        return "w_clarify" if state.get("wealth_missing") else "w_report"

    def after_w_subscribe(state: AgentState) -> str:
        if state.get("notice"):
            return "w_report"
        if state.get("wealth_missing"):  # 多产品命中 → 澄清反问后重定位
            return "w_clarify"
        return "w_gate"

    def after_w_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "w_confirm"
        if decision == "no":
            return "w_report"  # notice=wealth_cancelled 已由闸门设好
        return "w_gate"

    def after_w_redeem(state: AgentState) -> str:
        if state.get("notice"):
            return "w_report"
        if state.get("wealth_missing"):  # 多持仓命中 → 澄清反问后重定位
            return "w_clarify"
        return "w_redeem_gate"

    def after_w_redeem_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "w_redeem_exec"
        if decision == "no":
            return "w_report"
        return "w_redeem_gate"

    # ---- 卡片路由 ----

    def after_k_extract(state: AgentState) -> str:
        if state.get("card_missing"):
            return "k_clarify"
        return {"list": "k_list", "apply": "k_apply", "limits": "k_limits",
                "status": "k_status"}.get((state.get("card") or {}).get("action"),
                                          "k_report")

    def after_k_clarify(state: AgentState) -> str:
        return "k_report" if state.get("notice") else "k_extract"

    def after_k_limits(state: AgentState) -> str:
        return "k_report" if state.get("notice") else "k_gate"

    def after_k_status(state: AgentState) -> str:
        return "k_report" if state.get("notice") else "k_gate"

    def after_k_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            view = (state.get("card") or {}).get("card_view") or {}
            if view.get("kind") == "status" and view.get("target") == "lost":
                return "k_lost_gate"  # 挂失不可逆:第一道确认后再过第二道闸
            return "k_exec"
        if decision == "no":
            return "k_report"  # notice=card_cancelled 已由闸门设好
        return "k_gate"

    def after_k_lost_gate(state: AgentState) -> str:
        decision = state.get("decision")
        if decision == "yes":
            return "k_exec"  # 两道都明确 yes 才真正 set_card_status(lost)
        if decision == "no":
            return "k_report"
        return "k_lost_gate"

    # ---------------------------------------------------------------- 组装

    g = StateGraph(AgentState)
    for name, fn in [("router", router), ("t_extract", t_extract), ("t_clarify", t_clarify),
                     ("t_resolve", t_resolve), ("t_disambiguate", t_disambiguate),
                     ("t_policy", t_policy), ("t_create", t_create), ("t_gate", t_gate),
                     ("t_confirm", t_confirm), ("t_cancel", t_cancel), ("t_report", t_report),
                     ("sb_extract", sb_extract), ("sb_clarify", sb_clarify),
                     ("sb_gate", sb_gate), ("sb_create", sb_create), ("sb_report", sb_report),
                     ("ss_extract", ss_extract), ("ss_gate", ss_gate),
                     ("ss_settle", ss_settle), ("ss_report", ss_report),
                     ("c_extract", c_extract), ("c_clarify", c_clarify),
                     ("c_gate", c_gate), ("c_create", c_create), ("c_report", c_report),
                     ("l_extract", l_extract), ("l_clarify", l_clarify),
                     ("l_plan", l_plan), ("l_gate", l_gate),
                     ("l_confirm", l_confirm), ("l_cancel", l_cancel),
                     ("l_report", l_report),
                     ("l_due", l_due), ("l_due_gate", l_due_gate),
                     ("l_due_exec", l_due_exec), ("l_due_skip", l_due_skip),
                     ("b_extract", b_extract), ("b_clarify", b_clarify),
                     ("b_run", b_run), ("b_report", b_report),
                     ("w_extract", w_extract), ("w_clarify", w_clarify),
                     ("w_query", w_query), ("w_assess", w_assess),
                     ("w_subscribe", w_subscribe), ("w_gate", w_gate),
                     ("w_confirm", w_confirm),
                     ("w_redeem", w_redeem), ("w_redeem_gate", w_redeem_gate),
                     ("w_redeem_exec", w_redeem_exec), ("w_report", w_report),
                     ("k_extract", k_extract), ("k_clarify", k_clarify),
                     ("k_list", k_list), ("k_apply", k_apply),
                     ("k_limits", k_limits), ("k_status", k_status),
                     ("k_gate", k_gate), ("k_lost_gate", k_lost_gate),
                     ("k_exec", k_exec), ("k_report", k_report),
                     ("auth_guard", auth_guard), ("chat", chat)]:
        g.add_node(name, fn)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_intent, {
        "auth_guard": "auth_guard",
        "transfer": "t_extract", "split_bill": "sb_extract",
        "split_settle": "ss_extract", "contact_add": "c_extract",
        "l_extract": "l_extract", "l_due": "l_due",
        "bill_analysis": "b_extract", "wealth": "w_extract",
        "card": "k_extract", "chat": "chat"})
    g.add_conditional_edges("t_extract", after_extract, ["t_clarify", "t_resolve"])
    g.add_conditional_edges("t_clarify", after_clarify, ["t_extract", "t_report"])
    g.add_conditional_edges("t_resolve", after_resolve,
                            ["t_report", "t_disambiguate", "t_policy"])
    g.add_conditional_edges("t_disambiguate", after_disambiguate,
                            ["t_policy", "t_report", "t_disambiguate"])
    g.add_conditional_edges("t_policy", after_policy, ["t_report", "t_create"])
    g.add_conditional_edges("t_create", after_create, ["t_report", "t_gate"])
    g.add_conditional_edges("t_gate", after_gate,
                            ["t_confirm", "t_cancel", "t_gate"])
    g.add_edge("t_confirm", "t_report")
    g.add_edge("t_cancel", "t_report")
    g.add_edge("t_report", END)
    g.add_conditional_edges("sb_extract", after_sb_extract, ["sb_clarify", "sb_gate"])
    g.add_conditional_edges("sb_clarify", after_sb_clarify, ["sb_extract", "sb_report"])
    g.add_conditional_edges("sb_gate", after_sb_gate,
                            ["sb_create", "sb_report", "sb_gate"])
    g.add_edge("sb_create", "sb_report")
    g.add_edge("sb_report", END)
    g.add_conditional_edges("ss_extract", after_ss_extract, ["ss_gate", "ss_report"])
    g.add_conditional_edges("ss_gate", after_ss_gate,
                            ["ss_settle", "ss_report", "ss_gate"])
    g.add_edge("ss_settle", "ss_report")
    g.add_edge("ss_report", END)
    g.add_conditional_edges("c_extract", after_c_extract, ["c_clarify", "c_gate"])
    g.add_conditional_edges("c_clarify", after_c_clarify, ["c_extract", "c_report"])
    g.add_conditional_edges("c_gate", after_c_gate,
                            ["c_create", "c_report", "c_gate"])
    g.add_edge("c_create", "c_report")
    g.add_edge("c_report", END)
    g.add_conditional_edges("l_extract", after_l_extract, ["l_clarify", "l_plan"])
    g.add_conditional_edges("l_clarify", after_l_clarify, ["l_extract", "l_report"])
    g.add_conditional_edges("l_plan", after_l_plan, ["l_report", "l_gate"])
    g.add_conditional_edges("l_gate", after_l_gate,
                            ["l_confirm", "l_cancel", "l_gate"])
    g.add_edge("l_confirm", "l_report")
    g.add_edge("l_cancel", "l_report")
    g.add_edge("l_report", END)
    g.add_conditional_edges("l_due", after_l_due, ["l_due_gate", "l_report"])
    g.add_conditional_edges("l_due_gate", after_l_due_gate,
                            ["l_due_exec", "l_due_skip", "l_due_gate"])
    g.add_conditional_edges("l_due_exec", after_l_due_step,
                            ["l_due_gate", "l_report"])
    g.add_conditional_edges("l_due_skip", after_l_due_step,
                            ["l_due_gate", "l_report"])
    # ---- 账单分析(只读) ----
    g.add_conditional_edges("b_extract", after_b_extract, ["b_clarify", "b_run"])
    g.add_conditional_edges("b_clarify", after_b_clarify, ["b_extract", "b_report"])
    g.add_edge("b_run", "b_report")
    g.add_edge("b_report", END)
    # ---- 理财 ----
    g.add_conditional_edges("w_extract", after_w_extract,
                            ["w_clarify", "w_query", "w_assess", "w_subscribe",
                             "w_redeem", "w_report"])
    g.add_conditional_edges("w_clarify", after_w_clarify, ["w_extract", "w_report"])
    g.add_edge("w_query", "w_report")
    g.add_conditional_edges("w_assess", after_w_assess, ["w_clarify", "w_report"])
    g.add_conditional_edges("w_subscribe", after_w_subscribe,
                            ["w_report", "w_clarify", "w_gate"])
    g.add_conditional_edges("w_gate", after_w_gate,
                            ["w_confirm", "w_report", "w_gate"])
    g.add_edge("w_confirm", "w_report")
    g.add_conditional_edges("w_redeem", after_w_redeem,
                            ["w_report", "w_clarify", "w_redeem_gate"])
    g.add_conditional_edges("w_redeem_gate", after_w_redeem_gate,
                            ["w_redeem_exec", "w_report", "w_redeem_gate"])
    g.add_edge("w_redeem_exec", "w_report")
    g.add_edge("w_report", END)
    # ---- 卡片 ----
    g.add_conditional_edges("k_extract", after_k_extract,
                            ["k_clarify", "k_list", "k_apply", "k_limits",
                             "k_status", "k_report"])
    g.add_conditional_edges("k_clarify", after_k_clarify, ["k_extract", "k_report"])
    g.add_edge("k_list", "k_report")
    g.add_edge("k_apply", "k_gate")
    g.add_conditional_edges("k_limits", after_k_limits, ["k_report", "k_gate"])
    g.add_conditional_edges("k_status", after_k_status, ["k_report", "k_gate"])
    g.add_conditional_edges("k_gate", after_k_gate,
                            ["k_lost_gate", "k_exec", "k_report", "k_gate"])
    g.add_conditional_edges("k_lost_gate", after_k_lost_gate,
                            ["k_exec", "k_report", "k_lost_gate"])
    g.add_edge("k_exec", "k_report")
    g.add_edge("k_report", END)
    g.add_edge("auth_guard", "t_report")
    g.add_edge("chat", END)

    return g.compile(checkpointer=checkpointer)


# ===================================================================== 检查点器

async def make_sqlite_checkpointer(path: str | Path) -> tuple[AsyncSqliteSaver, Any]:
    """创建基于 langgraph-checkpoint-sqlite 的持久化检查点器(thread 会话可恢复)。

    为什么是 AsyncSqliteSaver 而不是同步 SqliteSaver(实测结论,勿改回):
    - MCP 适配器 0.3.2 的工具只注册了 coroutine(同步 invoke 直接
      NotImplementedError: StructuredTool does not support sync invocation)
      → 图必须以 ainvoke 运行;
    - 同步 SqliteSaver 的异步方法在 ainvoke 下直接抛
      NotImplementedError: The SqliteSaver does not support async methods
      (langgraph 1.2.11 实测);
    - AsyncSqliteSaver 出自同一包 langgraph-checkpoint-sqlite,底层 aiosqlite
      自带专用工作线程,Windows 下天然规避 sqlite 跨线程问题
      (同步版所需的 check_same_thread=False 参数在 aiosqlite 中不适用)。

    Returns:
        (saver, conn):conn 供调用方在事件循环收尾时 await conn.close()。
    """
    import aiosqlite

    conn = aiosqlite.connect(str(path))
    await conn  # aiosqlite 0.22:Connection 可等待,即完成惰性连接
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    return saver, conn


__all__ = [
    "build_agent_graph", "make_sqlite_checkpointer", "load_bank_tools",
    "parse_confirmation", "pick_candidate", "pick_account", "even_split",
    "json_from_content",
]
