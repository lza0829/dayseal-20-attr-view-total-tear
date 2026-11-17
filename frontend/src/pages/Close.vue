<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const declared = ref(0)
const batches = ref([])
const msg = ref('')
const err = ref('')

async function load() {
  preview.value = await api.closePreview()
  declared.value = preview.value.system_total
  batches.value = await api.batches()
}

async function seal() {
  msg.value = ''; err.value = ''
  try {
    const r = await api.seal({
      business_day: preview.value.business_day,
      declared_cash: declared.value,
    })
    msg.value = `已封账：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 长短款 ${r.variance.toFixed(2)}`
    await load()
  } catch (e) { err.value = e.message }
}

onMounted(load)

/* align-20 */
function alignHint(batch) {
  if (!batch) return ''
  const v = Number(batch.variance || 0)
  const s = Number(batch.system_total || 0)
  const d = Number(batch.declared_cash || 0)
  return [s, d, v].join('/')
}
function alignRows(rows) {
  return (rows || []).map(r => ({ ...r, align: alignHint(r) }))
}
</script>

<template>
  <h2>日结封账</h2>
  <div v-if="preview" class="panel">
    <p>营业日 {{ preview.business_day }} · 笔数 {{ preview.line_count }} · 系统合计 {{ preview.system_total.toFixed(2) }}
      <span class="tag" :class="preview.sealed ? 'ok' : 'warn'">{{ preview.sealed ? '已封' : '未封' }}</span>
    </p>
    <p class="msg" v-if="preview.corrected_count">
      其中 {{ preview.corrected_count }} 笔为跨日改归（合计 {{ preview.corrected_total.toFixed(2) }}），已计入上式；预览按落库营业日归集。
    </p>
    <div class="msg err" v-if="preview.conflicting_count">
      <p>
        本日有 {{ preview.conflicting_count }} 笔流水的落库营业日与发生日不一致，
        无法封账：这些行只按落库营业日计入一次，请先归位或移出该日。
      </p>
      <ul>
        <li v-for="c in preview.conflicting_lines" :key="c.id">
          {{ c.line_no }} · 落库 {{ c.business_day }} · 发生 {{ c.occurred_day }}
        </li>
      </ul>
    </div>
    <p class="msg" v-if="preview.incoming_conflicting_count">
      另有 {{ preview.incoming_conflicting_count }} 笔发生日为本日、但落库在别处，未计入本日合计；
      请核对它们的落库营业日。
    </p>
    <p class="msg">归属策略：{{ preview.attribution.label }} —— {{ preview.attribution.description }}</p>
    <div class="row">
      <label>实盘现金 <input type="number" step="0.01" v-model.number="declared" /></label>
      <button class="btn" :disabled="preview.sealed || !preview.sealable" @click="seal">封账</button>
      <button class="btn ghost" @click="load">刷新</button>
    </div>
  </div>
  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>
  <div class="panel">
    <h2>封账批次</h2>
    <table>
      <thead><tr><th>批次</th><th>营业日</th><th>系统</th><th>实盘</th><th>差额</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td><td>{{ b.business_day }}</td>
          <td>{{ b.system_total.toFixed(2) }}</td><td>{{ b.declared_cash.toFixed(2) }}</td>
          <td>{{ b.variance.toFixed(2) }}</td><td>{{ b.status }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
