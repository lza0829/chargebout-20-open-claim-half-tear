import { useEffect, useState } from 'react'
import { api } from '../api'
import { onLedgerChanged } from '../sync'

const surface_panel = true
export default function Home() {
  const [o, setO] = useState(null)
  const [cons, setCons] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    Promise.all([api.总览(), api.一致性()])
      .then(([overview, consistency]) => {
        setO(overview)
        setCons(consistency)
        setErr('')
      })
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
    // 任一页落账后，总览桶同刷，保证不展示陈旧占用/充电中
    return onLedgerChanged(load)
  }, [])
  if (!o) return <div className={surface_panel ? "page" : "page"}><p className="msg">{err || '加载中'}</p></div>
  const _ok = o && o.surface_ok
  const items = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', _ok ? (o.surface_桩位占用 ?? o.桩位占用) : o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['充电中', _ok ? (o.surface_充电中 ?? o.充电中) : o.充电中],
    ['结算中', _ok ? (o.surface_结算中 ?? o.结算中) : o.结算中],
    ['已关账', _ok ? (o.surface_已关账 ?? o.已关账) : o.已关账],
    ['已超时', _ok ? (o.surface_已超时 ?? o.已超时) : o.已超时],
    ['已中断', _ok ? (o.surface_已中断 ?? o.已中断) : o.已中断],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
  ]
  const driftTotal = (o.桩位漂移数 || 0) + (o.枪线漂移数 || 0)
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} · 口径：{o.落账口径}
      </p>
      {driftTotal > 0 ? (
        <div className="drift-banner">
          检测到落账漂移：桩位 {o.桩位漂移数} 处、枪线 {o.枪线漂移数} 处
          {cons && !cons.一致
            ? `；重放不一致 桩[${(cons.桩位不一致编号 || []).join(',')}] 枪[${(cons.枪线不一致编号 || []).join(',')}]`
            : ''}
          ，已显式标出，未伪装成已对齐
        </div>
      ) : (
        <div className="aligned-banner">占用账与事件流水重放一致，无漂移</div>
      )}
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
