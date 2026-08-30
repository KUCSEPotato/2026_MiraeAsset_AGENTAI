const app = document.querySelector("#app");

const examples = [
  { icon: "trend", text: "국내 ETF 중 수익률이 높은 상위 3개 알려줘" },
  { icon: "chart", text: "위험이 낮은 채권형 상품을 비교해줘" },
  { icon: "sprout", text: "미래에셋 상품 중 운용보수가 낮은 ETF는?" },
  { icon: "search", text: "TIGER 미국S&P500 ETF의 수익률과 위험 정보를 알려줘" },
];

let messages = [
  {
    role: "assistant",
    content:
      "안녕하세요, 저는 ORY입니다. 상품, 수익률, 위험, 운용 정보까지 함께 탐색해볼게요.",
  },
];

function navigate(path) {
  history.pushState({}, "", path);
  render();
}

function render() {
  if (location.pathname.startsWith("/chat")) {
    renderChat();
    return;
  }
  renderLanding();
}

function renderLanding() {
  app.innerHTML = `
    <main class="landing">
      <nav class="topbar" aria-label="주요 메뉴">
        <button class="brand landing-brand" type="button" data-route="/" aria-label="Finory 홈">
          <img src="/assets/logo.png" width="75" height="60" alt="Finory logo" />
        </button>
      </nav>

      <section class="hero" aria-labelledby="hero-title">
        <div class="hero-copy">
          <p class="brand-meaning">Finance in Ory</p>
          <h1 id="hero-title">
            <span>혼자였던 금융 탐색,</span>
            <span><strong>ORY</strong>와 함께 우리의 탐색으로.</span>
          </h1>
          <p class="hero-sub">
            어려운 금융 정보 앞에서 더 이상 혼자 헤매지 마세요.<br />
            질문하는 순간, ORY가 당신과 함께 금융 정보를 탐색합니다.
          </p>
          <button class="primary-cta" type="button" data-route="/chat">
            <span>함께 탐색하기</span>
            <span aria-hidden="true">→</span>
          </button>
        </div>
        <div class="ory-stage" aria-label="ORY 마스코트">
          <div class="orbit" aria-hidden="true"></div>
          <span class="sparkle sparkle-one" aria-hidden="true">✦</span>
          <span class="sparkle sparkle-two" aria-hidden="true">✦</span>
          <span class="dot dot-one" aria-hidden="true"></span>
          <span class="dot dot-two" aria-hidden="true"></span>
          <div class="signal-card rate">
            <span class="card-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path d="M4 16.5 9.2 11.3l3.5 3.5L20 7.5" />
                <path d="M14.5 7.5H20v5.5" />
              </svg>
            </span>
            <span class="card-copy">
              <span>수익률</span>
              <strong>비교 중</strong>
            </span>
          </div>
          <img class="ory-hero" src="/assets/ory.png" width="484" height="484" alt="ORY, Finory AI agent mascot" />
          <div class="signal-card risk">
            <span class="card-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path d="M12 3.5 18.5 6v5.4c0 4.1-2.6 7.8-6.5 9.1-3.9-1.3-6.5-5-6.5-9.1V6L12 3.5Z" />
                <path d="m9 12 2 2 4-4" />
              </svg>
            </span>
            <span class="card-copy">
              <span>위험</span>
              <strong>검토 중</strong>
            </span>
          </div>
        </div>
      </section>

      <section class="brand-section" id="intro" aria-labelledby="brand-panel-title">
        <div class="brand-panel">
          <img class="brand-panel-logo" src="/assets/logo.png" width="112" height="89" alt="Finory logo" />
          <span class="brand-divider" aria-hidden="true"></span>
          <div class="brand-panel-copy">
            <h2 id="brand-panel-title">Finance in Ory</h2>
            <p>당신의 금융 탐색 파트너, ORY.<br />넓고 복잡한 금융의 영역에서 길을 찾아드릴게요.</p>
          </div>
          <div class="brand-action">
            <span class="action-icon search-icon" aria-hidden="true"></span>
            <div>
              <strong>찾고</strong>
              <span>필요한 정보를 빠르게</span>
            </div>
          </div>
          <div class="brand-action">
            <span class="action-icon link-icon" aria-hidden="true"></span>
            <div>
              <strong>연결하고</strong>
              <span>흩어진 정보를 하나로</span>
            </div>
          </div>
          <div class="brand-action">
            <span class="action-icon bulb-icon" aria-hidden="true"></span>
            <div>
              <strong>이해하고</strong>
              <span>복잡한 금융 정보를 쉽게</span>
            </div>
          </div>
        </div>
      </section>
    </main>
  `;
  bindRoutes();
}

