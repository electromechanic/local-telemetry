import { test } from 'node:test'
import assert from 'node:assert/strict'
import { AvianLocalTelemetry } from './avian_local_telemetry.js'

const attrs = (values) => Object.fromEntries(values.map(({key, value}) => [key, Object.values(value)[0]]))

test('local plus gateway usage counts once; remote counts; unknown stays diagnostic', async () => {
  const saved = globalThis.fetch
  const sent = []
  globalThis.fetch = async (url, init) => { sent.push({url, payload: JSON.parse(init.body)}); return {ok:true} }
  try {
    const plugin = await AvianLocalTelemetry({project:{name:'fixture'},directory:'/fixture',client:{app:{log:async()=>{}}}})
    for (const [id, providerID] of [['local','ollama'],['cloud','anthropic'],['unknown','custom']]) {
      const event = {type:'message.updated',properties:{info:{id,sessionID:'private-session',role:'assistant',providerID,modelID:'qwen',time:{created:1000,completed:2000},tokens:{input:7,output:11},text:'private content'}}}
      await plugin.event({event})
      await plugin.event({event}) // Duplicate client updates must not create usage.
    }
    const metrics = sent.flatMap(x => x.payload.resourceMetrics?.flatMap(r=>r.scopeMetrics.flatMap(s=>s.metrics)) ?? [])
    assert.equal(metrics.filter(m=>m.name==='avian.ai.client.operations').length,3)
    const inferences = metrics.filter(m=>m.name==='avian.ai.inferences')
    assert.equal(inferences.length,1)
    assert.equal(attrs(inferences[0].sum.dataPoints[0].attributes)['ai.provider'],'anthropic')
    const tokenMetrics = metrics.filter(m=>m.name==='avian.ai.tokens')
    assert.equal(tokenMetrics.length,2)
    const records = sent.flatMap(x=>x.payload.resourceLogs?.flatMap(r=>r.scopeLogs.flatMap(s=>s.logRecords)) ?? [])
    const observations = records.map(r=>attrs(r.attributes))
    assert.deepEqual(observations.map(x=>x['ai.usage.authority']),[false,true,false])
    assert.deepEqual(observations.map(x=>x['ai.event.name']),['ai.client.operation.completed','ai.inference.completed','ai.client.operation.completed'])
    assert.ok(!JSON.stringify(sent).includes('private content'))
    assert.ok(!JSON.stringify(sent).includes('private-session'))
  } finally { globalThis.fetch = saved }
})
