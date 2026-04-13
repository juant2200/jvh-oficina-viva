/**
 * chat_agent.js — Real-time AI Chat Module for Oficina Viva
 * Injects into OFFICE_SIM.html to enable live conversations with each agent
 * via Claude API streaming (SSE)
 */
(function() {
  'use strict';

  // ========== CONFIG ==========
  const CHAT_WIDTH = 420;

  const AGENT_META = {
    coo:       { emoji: '🎯', role: 'Chief Operating Officer' },
    finance:   { emoji: '💰', role: 'Director Financiero' },
    legal:     { emoji: '⚖️', role: 'Legal / RRHH' },
    ops:       { emoji: '⚙️', role: 'Director de Operaciones' },
    bd:        { emoji: '🤝', role: 'Business Development' },
    marketing: { emoji: '📣', role: 'Director de Marketing' },
    strategy:  { emoji: '♟️', role: 'Director de Estrategia' },
    research:  { emoji: '🔬', role: 'Director de Research' },
    exec:      { emoji: '📋', role: 'Asistente Ejecutivo CEO' }
  };

  const MOOD_EMOJI = {
    focused: '🎯', working: '💪', thinking: '🤔',
    creative: '✨', alert: '⚡', calm: '😌',
    stressed: '😰', happy: '😊', neutral: '😐'
  };

  // ========== STATE ==========
  let currentAgentId = null;
  let chatPanel = null;
  let isStreaming = false;
  let currentAbortController = null;
  const _originalOpenAgentPanel = window.openAgentPanel;

  // ========== STYLES ==========
  // Inject CSS
  const style = document.createElement('style');
  style.textContent = `
    /* Chat Panel */
    .ai-chat-panel {
      position: fixed;
      top: 0;
      right: -${CHAT_WIDTH + 20}px;
      width: ${CHAT_WIDTH}px;
      height: 100vh;
      background: #0f172a;
      border-left: 1px solid rgba(255,255,255,0.08);
      z-index: 9999;
      display: flex;
      flex-direction: column;
      transition: right 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: -8px 0 32px rgba(0,0,0,0.5);
    }
    .ai-chat-panel.open {
      right: 0;
    }
    .ai-chat-panel::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: var(--agent-color, #10b981);
      box-shadow: 0 0 20px var(--agent-color, #10b981);
    }

    /* Header */
    .ai-chat-header {
      padding: 16px 20px;
      background: #1e293b;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }
    .ai-chat-avatar {
      width: 42px;
      height: 42px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      background: rgba(255,255,255,0.05);
      border: 2px solid var(--agent-color);
      flex-shrink: 0;
    }
    .ai-chat-agent-info {
      flex: 1;
      min-width: 0;
    }
    .ai-chat-agent-name {
      font-size: 16px;
      font-weight: 700;
      color: #f1f5f9;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ai-chat-agent-name .mood {
      font-size: 14px;
    }
    .ai-chat-agent-role {
      font-size: 12px;
      color: #94a3b8;
      margin-top: 2px;
    }
    .ai-chat-header-actions {
      display: flex;
      gap: 6px;
    }
    .ai-chat-header-btn {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(255,255,255,0.04);
      color: #94a3b8;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      transition: all 0.15s;
    }
    .ai-chat-header-btn:hover {
      background: rgba(255,255,255,0.1);
      color: #f1f5f9;
    }

    /* Messages */
    .ai-chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      scroll-behavior: smooth;
    }
    .ai-chat-messages::-webkit-scrollbar {
      width: 4px;
    }
    .ai-chat-messages::-webkit-scrollbar-track {
      background: transparent;
    }
    .ai-chat-messages::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }

    .ai-chat-msg {
      max-width: 88%;
      padding: 10px 14px;
      border-radius: 14px;
      font-size: 14px;
      line-height: 1.5;
      color: #e2e8f0;
      word-wrap: break-word;
      animation: msgIn 0.2s ease-out;
    }
    @keyframes msgIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .ai-chat-msg.user {
      align-self: flex-end;
      background: #1e3a5f;
      border-bottom-right-radius: 4px;
    }
    .ai-chat-msg.assistant {
      align-self: flex-start;
      background: #1e293b;
      border-left: 3px solid var(--agent-color);
      border-bottom-left-radius: 4px;
    }
    .ai-chat-msg .timestamp {
      font-size: 10px;
      color: #64748b;
      margin-top: 4px;
      display: block;
    }

    /* Typing indicator */
    .ai-chat-typing {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      align-self: flex-start;
      color: #94a3b8;
      font-size: 13px;
    }
    .ai-chat-typing .dots {
      display: flex;
      gap: 3px;
    }
    .ai-chat-typing .dots span {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--agent-color);
      animation: dotPulse 1.4s ease-in-out infinite;
    }
    .ai-chat-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
    .ai-chat-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dotPulse {
      0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
      40% { opacity: 1; transform: scale(1.1); }
    }

    /* Input */
    .ai-chat-input-area {
      padding: 12px 16px;
      background: #1e293b;
      border-top: 1px solid rgba(255,255,255,0.06);
      display: flex;
      gap: 8px;
      align-items: flex-end;
      flex-shrink: 0;
    }
    .ai-chat-textarea {
      flex: 1;
      background: #0f172a;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 10px 14px;
      color: #f1f5f9;
      font-size: 14px;
      font-family: inherit;
      resize: none;
      min-height: 40px;
      max-height: 120px;
      outline: none;
      transition: border-color 0.2s;
    }
    .ai-chat-textarea:focus {
      border-color: var(--agent-color);
    }
    .ai-chat-textarea::placeholder {
      color: #475569;
    }
    .ai-chat-send-btn {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      border: none;
      background: var(--agent-color);
      color: white;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      transition: all 0.15s;
      flex-shrink: 0;
    }
    .ai-chat-send-btn:hover {
      filter: brightness(1.15);
      transform: scale(1.05);
    }
    .ai-chat-send-btn:disabled {
      opacity: 0.4;
      cursor: not-allowed;
      transform: none;
    }

    /* Welcome message */
    .ai-chat-welcome {
      text-align: center;
      padding: 40px 20px;
      color: #64748b;
    }
    .ai-chat-welcome .emoji {
      font-size: 48px;
      margin-bottom: 12px;
    }
    .ai-chat-welcome h3 {
      color: #e2e8f0;
      font-size: 18px;
      margin-bottom: 8px;
    }
    .ai-chat-welcome p {
      font-size: 13px;
      line-height: 1.5;
    }

    /* Overlay when chat is open */
    .ai-chat-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.3);
      z-index: 9998;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s;
    }
    .ai-chat-overlay.visible {
      opacity: 1;
      pointer-events: auto;
    }

    /* Mobile */
    @media (max-width: 768px) {
      .ai-chat-panel {
        width: 100vw;
        right: -105vw;
      }
    }

    /* Markdown-ish rendering in messages */
    .ai-chat-msg.assistant strong { color: #f1f5f9; }
    .ai-chat-msg.assistant code {
      background: rgba(255,255,255,0.08);
      padding: 1px 5px;
      border-radius: 4px;
      font-size: 13px;
    }
    .ai-chat-msg.assistant pre {
      background: rgba(0,0,0,0.3);
      padding: 10px;
      border-radius: 8px;
      overflow-x: auto;
      margin: 8px 0;
    }
    .ai-chat-msg.assistant pre code {
      background: none;
      padding: 0;
    }
    .ai-chat-msg.assistant ul, .ai-chat-msg.assistant ol {
      padding-left: 20px;
      margin: 6px 0;
    }
  `;
  document.head.appendChild(style);

  // ========== CREATE PANEL ==========
  function createPanel() {
    // Overlay
    const overlay = document.createElement('div');
    overlay.className = 'ai-chat-overlay';
    overlay.addEventListener('click', closeChat);
    document.body.appendChild(overlay);

    // Panel
    const panel = document.createElement('div');
    panel.className = 'ai-chat-panel';
    panel.innerHTML = `
      <div class="ai-chat-header">
        <div class="ai-chat-avatar"></div>
        <div class="ai-chat-agent-info">
          <div class="ai-chat-agent-name"><span class="name"></span><span class="mood"></span></div>
          <div class="ai-chat-agent-role"></div>
        </div>
        <div class="ai-chat-header-actions">
          <button class="ai-chat-header-btn" id="ai-chat-info-btn" title="Ver info del agente">📋</button>
          <button class="ai-chat-header-btn" id="ai-chat-clear-btn" title="Limpiar chat">🗑️</button>
          <button class="ai-chat-header-btn" id="ai-chat-close-btn" title="Cerrar">✕</button>
        </div>
      </div>
      <div class="ai-chat-messages" id="ai-chat-messages"></div>
      <div class="ai-chat-input-area">
        <textarea class="ai-chat-textarea" id="ai-chat-input"
          placeholder="Escribe tu mensaje..." rows="1"></textarea>
        <button class="ai-chat-send-btn" id="ai-chat-send">▶</button>
      </div>
    `;
    document.body.appendChild(panel);

    // Event listeners
    panel.querySelector('#ai-chat-close-btn').addEventListener('click', closeChat);
    panel.querySelector('#ai-chat-info-btn').addEventListener('click', () => {
      if (currentAgentId && _originalOpenAgentPanel) {
        _originalOpenAgentPanel(currentAgentId);
      }
    });
    panel.querySelector('#ai-chat-clear-btn').addEventListener('click', clearChat);
    panel.querySelector('#ai-chat-send').addEventListener('click', sendMessage);

    const textarea = panel.querySelector('#ai-chat-input');
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    // Auto-resize textarea
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    });

    // Global escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && chatPanel && chatPanel.classList.contains('open')) {
        closeChat();
      }
    });

    return panel;
  }

  // ========== OPEN CHAT ==========
  function openChat(agentId) {
    if (!chatPanel) chatPanel = createPanel();

    currentAgentId = agentId;

    // Get agent info from state
    const stateUrl = 'office_state.json';
    fetch(stateUrl + '?t=' + Date.now())
      .then(r => r.json())
      .then(state => {
        const agent = (state.agents || []).find(a => a.id === agentId);
        const meta = AGENT_META[agentId] || { emoji: '🤖', role: 'Agente' };
        const agentColor = agent?.color || '#10b981';
        const mood = agent?.mood || 'neutral';

        chatPanel.style.setProperty('--agent-color', agentColor);
        chatPanel.querySelector('.ai-chat-avatar').textContent = meta.emoji;
        chatPanel.querySelector('.ai-chat-avatar').style.borderColor = agentColor;
        chatPanel.querySelector('.ai-chat-agent-name .name').textContent = agent?.name || agentId;
        chatPanel.querySelector('.ai-chat-agent-name .mood').textContent = MOOD_EMOJI[mood] || '';
        chatPanel.querySelector('.ai-chat-agent-role').textContent = meta.role;

        // Load history
        loadHistory(agentId);

        // Open
        chatPanel.classList.add('open');
        document.querySelector('.ai-chat-overlay').classList.add('visible');

        // Focus input
        setTimeout(() => chatPanel.querySelector('#ai-chat-input').focus(), 350);
      })
      .catch(() => {
        // Open anyway with defaults
        const meta = AGENT_META[agentId] || { emoji: '🤖', role: 'Agente' };
        chatPanel.style.setProperty('--agent-color', '#10b981');
        chatPanel.querySelector('.ai-chat-avatar').textContent = meta.emoji;
        chatPanel.querySelector('.ai-chat-agent-name .name').textContent = agentId;
        chatPanel.querySelector('.ai-chat-agent-role').textContent = meta.role;
        chatPanel.classList.add('open');
        document.querySelector('.ai-chat-overlay').classList.add('visible');
      });
  }

  // ========== CLOSE CHAT ==========
  function closeChat() {
    if (currentAbortController) {
      currentAbortController.abort();
      currentAbortController = null;
    }
    isStreaming = false;
    if (chatPanel) chatPanel.classList.remove('open');
    document.querySelector('.ai-chat-overlay')?.classList.remove('visible');
  }

  // ========== LOAD HISTORY ==========
  function loadHistory(agentId) {
    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = '';

    fetch(`/api/chat_history/${agentId}`)
      .then(r => r.json())
      .then(data => {
        if (!data.ok || !data.history || data.history.length === 0) {
          showWelcome(agentId);
          return;
        }
        data.history.forEach(msg => {
          appendMessage(msg.role, msg.content, msg.ts);
        });
        scrollToBottom();
      })
      .catch(() => {
        showWelcome(agentId);
      });
  }

  function showWelcome(agentId) {
    const meta = AGENT_META[agentId] || { emoji: '🤖', role: 'Agente' };
    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = `
      <div class="ai-chat-welcome">
        <div class="emoji">${meta.emoji}</div>
        <h3>Hola, soy ${agentId.charAt(0).toUpperCase() + agentId.slice(1)}</h3>
        <p>${meta.role} de JV Holdings.<br>¿En qué te puedo ayudar hoy?</p>
      </div>
    `;
  }

  // ========== APPEND MESSAGE ==========
  function appendMessage(role, content, timestamp) {
    const msgContainer = document.getElementById('ai-chat-messages');
    // Remove welcome if present
    const welcome = msgContainer.querySelector('.ai-chat-welcome');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `ai-chat-msg ${role}`;

    // Simple markdown rendering for assistant messages
    let html = role === 'assistant' ? renderMarkdown(content) : escapeHtml(content);

    const ts = timestamp ? new Date(timestamp).toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit'}) : '';
    div.innerHTML = `${html}${ts ? `<span class="timestamp">${ts}</span>` : ''}`;

    msgContainer.appendChild(div);
    scrollToBottom();
    return div;
  }

  // ========== SEND MESSAGE ==========
  function sendMessage() {
    if (isStreaming) return;

    const input = document.getElementById('ai-chat-input');
    const text = input.value.trim();
    if (!text || !currentAgentId) return;

    input.value = '';
    input.style.height = 'auto';

    // Append user message
    appendMessage('user', text, new Date().toISOString());

    // Show typing indicator
    showTyping();

    // Disable send button
    const sendBtn = document.getElementById('ai-chat-send');
    sendBtn.disabled = true;
    isStreaming = true;

    // Create abort controller
    currentAbortController = new AbortController();

    // Stream response via SSE (using fetch, not EventSource, because we POST)
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: currentAgentId, message: text }),
      signal: currentAbortController.signal
    })
    .then(response => {
      if (!response.ok) throw new Error('Chat request failed');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let msgDiv = null;
      let fullText = '';

      function processChunk(chunk) {
        buffer += chunk;
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.substring(6);

          try {
            const data = JSON.parse(jsonStr);

            if (data.error) {
              hideTyping();
              appendMessage('assistant', '❌ Error: ' + data.error, new Date().toISOString());
              finishStreaming();
              return false;
            }

            if (data.done) {
              hideTyping();
              if (msgDiv) {
                // Re-render with full markdown
                const ts = new Date().toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit'});
                msgDiv.innerHTML = renderMarkdown(fullText) + `<span class="timestamp">${ts}</span>`;
              }
              finishStreaming();
              return false;
            }

            if (data.token) {
              hideTyping();
              fullText += data.token;
              if (!msgDiv) {
                msgDiv = appendMessage('assistant', '', null);
              }
              // Update with streaming text (basic rendering during stream)
              msgDiv.innerHTML = renderMarkdown(fullText) + '<span class="timestamp">escribiendo...</span>';
              scrollToBottom();
            }
          } catch(e) {
            // skip malformed JSON
          }
        }
        return true;
      }

      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) {
            // Process any remaining buffer
            if (buffer) processChunk('\n');
            finishStreaming();
            return;
          }
          const text = decoder.decode(value, { stream: true });
          const shouldContinue = processChunk(text);
          if (shouldContinue !== false) return pump();
        });
      }

      return pump();
    })
    .catch(err => {
      if (err.name === 'AbortError') return;
      hideTyping();
      appendMessage('assistant', '❌ Error de conexión: ' + err.message, new Date().toISOString());
      finishStreaming();
    });
  }

  function finishStreaming() {
    isStreaming = false;
    currentAbortController = null;
    const sendBtn = document.getElementById('ai-chat-send');
    if (sendBtn) sendBtn.disabled = false;
    const input = document.getElementById('ai-chat-input');
    if (input) input.focus();
  }

  // ========== TYPING INDICATOR ==========
  function showTyping() {
    hideTyping();
    const msgContainer = document.getElementById('ai-chat-messages');
    const typing = document.createElement('div');
    typing.className = 'ai-chat-typing';
    typing.id = 'ai-chat-typing';
    typing.innerHTML = `
      <div class="dots"><span></span><span></span><span></span></div>
      <span>pensando...</span>
    `;
    msgContainer.appendChild(typing);
    scrollToBottom();
  }

  function hideTyping() {
    const typing = document.getElementById('ai-chat-typing');
    if (typing) typing.remove();
  }

  // ========== CLEAR CHAT ==========
  function clearChat() {
    if (!currentAgentId) return;
    if (!confirm('¿Limpiar el historial de chat con este agente?')) return;

    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = '';
    showWelcome(currentAgentId);

    // TODO: Could add a /api/clear_chat endpoint to clear server-side history
  }

  // ========== HELPERS ==========
  function scrollToBottom() {
    const el = document.getElementById('ai-chat-messages');
    if (el) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function renderMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);
    // Code blocks
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Line breaks
    html = html.replace(/\n/g, '<br>');
    // Lists (basic)
    html = html.replace(/^- (.+)/gm, '• $1');
    return html;
  }

  // ========== OVERRIDE openAgentPanel ==========
  window.openAgentPanel = function(id) {
    openChat(id);
  };

  console.log('[chat_agent.js] ✅ Real-time AI chat module loaded');
})();
/**
 * chat_agent.js â Real-time AI Chat Module for Oficina Viva
 * Injects into OFFICE_SIM.html to enable live conversations with each agent
 * via Claude API streaming (SSE)
 */
