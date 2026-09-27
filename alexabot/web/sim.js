// The simulated Alexa+ page. No build step: an ES module the sim serves, plus
// three modules it serves read-only from frontend/src/lib (voice.js and the two
// it imports), so the SPA and this page share one source of voice behaviour.
//
// Rules carried over from voice.js and held here too:
//   - nothing listens without a click: the microphone never opens by itself,
//     even after Ada asks a question (the button says "Tap to answer");
//   - every card field comes from a tool's structuredContent via the server;
//     the page draws nothing the tool did not supply and never shows a 0 for
//     an unknown;
//   - the memory pill's words are built on the server (memory.chip_label), so
//     the page never says it remembers something the server did not report;
//   - no server text is ever parsed as HTML. The one markup the page imports is
//     the board SVG, which is refused if it holds a script, a foreignObject or
//     an event handler.

import {
  createDictation,
  createSpeaker,
  recognitionCtorOf,
  synthesisOf,
  utteranceCtorOf,
} from '/lib/voice.js'

const CONVERSATION_KEY = 'ada-alexa-sim.conversation'
const MUTE_KEY = 'ada-alexa-sim.muted'
const MIC_HINT = 'Tap the microphone, speak, and pause; Ada answers when you stop.'
const ASKED_HINT = 'Ada asked you something. Tap the microphone to answer, or type.'
const KINDS = ['user', 'agent_status', 'card', 'focus', 'ada', 'progress', 'notice', 'error', 'trace', 'memory']
const MEMORY_FOOT = {
  agentcore:
    'With memory on, what you say, and the question you were answering, is sent to Amazon Bedrock AgentCore Memory, which extracts your design preferences for next time. Ada’s own replies are not sent.',
  scripted:
    'Scripted memory: a few rules on this machine pick design preferences out of what you say, at once; nothing leaves this machine.',
  off: 'Memory is off: Ada keeps your boards between conversations, but not your preferences.',
}

const $ = (id) => document.getElementById(id)

// -- storage, which may be missing or throw ----------------------------------

function readStore(kind, key) {
  try {
    return window[kind].getItem(key)
  } catch {
    return null
  }
}

function writeStore(kind, key, value) {
  try {
    if (value === null || value === undefined) window[kind].removeItem(key)
    else window[kind].setItem(key, value)
  } catch {
    // Private mode or blocked storage: the page works without it.
  }
}

// -- a tiny element builder: text nodes only, never HTML strings --------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag)
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue
    if (key === 'class') el.className = value
    else if (key === 'dataset') Object.assign(el.dataset, value)
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value)
    else el.setAttribute(key, value === true ? '' : String(value))
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue
    el.append(child instanceof Node ? child : document.createTextNode(String(child)))
  }
  return el
}

// -- state --------------------------------------------------------------------

const state = {
  config: null,
  cid: null,
  source: null,
  lastSeq: 0,
  speakFrom: Infinity,
  cards: {},
  focus: null,
  busy: false,
  checking: false,
  expanded: {},
  svg: {},
  muted: readStore('localStorage', MUTE_KEY) === '1',
  listening: false,
  heard: '',
  memory: null,
}

// -- voice out: Polly audio when the server has it, else this browser ---------

const audioEl = new Audio()
audioEl.preload = 'auto'
let queue = []
let playing = null
let starting = false
let polly = null

const speaker = createSpeaker(synthesisOf(window), utteranceCtorOf(window), {
  onChange(speaking) {
    if (starting) return
    if (!speaking && playing && playing.via === 'browser') {
      playing = null
      next()
    }
  },
})

function setBar() {
  const bar = $('statusbar')
  let mode = 'idle'
  if (state.listening) mode = 'listening'
  else if (playing) mode = 'speaking'
  else if (state.busy) mode = 'thinking'
  bar.dataset.state = mode
}

function browserSay(item) {
  if (!speaker.supported) {
    playing = null
    next()
    return
  }
  const token = Symbol('say')
  playing = { ...item, via: 'browser', token }
  starting = true
  speaker.speak(item.text)
  starting = false
  setBar()
  // Some engines never fire end (no voices installed, a muted tab): move on
  // after a generous reading time so the queue cannot stall.
  const words = String(item.text).split(/\s+/).length
  setTimeout(() => {
    if (playing && playing.token === token) {
      playing = null
      next()
    }
  }, 3000 + 450 * words)
}

