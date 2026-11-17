<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const rows = ref([])
const tills = ref([])
const corrections = ref([])
const day = ref('')
const form = ref({ till_code: 'TILL-A', amount: 10, pay_method: '现金', note: '', occurred_at: '' })
const importText = ref('')
const msg = ref('')
const err = ref('')

const correctedIds = computed(() => new Set(corrections.value.map((c) => c.line_no)))

function nowLocal() {
  const d = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`
}

async function load() {
  err.value = ''
  const d = await api.getDay()
  day.value = d.business_day
  tills.value = await api.tills()
  rows.value = await api.lines(day.value)
  corrections.value = await api.corrections(day.value)
}

async function add() {
  msg.value = ''; err.value = ''
  try {
    const body = { ...form.value, business_day: day.value }
    if (!body.occurred_at) delete body.occurred_at
    const r = await api.createLine(body)
    msg.value = r.attribution_corrected
      ? `已登记；发生日 ${r.occurred_day} 与声明日 ${r.declared_business_day} 不一致，已归入发生日并留痕`
      : '已登记流水'
    await load()
  } catch (e) { err.value = e.message }
}

async function doImport() {
  msg.value = ''; err.value = ''
  try {
    const parsed = JSON.parse(importText.value)
    const rows = Array.isArray(parsed) ? parsed : parsed.rows
    const r = await api.importLines({ business_day: day.value, rows })
    msg.value = `已导入 ${r.imported} 笔，按落库营业日归集：` +
      r.days.map((d) => `${d.business_day} ${d.line_count} 笔 / ${d.system_total.toFixed(2)}`).join('；') +
      (r.corrections ? `（${r.corrections} 笔归属校正）` : '')
    await load()
  } catch (e) { err.value = e.message }
}

onMounted(load)
</script>

<template>
  <h2>收银流水</h2>
  <p class="msg">营业日 {{ day }} · 列表只按落库营业日过滤，封账预览与批次合计同源</p>
  <div class="row">
    <label>收银机
      <select v-model="form.till_code">
        <option v-for="t in tills" :key="t.till_code" :value="t.till_code">{{ t.title }}</option>
      </select>
    </label>
    <label>发生时刻
      <input v-model="form.occurred_at" :placeholder="nowLocal()" />
    </label>
    <label>金额 <input type="number" step="0.01" v-model.number="form.amount" /></label>
    <label>支付方式
      <select v-model="form.pay_method">
        <option>现金</option><option>微信</option><option>支付宝</option>
      </select>
    </label>
    <button class="btn" @click="add">登记</button>
  </div>
  <p class="msg">发生日与声明营业日不一致时，请求可成功，但落库营业日以发生日为准并留痕。</p>
  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <h2>批量导入</h2>
    <p class="msg">JSON 数组，每行含 till_code / amount，可选 occurred_at、business_day、pay_method；整批一个事务。</p>
    <label>
      <textarea v-model="importText" rows="4" placeholder='[{"till_code":"TILL-A","amount":12.5,"occurred_at":"2026-11-16T09:30:00Z"}]'></textarea>
    </label>
    <div class="row"><button class="btn ghost" @click="doImport">导入</button></div>
  </div>

  <div class="panel">
    <h2>当日流水</h2>
    <table>
      <thead><tr><th>单号</th><th>机</th><th>发生时刻</th><th>落库营业日</th><th>金额</th><th>方式</th><th>归属</th></tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.line_no }}</td><td>{{ r.till_code }}</td><td>{{ r.occurred_at }}</td>
          <td>{{ r.business_day }}</td>
          <td>{{ r.amount.toFixed(2) }}</td><td>{{ r.pay_method }}</td>
          <td>
            <span v-if="correctedIds.has(r.line_no)" class="tag warn">已归入发生日</span>
            <span v-else class="tag ok">一致</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="panel" v-if="corrections.length">
    <h2>归属校正留痕</h2>
    <table>
      <thead><tr><th>单号</th><th>动作</th><th>声明日</th><th>发生日</th><th>落库日</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-for="c in corrections" :key="c.id">
          <td>{{ c.line_no }}</td><td>{{ c.action }}</td>
          <td>{{ c.declared_business_day }}</td><td>{{ c.occurred_day }}</td>
          <td>{{ c.stored_business_day }}</td><td>{{ c.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
