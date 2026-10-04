<template>
  <div class="wall">
    <h1 class="serif">镜像对账</h1>

    <section class="card" :class="rep && rep.ok ? 'ok' : 'bad'">
      <h3>镜像链状态</h3>
      <p v-if="!rep">加载中…</p>
      <template v-else>
        <p>
          <strong>{{ rep.chain.ok ? '校验和链完整' : '校验和链断裂（seq ' + rep.chain.broken_seq + '）' }}</strong>
          ｜事件 {{ rep.chain.events }} ｜愿望 {{ rep.wish_count }}
        </p>
        <p class="tag">尾校验和：<code>{{ rep.chain.tail_checksum }}</code></p>
        <p class="tag">差异 {{ rep.diff_count }} 条（主库当前态 vs 最近镜像）</p>
      </template>
    </section>

    <section class="card">
      <h3>force 重建</h3>
      <p class="tag">已拍板：重建<strong>只追加快照</strong>，历史镜像行一行不改；<code>overwrite</code> 永久拒绝并写入履历。</p>
      <button :disabled="busy" @click="rebuild('append_only')">追加快照重建</button>
      <button class="ghost" :disabled="busy" @click="rebuild('overwrite')">试探 overwrite（应拒绝）</button>
      <p v-if="msg" class="tag">{{ msg }}</p>
    </section>

    <section class="card">
      <h3>差异明细</h3>
      <p v-if="rep && !rep.diffs.length" class="tag">无差异。</p>
      <table v-else class="rec-table">
        <thead><tr><th>wish_id</th><th>差异</th><th>主库</th><th>最近镜像</th></tr></thead>
        <tbody>
          <tr v-for="(d,i) in (rep ? rep.diffs : [])" :key="i">
            <td>{{ d.wish_id }}</td>
            <td>{{ kindText(d.kind) }}</td>
            <td>{{ stateText(d.primary) }}</td>
            <td>{{ stateText(d.mirror) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card">
      <h3>重建履历</h3>
      <p v-if="!rep || !rep.rebuild_history.length" class="tag">尚无重建记录。</p>
      <table v-else class="rec-table">
        <thead><tr><th>#</th><th>模式</th><th>结果</th><th>原因</th><th>愿望/追加</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="h in rep.rebuild_history" :key="h.rebuild_seq">
            <td>{{ h.rebuild_seq }}</td>
            <td>{{ h.mode }}</td>
            <td>{{ h.result }}</td>
            <td>{{ h.reason || '—' }}</td>
            <td>{{ h.wish_count }} / {{ h.rows_appended }}</td>
            <td class="tag">{{ h.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rep = ref(null)
const busy = ref(false)
const msg = ref('')

const KINDS = {
  missing_mirror: '主库有、镜像缺',
  status_mismatch: '状态不一致',
  claimer_mismatch: '认领人不一致',
  primary_missing: '镜像有、主库缺',
}
const kindText = k => KINDS[k] || k
const stateText = s => s ? `${s.status}${s.claimer ? '/' + s.claimer : ''}${s.seq != null ? ' (seq ' + s.seq + ')' : ''}` : '—'

async function load() { rep.value = await api('/reconcile') }

async function rebuild(mode) {
  busy.value = true; msg.value = ''
  try {
    const out = await api('/reconcile/rebuild', { method: 'POST', body: JSON.stringify({ mode }) })
    msg.value = mode === 'append_only'
      ? `已追加 ${out.rebuild.rows_appended} 行快照，剩余差异 ${out.after.diff_count}`
      : 'overwrite 已按策略拒绝'
  } catch (e) {
    msg.value = '拒绝：' + e.message
  } finally {
    busy.value = false
    await load()
  }
}

onMounted(load)
</script>
<style scoped>
.ok { border-color: #8fbf8f; }
.bad { border-color: var(--rose); }
.rec-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.rec-table th, .rec-table td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
button { margin: 6px 8px 0 0; padding: 8px 14px; border-radius: 999px; border: 1px solid var(--rose); background: var(--rose); color: #fff; cursor: pointer; }
button.ghost { background: transparent; color: var(--ink); }
button:disabled { opacity: .5; }
code { font-size: 12px; word-break: break-all; }
</style>
