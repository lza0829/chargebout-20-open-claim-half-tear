import { useEffect, useState } from 'react'
import { api } from '../api'
import { notifyLedgerChanged, onLedgerChanged } from '../sync'

const surface_panel = true
export default function Settle() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([api.会话('充电中'), api.会话('已超时'), api.会话('结算中')]).then(
      ([a, b, c]) => setList([...a, ...b, ...c]),
    )
  useEffect(() => {
    load()
    return onLedgerChanged(load)
  }, [])
  const go = async (id) => {
    setMsg('')
    try {
      const r = await api.结算(id)
      setMsg(`已关账：电量 ${r.电量}，金额 ${r.金额}；桩/枪同笔释放`)
      // 结算同笔释放桩枪，广播让占用列与总览同刷
      notifyLedgerChanged({ 动作: '结算', 会话编号: id })
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>结算关账</h2>
      <p className="lead">按锁定电价与累计电量关账，同笔释放桩位与枪线，失败整笔回滚。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>预估金额</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>
                  <button type="button" className="primary" onClick={() => go(s.id)}>
                    结算关账
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className={`msg ${msg.startsWith('已关账') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
