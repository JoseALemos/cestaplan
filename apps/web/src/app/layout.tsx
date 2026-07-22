import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Figtree } from "next/font/google";

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

export const metadata: Metadata = {
  metadataBase: new URL(process.env.WEB_PUBLIC_URL ?? "http://localhost:3000"),
  title: {
    default: "CestaPlan — Del presupuesto al plato, sin sorpresas en la caja",
    template: "%s · CestaPlan",
  },
  description:
    "Dime dónde compras, cuánto quieres gastar, para cuántas personas y qué comidas necesitas. CestaPlan genera recetas, calcula los envases necesarios y prepara una lista de compra adaptada a una tienda concreta.",
  applicationName: "CestaPlan",
  appleWebApp: {
    capable: true,
    statusBarStyle: "default",
    title: "CestaPlan",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#faf9fc" },
    { media: "(prefers-color-scheme: dark)", color: "#1a1520" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es" className={`${bricolageGrotesque.variable} ${figtree.variable}`}>
      <body className="flex min-h-screen flex-col">
        <Providers>
          <ToastProvider>
            <ServiceWorkerRegister />
            <SiteHeader />
            <main className="flex-1">{children}</main>
            <SiteFooter />
          </ToastProvider>
        </Providers>
      </body>
    </html>
  );
}
