/** Synthetic fixtures only. Never imported by a production package. */
export const USER = '11111111-1111-4111-8111-111111111111';
export const CSRF = 'a'.repeat(64);
export const CREATED = '2026-11-07T00:00:00.000Z';
export const EXPIRES = '2099-11-07T00:00:00.000Z';
export const uuid = (group, n) => `${group.repeat(8)}-${group.repeat(4)}-4${group.repeat(3)}-8${group.repeat(3)}-${String(n).padStart(12, '0')}`;
export const scenarios = Object.freeze({
  knowledge: { title: '1 · 知识引用', mode: 'rag', question: '数据库连接超时应该怎样排查？请引用原文。' },
  proposal: { title: '2 · 联合查询与独立确认', mode: 'agent', question: '结合知识和蓝鹤工单分析连接超时，提出一张跟进工单供我确认。' },
  failure: { title: '3 · 失败与只读恢复', mode: 'agent', question: '演示任务预算耗尽后，如何按原任务编号只读核实？' },
});
export const citation = { number: 1, text: '【模拟资料】连接超时先核对连接池占用、连接上限与慢查询。对照发生时段的日志定位原因；变更前先确认影响范围。',
  metadata: { source_name: '模拟资料 · 连接池排查手册.md', chunk_id: uuid('5', 1), page: 0 } };
export const existingTicket = { id: uuid('6', 1), title: '【模拟工单】蓝鹤服务高峰期连接超时',
  description: '合成案例：高峰时段连接池等待增加，已有慢查询排查记录。此记录来自固定fixture，未读取数据库。', status: 'open', created_at: CREATED, updated_at: CREATED };
export const proposalPayload = { title: '【模拟提案】核对蓝鹤连接池与慢查询',
  description: '根据模拟资料和模拟工单，核对高峰时段连接池、慢查询及连接上限。完成影响评估后再讨论配置变更。仅在点击独立确认按钮后新增一条内存工单。' };
export function resultFor(job, action) {
  const knowledge = { id: `knowledge:${citation.metadata.chunk_id}`, kind: 'knowledge', data: citation };
  if (job.scenario === 'proposal') return { status: 'pending_confirmation', answer: '## 联合分析（模拟）\n\n固定资料提示先检查连接池与慢查询 [1]；模拟蓝鹤工单已有相似现象 [2]。\n\n已整理跟进提案，请核对下方内容，再通过**独立确认按钮**决定是否创建。聊天中的“确认”不会代替该按钮。\n\n所有内容与工具进度均为本地合成数据，未执行模型、检索或数据库操作。',
    sources: [knowledge, { id: `ticket:${existingTicket.id}`, kind: 'ticket', data: existingTicket },
      { id: `proposal:${action.action_id}`, kind: 'proposal', data: action }], citations: [citation] };
  return { status: 'answered', answer: '## 连接超时排查（模拟）\n\n1. 核对连接池占用与连接上限。\n2. 关联同一时段的慢查询和日志。\n3. 评估影响后再提出变更 [1]。\n\n点击「查看 1 条依据」可核对固定原文。此回答来自预写fixture，**不是模型生成结果或真实检索证据**。', citations: [citation], sources: [knowledge] };
}