function next() {
  const item = queue.shift()
  if (!item) {
    playing = null
    setBar()
    return
  }
  if (item.audio && polly !== false) {
    const token = Symbol('play')
    playing = { ...item, via: 'audio', token }
    audioEl.src = item.audio
    audioEl.play().catch(() => {
      if (playing && playing.token === token) browserSay(item)
    })
  } else {
    browserSay(item)
  }
  setBar()
}

audioEl.addEventListener('ended', () => {
  if (playing && playing.via === 'audio') {
    playing = null
    next()
  }
})
audioEl.addEventListener('error', () => {
  if (playing && playing.via === 'audio') browserSay(playing)
})

function say(event) {
  if (state.muted || !event.text) return
  queue.push({ text: event.text, audio: event.audio || null })
  if (!playing) next()
}

function stopSpeech() {
  queue = []
  if (playing && playing.via === 'audio') {
    audioEl.pause()
    audioEl.removeAttribute('src')
  }
  playing = null
  speaker.stop()
  setBar()
}

// -- voice in: the browser's recognition, one utterance per tap ---------------

const dictation = createDictation(recognitionCtorOf(window), {
  lang: navigator.language || 'en-US',
  onState(listening) {
    state.listening = listening
    const mic = $('mic')
    mic.setAttribute('aria-pressed', listening ? 'true' : 'false')
    mic.setAttribute('aria-label', listening ? 'Listening, tap to stop' : 'Tap to talk')
    setBar()
    if (!listening) {
      const heard = state.heard.trim()
      state.heard = ''
      if (heard) sendTurn(heard, 'voice')
    }
  },
  onInterim(text) {
    $('interim').textContent = text ? `${text}…` : ''
  },
  onFinal(text) {
    state.heard = `${state.heard} ${text}`.trim()
    $('interim').textContent = state.heard
  },
  onError(message) {
    note(message)
  },
})

function note(text) {
  $('mic-hint').textContent = text
}

// -- the API ------------------------------------------------------------------

async function api(path, options = {}) {
  const init = { ...options, headers: { 'Content-Type': 'application/json' } }
  const response = await fetch(path, init)
  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  return { status: response.status, body }
}

async function loadConfig() {
  const { status, body } = await api('/api/config')
  if (status !== 200 || !body) return
  state.config = body
  const pill = $('mode-pill')
  pill.dataset.mode = body.mode
  const tts = body.tts || {}
  const agent = body.agent || {}
  if (body.mode === 'scripted') {
    pill.textContent = 'Scripted: no model'
  } else {
    const model = /nova-2-lite/.test(agent.model_id || '') ? 'Nova 2 Lite' : agent.model_id
    pill.textContent = `Live · ${model} on Bedrock${tts.kind === 'polly' ? ' · Polly' : ''}`
  }
  const tools = (body.mcp && body.mcp.tools) || []
  const who =
    body.mode === 'scripted'
      ? `a rule-based scripted model standing in for the language model (no AWS, no key)`
      : `${agent.model_id} on Amazon Bedrock (${agent.region})`
  const voiceWords = tts.kind === 'polly' ? `Amazon Polly, ${tts.voice} (${tts.engine})` : "this browser's own voice"
  $('foot-mode').textContent =
    `Agent: ${agent.framework || 'Strands Agents'} with ${who}. ` +
    `Tools: ${tools.length} MCP tools from Ada's server (MCP ${body.mcp ? body.mcp.spec : ''}). ` +
    `Ada's own workers: ${body.workers === 'scripted' ? 'canned practice answers' : 'the configured model'}. ` +
    `Voice: ${voiceWords}.`
  $('voice-name').textContent = `Voice: ${voiceWords}.`
  const memoryKind = (body.memory && body.memory.kind) || 'off'
  $('foot-memory').textContent = MEMORY_FOOT[memoryKind] || MEMORY_FOOT.off
  renderMemory(state.memory)
  if (body.mode === 'scripted') {
    $('intro-fine').textContent =
      'Scripted mode: a rule-based agent and canned answers, so every board is the practice regulator. Everything runs on this machine.'
  }
}

