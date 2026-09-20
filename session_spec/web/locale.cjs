const english = {
  '技术对照表': 'Technical comparison', '明确不做 · Non-goals': 'Non-goals', '本次涉及的范围': 'Scope',
  '限制与前提': 'Constraints and prerequisites', '用户约束': 'User constraint', '环境前提': 'Environment',
  '阅读起点': 'START HERE', '先把这件事说清楚': 'The starting point', '要解决什么': 'The problem',
  '希望达到的状态 · Goal': 'Goal', '解决思路': 'Approach', '这次走到了哪里': 'Where the session ended',
  '回到最初的问题': 'BACK TO THE ORIGINAL PROBLEM', '背景、目标与边界': 'Context, goals and boundaries',
  '故事路线': 'Story route', '这次留下了什么': 'What this session produced', '文档入口': 'Documents',
  '两篇故事': 'Stories', '想核对结论？展开本次验证的范围': 'Check the evidence and the scope of validation',
  '本次验证范围': 'Validation scope', '要回答的问题': 'Question', '实际看到的': 'Observed', '还不能据此断言': 'Not established',
  '这里只汇总会话中已有的观察，不代表今天重新执行过这些检查。完整依据随交接文档保存在 _support 目录。': 'These observations come from the historical session; the checks were not rerun today. Supporting evidence is in _support.',
  '继续阅读': 'Continue reading', '查看／复制 Agent 交接版': 'View / copy Agent handoff',
  '独立交接文件': 'Companion files:',
  '本文记录这段会话的工作与判断。接续建议不是已完成的操作，历史操作也不是新的执行授权。': 'This document records the session’s work and decisions. Suggested next steps are not completed work, and historical actions are not fresh authorization.',
  '这篇故事': 'In this story', '阅读路线': 'Reading guide',
  '先理解发生了什么，再看方案怎样工作。具体命令与证据留给需要深入的读者。': 'Start with what happened, then explore how the solution works. The Agent handoff provides detailed commands, trajectory and evidence.',
  'Agent 交接版 · Markdown 原文': 'Agent handoff · Markdown source', '关闭': 'Close',
  '独立文件仍是 agent-spec.md。这里仅显示同一份原文，避免浏览器拦截文件跳转；不会执行其中的命令。': 'The standalone file is agent-spec.md. This viewer shows the identical Markdown without navigating to a download. Commands are never executed.',
  '复制完整 Markdown': 'Copy complete Markdown', 'Agent Markdown 原文': 'Agent Markdown source',
  '已复制完整 Markdown，可直接粘贴给 Agent。': 'Complete Markdown copied. You can paste it into an agent.',
  '已选中全文，请按 Ctrl+C（Mac 为 ⌘C）复制。': 'Full text selected. Press Ctrl+C (Cmd+C on Mac) to copy.',
  '一段真实的技术工作': 'A REAL ENGINEERING SESSION', '回看这次留下的认识': 'Return to the takeaway',
  '工程会话记录': 'ENGINEERING SESSION',
  '入口': 'Entry', '控制': 'Control', '处理': 'Processing', '存储': 'Storage', '产物': 'Artifact', '外部': 'External',
  '当次观察': 'observed in session', '实现约定': 'implementation', '返回/反馈': 'return / feedback',
  '产物架构': 'Artifact architecture', '实现流程': 'Implementation flow', '架构图，可横向滚动': 'Architecture diagram; scroll horizontally',
  '节点职责与连线的文字等价说明在图后。': 'A text equivalent of the nodes and edges follows the diagram.',
  '实现／配置关系': 'Implementation / configuration', '当次观察关系': 'Observed relationship', '返回／反馈方向': 'Return / feedback',
  '用文字读这张图': 'Read the diagram as text',
};

function translator(language = 'zh-CN') {
  return text => language.startsWith('zh') ? text : (english[text] || text);
}

module.exports = { translator };
