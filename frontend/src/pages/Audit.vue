<template>
  <div class="wall">
    <h1 class="serif">对账 · 镜像仓</h1>
    <p v-if="err" class="err">{{ err }}</p>

    <div class="audit-grid">
      <div class="card">
        <h3>总览</h3>
        <p class="tag">主库愿望 {{ r.summary?.primary_wishes ?? '—' }}
          · 已镜像 {{ r.summary?.mirrored_wishes ?? '—' }}
          · 未镜像 open {{ r.summary?.unmirrored_open ?? '—' }}</p>
        <p>
          <span class="pill" :class="r.summary?.in_sync ? 'ok' : 'bad'">
            {{ r.summary?.in_sync ? '主库与镜像一致' : `差异 ${r.summary?.drift_count ?? 0} 项` }}
          </span>
          <span class="pill" :class="r.chain?.ok ? 'ok' : 'bad'">
            哈希链 {{ r.chain?.ok ? '完整' : '断裂' }}
          </span>
        </p>
        <p class="tag">镜像链长度 {{ r.chain?.length ?? 0 }} · 末端 seq {{ r.chain?.tip_seq ?? 0 }}</p>
        <p class="hash">链首校验和（末端）<br><code>{{ r.chain?.tip_checksum || '—' }}</code></p>
      </div>

      <div class="card">
        <h3>重建政策（已拍板）</h3>
        <p>模式：<strong>{{ r.policy?.mode === 'append_only' ? '只追加 append-only' : r.policy?.mode }}</strong></p>
        <p class="tag">覆盖最新快照：{{ r.policy?.overwrite ? '是' : '否（永不覆盖/删除历史镜像行）' }}</p>
        <p class="tag">{{ r.policy?.description }}</p>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input v-model="reason" placeholder="重建事由（可选）" style="flex:1;min-width:160px;margin:0" />
          <button @click="rebuild" :disabled="busy">{{ busy ? '重建中…' : '强制重建（只追加）' }}</button>
        </div>
      </div>
    </div>

    <h3 class="serif">主库当前态 vs 最近镜像</h3>
    <table v-if="r.diffs && r.diffs.length" class="audit-table">
      <thead><tr><th>wish_id</th><th>差异</th><th>主库</th><th>最近镜像</th></tr></thead>
      <tbody>
        <tr v-for="d in r.diffs" :key="d.wish_id + d.kind">
          <td>{{ d.wish_id }}</td>
          <td><span class="pill bad">{{ kindText(d.kind) }}</span></td>
          <td>{{ fmt(d.primary) }}</td>
          <td>{{ fmt(d.mirror) }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="tag">无差异。</p>

    <p v-if="r.chain && !r.chain.ok" class="err">
      链断裂位置：{{ r.chain.breaks.map(b => `#${b.seq} ${b.reason}`).join('，') }}
    </p>

    <h3 class="serif">重建履历</h3>
    <table v-if="r.rebuilds && r.rebuilds.length" class="audit-table">
      <thead><tr><th>#</th><th>时刻</th><th>模式</th><th>事由</th><th>漂移前</th><th>追加行</th><th>履历校验和</th></tr></thead>
      <tbody>
        <tr v-for="rb in r.rebuilds" :key="rb.id">
          <td>{{ rb.id }}</td>
          <td class="tag">{{ rb.ts }}</td>
          <td>{{ rb.mode }}</td>
          <td>{{ rb.reason || '—' }}</td>
          <td>{{ rb.drift_before }}</td>
          <td>{{ rb.events_appended }}</td>
          <td class="hash"><code>{{ rb.chain_checksum.slice(0,16) }}…</code></td>
        </tr>
      </tbody>
    </table>
    <p v-else class="tag">尚无重建履历。</p>

    <p style="margin-top:24px">
      <button class="ghost" @click="load">刷新对账</button>
    </p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const r = ref({})
const reason = ref('')
const err = ref('')
const busy = ref(false)
const KIND = {
  status_mismatch: '状态不一致',
  claimer_mismatch: '认领人不一致',
  missing_mirror: '缺镜像',
  phantom_mirror: '幽灵镜像',
}
const kindText = k => KIND[k] || k
function fmt(o) {
  if (!o) return '—'
  const who = o.claimer || '无'
  return `${o.status} / ${who}` + (o.seq != null ? `（${o.action} #${o.seq}）` : '')
}
async function load() {
  err.value = ''
  try { r.value = await api('/audit/reconcile') } catch (e) { err.value = e.message }
}
async function rebuild() {
  if (!confirm('强制重建只会追加 rebuild 修正快照，不覆盖任何历史镜像行。继续？')) return
  busy.value = true; err.value = ''
  try { r.value = await api('/audit/rebuild', { method: 'POST', body: JSON.stringify({ reason: reason.value || null }) }) }
  catch (e) { err.value = e.message }
  finally { busy.value = false }
}
onMounted(load)
</script>
<style scoped>
.audit-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 700px) { .audit-grid { grid-template-columns: 1fr; } }
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; margin-right: 6px; }
.pill.ok { background: #dcebd8; color: #3a5a32; }
.pill.bad { background: #f3d4d0; color: #8a3a2e; }
.audit-table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 14px; overflow: hidden; }
.audit-table th, .audit-table td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); font-size: 14px; }
.audit-table th { color: var(--muted); font-weight: 400; }
.hash code { font-size: 11px; word-break: break-all; color: var(--muted); }
</style>
