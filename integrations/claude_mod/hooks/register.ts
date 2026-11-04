import type { EngineInterface, Register } from 'claude-code'

// Claude 桌宠 × Claude Code（mod）
//
// 让桌面版 Claude 的 Code 标签页（自带 Claude Code ≥ 2.1.287 的 mod 引擎）也能驱动桌宠；新版终端 Claude Code 同样适用。
// 结构参考 CrossPet 的 mod（MIT），只保留桌宠需要的两件事：
//   1. 正在做什么 → <状态目录>/claude-state.json   {pose, event, tool, ts, src: "mod"}
//   2. 官方额度     → <状态目录>/claude-quota.json   {text, low, bars: [{label, used, left, reset}], source: "mod"}
// 隐私：只写事件名、工具名、时间和额度百分比；不读、不写任何提示词 / 回复 / 文件内容；不发任何网络请求。
// 状态目录：CROSSPET_STATE_DIR > Windows 的 %LOCALAPPDATA%\CrossPet\state > /tmp/crosspet（和桌宠、钩子一致）。

let dir: string | undefined
async function stateDir($: EngineInterface): Promise<string> {
  if (dir) return dir
  const custom = await $.env.get('CROSSPET_STATE_DIR')
  const local = await $.env.get('LOCALAPPDATA')
  dir = custom || (local ? `${local}\\CrossPet\\state` : '/tmp/crosspet')
  return dir
}
async function write($: EngineInterface, file: string, data: unknown): Promise<void> {
  await $.fs.write(`${await stateDir($)}/${file}`, JSON.stringify(data))
}

// 同一个会话如果也装了终端钩子（claude_hook.py），钩子看到这里记过这个会话就不再重复写状态
async function markSession($: EngineInterface): Promise<void> {
  try {
    const sid = await $.session.id()
    const file = `${await stateDir($)}/mod-sessions.json`
    let known: Record<string, number> = {}
    try { known = JSON.parse(await $.fs.read(file)) } catch {}
    const now = Date.now() / 1000
    known[sid] = now
    for (const k of Object.keys(known)) if (now - known[k] > 3600) delete known[k]
    await $.fs.write(file, JSON.stringify(known))
  } catch {}
}

async function setState($: EngineInterface, pose: string, event: string, tool = ''): Promise<void> {
  try {
    await write($, 'claude-state.json', { pose, event, tool, ts: Date.now() / 1000, src: 'mod' })
    await markSession($)
  } catch {}   // 写不了也绝不影响 Claude 干活
}

function poseForTool(tool: string): string {
  const t = tool.toLowerCase()
  if (t === 'askuserquestion') return 'asking'
  if (/grep|glob|web|fetch|browse|search|http/.test(t)) return 'searching'
  if (/read|list|view|cat/.test(t)) return 'reading'
  if (/write|edit|patch|replace|create|notebook/.test(t)) return 'writing'
  if (/agent|task|subagent|delegate/.test(t)) return 'delegating'
  return 'running'
}

type Limit = { kind: string; percentUsed: number; resetsAt?: string }
function quotaOf(limits: Limit[]) {
  const shown = (limits || []).filter(l => l.kind === 'five_hour' || l.kind === 'seven_day')
  if (shown.length === 0) return null
  const label = (k: string) => (k === 'five_hour' ? '5小时' : '本周')
  const pad = (n: number) => String(n).padStart(2, '0')
  const bars = shown.map(l => ({
    label: label(l.kind),
    used: Math.round(l.percentUsed * 10) / 10,
    left: Math.max(0, 100 - l.percentUsed) / 100,
    ...(l.resetsAt ? { reset: Math.round(new Date(l.resetsAt).getTime() / 1000) } : {}),
  }))
  const text = shown.map(l => {
    let s = `${label(l.kind)}已用 ${Math.round(l.percentUsed)}%`
    if (l.resetsAt) {
      const d = new Date(l.resetsAt)
      const soon = d.getTime() - Date.now() < 86_400_000
      s += `（${soon ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()}`}重置）`
    }
    return s
  }).join(' ｜ ')
  return { text, low: shown.some(l => l.percentUsed >= 90), bars, source: 'mod', ts: Date.now() / 1000 }
}
async function writeQuota($: EngineInterface, limits: Limit[]): Promise<void> {
  try {
    const q = quotaOf(limits)
    if (q) await write($, 'claude-quota.json', q)
  } catch {}
}

// 桌宠没开着就把它叫出来（交给 claude_hook.py --ensure：已在运行、用户手动退出过、设置里关掉了都不会打开）。
// 安装时把 python 和脚本路径写在 <状态目录>/pet-launch.json；一分钟内最多检查一次，不等它跑完
let lastEnsure = 0
async function ensurePet($: EngineInterface): Promise<void> {
  if (Date.now() - lastEnsure < 60_000) return
  lastEnsure = Date.now()
  try {
    const cfg = JSON.parse(await $.fs.read(`${await stateDir($)}/pet-launch.json`))
    if (cfg.python && cfg.hook) await $.process.run([cfg.python, '-I', '-S', cfg.hook, '--ensure'], { timeoutMs: 10_000 })
  } catch {}
}

const BIG_JOB_TOOLS = 8

export const register: Register = on => {
  let tools = 0

  on('session.start', async ($, e, next) => {
    void ensurePet($).catch(() => {})
    await setState($, 'idle', 'SessionStart')
    try { await writeQuota($, (await $.session.usage()).rateLimits) } catch {}
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    void ensurePet($).catch(() => {})
    tools = 0
    await setState($, 'listening', 'UserPromptSubmit')
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    tools += 1
    await setState($, poseForTool(e.tool), 'PreToolUse', e.tool)
    const r = await next(e)
    const failed = 'deny' in r && r.deny !== undefined ? true : r.isError === true
    await setState($, failed ? 'oops' : 'thinking', 'PostToolUse', e.tool)
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const pose = e.reason === 'answer' ? (tools >= BIG_JOB_TOOLS ? 'proud' : 'happy') : e.reason === 'aborted' ? 'idle' : 'oops'
    tools = 0
    await setState($, pose, 'Stop')
    return r
  })

  // 弹到你面前要授权 / 要你填表的那一刻：她看着你等你回答
  on('classic.Notification', async ($, e, next) => {
    const kind = String(e.notification_type ?? '').toLowerCase()
    if (kind === 'permission_prompt' || kind === 'elicitation_dialog') await setState($, 'asking', 'Notification')
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    const shown = (e.trigger === 'manual' || e.trigger === 'auto') && !e.agentId
    if (shown) await setState($, 'compact', 'PreCompact')
    const r = await next(e)
    if (shown) await setState($, e.trigger === 'manual' ? 'happy' : 'thinking', e.trigger === 'manual' ? 'Stop' : 'PostCompact')
    return r
  })

  // 官方 5 小时 / 每周额度：Claude Code 每次拿到最新的限额信息都会触发
  on('session.measure', async ($, e, next) => {
    await writeQuota($, (e.rateLimits || []).map(l => ({ kind: l.kind, percentUsed: l.percentUsed, resetsAt: l.resetsAt })))
    return next(e)
  })
}
