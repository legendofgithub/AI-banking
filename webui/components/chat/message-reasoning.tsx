"use client";

import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "../ai-elements/reasoning";

type MessageReasoningProps = {
  isLoading: boolean;
  reasoning: string;
};

export function MessageReasoning({
  isLoading,
  reasoning,
}: MessageReasoningProps) {
  return (
    <Reasoning
      data-testid="message-reasoning"
      // DeepSeek 效果:流式期间自动展开思考面板,结束后 1 秒自动收起
      // (Reasoning 内部按 isStreaming 驱动开关;defaultOpen=false 会把它钉死)
      isStreaming={isLoading}
    >
      <ReasoningTrigger />
      <ReasoningContent>{reasoning}</ReasoningContent>
    </Reasoning>
  );
}
