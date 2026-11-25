import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/总览').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const d = data.分布 || {}
  const lights = data.终态灯 || data.对账?.终态灯 || { 回流: 0, 集包: 0, 双挂: 0 }
  const frozen = data.冻结宣称 ?? data.对账?.冻结宣称 ?? false
  return (
    <div className="panel">
      <h2>总览 · 总览对账</h2>
      <p className="muted">当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'}</p>
      {data.争用口径 && <p className="muted">争用口径：{data.争用口径}</p>}
      <div className={frozen ? 'err' : 'msg'} data-testid="overview-claim-state">
        {frozen
          ? '争用证据未收敛：成功灯已冻结，请先处理对账差异。'
          : '宣称口径正常：每一件扫描只允许一个可重放结局。'}
      </div>
      <div className="row" data-testid="terminal-lights">
        <span className={`light ${lights.回流 ? 'on divert' : ''}`}>回流灯：{lights.回流 ?? 0}</span>
        <span className={`light ${lights.集包 ? 'on bag' : ''}`}>集包灯：{lights.集包 ?? 0}</span>
        <span className={`light ${lights.双挂 ? 'dual' : ''}`}>双挂：{lights.双挂 ?? 0}</span>
      </div>
      {data.对账 && (
        <div className={data.对账.平衡 ? 'msg' : 'err'}>
          对账{data.对账.平衡 ? '平衡' : `发现 ${data.对账.差异?.length || 0} 项差异`}
          {typeof data.对账.未清件数 === 'number' ? ` · 未清 ${data.对账.未清件数}` : ''}
          {typeof data.对账.回流件数 === 'number' ? ` · 回流件 ${data.对账.回流件数}` : ''}
        </div>
      )}
      {data.对账?.差异?.length > 0 && (
        <ul className="err">
          {data.对账.差异.slice(0, 8).map((x, i) => <li key={i}>{x.说明}</li>)}
        </ul>
      )}
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div className="stat" key={k}><div className="n">{v}</div><div className="l">{k}</div></div>
        ))}
      </div>
      <table>
        <thead><tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th></tr></thead>
        <tbody>
          {(data.口门 || []).map((c) => (
            <tr key={c.id}><td>{c.code}</td><td>{c.title}</td><td>{c.status}</td><td>{c.在口件数}</td></tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
