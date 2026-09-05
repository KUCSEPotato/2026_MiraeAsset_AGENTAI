import Image from "next/image";
import Link from "next/link";
import { Icon } from "../components/icons";

function SignalCard({ type, label, value, className }) {
  return (
    <div
      className={`pointer-events-none absolute z-20 flex min-h-[82px] w-36 items-center gap-3 rounded-2xl border border-orange-200/60 bg-white px-4 py-4 shadow-[0_10px_30px_rgba(30,25,20,0.065)] sm:min-h-[88px] sm:w-[152px] sm:gap-3.5 sm:px-[18px] sm:py-[19px] ${className}`}
    >
      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-finory-accent/60 bg-[#fffdfb] text-finory-accent sm:h-[38px] sm:w-[38px]">
        <Icon name={type} className="h-5 w-5 stroke-[1.85]" />
      </span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium leading-tight text-finory-muted">
          {label}
        </span>
        <strong className="mt-1 block text-base font-bold leading-tight text-finory-text sm:text-[17px]">
          {value}
        </strong>
      </span>
    </div>
  );
}

export default function LandingPage() {
  return (
    <main className="min-h-screen overflow-hidden bg-[radial-gradient(circle_at_77%_33%,rgba(245,130,32,0.055),transparent_30%),#fcfbf9]">
      <nav
        className="mx-auto flex h-[76px] w-[min(1280px,calc(100%-64px))] items-center border-b border-black/5 max-[860px]:w-[min(calc(100%-40px),1280px)] max-[560px]:h-[74px] max-[560px]:w-[min(calc(100%-28px),1280px)]"
        aria-label="주요 메뉴"
      >
        <Link href="/" aria-label="Finory 홈" className="inline-flex items-center">
          <Image
            src="/assets/logo.png"
            width={75}
            height={60}
            alt="Finory logo"
            priority
            className="h-auto w-[70px] max-[560px]:w-[64px]"
          />
        </Link>
      </nav>

      <section className="mx-auto grid min-h-[calc(100dvh-76px)] w-[min(1280px,calc(100%-64px))] grid-cols-[minmax(0,0.55fr)_minmax(390px,0.45fr)] items-center gap-[clamp(90px,8.5vw,120px)] py-14 max-[860px]:min-h-0 max-[860px]:w-[min(calc(100%-40px),1280px)] max-[860px]:grid-cols-1 max-[860px]:gap-10 max-[560px]:w-[min(calc(100%-28px),1280px)] max-[560px]:py-10">
        <div>
          <p className="mb-7 text-lg font-semibold text-finory-accent max-[560px]:mb-5 max-[560px]:text-base">
            Finance in Ory
          </p>
          <h1 className="m-0 max-w-[680px] text-[clamp(46px,3vw,56px)] font-[760] leading-[1.1] tracking-normal text-finory-text max-[860px]:text-[clamp(40px,7.6vw,52px)] max-[560px]:text-[clamp(34px,10vw,40px)]">
            <span className="block whitespace-nowrap max-[860px]:whitespace-normal">
              혼자였던 금융 탐색,
            </span>
            <span className="block whitespace-nowrap max-[860px]:whitespace-normal">
              <strong className="font-extrabold text-finory-accent">ORY</strong>와 함께 우리의 탐색으로.
            </span>
          </h1>
          <p className="mt-8 max-w-[590px] break-keep text-lg leading-[1.72] text-finory-muted max-[560px]:text-base">
            어려운 금융 정보 앞에서 더 이상 오래 헤매지 마세요.
            <br className="max-[560px]:hidden" />
            질문하는 순간, ORY가 함께 금융 정보를 탐색합니다.
          </p>
          <Link
            href="/chat"
            className="mt-8 inline-flex min-h-[54px] items-center gap-2.5 rounded-xl bg-finory-accent px-8 font-bold text-white shadow-[0_9px_18px_rgba(245,130,32,0.15)] transition hover:-translate-y-px hover:bg-finory-accentDark max-[560px]:min-h-[52px] max-[560px]:px-6"
          >
            함께 탐색하기
            <span aria-hidden="true">→</span>
          </Link>
        </div>

        <div className="relative grid min-h-[398px] w-[min(100%,520px)] place-items-center justify-self-end max-[860px]:min-h-[348px] max-[860px]:justify-self-center max-[560px]:min-h-[284px]">
          <div className="absolute aspect-square w-[405px] rounded-full bg-[#fff1e6] opacity-80 shadow-[inset_0_0_0_1px_rgba(245,130,32,0.035)] max-[860px]:w-[min(100%,350px)] max-[560px]:w-[280px]" />
          <div className="absolute z-10 h-[154px] w-[455px] rotate-[-24deg] rounded-full border border-finory-accent/20 max-[860px]:h-[136px] max-[860px]:w-[min(100%,404px)] max-[560px]:hidden" />
          <span className="absolute left-[76px] top-[52px] z-20 text-base text-finory-accent/40 max-[560px]:hidden">
            ✦
          </span>
          <span className="absolute bottom-12 right-12 z-20 text-sm text-finory-accent/40 max-[560px]:hidden">
            ✦
          </span>
          <SignalCard
            type="trend"
            label="수익률"
            value="비교 중"
            className="left-[-18px] top-[94px] max-[860px]:left-6 max-[860px]:top-[78px] max-[560px]:left-0 max-[560px]:top-5 max-[560px]:min-h-[74px] max-[560px]:w-[132px]"
          />
          <Image
            src="/assets/ory.png"
            width={484}
            height={484}
            alt="ORY, Finory AI agent mascot"
            priority
            className="relative z-10 h-auto w-[300px] object-contain max-[860px]:w-[min(72%,285px)] max-[560px]:w-[238px]"
          />
          <SignalCard
            type="shield"
            label="위험"
            value="검토 중"
            className="bottom-[82px] right-[-22px] max-[860px]:bottom-[68px] max-[860px]:right-6 max-[560px]:hidden"
          />
        </div>
      </section>

      <section className="bg-finory-bg py-24 max-[860px]:py-20 max-[560px]:py-16">
        <div className="mx-auto grid min-h-44 w-[min(1280px,calc(100%-64px))] grid-cols-[128px_1px_minmax(320px,1.25fr)_repeat(3,minmax(160px,0.8fr))] items-center gap-x-8 rounded-[26px] border border-finory-accent/10 bg-finory-soft/70 px-11 py-8 max-[860px]:w-[min(calc(100%-40px),1280px)] max-[860px]:grid-cols-[96px_1px_minmax(280px,1fr)] max-[860px]:gap-6 max-[860px]:p-7 max-[560px]:w-[min(calc(100%-28px),1280px)] max-[560px]:grid-cols-1 max-[560px]:rounded-[20px] max-[560px]:p-5">
          <Image
            src="/assets/logo.png"
            width={112}
            height={89}
            alt="Finory logo"
            className="h-auto w-28 max-[860px]:w-[94px] max-[560px]:w-[82px]"
          />
          <span className="block h-[78px] w-px bg-finory-accent/20 max-[560px]:hidden" aria-hidden="true" />
          <div className="min-w-80 break-keep max-[560px]:min-w-0">
            <h2 className="mb-2 text-lg font-bold leading-tight text-finory-text">
              Finance in Ory
            </h2>
            <p className="max-w-[340px] text-sm leading-[1.62] text-finory-muted max-[560px]:max-w-none">
              당신의 금융 탐색 파트너 ORY. 복잡한 금융 정보의 영역에서 길을 찾아드립니다.
            </p>
          </div>
          {[
            ["search", "찾고", "필요한 정보를 빠르게"],
            ["trend", "연결하고", "흩어진 정보를 하나로"],
            ["shield", "이해하고", "복잡한 금융 정보를 쉽게"]
          ].map(([icon, title, description]) => (
            <div
              key={title}
              className="flex min-h-14 min-w-40 items-center gap-4 border-l border-[#5b4c3f]/10 pl-7 break-keep max-[860px]:col-span-full max-[860px]:min-w-0 max-[860px]:border-l-0 max-[860px]:border-t max-[860px]:pt-5 max-[860px]:pl-0"
            >
              <Icon name={icon} className="h-8 w-8 shrink-0 text-[#76746e] stroke-[1.7]" />
              <div>
                <strong className="mb-1 block text-base font-bold leading-tight text-finory-text">
                  {title}
                </strong>
                <span className="block text-[13px] leading-snug text-finory-muted">
                  {description}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