function renderChat() {
  app.innerHTML = `
    <main class="chat-shell">
      <aside class="sidebar">
        <div class="sidebar-head">
          <button class="brand sidebar-brand" type="button" data-route="/" aria-label="Finory 홈">
            <img src="/assets/logo.png" width="86" height="69" alt="Finory logo" />
          </button>
          <button class="icon-button" type="button" id="new-chat" title="새 채팅" aria-label="새 채팅">
            ${iconSvg("plus")}
          </button>
        </div>
        <button class="new-chat" type="button" id="new-chat-wide">
          ${iconSvg("message")}
          <span>새 탐색 시작</span>
        </button>
        <div class="history-label">최근 탐색</div>
        <button class="history-item active" type="button">
          ${iconSvg("trend")}
          <span>금융 상품 탐색</span>
        </button>
        <button class="history-item" type="button">
          ${iconSvg("clock")}
          <span>ETF 수익률 비교</span>
        </button>
        <button class="history-item" type="button">
          ${iconSvg("shield")}
          <span>위험 지표 확인</span>
        </button>
        <div class="sidebar-foot">
          <span class="profile-avatar">
            <img src="/assets/ory.png" width="64" height="64" alt="ORY 마스코트" />
          </span>
          <div>
            <strong>ORY</strong>
            <span>금융 탐색 파트너</span>
          </div>
        </div>
      </aside>

      <section class="chat-main" aria-label="Finory 채팅">
        <header class="chat-header">
          <button class="mobile-home" type="button" data-route="/">Finory</button>
          <div>
            <strong>ORY와 대화하기</strong>
            <span>검증된 데이터 기반 금융 탐색</span>
          </div>
        </header>

        <div class="thread" id="thread">
          ${messages.length === 1 ? welcomeTemplate() : messages.map(messageTemplate).join("")}
          ${messages.length === 1 ? introTemplate() : ""}
        </div>

        <form class="composer" id="composer">
          <div class="composer-box">
            <textarea id="question" rows="1" placeholder="Finory에게 금융 상품에 대해 물어보세요" autocomplete="off"></textarea>
            <button class="send-button" type="submit" aria-label="보내기">
              ${iconSvg("arrowUp")}
            </button>
          </div>
          <p class="fine-print">Finory는 제공된 데이터와 검증 가능한 근거를 바탕으로 답변합니다.</p>
        </form>
      </section>
    </main>
  `;

  bindRoutes();
  bindChat();
  scrollThread();
}

function welcomeTemplate() {
  return `
    <section class="welcome" aria-label="ORY 인사">
      <span class="welcome-avatar">
        <img src="/assets/ory.png" width="72" height="72" alt="ORY, Finory AI agent mascot" />
      </span>
      <p>
        <span>안녕하세요, 저는 ORY입니다.</span>
        <span>상품, 수익률, 위험, 운용 정보까지 함께 탐색해볼게요.</span>
      </p>
    </section>
  `;
}

function introTemplate() {
  return `
    <div class="prompt-grid">
      ${examples
        .map(
          (example) => `
            <button class="prompt-card" type="button" data-question="${escapeAttr(example.text)}">
              <span class="prompt-icon" aria-hidden="true">${iconSvg(example.icon)}</span>
              <span>${example.text}</span>
            </button>
          `,
        )
        .join("")}
    </div>
  `;
}