// -- memory: the pill and its popover -----------------------------------------

// Before a conversation reports its memory, the pill says what the sim is
// configured with; a conversation's `memory` events replace that.
function configMemoryView() {
  const m = (state.config && state.config.memory) || { kind: 'off' }
  if (m.kind === 'off') return { kind: 'off', state: 'off', label: 'Memory off', title: m.reason, preferences: [] }
  const label = m.kind === 'scripted' ? 'Scripted memory' : 'Memory on'
  return { kind: m.kind, state: 'idle', label, title: m.reason || null, preferences: [] }
}

function renderMemory(view) {
  const v = view || configMemoryView()
  const pill = $('memory-pill')
  pill.hidden = false
  pill.dataset.state = v.state
  pill.dataset.kind = v.kind
  $('memory-pill-text').textContent = v.label
  if (v.title) pill.title = v.title
  else pill.removeAttribute('title')
  const body = $('memory-pop-body')
  const prefs = v.preferences || []
  const parts = []
  if (prefs.length) {
    parts.push(
      h(
        'ul',
        { dataset: { testid: 'memory-list' } },
        prefs.map((p) => h('li', {}, h('span', {}, p.short || p.text), p.created_at ? h('span', { class: 'when' }, `saved ${p.created_at}`) : null)),
      ),
    )
  } else {
    const empty =
      v.state === 'off' || v.state === 'unavailable'
        ? v.title || v.detail || 'Memory is off.'
        : v.state === 'checking'
          ? 'Checking what Ada remembers…'
          : v.state === 'idle'
            ? 'Start a conversation to see what Ada remembers.'
            : 'Nothing saved yet. Tell Ada what you like, such as USB-C power or 3.3 V logic.'
    parts.push(h('p', { class: 'memory-empty' }, empty))
  }
  if (v.source) parts.push(h('p', { class: 'memory-source' }, `Source: ${v.source}`))
  body.replaceChildren(...parts)
}

function setMemoryOpen(open) {
  $('memory-pop').hidden = !open
  $('memory-pill').setAttribute('aria-expanded', open ? 'true' : 'false')
}

// -- conversations ---------------------------------------------------------

async function startConversation(reason) {
  const { status, body } = await api('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ locale: navigator.language || 'en-US' }),
  })
  if (status !== 201 || !body) {
    note('Ada could not start a conversation. Is the sim still running?')
    return false
  }
  resetView()
  state.cid = body.conversation_id
  writeStore('sessionStorage', CONVERSATION_KEY, state.cid)
  state.speakFrom = 0
  showStage()
  if (reason) addLine(h('li', { class: 'line line-notice' }, reason))
  connect(0)
  return true
}

async function resume(cid) {
  const { status, body } = await api(`/api/conversations/${encodeURIComponent(cid)}`)
  if (status !== 200 || !body) {
    writeStore('sessionStorage', CONVERSATION_KEY, null)
    return false
  }
  state.cid = cid
  // Replay the transcript, but only speak what arrives from now on.
  state.speakFrom = body.next_seq
  showStage()
  connect(0)
  return true
}

function resetView() {
  if (state.source) state.source.close()
  state.source = null
  stopSpeech()
  state.lastSeq = 0
  state.cards = {}
  state.focus = null
  state.expanded = {}
  state.busy = false
  state.memory = null
  renderMemory(null)
  setMemoryOpen(false)
  $('transcript').replaceChildren()
  $('trace-list').replaceChildren()
  renderScreen()
}

function showStage() {
  $('intro').hidden = true
  $('stage').hidden = false
  $('composer').hidden = false
  $('new-conversation').hidden = false
}

