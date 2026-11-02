import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MedContour｜医学影像标注平台",
  description: "本机部署的医学影像人工标注与审核工作台",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
