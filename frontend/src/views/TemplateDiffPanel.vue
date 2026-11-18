<template>
  <section class="diff-panel" data-testid="template-diff-panel">
    <header class="diff-head">
      <h3>模板版本比对</h3>
      <p class="page-desc">
        模板更新后按来源批次核对字段级差异（新增 / 删除 / 类型变化），
        输出文件与两版模板标识一起生成，交付回执持久化两版标识。
      </p>
    </header>

    <form class="diff-form" @submit.prevent="runFlow">
      <label>
        <span>来源批次</span>
        <input v-model="batchRef" data-testid="diff-batch-ref" placeholder="如 BATC-20261118-1" required />
      </label>
      <label>
        <span>模板ID</span>
        <input v-model.number="templateId" data-testid="diff-template-id" type="number" min="1" required />
      </label>
      <label>
        <span>生成策略</span>
        <select v-model="strategy" data-testid="diff-strategy">
          <option value="batch_start">按批次启动时模板生成</option>
          <option value="current">按当前模板重新生成</option>
        </select>
      </label>
      <label>
        <span>新到达模板字段映射（可选）</span>
        <input v-model="incomingMapping" data-testid="diff-incoming-mapping" placeholder="名称:text,金额:numeric" />
      </label>
      <label>
        <span>新到达模板空值口径（可选）</span>
        <input v-model="incomingNullPolicy" data-testid="diff-incoming-null" placeholder="NA" />
      </label>
      <button class="btn primary" data-testid="run-compare" type="submit" :disabled="running">
        {{ running ? '比对中…' : '发起版本比对' }}
      </button>
      <button class="btn ghost" type="button" data-testid="refresh-diff" @click="reload">刷新清单</button>
    </form>

    <p v-if="flowMessage" class="diff-message" data-testid="flow-message">{{ flowMessage }}</p>

    <div v-if="diffs.length" class="diff-lists">
      <article v-for="diff in diffs" :key="diff.diff_id" class="diff-card" :data-testid="`diff-${diff.diff_id}`">
        <header class="diff-card-head">
          <strong>{{ diff.diff_id }}</strong>
          <span>批次 {{ diff.batch_ref }}</span>
          <span>基准版本 {{ diff.base_version_id }} → 当前版本 {{ diff.current_version_id }}</span>
          <span class="receipt-kind" :data-kind="diff.receipt_kind">{{ receiptKindLabel(diff.receipt_kind) }}</span>
        </header>
        <div class="diff-columns">
          <div class="diff-col" data-testid="diff-added">
            <h4>新增字段（{{ diff.added.length }}）</h4>
            <ul>
              <li v-for="f in diff.added" :key="`a-${f.field}`">{{ f.field }} <em>{{ f.type }}</em></li>
              <li v-if="!diff.added.length" class="empty-state">无</li>
            </ul>
          </div>
          <div class="diff-col" data-testid="diff-removed">
            <h4>删除字段（{{ diff.removed.length }}）</h4>
            <ul>
              <li v-for="f in diff.removed" :key="`r-${f.field}`">{{ f.field }} <em>{{ f.type }}</em></li>
              <li v-if="!diff.removed.length" class="empty-state">无</li>
            </ul>
          </div>
          <div class="diff-col" data-testid="diff-type-changed">
            <h4>类型变化（{{ diff.type_changed.length }}）</h4>
            <ul>
              <li v-for="f in diff.type_changed" :key="`t-${f.field}`">
                {{ f.field }} <em>{{ f.old_type }} → {{ f.new_type }}</em>
              </li>
              <li v-if="!diff.type_changed.length" class="empty-state">无</li>
            </ul>
          </div>
        </div>
        <p v-if="diff.null_policy_changed" class="null-policy-note">
          空值口径变化：{{ diff.base_null_policy }} → {{ diff.current_null_policy }}
        </p>
      </article>
    </div>
    <p v-else class="empty-state">暂无差异清单，可先发起版本比对</p>

    <h3 class="items-title">处理项（受控只读）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>处理项</th><th>来源批次</th><th>状态</th>
          <th>批次启动版本</th><th>交付版本</th><th>输出文件</th><th>交付回执</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.item_id" :data-testid="`item-${item.item_id}`">
          <td>{{ item.item_id }}</td>
          <td>{{ item.batch_ref }}</td>
          <td>{{ item.status }}</td>
          <td>{{ item.batch_start_version_id }}</td>
          <td>{{ item.delivered_version_id ?? '—' }}</td>
          <td>{{ item.output_file ? String(item.output_file.file_id) : '—' }}</td>
          <td class="row-actions">
            <template v-if="item.receipt">
              <span class="receipt-kind" :data-kind="String(item.receipt.receipt_kind)">
                {{ receiptKindLabel(String(item.receipt.receipt_kind)) }}
              </span>
              <span class="receipt-action">{{ item.receipt.downstream_action }}</span>
            </template>
            <button
              v-else-if="item.output_file"
              class="link"
              type="button"
              :data-testid="`deliver-${item.item_id}`"
              @click="deliver(item.item_id)"
            >
              交付
            </button>
            <span v-else>—</span>
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="7" class="empty-state">暂无处理项</td>
        </tr>
      </tbody>
    </table>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  deliverProcessingItem,
  loadProcessingItems,
  loadTemplateDiffs,
  runTemplateVersionFlow,
  type ProcessingItem,
  type TemplateDiff,
} from '@/api/backend-service'

