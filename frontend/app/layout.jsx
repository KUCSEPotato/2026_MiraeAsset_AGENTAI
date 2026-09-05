import "./globals.css";

export const metadata = {
  title: "Finory",
  description: "검증된 데이터 기반 금융 상품 탐색 에이전트"
};

export default function RootLayout({ children }) {
  return (
    <html lang="ko">
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
