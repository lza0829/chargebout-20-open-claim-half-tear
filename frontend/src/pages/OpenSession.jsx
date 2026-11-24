import { useEffect, useState } from 'react'
import { api } from '../api'
import { notifyLedgerChanged, onLedgerChanged } from '../sync'

const surface_panel = true
export default function OpenSession() {
  const [piles, setPiles] = useState([])
  const [guns, setGuns] = useState([])
  const [pileId, setPileId] = useState('')
  const [gunId, setGunId] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const loadPiles = () =>
    api.桩位().then((list) => {
      setPiles(list)
      if (!pileId) {
        const idle = list.find((p) => p.status === '空闲')
        if (idle) setPileId(String(idle.id))
      }
    })
  useEffect(() => {
    loadPiles()
    // 其它页面落账后，本页桩/枪占用态同步刷新
    return onLedgerChanged(() => {
      loadPiles()
    })
  }, [])
  useEffect(() => {
    if (!pileId) return
    api.枪线(Number(pileId)).then((list) => {
      setGuns(list)
      const ok = list.find((g) => g.status === '可用')
      setGunId(ok ? String(ok.id) : '')
    })
  }, [pileId, piles])

  const submit = async () => {
    setMsg('')
    setBusy(true)
    try {
      const body = { 桩位编号: Number(pileId) }
      if (gunId) body.枪线编号 = Number(gunId)
      const r = await api.开会话(body)
      // 落账成功：广播让总览/桩位/枪线/会话册等所有读路径同刷
      setMsg(`已开 ${r.会话号}，锁定电价 ${r.锁定电价}；桩/枪同笔占用`)
      notifyLedgerChanged({ 动作: '开会话', 会话编号: r.会话编号 })
      await Promise.all([loadPiles()])
    } catch (e) {
      setMsg(e.message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>开会话</h2>
      <p className="lead">选空闲桩与可用枪，同笔多写原子落账：会话、桩占、枪占、开充事件同成败。</p>
      <div className="panel">
        <div className="row">
          <label>桩位</label>
          <select value={pileId} onChange={(e) => setPileId(e.target.value)}>
            {piles.map((p) => (
              <option key={p.id} value={p.id} disabled={p.status !== '空闲'}>
                {p.site_name} · {p.name} · {p.status}
              </option>
            ))}
          </select>
          <label>枪线</label>
          <select value={gunId} onChange={(e) => setGunId(e.target.value)}>
            <option value="">自动选可用</option>
            {guns.map((g) => (
              <option key={g.id} value={g.id} disabled={g.status !== '可用'}>
                {g.name} · {g.status}
              </option>
            ))}
          </select>
          <button type="button" className="primary" onClick={submit} disabled={busy || !pileId}>
            {busy ? '落账中…' : '发起开充'}
          </button>
        </div>
        <p className={`msg ${msg.startsWith('已开') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