const props = defineProps<{ defaultTemplateId?: number }>()

const batchRef = ref('')
const templateId = ref(props.defaultTemplateId ?? 2)
const strategy = ref('batch_start')
const incomingMapping = ref('')
const incomingNullPolicy = ref('')
const diffs = ref<TemplateDiff[]>([])
const items = ref<ProcessingItem[]>([])
const flowMessage = ref('')
const errorMessage = ref('')
const running = ref(false)

function receiptKindLabel(kind: string): string {
  return (
    {
      standard: '标准交付',
      additive: '增量交付（下游兼容）',
      breaking: '变更交付（需下游调整）',
    }[kind] ?? kind
  )
}

async function runFlow() {
  running.value = true
  flowMessage.value = ''
  errorMessage.value = ''
  try {
    const payload: Record<string, unknown> = {
      batch_ref: batchRef.value.trim(),
      template_id: templateId.value,
      strategy: strategy.value,
    }
    if (incomingMapping.value.trim()) {
      payload.incoming_template = {
        id: templateId.value,
        模板名称: '回调交付模板',
        字段映射: incomingMapping.value.trim(),
        空值表示: incomingNullPolicy.value.trim() || 'NA',
      }
    }
    const result = await runTemplateVersionFlow(
      payload as Parameters<typeof runTemplateVersionFlow>[0],
    )
    if (result.ok === false) throw new Error(String(result.error ?? '比对失败'))
    flowMessage.value = result.reused
      ? String(result.message ?? '已复用既有输出文件')
      : `已生成输出文件 ${String((result.item as ProcessingItem).output_file && ((result.item as ProcessingItem).output_file as Record<string, unknown>).file_id)}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '比对失败'
  } finally {
    running.value = false
  }
}

async function deliver(itemId: string) {
  errorMessage.value = ''
  try {
    const result = await deliverProcessingItem(itemId)
    if (result.ok === false) throw new Error(String(result.error ?? '交付失败'))
    const receipt = result.receipt as Record<string, unknown>
    flowMessage.value = `交付完成：${receiptKindLabel(String(receipt.receipt_kind))}，${String(receipt.downstream_action)}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const query = batchRef.value.trim() || undefined
    const [diffResult, itemResult] = await Promise.all([
      loadTemplateDiffs(query),
      loadProcessingItems(query),
    ])
    diffs.value = diffResult.diffs
    items.value = itemResult.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.diff-panel { margin-top: 20px; border-top: 2px solid var(--border); padding-top: 12px; }
.diff-head h3 { margin: 0 0 4px; }
.diff-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.diff-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.diff-form input, .diff-form select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.diff-message { color: var(--brand); font-size: 13px; }
.diff-lists { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
.diff-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.diff-card-head { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.diff-card-head strong { color: #1f2937; }
.diff-columns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.diff-col h4 { margin: 0 0 6px; font-size: 13px; }
.diff-col ul { margin: 0; padding-left: 18px; font-size: 13px; }
.diff-col em { color: var(--muted); font-style: normal; font-size: 12px; }
.null-policy-note { margin: 8px 0 0; font-size: 12px; color: #b45309; }
.receipt-kind { font-size: 12px; border-radius: 4px; padding: 1px 6px; background: #e2e8f0; }
.receipt-kind[data-kind='additive'] { background: #dbeafe; color: #1d4ed8; }
.receipt-kind[data-kind='breaking'] { background: #fee2e2; color: #b91c1c; }
.receipt-action { display: block; font-size: 12px; color: var(--muted); }
.items-title { margin: 16px 0 8px; }
</style>
