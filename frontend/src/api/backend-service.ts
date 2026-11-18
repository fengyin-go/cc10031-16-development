import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨模块数据流：获取全平台领域快照 */
export async function loadNodeStats(module?: string): Promise<Record<string, unknown>> {
  const query = module ? `?module=${module}` : ''
  const response = await request(`/api/nodestats${query}`)
  if (!response.ok) throw new Error('节点统计读取失败')
  return await response.json()
}

/** 跨模块数据流：获取指定 topic 的通道消息 */
export async function loadChannelSnapshot(topic: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/channel/${topic}`)
  if (!response.ok) throw new Error('通道快照读取失败')
  return await response.json()
}

/** 跨模块数据流：查询脏数据隔离区 */
export async function loadDirtyVault(source?: string): Promise<Record<string, unknown>> {
  const query = source ? `?source=${source}` : ''
  const response = await request(`/api/dirty-vault${query}`)
  if (!response.ok) throw new Error('脏数据查询失败')
  return await response.json()
}

/** 跨模块数据流：查询采集记录 */
export async function loadCollectRecords(batchRef?: string): Promise<Record<string, unknown>> {
  const query = batchRef ? `?batch_ref=${batchRef}` : ''
  const response = await request(`/api/collect-records${query}`)
  if (!response.ok) throw new Error('采集记录查询失败')
  return await response.json()
}

/** 跨模块数据流：执行RPA全生命周期管道 */
export async function triggerPipeline(lifecycle: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/${lifecycle}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('管道执行失败')
  return await response.json()
}

/** 跨模块数据流：获取失败恢复状态 */
export async function loadRecoveryStats(): Promise<Record<string, unknown>> {
  const response = await request('/api/recovery')
  if (!response.ok) throw new Error('恢复状态查询失败')
  return await response.json()
}

/* ---- 模板版本比对 ---- */

export type TemplateDiff = {
  diff_id: string
  batch_ref: string
  base_version_id: string
  current_version_id: string
  added: { field: string; type: string }[]
  removed: { field: string; type: string }[]
  type_changed: { field: string; old_type: string; new_type: string }[]
  null_policy_changed: boolean
  base_null_policy: string
  current_null_policy: string
  receipt_kind: string
  created_at: string
}

export type ProcessingItem = {
  item_id: string
  batch_ref: string
  template_id: number
  batch_start_version_id: string
  delivered_version_id: string | null
  diff_id: string | null
  status: string
  output_file: Record<string, unknown> | null
  receipt: Record<string, unknown> | null
  created_at: string
}

/** 模板版本比对：触发 快照→差异→输出文件→处理项 链路 */
export async function runTemplateVersionFlow(payload: {
  batch_ref: string
  template_id: number
  strategy?: string
  incoming_template?: Record<string, unknown>
}): Promise<Record<string, unknown>> {
  const response = await request('/api/pipeline/template-version-flow', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return await response.json()
}

/** 模板版本比对：按来源批次查询字段级差异清单 */
export async function loadTemplateDiffs(batchRef?: string): Promise<{ count: number; diffs: TemplateDiff[] }> {
  const query = batchRef ? `?batch_ref=${encodeURIComponent(batchRef)}` : ''
  const response = await request(`/api/template-diffs${query}`)
  if (!response.ok) throw new Error('差异清单读取失败')
  return await response.json()
}

/** 模板版本比对：处理项列表（受控只读） */
export async function loadProcessingItems(batchRef?: string): Promise<{ count: number; items: ProcessingItem[] }> {
  const query = batchRef ? `?batch_ref=${encodeURIComponent(batchRef)}` : ''
  const response = await request(`/api/processing-items${query}`)
  if (!response.ok) throw new Error('处理项读取失败')
  return await response.json()
}

/** 模板版本比对：交付处理项，按字段差异类别选择回执 */
export async function deliverProcessingItem(itemId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/processing-items/${encodeURIComponent(itemId)}/deliver`, {
    method: 'POST',
  })
  return await response.json()
}
