import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [rows, setRows] = useState([])
  const [receipts, setReceipts] = useState([])
  const [replay, setReplay] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = async () => {
    setErr('')
    try {
      const [auditRows, replayResult, receiptRows] = await Promise.all([
        api('/审计?limit=200'),
        api('/审计/重放'),
        api('/回执'),
      ])
      setRows(auditRows)
      setReplay(replayResult)
      setReceipts(receiptRows)
    } catch (e) {
      setErr(e.message)
    }
  }

  useEffect(() => { load() }, [])

  const runReplay = async () => {
    try {
      const result = await api('/审计/重放')
      setReplay(result)
      setMsg(result.一致 ? '审计重放与当前落账一致' : '审计重放发现差异')
    } catch (e) {
      setErr(e.message)
    }
  }

  return (
    <div className="panel">
      <h2>审计与争用重放</h2>
      <p className="muted">回流/入袋同一事务写审计、回执、状态与对账；争用结局可按审计事件重放。</p>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      {replay && (
        <div className={replay.一致 ? 'msg' : 'err'}>
          重放{replay.已重放事件数}个事件 / {replay.重放扫描数}件扫描：
          {replay.一致 ? '一致闭合' : `存在 ${replay.差异?.length || 0} 项差异`}
        </div>
      )}
      {replay?.差异?.length > 0 && (
        <table>
          <thead><tr><th>代码</th><th>说明</th><th>明细</th></tr></thead>
          <tbody>
            {replay.差异.map((d, i) => (
              <tr key={i}>
                <td>{d.代码 || '-'}</td>
                <td>{d.说明}</td>
                <td><code>{JSON.stringify(d)}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="row">
        <button type="button" onClick={runReplay}>审计重放/总览对账</button>
        <button type="button" className="secondary" onClick={load}>刷新审计与回执</button>
      </div>
      <h3>回执与冲正链</h3>
      <p className="muted">入袋成功必有有效入袋回执；被回流带出时，原回执在同一事务中标记为已冲正并链接回流回执。</p>
      <table>
        <thead>
          <tr>
            <th>编号</th><th>回执号</th><th>类型</th><th>扫描</th>
            <th>袋</th><th>批次</th><th>状态</th><th>冲正指向</th><th>摘要</th>
          </tr>
        </thead>
        <tbody>
          {receipts.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td><code>{r.receipt_no}</code></td>
              <td>{r.kind}</td>
              <td>{r.scan_id}</td>
              <td>{r.bag_id || '-'}</td>
              <td>{r.batch_id || '-'}</td>
              <td>{r.status}</td>
              <td>{r.reversed_by_receipt_id || '-'}</td>
              <td>{r.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>审计事件</h3>
      <table>
        <thead><tr><th>编号</th><th>类型</th><th>引用</th><th>动作</th><th>时间</th><th>备注</th></tr></thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td>{a.id}</td>
              <td>{a.kind}</td>
              <td>{a.ref_id || '-'}</td>
              <td>{a.action}</td>
              <td>{a.created_at}</td>
              <td>{a.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