function connect(after) {
  const url = `/api/conversations/${encodeURIComponent(state.cid)}/events?after=${after}`
  const source = new EventSource(url)
  state.source = source
  for (const kind of KINDS) {
    source.addEventListener(kind, (message) => {
      try {
        handle(JSON.parse(message.data))
      } catch {
        // A malformed frame is skipped, never rendered.
      }
    })
  }
  source.addEventListener('error', async () => {
    if (source.readyState !== EventSource.CLOSED || state.source !== source) return
    const { status } = await api(`/api/conversations/${encodeURIComponent(state.cid)}`)
    if (status === 404) {
      await startConversation('The sim restarted, so this is a new conversation. Your boards are still saved.')
    }
  })
}

async function sendTurn(text, source, hint) {
  const words = String(text || '').trim()
  if (!words || !state.cid) return
  if (state.busy) {
    note("One moment, I'm still answering.")
    return
  }
  stopSpeech()
  const payload = { text: words.slice(0, 500), source }
  if (hint) payload.hint = hint
  state.busy = true
  syncControls()
  const { status, body } = await api(`/api/conversations/${encodeURIComponent(state.cid)}/turns`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (status === 202) return
  state.busy = false
  syncControls()
  if (status === 409) note((body && body.speech) || "One moment, I'm still answering.")
  else if (status === 404) await startConversation('That conversation had ended, so this is a new one. Your boards are still saved.')
  else note((body && body.detail) || 'That did not go through. Try again.')
}

// -- events -------------------------------------------------------------------

function handle(event) {
  if (!event || typeof event.seq !== 'number' || event.seq <= state.lastSeq) return
  state.lastSeq = event.seq
  const live = event.seq >= state.speakFrom
  switch (event.kind) {
    case 'user':
      addLine(
        h(
          'li',
          { class: 'line line-user', dataset: { seq: event.seq } },
          h('span', { class: 'src' }, event.source === 'voice' ? 'You said' : event.source === 'chip' ? 'You tapped' : 'You typed'),
          event.text,
        ),
      )
      break
    case 'agent_status':
      if (event.state === 'thinking') state.busy = true
      if (event.state === 'idle' && event.turn_id) state.busy = false
      state.checking = event.state === 'checking'
      if (event.state === 'idle' && !event.turn_id) state.checking = false
      syncControls()
      break
    case 'card':
      takeCard(event.card)
      break
    case 'focus':
      // A tool Ada called answered with a card that had not changed ("back
      // to the board"): show it again.
      if (event.card_id && state.cards[event.card_id]) {
        state.focus = event.card_id
        renderScreen()
      }
      break
    case 'ada':
      addLine(h('li', { class: 'line line-ada', dataset: { seq: event.seq, origin: event.origin } }, h('span', { class: 'who' }, 'Ada'), event.text))
      if (live) say(event)
      if (live) note(event.expects_reply ? ASKED_HINT : MIC_HINT)
      break
    case 'progress':
      addLine(h('li', { class: 'line line-progress', dataset: { seq: event.seq } }, event.text))
      if (live) say(event)
      break
    case 'notice':
      addLine(h('li', { class: 'line line-notice' }, event.text))
      if (/Polly/.test(event.text)) {
        polly = false
        $('voice-name').textContent = "Voice: this browser's own (Amazon Polly unavailable)."
      }
      break
    case 'error':
      addLine(h('li', { class: 'line line-error' }, event.text))
      if (live) say(event)
      break
    case 'trace':
      addTrace(event)
      break
    case 'memory':
      state.memory = event.memory || null
      renderMemory(state.memory)
      break
    default:
      break
  }
}

function addLine(li) {
  const list = $('transcript')
  list.append(li)
  list.scrollTop = list.scrollHeight
}

function traceText(event) {
  if (event.step === 'model') {
    return `model call ${event.n} · ${event.model_id || 'scripted'} · ${event.ms} ms${event.error ? ` · ${event.error}` : ''}`
  }
  if (event.step === 'tool') {
    const who = event.origin === 'host-poll' ? 'host poll' : 'model'
    const args = JSON.stringify(event.arguments || {})
    return `MCP ${event.tool} (${who})${event.is_error ? ' · error' : ''}${event.ms !== null && event.ms !== undefined ? ` · ${event.ms} ms` : ''} · ${args}`
  }
  if (event.step === 'guard') return `voice guard replaced model text (${event.rule})`
  if (event.step === 'polly') return event.error ? `Polly: ${event.reason}` : `Polly · ${event.bytes} bytes · ${event.billed_chars ?? '?'} characters`
  if (event.step === 'poll') return `host poll failed · ${event.error}`
  if (event.step === 'turn') return `turn failed · ${event.error}`
  if (event.step === 'memory') {
    const scripted = state.memory && state.memory.kind === 'scripted'
    const retrieve = event.op === 'retrieve'
    const who = scripted
      ? `scripted memory ${retrieve ? 'recall' : 'write'}`
      : `AgentCore ${retrieve ? 'RetrieveMemoryRecords' : 'CreateEvent'}`
    const count = event.op === 'retrieve' && event.state === 'ok' ? ` · ${event.count} remembered` : ''
    return `${who} · ${event.state}${count}${event.ms ? ` · ${event.ms} ms` : ''}`
  }
  return null
}

function addTrace(event) {
  if (event.step === 'budget') {
    const m = event.model_calls || {}
    const p = event.polly_calls || {}
    const mem = event.memory_calls
    const memWords = mem ? ` · Memory calls ${mem.used} of ${mem.max}` : ''
    $('trace-lead').textContent = `Model calls ${m.used} of ${m.max} · Polly calls ${p.used} of ${p.max}${memWords}`
    return
  }
  const text = traceText(event)
  if (!text) return
  const list = $('trace-list')
  list.append(h('li', {}, text))
  list.scrollTop = list.scrollHeight
}

function syncControls() {
  const busy = state.busy
  $('send').disabled = busy
  $('mic').disabled = busy || !dictation.supported
  $('thinking').hidden = !(busy || state.checking)
  $('thinking').textContent = busy ? 'Ada is thinking' : 'Ada is checking the board'
  for (const chip of document.querySelectorAll('.chip, .finding, .board-item')) chip.disabled = busy
  setBar()
}

// -- cards ----------------------------------------------------------------------

function takeCard(card) {
  if (!card || !card.id) return
  const before = state.cards[card.id]
  state.cards[card.id] = card
  const focused = state.focus ? state.cards[state.focus] : null
  // A board card that only gained highlights does not pull focus from the
  // finding that caused them.
  const onlyHighlight =
    card.type === 'board' && before && before.state === card.state && focused && focused.type === 'finding' && focused.sessionId === card.sessionId
  if (!onlyHighlight) state.focus = card.id
  renderScreen()
}

function renderScreen() {
  const body = $('screen-body')
  const card = state.focus ? state.cards[state.focus] : null
  if (!card) {
    body.replaceChildren(
      h(
        'div',
        { class: 'screen-empty' },
        h('p', { class: 'screen-empty-title' }, 'Ada is listening for a board to design.'),
        h('p', { class: 'screen-empty-sub' }, 'Try “a three point three volt regulator powered from USB-C”.'),
      ),
    )
    return
  }
  let view
  if (card.type === 'board') view = boardView(card)
  else if (card.type === 'finding') view = findingView(card)
  else view = boardsView(card)
  body.replaceChildren(...view)
  syncControls()
}

function head(title, subtitle, extras = []) {
  return h(
    'div',
    { class: 'card-head' },
    h('div', { class: 'card-head-text' }, h('h2', { class: 'card-title' }, title, ...extras), subtitle ? h('p', { class: 'card-subtitle' }, subtitle) : null),
    h('span', { class: 'card-attrib' }, 'Simulated Alexa+ experience'),
  )
}

function rail(steps) {
  return h(
    'ol',
    { class: 'rail', 'aria-label': 'Progress' },
    (steps || []).map((s) => h('li', { dataset: { status: s.status }, 'aria-current': s.status === 'current' ? 'step' : null }, s.label)),
  )
}

function chips(buttons) {
  if (!buttons || !buttons.length) return null
  return h(
    'div',
    { class: 'chips' },
    buttons.map((b, i) =>
      h('button', { type: 'button', class: i === 0 ? 'chip chip-primary' : 'chip', onclick: () => sendTurn(b.utterance, 'chip', b.hint) }, b.text),
    ),
  )
}

function foot(card) {
  const badges = (card.badges || []).map((b) => h('span', { class: 'badge' }, b))
  if (!card.hintText && !badges.length) return null
  return h('div', { class: 'card-foot' }, h('span', {}, card.hintText || ''), h('span', {}, ...badges))
}

function sanitizeSvg(text) {
  const doc = new DOMParser().parseFromString(text, 'image/svg+xml')
  const root = doc.documentElement
  if (!root || root.nodeName !== 'svg' || doc.querySelector('parsererror')) return null
  if (root.querySelector('script, foreignObject')) return null
  for (const el of [root, ...root.querySelectorAll('*')]) {
    for (const attr of el.attributes) {
      if (/^on/i.test(attr.name) || /javascript:/i.test(attr.value)) return null
    }
  }
  return document.importNode(root, true)
}

function boardVisual(boardCard, highlight) {
  const wrap = h('div', { class: 'visual visual-board' })
  const figure = h('div', { class: 'board-live', role: 'img', 'aria-label': boardCard.image.alt })
  wrap.append(figure, h('p', { class: 'visual-caption' }, boardCard.image.caption))
  const apply = (svgText) => {
    const svg = sanitizeSvg(svgText)
    if (!svg) {
      figure.replaceChildren(h('p', { class: 'visual-caption' }, 'The board drawing could not be shown.'))
      return
    }
    svg.setAttribute('aria-hidden', 'true')
    svg.removeAttribute('width')
    svg.removeAttribute('height')
    const refs = new Set(highlight || [])
    figure.classList.toggle('has-highlight', refs.size > 0)
    for (const part of svg.querySelectorAll('g.part')) {
      part.classList.toggle('is-highlighted', refs.has(part.getAttribute('data-ref')))
    }
    figure.replaceChildren(svg)
  }
  const src = boardCard.image.src
  if (state.svg[src]) apply(state.svg[src])
  else {
    figure.append(h('p', { class: 'visual-caption' }, 'Drawing the board…'))
    fetch(src)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((text) => {
        state.svg[src] = text
        apply(text)
      })
      .catch(() => figure.replaceChildren(h('p', { class: 'visual-caption' }, 'The board drawing is not available.')))
  }
  return wrap
}

function waitingVisual(title, sub) {
  return h('div', { class: 'visual visual-waiting' }, h('span', { class: 'big' }, title), sub ? h('span', {}, sub) : null)
}

function questionVisual(q) {
  return h(
    'div',
    { class: 'visual visual-question', dataset: { testid: 'question' } },
    h('p', { class: 'q-kicker' }, 'Ada needs one answer'),
    h('p', { class: 'q-ask' }, q.ask),
    h('p', { class: 'q-default' }, "If you don't say: ", h('strong', {}, q.default)),
    q.remaining ? h('p', { class: 'q-remaining' }, `${q.remaining} more after this`) : null,
  )
}

function stats(list) {
  if (!list || !list.length) return null
  return h(
    'dl',
    { class: 'stats' },
    list.map((s) => h('div', { class: 'stat' }, h('dt', {}, s.label), h('dd', {}, s.value))),
  )
}

function unroutedBlock(card) {
  if (card.unrouted === null || card.unrouted === undefined) return null
  if (!card.unrouted.length) return h('p', { class: 'fully-routed' }, 'Fully routed')
  const shown = state.expanded[card.id] ? card.unrouted : card.unrouted.slice(0, card.unroutedShown || 5)
  const rest = card.unrouted.length - shown.length
  return h(
    'div',
    {},
    h('p', { class: 'section-label' }, `Unrouted nets (${card.unrouted.length})`),
    h(
      'ul',
      { class: 'nets' },
      shown.map((n) => h('li', {}, h('code', {}, n.net), n.reason ? ` ${n.reason}` : '')),
    ),
    rest > 0
      ? h(
          'button',
          {
            type: 'button',
            class: 'more',
            onclick: () => {
              state.expanded[card.id] = true
              renderScreen()
            },
          },
          `and ${rest} more`,
        )
      : null,
  )
}

function reviewBlock(review) {
  if (!review) return null
  const out = h('div', { class: 'review', dataset: { status: review.status } }, h('strong', {}, 'Design review'), review.label)
  if (review.detail) out.append(h('div', {}, review.detail))
  return out
}

function findingsBlock(items) {
  if (!items || !items.length) return null
  return h(
    'ul',
    { class: 'findings' },
    items.map((f) =>
      h(
        'li',
        {},
        h(
          'button',
          { type: 'button', class: 'finding', onclick: () => sendTurn(f.utterance, 'chip', f.hint) },
          h('span', { class: 'sev', dataset: { sev: f.severity } }, f.severity),
          h('span', {}, `${f.number}. ${f.primaryText}`),
          f.secondaryText ? h('span', { class: 'refs' }, f.secondaryText) : null,
        ),
      ),
    ),
  )
}

function answersBlock(answers) {
  if (!answers || !answers.length) return null
  return h(
    'div',
    {},
    h('p', { class: 'section-label' }, 'Answers so far'),
    h(
      'ul',
      { class: 'answers' },
      answers.map((a) => h('li', {}, `${a.ask} `, h('b', {}, a.answer), a.source === 'default' ? ' (Ada’s default)' : ' (you said)')),
    ),
  )
}

function filesBlock(files) {
  if (!files || !files.board) return null
  return h(
    'div',
    { class: 'files' },
    h('a', { href: files.board, download: '' }, 'Open in KiCad (download the .kicad_pcb)'),
    files.boardPath ? h('span', { class: 'path' }, files.boardPath) : null,
  )
}

function boardView(card) {
  const extras = [h('span', { class: 'state', dataset: { state: card.state } }, card.stateLabel)]
  let visual
  if (card.image) visual = boardVisual(card, card.highlightRefs)
  else if (card.question) visual = questionVisual(card.question)
  else if (card.state === 'failed') visual = waitingVisual('Stopped', 'Nothing was ordered.')
  else visual = waitingVisual(card.stateLabel, card.imageNote)
  const detail = h('div', { class: 'detail' })
  if (card.primaryText) detail.append(h('p', { class: 'primary' }, card.primaryText))
  // The speech is in the transcript; on a finished board the review and its
  // findings say the same thing, so the screen shows those instead.
  const finished = card.state === 'done' && card.review
  if (!card.question && !finished && card.secondaryText) detail.append(h('p', { class: 'secondary' }, card.secondaryText))
  if (card.failure) {
    detail.append(
      h(
        'div',
        { class: 'failure' },
        h('p', {}, h('strong', {}, card.failure.text)),
        card.failure.reason ? h('p', {}, card.failure.reason) : null,
        h('p', {}, card.failure.ordered),
      ),
    )
  }
  const review = reviewBlock(card.review)
  if (review) detail.append(review)
  const findings = findingsBlock(card.listItems)
  if (findings) detail.append(findings)
  const unrouted = unroutedBlock(card)
  if (unrouted) detail.append(unrouted)
  const s = stats(card.stats)
  if (s) detail.append(s)
  if (['questions', 'proposing', 'drafted'].includes(card.state)) {
    const answers = answersBlock(card.answers)
    if (answers) detail.append(answers)
  }
  // The KiCad file sits under the drawing of it, which also keeps the two
  // columns of a finished board close in height.
  const files = filesBlock(card.files)
  const left = files ? h('div', { class: 'visual-col' }, visual, files) : visual
  // Nothing for the detail column (the first question): the one block takes
  // the whole width instead of leaving half the screen blank.
  const main = detail.childElementCount
    ? h('div', { class: 'card-main' }, left, detail)
    : h('div', { class: 'card-main card-main-single' }, left)
  return [head('Ada', card.headerSubtitle, extras), rail(card.steps), main, chips(card.buttons), foot(card)].filter(Boolean)
}

function findingView(card) {
  const board = state.cards[`board:${card.sessionId}`]
  const extras = card.severity ? [h('span', { class: 'sev', dataset: { sev: card.severity } }, card.severity)] : []
  const visual = board && board.image ? boardVisual(board, card.highlightRefs) : waitingVisual('The board is drawn once it is routed.', null)
  const detail = h('div', { class: 'detail' })
  if (card.primaryText) detail.append(h('p', { class: 'primary' }, card.primaryText))
  if (card.severityLabel) detail.append(h('p', { class: 'section-label' }, card.severityLabel))
  if (card.secondaryText) detail.append(h('p', { class: 'secondary' }, card.secondaryText))
  if (card.refs && card.refs.length) {
    detail.append(
      h('p', { class: 'secondary' }, 'Parts: ', (card.parts || []).join(', '), ' ', h('span', { class: 'path' }, `(${card.refs.join(', ')})`)),
    )
  }
  if (card.suggestedFix) {
    detail.append(h('div', {}, h('p', { class: 'fix' }, h('b', {}, 'Suggested fix: '), card.suggestedFix), h('p', { class: 'fix-note' }, card.fixNote)))
  }
  if (card.citation) detail.append(h('p', { class: 'fix-note' }, `Source: ${card.citation}`))
  return [
    head(card.headerTitle, board ? board.headerSubtitle : null, extras),
    h('div', { class: 'card-main' }, visual, detail),
    chips(card.buttons),
    foot(card),
  ].filter(Boolean)
}

function whenWords(iso) {
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return ''
  const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const days = Math.round((day(new Date()) - day(then)) / 86400000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  return `${days} days ago`
}

function boardsView(card) {
  const list = h(
    'ul',
    { class: 'boards' },
    (card.listItems || []).map((b) =>
      h(
        'li',
        {},
        h(
          'button',
          { type: 'button', class: 'board-item', onclick: () => sendTurn(b.utterance, 'chip', b.hint) },
          h('span', { class: 'when' }, whenWords(b.createdAt)),
          h('span', {}, b.primaryText),
          h('span', { class: 'how' }, b.secondaryText),
        ),
      ),
    ),
  )
  const detail = h('div', { class: 'detail' })
  if (card.secondaryText) detail.append(h('p', { class: 'secondary' }, card.secondaryText))
  if ((card.listItems || []).length) detail.append(list)
  return [head(card.headerTitle, card.headerSubtitle), detail, foot(card)].filter(Boolean)
}

// -- wiring ---------------------------------------------------------------------

function setMuted(muted) {
  state.muted = muted
  writeStore('localStorage', MUTE_KEY, muted ? '1' : null)
  const button = $('mute')
  button.setAttribute('aria-pressed', muted ? 'true' : 'false')
  button.setAttribute('aria-label', muted ? "Unmute Ada's voice" : "Mute Ada's voice")
  button.title = button.getAttribute('aria-label')
  if (muted) stopSpeech()
}

function wire() {
  $('start').addEventListener('click', () => startConversation(null))
  $('new-conversation').addEventListener('click', () =>
    startConversation('A new conversation. Ada remembers your boards, so ask how an earlier one went.'),
  )
  $('composer').addEventListener('submit', (event) => {
    event.preventDefault()
    const input = $('composer-text')
    const text = input.value
    input.value = ''
    sendTurn(text, 'typed')
  })
  $('mic').addEventListener('click', () => {
    stopSpeech()
    if (dictation.listening()) dictation.stop()
    else if (!dictation.start()) note('The microphone could not start. You can type instead.')
  })
  $('mute').addEventListener('click', () => setMuted(!state.muted))
  $('memory-pill').addEventListener('click', (event) => {
    event.stopPropagation()
    setMemoryOpen($('memory-pop').hidden)
  })
  document.addEventListener('click', (event) => {
    if (!$('memory-pop').hidden && !$('memory-pop').contains(event.target)) setMemoryOpen(false)
  })
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('memory-pop').hidden) {
      setMemoryOpen(false)
      $('memory-pill').focus()
    }
  })
  if (!dictation.supported) {
    $('mic').disabled = true
    note("This browser has no speech recognition (Firefox doesn't), so type to Ada. Chrome and Edge can listen.")
  }
  setMuted(state.muted)
}

async function boot() {
  wire()
  await loadConfig()
  const saved = readStore('sessionStorage', CONVERSATION_KEY)
  if (saved) await resume(saved)
}

boot()
