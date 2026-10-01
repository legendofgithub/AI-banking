"""bank_core —— AI Banking Agent 练功假银行（Mock 银行核心）

设计原则（对应研发计划"四条铁律"）：
1. 金额一律用整数"分"存储/计算，杜绝浮点误差；对外接口接受字符串"元"。
2. 动钱操作分两步：先建单（pending_confirm），确认后才执行 —— 审批闸门。
3. 每个工具调用写入 audit_log，全程留痕。
4. 分析类数字全部由本模块的确定性代码计算，LLM 只负责解读。
"""

from .db import init_db, connect, DB_PATH, default_db_path, reset_db
from .ledger import LedgerService
from .analysis import AnalysisService
from .wealth import WealthService
from .events import EventService

__version__ = "0.1.0"

__all__ = [
    "init_db",
    "connect",
    "reset_db",
    "default_db_path",
    "DB_PATH",
    "LedgerService",
    "AnalysisService",
    "WealthService",
    "EventService",
]
