import type { Metadata } from "next";
import Image from "next/image";
import "./globals.css";

export const metadata: Metadata = {
  title: "Micro Integration Factory",
  description: "Capture Solace events, chat with an AI builder, and generate MDK micro-integrations."
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="app-frame">
          <div className="shell">
            <header className="topbar">
              <div className="brand-row">
                <Image
                  className="solace-logo"
                  src="/solace-logo.svg"
                  alt="Solace"
                  width={410}
                  height={123}
                  priority
                />
                <div className="brand">
                  <span className="brand-title">Micro Integration Factory</span>
                  <span className="brand-subtitle">by Joe Schulze and Raphael Caillon</span>
                </div>
              </div>
              <a
                className="source-link"
                href="https://github.com/solacese/micro-integration-factory"
                target="_blank"
                rel="noreferrer"
              >
                GitHub repo
              </a>
            </header>
            {children}
          </div>
        </div>
      </body>
    </html>
  );
}
