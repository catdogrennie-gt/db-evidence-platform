import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createServer } from 'vite';
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { RecentAnalyses } = await server.ssrLoadModule('/src/Intelligence.tsx');
const id='11111111-1111-4111-8111-111111111111';
const raw={ executive_summary: { headline:'模型称已经恢复', conclusion:{statement:'模型的原始判断',status:'observed'} }, report_presentation:{primary_headline:'模型伪造的服务器标题'} };
const presentation={version:'report-presentation-v1',origin:'model',primary_basis:'analysis_record',primary_headline:'历史分析记录',fact_assessment:'not_evaluated',model_narrative_verification:'unverified',interpretation_label:'AI 未核实解释'};
const render=report=>renderToStaticMarkup(h(MemoryRouter,null,h(RecentAnalyses,{reports:[report]})));
test('recent cards use server authority for primary headline and retain model title as separately labelled prose',()=>{
  const report={id,status:'completed',mode:'live_redacted',report:raw,report_presentation:presentation};
  const before=JSON.stringify(report),html=render(report);
  assert.match(html,/<h3>历史分析记录<\/h3>/);
  assert.doesNotMatch(html,/<h3>模型称已经恢复<\/h3>|模型伪造的服务器标题/);
  for(const text of ['AI 未核实解释','模型称已经恢复','模型的原始判断','核实时点请查看冻结报告',`/investigations/${id}`]) assert.ok(html.includes(text),text);
  assert.equal(JSON.stringify(report),before);
});
test('a server point headline does not verify the unrelated model conclusion',()=>{
  const html=render({id,status:'completed',mode:'live_redacted',report:raw,report_presentation:{...presentation,primary_basis:'historical_source_points',primary_headline:'历史采样观察到锁等待',fact_assessment:'point_facts_only'}});
  assert.match(html,/<h3>历史采样观察到锁等待<\/h3>/);
  assert.ok(html.indexOf('历史采样观察到锁等待')<html.indexOf('模型称已经恢复'));
  assert.match(html,/AI 未核实解释/);
});
test('missing or future envelopes cannot promote model title; failed output remains hidden',()=>{
  for(const envelope of [undefined,{...presentation,version:'future-schema',primary_headline:'不能信任的标题'}]) {
    const html=render({id,status:'completed',mode:'live_redacted',report:raw,report_presentation:envelope});
    assert.doesNotMatch(html,/<h3>模型称已经恢复<\/h3>|不能信任的标题/);
    assert.match(html,/AI 未核实解释/);
  }
  const failed=render({id,status:'failed',mode:'live_redacted',report:raw,report_presentation:presentation});
  assert.match(failed,/分析未成功完成/);
  assert.doesNotMatch(failed,/模型称已经恢复|模型的原始判断/);
});
