import { motion } from "framer-motion";
import { ShieldCheckIcon } from "lucide-react";

export const Greeting = () => (
  <div className="flex flex-col items-center px-4" key="overview">
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="text-center font-semibold text-2xl tracking-tight text-foreground md:text-3xl"
      initial={{ opacity: 0, y: 10 }}
      transition={{ delay: 0.35, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
    >
      你好，我是你的 AI 银行助手
    </motion.div>
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="mt-3 text-center text-muted-foreground/80 text-sm"
      initial={{ opacity: 0, y: 10 }}
      transition={{ delay: 0.5, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
    >
      转账、定时转账、AA 收款，一句话说清 —— 动钱前都会先请你确认。
    </motion.div>
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="mt-5 flex items-center gap-1.5 rounded-full border border-gold/20 bg-gold-bg px-3 py-1 text-[12px] text-gold-ink"
      initial={{ opacity: 0, y: 10 }}
      transition={{ delay: 0.65, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
    >
      <ShieldCheckIcon className="size-3.5" />
      <span>支付密码二次确认 · 全程审计留痕</span>
    </motion.div>
  </div>
);
