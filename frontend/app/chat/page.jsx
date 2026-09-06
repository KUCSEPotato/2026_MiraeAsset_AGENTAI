"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/icons";
import { fetchAnswer, fetchHealth } from "../lib/api";

const examples = [
  { icon: "trend", text: "국내 ETF 중 수익률이 높은 상위 3개를 알려줘" },
  { icon: "bar-chart", text: "위험도가 낮은 채권형 상품을 비교해줘" },
  { icon: "sprout", text: "미래에셋 상품 중 비용보수가 낮은 ETF는?" },
  { icon: "search", text: "TIGER 미국S&P500 ETF의 수익률과 위험 정보를 알려줘" }
];

const initialMessages = [
  {
    role: "assistant",
    content:
      "안녕하세요. 저는 ORY입니다. 상품, 수익률, 위험, 비용 정보까지 함께 탐색해드릴게요."
  }
];

function Sidebar({ onReset, recentChats, onSelectRecent }) {
  return (
    <aside className="relative flex min-h-0 flex-col border-r border-black/10 bg-[#fcfaf7] px-5 pb-8 pt-6 max-[860px]:hidden">
      <div className="flex items-center justify-between gap-3">
        <Link href="/" aria-label="Finory 홈">
          <Image
            src="/assets/logo.png"
            width={86}
            height={69}
            alt="Finory logo"
            className="h-auto w-[70px]"
          />
        </Link>
        <button
          type="button"
          onClick={onReset}
          title="새 채팅"
          aria-label="새 채팅"
          className="grid h-11 w-11 place-items-center rounded-xl border border-black/10 bg-white/70 text-finory-text transition hover:bg-finory-accent/10"
        >
          <Icon name="plus" className="h-5 w-5 stroke-[1.75]" />
        </button>
      </div>

      <button
        type="button"
        onClick={onReset}
        className="mt-9 flex min-h-[62px] w-full items-center gap-4 rounded-[14px] border border-black/10 bg-white px-4 text-left text-[17px] font-medium text-finory-text shadow-[0_8px_22px_rgba(45,35,25,0.035)] transition hover:bg-finory-accent/10"
      >
        <Icon name="message" className="h-6 w-6 text-finory-accent stroke-[1.75]" />
        <span>새 탐색 시작</span>
      </button>

      {recentChats.length > 0 ? (
        <div className="mt-12">
          <div className="px-1 pb-2.5 text-[13px] font-medium text-[#7d7972]">
            최근 탐색
          </div>
          {recentChats.map((chat) => (
            <button
              key={chat.id}
              type="button"
              onClick={() => onSelectRecent(chat.text)}
              className="flex min-h-[46px] w-full items-center gap-3 rounded-[12px] px-3.5 text-left text-[15px] font-medium text-[#2f2f2c] transition hover:bg-finory-accent/10"
            >
              <Icon name="message" className="h-5 w-5 shrink-0 text-[#7c7770] stroke-[1.7]" />
              <span className="truncate">{chat.text}</span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="mt-auto flex min-h-[76px] w-[min(100%,260px)] items-center gap-3 rounded-[13px] border border-black/10 bg-white/70 p-3 shadow-[0_8px_20px_rgba(45,35,25,0.025)]">
        <span className="grid h-[42px] w-[42px] shrink-0 place-items-center rounded-full bg-finory-accent/10">
          <Image
            src="/assets/ory.png"
            width={64}
            height={64}
            alt="ORY 마스코트"
            className="h-[39px] w-[39px] object-contain"
          />
        </span>
        <div>
          <strong className="block text-[15px] font-bold leading-tight text-finory-text">
            ORY
          </strong>
          <span className="mt-0.5 block text-xs leading-snug text-finory-muted">
            금융 탐색 파트너
          </span>
        </div>
      </div>
    </aside>
  );
}

function Welcome() {
  return (
    <section
      className="mx-auto flex w-[min(800px,100%)] items-center gap-5 max-[560px]:items-start max-[560px]:gap-3.5"
      aria-label="ORY 인사"
    >
      <span className="grid h-[62px] w-[62px] shrink-0 place-items-center rounded-full bg-finory-accent/10 max-[560px]:h-[52px] max-[560px]:w-[52px]">
        <Image
          src="/assets/ory.png"
          width={72}
          height={72}
          alt="ORY, Finory AI agent mascot"
          className="h-[57px] w-[57px] object-contain max-[560px]:h-12 max-[560px]:w-12"
        />
      </span>
      <p className="m-0 break-keep text-lg font-normal leading-relaxed text-finory-text max-[560px]:text-base">
        <span className="block">안녕하세요. 저는 ORY입니다.</span>
        <span className="block">상품, 수익률, 위험, 비용 정보까지 함께 탐색해드릴게요.</span>
      </p>
    </section>
  );
}

function PromptGrid({ onSelect }) {
  return (
    <div className="mx-auto mt-10 grid w-[min(800px,100%)] grid-cols-2 gap-5 max-[860px]:w-[min(680px,100%)] max-[860px]:gap-4 max-[560px]:grid-cols-1">
      {examples.map((example) => (
        <button
          key={example.text}
          type="button"
          onClick={() => onSelect(example.text)}
          className="flex min-h-[124px] items-center gap-5 rounded-2xl border border-[#503c2d]/10 bg-white/90 p-5 text-left text-base font-medium leading-relaxed text-finory-text transition hover:-translate-y-px hover:border-finory-accent/20 hover:bg-finory-soft/60 max-[860px]:min-h-28 max-[560px]:min-h-24 max-[560px]:gap-3.5 max-[560px]:p-4 max-[560px]:text-[15px]"
        >
          <span className="grid h-14 w-14 shrink-0 place-items-center rounded-full bg-finory-accent/10 text-finory-accent max-[560px]:h-11 max-[560px]:w-11">
            <Icon name={example.icon} className="h-6 w-6 stroke-[1.75] max-[560px]:h-[22px] max-[560px]:w-[22px]" />
          </span>
          <span className="break-keep">{example.text}</span>
        </button>
      ))}
    </div>
  );
}

function Message({ message }) {
  const isAssistant = message.role === "assistant";

  return (
    <article
      className={`mx-auto mb-5 flex w-[min(780px,100%)] items-start gap-3.5 max-[560px]:gap-2.5 ${
        isAssistant ? "justify-start" : "flex-row-reverse justify-start"
      }`}
    >
      <div
        className={`grid h-[34px] w-[34px] shrink-0 place-items-center overflow-hidden rounded-lg text-xs font-extrabold text-white max-[560px]:h-[30px] max-[560px]:w-[30px] ${
          isAssistant ? "bg-[#eff4ee]" : "bg-finory-accent"
        }`}
      >
        {isAssistant ? (
          <Image src="/assets/ory.png" width={34} height={34} alt="ORY" className="h-[34px] w-[34px] object-contain" />
        ) : (
          <span>나</span>
        )}
      </div>
      <div
        className={`max-w-[72%] break-keep rounded-2xl px-4 py-3 text-base leading-[1.72] text-[#202620] [overflow-wrap:anywhere] max-[560px]:max-w-[78%] max-[560px]:px-3.5 max-[560px]:py-2.5 max-[560px]:text-[15px] ${
          isAssistant
            ? "rounded-tl-md bg-[#f2f4f1]"
            : "rounded-tr-md bg-finory-accent text-white"
        }`}
      >
        {message.content.split("\n").map((line, index) => (
          <span key={`${line}-${index}`}>
            {line}
            {index < message.content.split("\n").length - 1 ? <br /> : null}
          </span>
        ))}
      </div>
    </article>
  );
}

export default function ChatPage() {
  const [messages, setMessages] = useState(initialMessages);
  const [question, setQuestion] = useState("");
  const [backendStatus, setBackendStatus] = useState("checking");
  const textareaRef = useRef(null);
  const threadRef = useRef(null);

  useEffect(() => {
    let active = true;

    fetchHealth()
      .then((health) => {
        if (!active) return;
        setBackendStatus(health.readiness_status === "READY" ? "ready" : "not_ready");
      })
      .catch(() => {
        if (active) {
          setBackendStatus("offline");
        }
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [question]);

  function resetChat() {
    setMessages(initialMessages);
    setQuestion("");
  }

  async function askFinory(event) {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed) return;

    setQuestion("");
    setMessages((current) => [
      ...current,
      { role: "user", content: trimmed },
      { role: "assistant", content: "ORY가 데이터를 살펴보고 있어요..." }
    ]);

    try {
      const data = await fetchAnswer(trimmed);
      setMessages((current) => [
        ...current.slice(0, -1),
        { role: "assistant", content: data.answer || "답변을 찾지 못했어요." }
      ]);
    } catch (error) {
      setMessages((current) => [
        ...current.slice(0, -1),
        {
          role: "assistant",
          content:
            error?.name === "AbortError"
              ? "응답 시간이 길어져 요청을 중단했어요. 잠시 뒤 다시 질문해 주세요."
              : `지금은 백엔드 응답을 불러오지 못했어요. ${error?.message || "서버 상태를 확인해 주세요."}`
        }
      ]);
    }
  }

  function handleQuestionKeyDown(event) {
    if (event.nativeEvent.isComposing) return;
    if (event.key !== "Enter" || event.shiftKey) return;

    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  const isIntro = messages.length === 1;
  const recentChats = messages
    .filter((message) => message.role === "user")
    .slice(-5)
    .reverse()
    .map((message, index) => ({
      id: `${index}-${message.content}`,
      text: message.content
    }));

  return (
    <main className="grid h-dvh grid-cols-[336px_minmax(0,1fr)] overflow-hidden bg-[#fffefd] max-[860px]:grid-cols-1">
      <Sidebar
        onReset={resetChat}
        recentChats={recentChats}
        onSelectRecent={setQuestion}
      />

      <section className="relative flex min-h-0 min-w-0 flex-col bg-[#fffefd]" aria-label="Finory 채팅">
        <header className="flex h-[94px] shrink-0 items-center gap-3 border-b border-black/10 px-11 max-[860px]:h-[78px] max-[860px]:px-6">
          <Link href="/" className="hidden bg-transparent font-bold text-finory-text max-[860px]:inline-flex">
            Finory
          </Link>
          <div>
            <strong className="block text-[21px] font-[780] leading-tight text-finory-text max-[860px]:text-[19px]">
              ORY와 대화하기
            </strong>
            <span className="mt-1 block text-sm font-normal text-finory-muted max-[860px]:text-[13px]">
              검증된 데이터 기반 금융 탐색
            </span>
          </div>
          <span
            title="Backend status"
            className={`ml-auto inline-flex h-8 shrink-0 items-center rounded-full border px-3 text-xs font-semibold ${
              backendStatus === "ready"
                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                : backendStatus === "checking"
                  ? "border-amber-200 bg-amber-50 text-amber-700"
                  : "border-rose-200 bg-rose-50 text-rose-700"
            }`}
          >
            {backendStatus === "ready"
              ? "API 연결됨"
              : backendStatus === "checking"
                ? "API 확인 중"
                : "API 확인 필요"}
          </span>
        </header>

        <div
          ref={threadRef}
          className="min-h-0 flex-1 overflow-y-auto px-11 pb-[178px] pt-14 max-[860px]:px-6 max-[860px]:pb-40 max-[860px]:pt-10 max-[560px]:px-3.5 max-[560px]:pb-[150px] max-[560px]:pt-8"
        >
          {isIntro ? (
            <>
              <Welcome />
              <PromptGrid onSelect={setQuestion} />
            </>
          ) : (
            messages.map((message, index) => (
              <Message key={`${message.role}-${index}`} message={message} />
            ))
          )}
        </div>

        <form
          onSubmit={askFinory}
          className="absolute bottom-4 left-1/2 m-0 w-[min(860px,calc(100%-72px))] -translate-x-1/2 p-0 max-[860px]:w-[min(680px,calc(100%-32px))] max-[560px]:bottom-2.5 max-[560px]:w-[calc(100%-24px)]"
        >
          <div className="grid min-h-[80px] grid-cols-[minmax(0,1fr)_50px] items-center gap-3.5 rounded-[22px] border border-[#413228]/10 bg-white py-3 pl-5 pr-3.5 shadow-composer max-[560px]:min-h-[64px] max-[560px]:grid-cols-[minmax(0,1fr)_44px] max-[560px]:rounded-[18px] max-[560px]:py-2 max-[560px]:pl-4 max-[560px]:pr-2.5">
            <textarea
              ref={textareaRef}
              rows={1}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={handleQuestionKeyDown}
              placeholder="Finory에게 금융 상품에 대해 물어보세요"
              autoComplete="off"
              className="h-[50px] max-h-[180px] w-full resize-none overflow-hidden border-0 px-0 py-[13px] text-base leading-6 text-finory-text outline-none placeholder:text-[#878e88] max-[560px]:h-11 max-[560px]:py-2.5 max-[560px]:text-[15px]"
            />
            <button
              type="submit"
              aria-label="보내기"
              className="grid h-[50px] w-[50px] place-items-center rounded-[13px] bg-finory-accent text-white transition hover:-translate-y-px hover:bg-finory-accentDark max-[560px]:h-11 max-[560px]:w-11 max-[560px]:rounded-[11px]"
            >
              <Icon name="arrow-up" className="h-6 w-6 stroke-[1.85] max-[560px]:h-5 max-[560px]:w-5" />
            </button>
          </div>
          <p className="mt-3 text-center text-[13px] text-[#7a817b] max-[560px]:mt-2.5 max-[560px]:text-xs">
            Finory는 제공된 데이터와 검증 가능한 근거를 바탕으로 답변합니다.
          </p>
        </form>
      </section>
    </main>
  );
}
