import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Figtree } from "next/font/google";

import { CookieNotice } from "@/components/layout/CookieNotice";
import { SiteFooter } from "@/components/layout/SiteFooter";
import { SiteHeader } from "@/components/layout/SiteHeader";
import { ToastProvider } from "@/components/ui/Toast";

import { Providers } from "./providers";
import { ServiceWorkerRegister } from "./sw-register";

import "./globals.css";

const bricolageGrotesque = Bricolage_Grotesque({
  subsets: ["latin"],
  variable: "--font-bricolage",
  display: "swap",
});

const figtree = Figtree({
  subsets: ["latin"],
  variable: "--font-figtree",
  display: "swap",
});

const DESCRIPTION =
  "Dime dónde compras, cuánto quieres gastar, para cuántas personas y qué comidas necesitas. CestaPlan genera recetas, calcula los envases necesarios y prepara una lista de compra adaptada a una tienda concreta.";

export const metadata: Metadata = {
  metadataBase: new URL(process.env.WEB_PUBLIC_URL ?? "http://localhost:3000"),
  title: {
    default: "CestaPlan — Del presupuesto al plato, sin sorpresas en la caja",
    template: "%s · CestaPlan",
  },
  description: DESCRIPTION,
  applicationName: "CestaPlan",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "CestaPlan",
  },
  openGraph: {
    type: "website",
    locale: "es_ES",
    url: "/",
    siteName: "CestaPlan",
    title: "CestaPlan — Del presupuesto al plato, sin sorpresas en la caja",
    description: DESCRIPTION,
    images: [{ url: "/icons/icon-512.png", width: 512, height: 512, alt: "CestaPlan" }],
  },
  twitter: {
    card: "summary",
    title: "CestaPlan — Del presupuesto al plato, sin sorpresas en la caja",
    description: DESCRIPTION,
    images: ["/icons/icon-512.png"],
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Draw under the notch/home indicator so we can pad with safe-area insets (PWA standalone).
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#faf9fc" },
    { media: "(prefers-color-scheme: dark)", color: "#1a1520" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es" className={`${bricolageGrotesque.variable} ${figtree.variable}`}>
      <body className="flex min-h-screen flex-col">
        <a href="#contenido" className="skip-link">
          Saltar al contenido
        </a>
        <Providers>
          <ToastProvider>
            <ServiceWorkerRegister />
            <SiteHeader />
            <main id="contenido" tabIndex={-1} className="flex-1">
              {children}
            </main>
            <SiteFooter />
            <CookieNotice />
          </ToastProvider>
        </Providers>
      </body>
    </html>
  );
}