function iconSvg(name) {
  const icons = {
    arrowUp: '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 19V5" /><path d="m5 12 7-7 7 7" /></svg>',
    chart: '<svg viewBox="0 0 24 24" focusable="false"><path d="M5 20V10" /><path d="M12 20V4" /><path d="M19 20v-7" /></svg>',
    clock: '<svg viewBox="0 0 24 24" focusable="false"><circle cx="12" cy="12" r="8" /><path d="M12 8v5l3 2" /></svg>',
    message: '<svg viewBox="0 0 24 24" focusable="false"><path d="M5 6.5A5 5 0 0 1 10 2h4a5 5 0 0 1 5 5v3.5a5 5 0 0 1-5 5h-3.5L6 20v-5.2a5 5 0 0 1-1-3V6.5Z" /><path d="M9 9.5h.01" /><path d="M12 9.5h.01" /><path d="M15 9.5h.01" /></svg>',
    plus: '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 5v14" /><path d="M5 12h14" /></svg>',
    search: '<svg viewBox="0 0 24 24" focusable="false"><circle cx="11" cy="11" r="7" /><path d="m16 16 4 4" /></svg>',
    shield: '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 3.5 18.5 6v5.4c0 4.1-2.6 7.8-6.5 9.1-3.9-1.3-6.5-5-6.5-9.1V6L12 3.5Z" /><path d="m9 12 2 2 4-4" /></svg>',
    sprout: '<svg viewBox="0 0 24 24" focusable="false"><path d="M12 20V10" /><path d="M12 10C9 7 6 6.5 4 7c.5 4 3.5 6 8 3Z" /><path d="M12 10c3-3 6-3.5 8-3-.5 4-3.5 6-8 3Z" /></svg>',
    trend: '<svg viewBox="0 0 24 24" focusable="false"><path d="M4 16.5 9.2 11.3l3.5 3.5L20 7.5" /><path d="M14.5 7.5H20v5.5" /></svg>',
  };
  return icons[name] || icons.search;
}

function messageTemplate(message) {
  const avatar =
    message.role === "assistant"
      ? '<img src="/assets/ory.png" alt="ORY" />'
      : '<span>나</span>';
  return `
    <article class="message ${message.role}">
      <div class="avatar">${avatar}</div>
      <div class="message-body">${formatText(message.content)}</div>
    </article>
  `;
}

function bindRoutes() {
  document.querySelectorAll("[data-route]").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.route));
  });
}

function bindChat() {
  const form = document.querySelector("#composer");
  const textarea = document.querySelector("#question");
  const resetButtons = [document.querySelector("#new-chat"), document.querySelector("#new-chat-wide")];

  resetButtons.forEach((button) => {
    button?.addEventListener("click", () => {
      messages = messages.slice(0, 1);
      renderChat();
    });
  });

  document.querySelectorAll("[data-question]").forEach((button) => {
    button.addEventListener("click", () => {
      textarea.value = button.dataset.question;
      textarea.focus();
    });
  });

  textarea.addEventListener("input", () => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = textarea.value.trim();
    if (!question) return;
    textarea.value = "";
    textarea.style.height = "auto";
    await askFinory(question);
  });
}

async function askFinory(question) {
  messages.push({ role: "user", content: question });
  messages.push({ role: "assistant", content: "ORY가 데이터를 살펴보고 있어요..." });
  renderChat();

  try {
    const params = new URLSearchParams({
      question_id: `finory-${Date.now()}`,
      question,
    });
    const response = await fetch(`/answer?${params.toString()}`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    messages[messages.length - 1] = {
      role: "assistant",
      content: data.answer || "답변을 찾지 못했어요.",
    };
  } catch (error) {
    messages[messages.length - 1] = {
      role: "assistant",
      content:
        "지금은 백엔드 답변을 불러오지 못했어요. 서버가 실행 중인지 확인한 뒤 다시 질문해 주세요.",
    };
  }
  renderChat();
}

function scrollThread() {
  const thread = document.querySelector("#thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
}

function formatText(text) {
  return escapeHtml(text).replace(/\n/g, "<br />");
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => {
    const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
    return entities[char];
  });
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/"/g, "&quot;");
}

window.addEventListener("popstate", render);
render();
