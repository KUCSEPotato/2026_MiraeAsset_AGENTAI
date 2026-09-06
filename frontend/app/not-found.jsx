import Link from "next/link";

export default function NotFound() {
  return (
    <main className="grid min-h-screen place-items-center bg-finory-bg px-6 text-center">
      <div>
        <p className="text-sm font-semibold text-finory-accent">404</p>
        <h1 className="mt-3 text-3xl font-bold text-finory-text">페이지를 찾을 수 없습니다</h1>
        <p className="mt-4 text-finory-muted">요청한 주소가 없거나 이동되었습니다.</p>
        <Link
          href="/"
          className="mt-8 inline-flex min-h-12 items-center rounded-xl bg-finory-accent px-6 font-bold text-white transition hover:bg-finory-accentDark"
        >
          홈으로 돌아가기
        </Link>
      </div>
    </main>
  );
}