(function() {
  'use strict';

  // ========== CONFIG ==========
  const CHAT_WIDTH = 420;

  const AGENT_META = {
    coo:       { emoji: 'ð¯', role: 'Chief Operating Officer' },
    finance:   { emoji: 'ð°', role: 'Director Financiero' },
    legal:     { emoji: 'âï¸', role: 'Legal / RRHH' },
    ops:       { emoji: 'âï¸', role: 'Director de Operaciones' },
    bd:        { emoji: 'ð¤', role: 'Business Development' },
    marketing: { emoji: 'ð£', role: 'Director de Marketing' },
    strategy:  { emoji: 'âï¸', role: 'Director de Estrategia' },
    research:  { emoji: 'ð¬', role: 'Director de Research' },
    exec:      { emoji: 'ð', role: 'Asistente Ejecutivo CEO' }
  };

  const MOOD_EMOJI = {
    focused: 'ð¯', working: 'ðª', thinking: 'ð¤',
    creative: 'â¨', alert: 'â¡', calm: 'ð',
    stressed: 'ð°', happy: 'ð', neutral: 'ð'
  };

  // ========== STATE ==========
  let currentAgentId = null;
  let chatPanel = null;
  let isStreaming = false;
  let currentAbortController = null;
  const _originalOpenAgentPanel = window.openAgentPanel;

  // ========== STYLES ==========
  // Inject CSS
  const style = document.createElement('style');
  style.textContent = `
    /* Chat Panel */
    .ai-chat-panel {
      position: fixed;
      top: 0;
      right: -${CHAT_WIDTH + 20}px;
      width: ${CHAT_WIDTH}px;
      height: 100vh;
      background: #0f172a;
      border-left: 1px solid rgba(255,255,255,0.08);
      z-index: 9999;
      display: flex;
      flex-direction: column;
      transition: right 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: -8px 0 32px rgba(0,0,0,0.5);
    }
    .ai-chat-panel.open {
      right: 0;
    }
    .ai-chat-panel::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: var(--agent-color, #10b981);
      box-shadow: 0 0 20px var(--agent-color, #10b981);
    }

    /* Header */
    .ai-chat-header {
      padding: 16px 20px;
      background: #1e293b;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }
    .ai-chat-avatar {
      width: 42px;
      height: 42px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      background: rgba(255,255,255,0.05);
      border: 2px solid var(--agent-color);
      flex-shrink: 0;
    }
    .ai-chat-agent-info {
      flex: 1;
      min-width: 0;
    }
    .ai-chat-agent-name {
      font-size: 16px;
      font-weight: 700;
      color: #f1f5f9;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ai-chat-agent-name .mood {
      font-size: 14px;
    }
    .ai-chat-agent-role {
      font-size: 12px;
      color: #94a3b8;
      margin-top: 2px;
    }
    .ai-chat-header-actions {
      display: flex;
      gap: 6px;
    }
    .ai-chat-header-btn {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(255,255,255,0.04);
      color: #94a3b8;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      transition: all 0.15s;
    }
    .ai-chat-header-btn:hover {
      background: rgba(255,255,255,0.1);
      color: #f1f5f9;
    }

    /* Messages */
    .ai-chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      scroll-behavior: smooth;
    }
    .ai-chat-messages::-webkit-scrollbar {
      width: 4px;
    }
    .ai-chat-messages::-webkit-scrollbar-track {
      background: transparent;
    }
    .ai-chat-messages::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }

    .ai-chat-msg {
      max-width: 88%;
      padding: 10px 14px;
      border-radius: 14px;
      font-size: 14px;
      line-height: 1.5;
      color: #e2e8f0;
      word-wrap: break-word;
      animation: msgIn 0.2s ease-out;
    }
    @keyframes msgIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .ai-chat-msg.user {
      align-self: flex-end;
      background: #1e3a5f;
      border-bottom-right-radius: 4px;
    }
    .ai-chat-msg.assistant {
      align-self: flex-start;
      background: #1e293b;
      border-left: 3px solid var(--agent-color);
      border-bottom-left-radius: 4px;
    }
    .ai-chat-msg .timestamp {
      font-size: 10px;
      color: #64748b;
      margin-top: 4px;
      display: block;
    }

    /* Typing indicator */
    .ai-chat-typing {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      align-self: flex-start;
      color: #94a3b8;
      font-size: 13px;
    }
    .ai-chat-typing .dots {
      display: flex;
      gap: 3px;
    }
    .ai-chat-typing .dots span {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--agent-color);
      animation: dotPulse 1.4s ease-in-out infinite;
    }
    .ai-chat-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
    .ai-chat-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dotPulse {
      0%, 80%, 100% { opacity: 0.3; transform: scale(0.8); }
      40% { opacity: 1; transform: scale(1.1); }
    }

    /* Input */
    .ai-chat-input-area {
      padding: 12px 16px;
      background: #1e293b;
      border-top: 1px solid rgba(255,255,255,0.06);
      display: flex;
      gap: 8px;
      align-items: flex-end;
      flex-shrink: 0;
    }
    .ai-chat-textarea {
      flex: 1;
      background: #0f172a;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 12px;
      padding: 10px 14px;
      color: #f1f5f9;
      font-size: 14px;
      font-family: inherit;
      resize: none;
      min-height: 40px;
      max-height: 120px;
      outline: none;
      transition: border-color 0.2s;
    }
    .ai-chat-textarea:focus {
      border-color: var(--agent-color);
    }
    .ai-chat-textarea::placeholder {
      color: #475569;
    }
    .ai-chat-send-btn {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      border: none;
      background: var(--agent-color);
      color: white;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      transition: all 0.15s;
      flex-shrink: 0;
    }
    .ai-chat-send-btn:hover {
      filter: brightness(1.15);
      transform: scale(1.05);
    }
    .ai-chat-send-btn:disabled {
      opacity: 0.4;
      cursor: not-allowed;
      transform: none;
    }

    /* Welcome message */
    .ai-chat-welcome {
      text-align: center;
      padding: 40px 20px;
      color: #64748b;
    }
    .ai-chat-welcome .emoji {
      font-size: 48px;
      margin-bottom: 12px;
    }
    .ai-chat-welcome h3 {
      color: #e2e8f0;
      font-size: 18px;
      margin-bottom: 8px;
    }
    .ai-chat-welcome p {
      font-size: 13px;
      line-height: 1.5;
    }

    /* Overlay when chat is open */
    .ai-chat-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.3);
      z-index: 9998;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s;
    }
    .ai-chat-overlay.visible {
      opacity: 1;
      pointer-events: auto;
    }

    /* Mobile */
    @media (max-width: 768px) {
      .ai-chat-panel {
        width: 100vw;
        right: -105vw;
      }
    }

    /* Markdown-ish rendering in messages */
    .ai-chat-msg.assistant strong { color: #f1f5f9; }
    .ai-chat-msg.assistant code {
      background: rgba(255,255,255,0.08);
      padding: 1px 5px;
      border-radius: 4px;
      font-size: 13px;
    }
    .ai-chat-msg.assistant pre {
      background: rgba(0,0,0,0.3);
      padding: 10px;
      border-radius: 8px;
      overflow-x: auto;
      margin: 8px 0;
    }
    .ai-chat-msg.assistant pre code {
      background: none;
      padding: 0;
    }
    .ai-chat-msg.assistant ul, .ai-chat-msg.assistant ol {
      padding-left: 20px;
      margin: 6px 0;
    }
  `;
  document.head.appendChild(style);

  // ========== CREATE PANEL ==========
  function createPanel() {
    // Overlay
    const overlay = document.createElement('div');
    overlay.className = 'ai-chat-overlay';
    overlay.addEventListener('click', closeChat);
    document.body.appendChild(overlay);

    // Panel
    const panel = document.createElement('div');
    panel.className = 'ai-chat-panel';
    panel.innerHTML = `
      <div class="ai-chat-header">
        <div class="ai-chat-avatar"></div>
        <div class="ai-chat-agent-info">
          <div class="ai-chat-agent-name"><span class="name"></span><span class="mood"></span></div>
          <div class="ai-chat-agent-role"></div>
        </div>
        <div class="ai-chat-header-actions">
          <button class="ai-chat-header-btn" id="ai-chat-info-btn" title="Ver info del agente">ð</button>
          <button class="ai-chat-header-btn" id="ai-chat-clear-btn" title="Limpiar chat">ðï¸</button>
          <button class="ai-chat-header-btn" id="ai-chat-close-btn" title="Cerrar">â</button>
        </div>
      </div>
      <div class="ai-chat-messages" id="ai-chat-messages"></div>
      <div class="ai-chat-input-area">
        <textarea class="ai-chat-textarea" id="ai-chat-input"
          placeholder="Escribe tu mensaje..." rows="1"></textarea>
        <button class="ai-chat-send-btn" id="ai-chat-send">â¶</button>
      </div>
    `;
    document.body.appendChild(panel);

    // Event listeners
    panel.querySelector('#ai-chat-close-btn').addEventListener('click', closeChat);
    panel.querySelector('#ai-chat-info-btn').addEventListener('click', () => {
      if (currentAgentId && _originalOpenAgentPanel) {
        _originalOpenAgentPanel(currentAgentId);
      }
    });
    panel.querySelector('#ai-chat-clear-btn').addEventListener('click', clearChat);
    panel.querySelector('#ai-chat-send').addEventListener('click', sendMessage);

    const textarea = panel.querySelector('#ai-chat-input');
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    // Auto-resize textarea
    textarea.addEventListener('input', () => {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    });

    // Global escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && chatPanel && chatPanel.classList.contains('open')) {
        closeChat();
      }
    });

    return panel;
  }

  // ========== OPEN CHAT ==========
  function openChat(agentId) {
    if (!chatPanel) chatPanel = createPanel();

    currentAgentId = agentId;

    // Get agent info from state
    const stateUrl = 'office_state.json';
    fetch(stateUrl + '?t=' + Date.now())
      .then(r => r.json())
      .then(state => {
        const agent = (state.agents || []).find(a => a.id === agentId);
        const meta = AGENT_META[agentId] || { emoji: 'ð¤', role: 'Agente' };
        const agentColor = agent?.color || '#10b981';
        const mood = agent?.mood || 'neutral';

        chatPanel.style.setProperty('--agent-color', agentColor);
        chatPanel.querySelector('.ai-chat-avatar').textContent = meta.emoji;
        chatPanel.querySelector('.ai-chat-avatar').style.borderColor = agentColor;
        chatPanel.querySelector('.ai-chat-agent-name .name').textContent = agent?.name || agentId;
        chatPanel.querySelector('.ai-chat-agent-name .mood').textContent = MOOD_EMOJI[mood] || '';
        chatPanel.querySelector('.ai-chat-agent-role').textContent = meta.role;

        // Load history
        loadHistory(agentId);

        // Open
        chatPanel.classList.add('open');
        document.querySelector('.ai-chat-overlay').classList.add('visible');

        // Focus input
        setTimeout(() => chatPanel.querySelector('#ai-chat-input').focus(), 350);
      })
      .catch(() => {
        // Open anyway with defaults
        const meta = AGENT_META[agentId] || { emoji: 'ð¤', role: 'Agente' };
        chatPanel.style.setProperty('--agent-color', '#10b981');
        chatPanel.querySelector('.ai-chat-avatar').textContent = meta.emoji;
        chatPanel.querySelector('.ai-chat-agent-name .name').textContent = agentId;
        chatPanel.querySelector('.ai-chat-agent-role').textContent = meta.role;
        chatPanel.classList.add('open');
        document.querySelector('.ai-chat-overlay').classList.add('visible');
      });
  }

  // ========== CLOSE CHAT ==========
  function closeChat() {
    if (currentAbortController) {
      currentAbortController.abort();
      currentAbortController = null;
    }
    isStreaming = false;
    if (chatPanel) chatPanel.classList.remove('open');
    document.querySelector('.ai-chat-overlay')?.classList.remove('visible');
  }

  // ========== LOAD HISTORY ==========
  function loadHistory(agentId) {
    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = '';

    fetch(`/api/chat_history/${agentId}`)
      .then(r => r.json())
      .then(data => {
        if (!data.ok || !data.history || data.history.length === 0) {
          showWelcome(agentId);
          return;
        }
        data.history.forEach(msg => {
          appendMessage(msg.role, msg.content, msg.ts);
        });
        scrollToBottom();
      })
      .catch(() => {
        showWelcome(agentId);
      });
  }

  function showWelcome(agentId) {
    const meta = AGENT_META[agentId] || { emoji: 'ð¤', role: 'Agente' };
    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = `
      <div class="ai-chat-welcome">
        <div class="emoji">${meta.emoji}</div>
        <h3>Hola, soy ${agentId.charAt(0).toUpperCase() + agentId.slice(1)}</h3>
        <p>${meta.role} de JV Holdings.<br>Â¿En quÃ© te puedo ayudar hoy?</p>
      </div>
    `;
  }

  // ========== APPEND MESSAGE ==========
  function appendMessage(role, content, timestamp) {
    const msgContainer = document.getElementById('ai-chat-messages');
    // Remove welcome if present
    const welcome = msgContainer.querySelector('.ai-chat-welcome');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `ai-chat-msg ${role}`;

    // Simple markdown rendering for assistant messages
    let html = role === 'assistant' ? renderMarkdown(content) : escapeHtml(content);

    const ts = timestamp ? new Date(timestamp).toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit'}) : '';
    div.innerHTML = `${html}${ts ? `<span class="timestamp">${ts}</span>` : ''}`;

    msgContainer.appendChild(div);
    scrollToBottom();
    return div;
  }

  // ========== SEND MESSAGE ==========
  function sendMessage() {
    if (isStreaming) return;

    const input = document.getElementById('ai-chat-input');
    const text = input.value.trim();
    if (!text || !currentAgentId) return;

    input.value = '';
    input.style.height = 'auto';

    // Append user message
    appendMessage('user', text, new Date().toISOString());

    // Show typing indicator
    showTyping();

    // Disable send button
    const sendBtn = document.getElementById('ai-chat-send');
    sendBtn.disabled = true;
    isStreaming = true;

    // Create abort controller
    currentAbortController = new AbortController();

    // Stream response via SSE (using fetch, not EventSource, because we POST)
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: currentAgentId, message: text }),
      signal: currentAbortController.signal
    })
    .then(response => {
      if (!response.ok) throw new Error('Chat request failed');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let msgDiv = null;
      let fullText = '';

      function processChunk(chunk) {
        buffer += chunk;
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const jsonStr = line.substring(6);

          try {
            const data = JSON.parse(jsonStr);

            if (data.error) {
              hideTyping();
              appendMessage('assistant', 'â Error: ' + data.error, new Date().toISOString());
              finishStreaming();
              return false;
            }

            if (data.done) {
              hideTyping();
              if (msgDiv) {
                // Re-render with full markdown
                const ts = new Date().toLocaleTimeString('es', {hour:'2-digit', minute:'2-digit'});
                msgDiv.innerHTML = renderMarkdown(fullText) + `<span class="timestamp">${ts}</span>`;
              }
              finishStreaming();
              return false;
            }

            if (data.token) {
              hideTyping();
              fullText += data.token;
              if (!msgDiv) {
                msgDiv = appendMessage('assistant', '', null);
              }
              // Update with streaming text (basic rendering during stream)
              msgDiv.innerHTML = renderMarkdown(fullText) + '<span class="timestamp">escribiendo...</span>';
              scrollToBottom();
            }
          } catch(e) {
            // skip malformed JSON
          }
        }
        return true;
      }

      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) {
            // Process any remaining buffer
            if (buffer) processChunk('\n');
            finishStreaming();
            return;
          }
          const text = decoder.decode(value, { stream: true });
          const shouldContinue = processChunk(text);
          if (shouldContinue !== false) return pump();
        });
      }

      return pump();
    })
    .catch(err => {
      if (err.name === 'AbortError') return;
      hideTyping();
      appendMessage('assistant', 'â Error de conexiÃ³n: ' + err.message, new Date().toISOString());
      finishStreaming();
    });
  }

  function finishStreaming() {
    isStreaming = false;
    currentAbortController = null;
    const sendBtn = document.getElementById('ai-chat-send');
    if (sendBtn) sendBtn.disabled = false;
    const input = document.getElementById('ai-chat-input');
    if (input) input.focus();
  }

  // ========== TYPING INDICATOR ==========
  function showTyping() {
    hideTyping();
    const msgContainer = document.getElementById('ai-chat-messages');
    const typing = document.createElement('div');
    typing.className = 'ai-chat-typing';
    typing.id = 'ai-chat-typing';
    typing.innerHTML = `
      <div class="dots"><span></span><span></span><span></span></div>
      <span>pensando...</span>
    `;
    msgContainer.appendChild(typing);
    scrollToBottom();
  }

  function hideTyping() {
    const typing = document.getElementById('ai-chat-typing');
    if (typing) typing.remove();
  }

  // ========== CLEAR CHAT ==========
  function clearChat() {
    if (!currentAgentId) return;
    if (!confirm('Â¿Limpiar el historial de chat con este agente?')) return;

    const msgContainer = document.getElementById('ai-chat-messages');
    msgContainer.innerHTML = '';
    showWelcome(currentAgentId);

    // TODO: Could add a /api/clear_chat endpoint to clear server-side history
  }

  // ========== HELPERS ==========
  function scrollToBottom() {
    const el = document.getElementById('ai-chat-messages');
    if (el) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function renderMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);
    // Code blocks
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Line breaks
    html = html.replace(/\n/g, '<br>');
    // Lists (basic)
    html = html.replace(/^- (.+)/gm, 'â¢ $1');
    return html;
  }

  // ========== OVERRIDE openAgentPanel ==========
  window.openAgentPanel = function(id) {
    openChat(id);
  };

  console.log('[chat_agent.js] â Real-time AI chat module loaded');
})();
